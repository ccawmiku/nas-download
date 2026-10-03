import argparse
import base64
import hashlib
import json
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from requests import RequestException
from gallery_dl.extractor.pixiv import PixivAppAPI


LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
DEFAULT_STATE_FILE = Path("/config/pixiv_oauth_state.json")
DEFAULT_OUTPUT_FILE = Path("/config/pixiv_refresh_token.txt")


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_verifier() -> str:
    return secrets.token_urlsafe(48)


def build_login_url(verifier: str) -> str:
    params = {
        "client": "pixiv-android",
        "code_challenge_method": "S256",
        "code_challenge": code_challenge(verifier),
    }
    return f"{LOGIN_URL}?{urlencode(params)}"


def extract_code(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.query:
        code = (parse_qs(parsed.query).get("code") or [""])[0]
        if code:
            return code.strip()
    if "=" in text:
        code = (parse_qs(text).get("code") or [""])[0]
        if code:
            return code.strip()
    return text.rpartition("=")[2].strip()


def save_state(path: Path, verifier: str, login_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"code_verifier": verifier, "login_url": login_url}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_verifier(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    verifier = str(data.get("code_verifier") or "").strip()
    if not verifier:
        raise RuntimeError(f"code_verifier not found in {path}")
    return verifier


def exchange_code(code: str, verifier: str, timeout: int = 30) -> dict:
    data = {
        "client_id": PixivAppAPI.CLIENT_ID,
        "client_secret": PixivAppAPI.CLIENT_SECRET,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "include_policy": "true",
        "redirect_uri": REDIRECT_URI,
    }
    last_error: RequestException | None = None
    for _attempt in range(2):
        try:
            response = requests.post(TOKEN_URL, headers={"User-Agent": USER_AGENT}, data=data, timeout=timeout)
            break
        except RequestException as error:
            last_error = error
    else:
        raise RuntimeError(
            "无法连接 Pixiv OAuth 服务。通常是 NAS 网络到 oauth.secure.pixiv.net 被重置或超时；"
            "请重新生成登录链接后再试，或检查 NAS 网络/代理/DNS。"
        ) from last_error
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"Pixiv token exchange returned non-JSON HTTP {response.status_code}") from error
    if response.status_code >= 400 or "error" in payload:
        raise RuntimeError(f"Pixiv token exchange failed: HTTP {response.status_code} {payload}")
    return payload


def write_refresh_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n", encoding="utf-8")


def start_flow(state_file: Path) -> str:
    verifier = generate_verifier()
    login_url = build_login_url(verifier)
    save_state(state_file, verifier, login_url)
    return login_url


def finish_flow(state_file: Path, output_file: Path, code_or_url: str, timeout: int) -> str:
    code = extract_code(code_or_url)
    if not code:
        raise RuntimeError("Pixiv callback/code is empty")
    payload = exchange_code(code, load_verifier(state_file), timeout=timeout)
    token = str(payload.get("refresh_token") or "").strip()
    if not token:
        raise RuntimeError(f"Pixiv response did not include refresh_token: {payload}")
    write_refresh_token(output_file, token)
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the gallery-dl Pixiv OAuth conversion flow.")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_FILE))
    parser.add_argument("--timeout", type=int, default=30)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true", help="Generate and store a Pixiv OAuth login URL.")
    action.add_argument("--finish", metavar="CALLBACK_OR_CODE", help="Exchange a callback URL or code for refresh-token.")
    action.add_argument("--interactive", action="store_true", help="Print login URL, read callback/code from stdin, and save token.")
    args = parser.parse_args()

    state_file = Path(args.state_file)
    output_file = Path(args.output)

    if args.start:
        print(start_flow(state_file))
        print(f"state file: {state_file}", file=sys.stderr)
        return 0

    if args.finish:
        finish_flow(state_file, output_file, args.finish, args.timeout)
        print(f"refresh-token saved: {output_file}")
        return 0

    login_url = start_flow(state_file)
    print(login_url)
    print("\nOpen the URL, log in, copy the final callback URL or code, then paste it here.")
    code_or_url = input("callback/code: ")
    finish_flow(state_file, output_file, code_or_url, args.timeout)
    print(f"refresh-token saved: {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
