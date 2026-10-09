from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from .store import PLATFORMS

THRESHOLDS = {
    "x": ("known_stop_consecutive", 10),
    "pixiv": ("stop_after_consecutive_done", 20),
    "douyin": ("fallback_stop_consecutive_skipped", 50),
}

CONFIG_PATHS = {
    key: Path(os.getenv(f"{key.upper()}_CONFIG_PATH", f"/config/{key}/config.json"))
    for key in PLATFORMS
    if key != "telegram"
}
CONFIG_PATHS["telegram"] = Path(
    os.getenv("TELEGRAM_CONFIG_PATH", "/config/telegram/settings.json")
)

DEFAULTS = {
    "workspace": {key: key in {"telegram", "xhs"} for key in PLATFORMS},
    "max_minutes": {"x": 15, "pixiv": 15, "douyin": 15},
    "schedule": {
        key: {"enabled": False, "hours": 12} for key in ("x", "pixiv", "douyin")
    },
    "workspace_root": os.getenv("NAS_WORKSPACE_ROOT", "/media/.nas-workspace"),
    "auth_enabled": False,
    "log_preview_lines": 30,
    "conversion_retries": 1,
}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default if default is not None else {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def flatten(value, prefix=""):
    output = {}
    for key, item in value.items():
        name = prefix + key
        if isinstance(item, dict):
            output.update(flatten(item, name + "."))
        else:
            output[name] = item
    return output


def apply_fields(config, fields):
    for dotted, value in fields.items():
        cursor = config
        parts = dotted.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):
                raise ValueError("配置字段结构不匹配")
        cursor[parts[-1]] = value
    return config


SECRET_KEYS = {
    "cookie",
    "api_hash",
    "bot_token",
    "admin_password_hash",
    "refresh_token",
    "password",
    "browser_cookie",
}


def public_config(key):
    config = read_json(CONFIG_PATHS[key])
    fields = flatten(config)
    if key == "telegram":
        fields = {
            k: v
            for k, v in fields.items()
            if k
            in {
                "api_id",
                "api_hash",
                "bot_token",
                "download_dir",
                "image_download_dir",
                "video_download_dir",
                "file_download_dir",
                "session_dir",
                "allowed_user_ids",
                "admin_user_ids",
                "session_name",
                "progress_interval_seconds",
                "progress_percent_step",
                "max_filename_stem_length",
                "max_auto_retries",
                "queue_maxsize",
                "history_flush_interval_seconds",
            }
        }
    if key == "xhs":
        upstream = read_json(
            os.getenv("XHS_SETTINGS_PATH", "/xhs-volume/settings.json")
        )
        fields.update(flatten(upstream, "upstream."))
    for name in list(fields):
        if name.split(".")[-1] in SECRET_KEYS:
            fields[name + "_set"] = bool(fields.pop(name))
    # Paths to engine databases and web servers are managed by deployment.
    credential_key = "refresh_token_file" if key == "pixiv" else "cookie_file"
    credential_file = config.get(credential_key)
    credential_set = bool(
        credential_file
        and Path(credential_file).is_file()
        and Path(credential_file).stat().st_size
    )
    if key == "xhs":
        credential_set = bool(upstream.get("cookie"))
    return {
        "fields": fields,
        "configured": bool(config),
        "credential_set": credential_set,
    }


def initialize_configs():
    seeds = {
        "x": {
            "database": "/state/x/x_auto.sqlite3",
            "cookie_file": "/config/x/x_cookies.txt",
            "download_dir": "/downloads/x",
            "known_stop_consecutive": 10,
            "browser": {"screen_name": "", "headless": True},
        },
        "pixiv": {
            "database": "/state/pixiv/pixiv_auto.sqlite3",
            "refresh_token_file": "/config/pixiv/pixiv_refresh_token.txt",
            "oauth_state_file": "/config/pixiv/pixiv_oauth_state.json",
            "download_dir": "/downloads/pixiv",
            "image_dir": "/downloads/pixiv/images",
            "metadata_dir": "/downloads/pixiv/downloads-metadata",
            "stop_after_consecutive_done": 20,
        },
        "douyin": {
            "download_dir": "/douyin",
            "cookie_file": "/config/douyin/douyin_cookie.txt",
            "f2_state_dir": "/state/douyin/f2",
            "f2_config_dir": "/config/douyin/f2",
            "jobs": [],
            "fallback_stop_consecutive_skipped": 50,
        },
        "xhs": {
            "database": "/state/xhs/xhs_queue.sqlite3",
            "queue_files": ["/queue/xhs/links.txt"],
            "request_delay_seconds": 1,
        },
    }
    for key, value in seeds.items():
        if not CONFIG_PATHS[key].exists():
            write_json(CONFIG_PATHS[key], value)
        elif key in {"x", "pixiv", "douyin"}:
            field, default = THRESHOLDS[key]
            existing = read_json(CONFIG_PATHS[key])
            if field not in existing:
                existing[field] = default
                write_json(CONFIG_PATHS[key], existing)


def sanitize(message):
    import re

    text = str(message)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?i)(cookie\s*[:=]).*", r"\1 [已隐藏]", text)
    text = re.sub(
        r"(?i)((?:token|api_hash|api_key|password|authorization)\s*[=:]\s*)[^\s,;]+",
        r"\1[已隐藏]",
        text,
    )
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[参数已隐藏]", text)
    # Known configured credentials also disappear from third-party exception text.
    configs = [read_json(path) for path in CONFIG_PATHS.values()]
    configs.append(
        read_json(os.getenv("XHS_SETTINGS_PATH", "/xhs-volume/settings.json"))
    )
    for config in configs:
        for name, value in flatten(config).items():
            if name.split(".")[-1] in SECRET_KEYS and isinstance(value, str) and value:
                text = text.replace(value, "[已隐藏]")
        for key in ("cookie_file", "refresh_token_file"):
            path = Path(config.get(key) or "/nonexistent")
            if path.is_file() and path.stat().st_size < 1024 * 1024:
                credential = path.read_text(
                    encoding="utf-8-sig", errors="replace"
                ).strip()
                if credential:
                    text = text.replace(credential, "[已隐藏]")
                # Netscape cookies are tab separated; mask token values in exceptions.
                for line in credential.splitlines():
                    parts = line.split("\t")
                    if len(parts) == 7 and len(parts[-1]) >= 8:
                        text = text.replace(parts[-1], "[已隐藏]")
    return text
