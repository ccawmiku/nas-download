#!/usr/bin/env python3
from __future__ import annotations

import html
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import yaml
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nas_auto.adapters import ServiceAdapterRegistry
from nas_auto.supervisor import ProcessSupervisor
from nas_auto.proxy import stream_proxy


PORT = int(os.environ.get("PORT", "14001"))
ROOT = Path("/opt/nas-auto")
APP_VERSION = os.environ.get("APP_VERSION", "v3.0.2")
_frontend_candidates = (
    Path(__file__).resolve().parent / "frontend" / "dist",
    Path(__file__).resolve().parents[1] / "frontend" / "dist",
)
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", next((str(path) for path in _frontend_candidates if path.exists()), str(_frontend_candidates[0]))))
XHS_QUEUE_FILE = Path(os.environ.get("XHS_QUEUE_FILE", "/queue/xhs/links.txt"))
MAX_XHS_API_BODY_BYTES = 5_000_000
DOUYIN_CONFIG_PATH = Path(os.environ.get("DOUYIN_CONFIG_PATH", "/config/douyin/config.json"))
DOUYIN_COOKIE_YAML_PLACEHOLDER = "__DOUYIN_COOKIE_PLACEHOLDER__"
DEFAULT_DOUYIN_CONFIG: dict[str, Any] = {
    "download_dir": "/douyin",
    "f2_config_dir": "/config/douyin/f2",
    "defaults": {
        "cover": False,
        "desc": False,
        "folderize": True,
        "interval": "all",
        "lyric": True,
        "max_connections": 5,
        "max_counts": 0,
        "max_retries": 5,
        "max_tasks": 10,
        "naming": "{create}-{nickname}-{aweme_id}",
        "page_counts": 20,
        "timeout": 10,
    },
    "jobs": [
        {
            "name": "like",
            "mode": "like",
            "url": "https://www.douyin.com/user/REPLACE_WITH_YOUR_USER_ID",
        },
        {
            "name": "collection",
            "mode": "collection",
            "url": "https://www.douyin.com/user/REPLACE_WITH_YOUR_USER_ID",
        },
    ],
}

SERVICES = {
    "xhs": {"name": "小红书", "port": 18081, "path": "/xhs/", "config": "/config/xhs/config.json"},
    "x": {"name": "X", "port": 18082, "path": "/x/", "config": "/config/x/config.json"},
    "pixiv": {"name": "Pixiv", "port": 18083, "path": "/pixiv/", "config": "/config/pixiv/config.json"},
    "douyin": {"name": "抖音", "port": 18084, "path": "/douyin/", "config": "/config/douyin/config.json"},
}
SERVICES["telegram"] = {"name": "Telegram", "host": os.getenv("TELEGRAM_HOST", "telegram-worker"), "port": int(os.getenv("TELEGRAM_PORT", "8000")), "path": "/telegram/", "config": ""}
SERVICE_ADAPTERS = ServiceAdapterRegistry(SERVICES)

DOUYIN_REFERENCE_COOKIE_ORDER = (
    "UIFID_TEMP",
    "UIFID",
    "my_rd",
    "volume_info",
    "WallpaperGuide",
    "FOLLOW_NUMBER_YELLOW_POINT_INFO",
    "_bd_ticket_crypt_doamin",
    "record_force_login",
    "stream_player_status_params",
    "passport_mfa_token",
    "d_ticket",
    "PhoneResumeUidCacheV1",
    "strategyABtestKey",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "sdk_source_info",
    "bit_env",
    "gulu_source_res",
    "passport_auth_mix_state",
    "download_guide",
    "passport_assist_user",
    "n_mh",
    "sid_guard",
    "uid_tt",
    "uid_tt_ss",
    "sid_tt",
    "sessionid",
    "sessionid_ss",
    "session_tlb_tag",
    "is_staff_user",
    "has_biz_token",
    "sid_ucp_v1",
    "ssid_ucp_v1",
    "_bd_ticket_crypt_cookie",
    "__security_mc_1_s_sdk_sign_data_key_web_protect",
    "__security_mc_1_s_sdk_cert_key",
    "__security_mc_1_s_sdk_crypt_sdk",
    "__security_server_data_status",
    "login_time",
    "publish_badge_show_info",
    "DiscoverFeedExposedAd",
    "ttwid",
    "enter_pc_once",
    "hevc_supported",
    "home_can_add_dy_2_desktop",
    "stream_recommend_feed_params",
    "SelfTabRedDotControl",
    "FOLLOW_LIVE_POINT_INFO",
    "is_dash_user",
    "bd_ticket_guard_client_data",
    "bd_ticket_guard_client_web_domain",
    "odin_tt",
    "bd_ticket_guard_client_data_v2",
    "IsDouyinActive",
    "xgplayer_user_id",
    "fpk1",
    "fpk2",
    "__ac_nonce",
    "__ac_signature",
    "s_v_web_id",
    "dy_swidth",
    "dy_sheight",
    "gd_random",
    "bd_ticket_guard_web_domain",
)
DOUYIN_REFERENCE_COOKIE_LINE_GROUPS = (
    ("UIFID_TEMP",),
    ("UIFID",),
    ("my_rd", "volume_info", "WallpaperGuide"),
    ("FOLLOW_NUMBER_YELLOW_POINT_INFO",),
    ("_bd_ticket_crypt_doamin", "record_force_login"),
    ("stream_player_status_params",),
    ("passport_mfa_token",),
    ("d_ticket", "PhoneResumeUidCacheV1"),
    ("strategyABtestKey", "passport_csrf_token"),
    ("passport_csrf_token_default", "sdk_source_info"),
    ("bit_env",),
    ("gulu_source_res",),
    ("passport_auth_mix_state", "download_guide"),
    ("passport_assist_user",),
    ("n_mh", "sid_guard"),
    ("uid_tt", "uid_tt_ss"),
    ("sid_tt", "sessionid"),
    ("sessionid_ss", "session_tlb_tag"),
    ("is_staff_user", "has_biz_token", "sid_ucp_v1"),
    ("ssid_ucp_v1",),
    ("_bd_ticket_crypt_cookie", "__security_mc_1_s_sdk_sign_data_key_web_protect"),
    ("__security_mc_1_s_sdk_cert_key", "__security_mc_1_s_sdk_crypt_sdk"),
    ("__security_server_data_status", "login_time", "publish_badge_show_info"),
    ("DiscoverFeedExposedAd", "ttwid"),
    ("enter_pc_once", "hevc_supported", "home_can_add_dy_2_desktop", "stream_recommend_feed_params"),
    ("SelfTabRedDotControl",),
    ("FOLLOW_LIVE_POINT_INFO",),
    ("is_dash_user", "bd_ticket_guard_client_data"),
    ("bd_ticket_guard_client_web_domain", "odin_tt"),
    ("bd_ticket_guard_client_data_v2",),
    ("IsDouyinActive", "xgplayer_user_id", "fpk1"),
    ("fpk2", "__ac_nonce", "__ac_signature"),
    ("s_v_web_id", "dy_swidth"),
    ("dy_sheight", "gd_random"),
    ("bd_ticket_guard_web_domain",),
)
DOUYIN_REFERENCE_COOKIE_NAMES = set(DOUYIN_REFERENCE_COOKIE_ORDER)

COOKIE_LINE_RE = re.compile(r"^\s*cookie\s*:\s*", re.IGNORECASE)
YAML_KEY_RE = re.compile(r"^\s*[A-Za-z0-9_-]+\s*:\s*")
XHS_NOTE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)/[^\s\"'<>]+",
    re.IGNORECASE,
)
log_lines: list[str] = []
log_lock = threading.Lock()
xhs_queue_lock = threading.Lock()


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    with log_lock:
        log_lines.append(line)
        del log_lines[:-5000]


def deep_update(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as error:
        log(f"配置读取失败 {path}: {error}")
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def ensure_config(path: Path, example: Path, patch: dict[str, Any]) -> None:
    data = read_json(example)
    if path.exists():
        data = deep_update(data, read_json(path))
    deep_update(data, patch)
    write_json(path, data)


def ensure_configs() -> None:
    ensure_config(
        Path("/config/xhs/config.json"),
        ROOT / "xhs" / "config.example.json",
        {
            "api_url": os.environ.get("XHS_API_URL", "http://xhs-api:5556/xhs/detail"),
            "database": "/state/xhs/xhs_queue.sqlite3",
            "queue_files": ["/queue/xhs/links.txt"],
            "settings_path": "/xhs-volume/settings.json",
            "xhs_api_log_file": "/xhs-volume/xhs-api.log",
            "image_format": os.environ.get("XHS_IMAGE_FORMAT", "JPEG"),
            "request_delay_seconds": int(os.environ.get("XHS_REQUEST_DELAY_SECONDS", "1")),
            "jitter_seconds": int(os.environ.get("XHS_JITTER_SECONDS", "0")),
            "max_items_per_run": int(os.environ.get("XHS_MAX_ITEMS_PER_RUN", "0") or "0"),
            "web": {"host": "127.0.0.1", "port": 18081, "log_lines": 5000},
            "sync_settings": {
                "path": "/xhs-volume/settings.json",
                "defaults": {"work_path": "/xhs"},
            },
        },
    )
    ensure_config(
        Path("/config/x/config.json"),
        ROOT / "x" / "config.example.json",
        {
            "database": "/state/x/x_auto.sqlite3",
            "cookie_file": "/config/x/x_cookies.txt",
            "download_dir": "/downloads/x",
            "redownload_missing_files": False,
            "web": {"host": "127.0.0.1", "port": 18082},
        },
    )
    ensure_config(
        Path("/config/pixiv/config.json"),
        ROOT / "pixiv" / "config.example.json",
        {
            "refresh_token_file": "/config/pixiv/pixiv_refresh_token.txt",
            "refresh_token_file_env": "PIXIV_REFRESH_TOKEN_FILE",
            "oauth_state_file": "/config/pixiv/pixiv_oauth_state.json",
            "database": "/state/pixiv/pixiv_auto.sqlite3",
            "download_dir": "/downloads/pixiv",
            "image_dir": "/downloads/pixiv/images",
            "metadata_dir": "/downloads/pixiv/downloads-metadata",
            "web": {"host": "127.0.0.1", "port": 18083},
        },
    )
    ensure_config(
        Path("/config/douyin/config.json"),
        ROOT / "douyin" / "config.example.json",
        {
            "cookie_file": "/config/douyin/douyin_cookie.txt",
            "f2_state_dir": "/state/douyin/f2",
            "f2_config_dir": "/config/douyin/f2",
            "download_dir": "/douyin",
            "web": {"host": "127.0.0.1", "port": 18084},
        },
    )
    for path in [
        Path("/queue/xhs"),
        Path("/state/xhs"),
        Path("/state/x"),
        Path("/state/pixiv"),
        Path("/state/douyin/f2"),
        Path("/downloads/x/images"),
        Path("/downloads/x/videos"),
        Path("/downloads/x/downloads-metadata"),
        Path("/downloads/pixiv"),
        Path("/douyin"),
    ]:
        path.mkdir(parents=True, exist_ok=True)


supervisor = ProcessSupervisor(log)


def start_process(name: str, command: list[str], cwd: str, env_patch: dict[str, str] | None = None) -> None:
    supervisor.start(name, command, cwd, env_patch)


def wait_for_port(name: str, host: str, port: int, timeout_seconds: int = 90) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            if sock.connect_ex((host, port)) == 0:
                log(f"{name} ready on {host}:{port}")
                return True
        time.sleep(2)
    log(f"{name} not ready after {timeout_seconds}s; workers will keep retrying through normal runs")
    return False


def start_children() -> None:
    ensure_configs()
    wait_for_port("xhs-api", os.environ.get("XHS_API_HOST", "xhs-api"), int(os.environ.get("XHS_API_PORT", "5556")))
    start_process(
        "xhs-worker",
        [sys.executable, "/opt/nas-auto/xhs/xhs_auto_worker.py", "--config", "/config/xhs/config.json"],
        "/opt/nas-auto/xhs",
    )
    start_process(
        "x-worker",
        [sys.executable, "/opt/nas-auto/x/x_auto_worker.py", "--config", "/config/x/config.json"],
        "/opt/nas-auto/x",
        {"X_COOKIE_FILE": "/config/x/x_cookies.txt"},
    )
    start_process(
        "pixiv-worker",
        [sys.executable, "/opt/nas-auto/pixiv/pixiv_auto_worker.py", "--config", "/config/pixiv/config.json"],
        "/opt/nas-auto/pixiv",
        {"PIXIV_REFRESH_TOKEN_FILE": "/config/pixiv/pixiv_refresh_token.txt"},
    )
    start_process(
        "douyin-worker",
        [sys.executable, "/opt/nas-auto/douyin/douyin_f2_worker.py", "--config", "/config/douyin/config.json"],
        "/opt/nas-auto/douyin",
    )
def parse_cookie_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in str(text or "").replace("\r", "").split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if name in seen:
            pairs = [pair for pair in pairs if pair[0] != name]
        seen.add(name)
        pairs.append((name, value))
    return pairs


def dedupe_cookie_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in pairs:
        if name in seen:
            deduped = [pair for pair in deduped if pair[0] != name]
        seen.add(name)
        deduped.append((name, value))
    return deduped


def is_ascii_cookie_pair(name: str, value: str) -> bool:
    return name.isascii() and value.isascii()


def select_douyin_cookie_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    values: dict[str, str] = {}
    for name, value in dedupe_cookie_pairs(pairs):
        if not value or not is_ascii_cookie_pair(name, value):
            continue
        values[name] = value
    ordered = [(name, values[name]) for name in DOUYIN_REFERENCE_COOKIE_ORDER if name in values]
    return ordered + [(name, value) for name, value in values.items() if name not in DOUYIN_REFERENCE_COOKIE_NAMES]


def extract_douyin_cookie_text(text: str) -> str:
    lines = str(text or "").replace("\r", "").splitlines()
    if not lines:
        return ""
    collected: list[str] = []
    collecting = False
    for raw in lines:
        if not raw.strip():
            continue
        if not collecting and COOKIE_LINE_RE.match(raw):
            collecting = True
            tail = COOKIE_LINE_RE.sub("", raw, count=1).strip()
            if tail:
                collected.append(tail)
            continue
        if collecting:
            stripped = raw.strip()
            if YAML_KEY_RE.match(raw) and "=" not in stripped:
                break
            collected.append(stripped)
            continue
        collected.append(raw.strip())
    pairs = select_douyin_cookie_pairs(parse_cookie_pairs(" ".join(collected)))
    return "; ".join(f"{name}={value}" for name, value in pairs)


def render_douyin_cookie_block_lines(cookie_text: str, base_indent: str = "") -> list[str]:
    values = {name: value for name, value in parse_cookie_pairs(cookie_text)}
    grouped_parts: list[list[str]] = []
    for group in DOUYIN_REFERENCE_COOKIE_LINE_GROUPS:
        line_parts = [f"{name}={values[name]}" for name in group if name in values]
        if line_parts:
            grouped_parts.append(line_parts)
    for name, value in values.items():
        if name not in DOUYIN_REFERENCE_COOKIE_NAMES:
            grouped_parts.append([f"{name}={value}"])
    if not grouped_parts:
        return [f"{base_indent}cookie:"]
    lines: list[str] = []
    last_index = len(grouped_parts) - 1
    for index, line_parts in enumerate(grouped_parts):
        suffix = ";" if index != last_index else ""
        prefix = f"{base_indent}cookie: " if index == 0 else f"{base_indent}  "
        lines.append(f"{prefix}{'; '.join(line_parts)}{suffix}")
    return lines


def render_douyin_cookie_block(cookie_text: str, base_indent: str = "") -> str:
    return "\n".join(render_douyin_cookie_block_lines(cookie_text, base_indent)) + "\n"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    return deep_update(merged, override)


def job_key(job: dict[str, Any], index: int) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(job.get("name") or "")).strip("-")
    return name or f"job-{index + 1}"


def render_douyin_job_yaml(douyin: dict[str, Any]) -> str:
    payload = {"douyin": dict(douyin, cookie=DOUYIN_COOKIE_YAML_PLACEHOLDER)}
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100000)
    placeholder_pattern = re.compile(
        rf"^  cookie:\s+['\"]?{re.escape(DOUYIN_COOKIE_YAML_PLACEHOLDER)}['\"]?\s*$"
    )
    output_lines: list[str] = []
    replaced = False
    for line in rendered.splitlines():
        if placeholder_pattern.match(line):
            output_lines.extend(render_douyin_cookie_block_lines(str(douyin.get("cookie") or ""), "  "))
            replaced = True
            continue
        output_lines.append(line)
    if not replaced:
        raise RuntimeError("未找到抖音 Cookie 占位符，无法生成任务 YAML")
    return "\n".join(output_lines) + "\n"


def build_douyin_job_payload(config: dict[str, Any], job: dict[str, Any], cookie_text: str) -> dict[str, Any]:
    defaults = dict(config.get("defaults") or {})
    return {
        "cookie": extract_douyin_cookie_text(cookie_text) if "cookie:" in cookie_text else "; ".join(
            f"{name}={value}" for name, value in parse_cookie_pairs(cookie_text)
        ),
        "cover": bool(defaults.get("cover", False)),
        "desc": bool(defaults.get("desc", False)),
        "folderize": bool(defaults.get("folderize", True)),
        "interval": str(defaults.get("interval") or "all"),
        "languages": defaults.get("languages"),
        "lyric": bool(defaults.get("lyric", True)),
        "max_connections": int(defaults.get("max_connections") or 5),
        "max_counts": int(defaults.get("max_counts") or 0),
        "max_retries": int(defaults.get("max_retries") or 5),
        "max_tasks": int(defaults.get("max_tasks") or 10),
        "mode": str(job.get("mode") or "like"),
        "music": defaults.get("music"),
        "naming": str(defaults.get("naming") or "{create}-{nickname}-{aweme_id}"),
        "page_counts": int(defaults.get("page_counts") or 20),
        "path": str(config.get("download_dir") or "/douyin"),
        "timeout": int(defaults.get("timeout") or 10),
        "url": str(job.get("url") or ""),
    }


def sync_douyin_job_configs(cookie_text: str) -> None:
    config = deep_merge(DEFAULT_DOUYIN_CONFIG, read_json(DOUYIN_CONFIG_PATH))
    normalized_cookie = extract_douyin_cookie_text(cookie_text) if "cookie:" in cookie_text else "; ".join(
        f"{name}={value}" for name, value in parse_cookie_pairs(cookie_text)
    )
    config_dir = Path(str(config.get("f2_config_dir") or "/config/douyin/f2"))
    config_dir.mkdir(parents=True, exist_ok=True)
    for index, job in enumerate(config.get("jobs") or []):
        payload = build_douyin_job_payload(config, job, normalized_cookie)
        (config_dir / f"{job_key(job, index)}.yaml").write_text(render_douyin_job_yaml(payload), encoding="utf-8")


def summarize_child_status(child: dict[str, Any]) -> dict[str, Any]:
    progress = child.get("progress") if isinstance(child.get("progress"), dict) else {}
    counts = child.get("counts") if isinstance(child.get("counts"), dict) else {}
    current = str(child.get("current_job") or progress.get("current_url") or "").strip()
    if current and len(current) > 90:
        current = current[:87] + "..."
    extra = ""
    if counts:
        retry = int(counts.get("retry", 0) or 0)
        extra = (
            f"待处理 {int(counts.get('pending', 0) or 0)}，"
            f"重试 {retry}，失败 {int(counts.get('failed', 0) or 0)}"
        )
    elif child.get("last_run_message"):
        extra = str(child.get("last_run_message") or "")
    return {
        "running": bool(child.get("running")),
        "next_run_at": str(child.get("next_run_at") or ""),
        "current": current,
        "extra": extra,
    }


def service_status() -> dict[str, Any]:
    services: list[dict[str, Any]] = []
    for key, svc in SERVICES.items():
        adapter = SERVICE_ADAPTERS[key]
        # One status request per worker. The previous readiness probe followed
        # by a status request doubled JSON/SQLite work every dashboard poll.
        child = adapter.status()
        ready = bool(child)
        services.append(
            {
                "key": key,
                "name": svc["name"],
                "path": svc["path"],
                "port": svc["port"],
                "ready": ready,
                **summarize_child_status(child),
            }
        )
    return {
        "processes": supervisor.state(),
        "services": services,
        "logs": list(log_lines[-1000:]),
        "version": APP_VERSION,
    }


def dedupe_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def extract_xhs_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for match in XHS_NOTE_URL_RE.finditer(str(text or "")):
        urls.append(match.group(0).strip().rstrip("),.;，。；"))
    return dedupe_ordered(urls)


def normalize_xhs_link_payload(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    candidates: list[str] = []
    if isinstance(payload.get("urls"), list):
        candidates.extend(str(item) for item in payload["urls"])
    if payload.get("url"):
        candidates.append(str(payload["url"]))
    if payload.get("text"):
        candidates.extend(extract_xhs_urls_from_text(str(payload["text"])))

    urls: list[str] = []
    invalid: list[str] = []
    for raw in candidates:
        value = str(raw or "").strip()
        if not value:
            continue
        found = extract_xhs_urls_from_text(value)
        if found:
            urls.extend(found)
        else:
            invalid.append(value[:200])
    return dedupe_ordered(urls), invalid


def append_xhs_queue_links(urls: list[str]) -> dict[str, Any]:
    XHS_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with xhs_queue_lock:
        existing: set[str] = set()
        if XHS_QUEUE_FILE.exists():
            try:
                existing.update(extract_xhs_urls_from_text(XHS_QUEUE_FILE.read_text(encoding="utf-8-sig")))
            except OSError:
                existing = set()
        accepted = [url for url in urls if url not in existing]
        if accepted:
            with XHS_QUEUE_FILE.open("a", encoding="utf-8") as handle:
                if XHS_QUEUE_FILE.stat().st_size > 0:
                    handle.write("\n")
                handle.write("\n".join(accepted))
                handle.write("\n")
        return {
            "accepted": accepted,
            "skipped": [url for url in urls if url in existing],
            "queue_file": str(XHS_QUEUE_FILE),
        }


def trigger_xhs_worker_run() -> dict[str, Any]:
    return SERVICE_ADAPTERS["xhs"].run_now()


def trigger_service_run(service_key: str) -> dict[str, Any]:
    return SERVICE_ADAPTERS[service_key].run_now()


def post_xhs_worker(path: str, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    svc = SERVICES["xhs"]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", int(svc["port"]), timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {"body": raw[:1000]}
        parsed.setdefault("ok", 200 <= resp.status < 300)
        parsed.setdefault("status", resp.status)
        return parsed
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        return {"ok": False, "status": 0, "error": str(error)}
    finally:
        conn.close()


def page(message: str = "") -> bytes:
    """Return the separately built frontend shell.

    Keeping this boundary in the Python server means the API and worker
    adapters can evolve independently from the visual application. The
    legacy server-rendered dashboard remains available as a development
    fallback when the frontend has not been built yet.
    """
    index_path = FRONTEND_DIST / "index.html"
    if message:
        return (
            "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            f"<title>NAS Auto Download</title><body><h1>NAS Auto Download</h1>"
            f"<p>{html.escape(message)}</p></body></html>"
        ).encode("utf-8")
    if index_path.exists():
        return index_path.read_bytes()
    return (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        "<title>NAS Auto Download</title><body><h1>NAS Auto Download</h1>"
        "<p>前端资源尚未构建，请重新构建集成镜像。</p></body></html>"
    ).encode("utf-8")


def frontend_asset(path: str) -> tuple[bytes, str] | None:
    """Read a built frontend asset without allowing path traversal."""
    relative = path.removeprefix("/assets/")
    if not relative or "/" in relative or "\\" in relative or relative in {".", ".."}:
        return None
    asset = FRONTEND_DIST / "assets" / relative
    if not asset.is_file():
        return None
    content_types = {
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }
    return asset.read_bytes(), content_types.get(asset.suffix.lower(), "application/octet-stream")


def rewrite_html(prefix: str, body: bytes, content_type: str) -> bytes:
    if "text/html" not in content_type.lower():
        return body
    text = body.decode("utf-8", errors="replace")
    replacements = {
        'action="/': f'action="{prefix}',
        'href="/': f'href="{prefix}',
        'src="/': f'src="{prefix}',
        'fetch("/': f'fetch("{prefix}',
        "fetch('/": f"fetch('{prefix}",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text.encode("utf-8")


def proxy(handler: BaseHTTPRequestHandler, service_key: str, prefix: str) -> None:
    svc = SERVICES[service_key]
    path = handler.path[len(prefix) - 1:] if handler.path.startswith(prefix) else "/"
    try:
        stream_proxy(handler, svc.get("host", "127.0.0.1"), svc["port"], path,
                     rewrite=None if service_key == "telegram" else lambda body, content_type: rewrite_html(prefix, body, content_type),
                     location_prefix="" if service_key == "telegram" else prefix)
    except (OSError, TimeoutError, http.client.HTTPException):
        if not getattr(handler, "_proxy_response_started", False):
            try:
                handler.send_error(HTTPStatus.BAD_GATEWAY, "Service temporarily unavailable")
            except OSError:
                pass
        handler.close_connection = True


class Handler(BaseHTTPRequestHandler):
    def send_json_payload(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_HEAD(self) -> None:
        for key, svc in SERVICES.items():
            if urlsplit(self.path).path.startswith(svc["path"]):
                proxy(self, key, svc["path"])
                return
        self.send_response(HTTPStatus.OK)
        self.end_headers()

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        if split.path.startswith("/assets/"):
            asset = frontend_asset(split.path)
            if asset is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data, content_type = asset
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if split.path == "/api/status":
            self.send_json_payload(service_status())
            return
        for key, svc in SERVICES.items():
            if split.path == svc["path"].rstrip("/"):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", svc["path"])
                self.end_headers()
                return
            if split.path.startswith(svc["path"]):
                proxy(self, key, svc["path"])
                return
        data = page()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        split = urlsplit(self.path)
        if split.path == "/api/xhs/links":
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_XHS_API_BODY_BYTES:
                self.send_json_payload({"ok": False, "error": "请求体过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError as error:
                self.send_json_payload({"ok": False, "error": f"JSON 解析失败：{error}"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(payload, dict):
                self.send_json_payload({"ok": False, "error": "请求体必须是 JSON 对象"}, HTTPStatus.BAD_REQUEST)
                return
            urls, invalid = normalize_xhs_link_payload(payload)
            queue_result = append_xhs_queue_links(urls)
            trigger_result = trigger_xhs_worker_run() if urls else {"ok": False, "status": 0, "body": "没有有效链接"}
            accepted = queue_result["accepted"]
            skipped = queue_result["skipped"]
            ok = bool(urls) and not invalid and len(urls) == len(accepted) + len(skipped)
            result = {
                "ok": ok,
                "submitted": len(urls) + len(invalid),
                "valid": len(urls),
                "accepted": len(accepted),
                "skipped": len(skipped),
                "invalid": invalid,
                "queue_file": queue_result["queue_file"],
                "triggered": trigger_result,
                "message": (
                    f"已确认接收 {len(accepted)} 条新链接，{len(skipped)} 条已在队列中。"
                    if ok
                    else "存在无效链接或未识别到有效小红书链接。"
                ),
            }
            log(
                f"浏览器脚本提交小红书链接：valid={len(urls)} accepted={len(accepted)} "
                f"skipped={len(skipped)} invalid={len(invalid)} trigger={trigger_result.get('ok')}"
            )
            self.send_json_payload(result)
            return
        for key, svc in SERVICES.items():
            if split.path.startswith(svc["path"]):
                proxy(self, key, svc["path"])
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def shutdown(_signum: int, _frame: Any) -> None:
    supervisor.stop(timeout=30)
    raise SystemExit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    ensure_configs()
    threading.Thread(target=supervisor.monitor, daemon=True).start()
    threading.Thread(target=start_children, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"统一 Web UI listening on 0.0.0.0:{PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
