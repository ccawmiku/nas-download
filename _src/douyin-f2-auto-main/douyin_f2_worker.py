#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import importlib.resources
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import yaml

COMMON_PATH = Path(__file__).resolve().parents[2] / "_common"
if COMMON_PATH.exists():
    sys.path.insert(0, str(COMMON_PATH))

try:
    from nas_auto_common.ui import app_css
except ModuleNotFoundError:
    def app_css(extra: str = "") -> str:
        return extra

try:
    import f2
except Exception:
    f2 = None


DEFAULT_CONFIG_PATH = Path("/config/config.json")
DEFAULT_CONFIG: dict[str, Any] = {
    "run_interval_hours": 12,
    "run_timeout_seconds": 180,
    "max_job_runtime_seconds": 300,
    "fallback_stop_consecutive_skipped": 10,
    "cookie_file": "/config/douyin/douyin_cookie.txt",
    "f2_state_dir": "/state/douyin/f2",
    "f2_config_dir": "/config/douyin/f2",
    "download_dir": "/douyin",
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
            "label": "点赞",
            "enabled": True,
            "mode": "like",
            "url": "https://www.douyin.com/user/REPLACE_WITH_YOUR_USER_ID",
        },
        {
            "name": "collection",
            "label": "收藏",
            "enabled": True,
            "mode": "collection",
            "url": "https://www.douyin.com/user/REPLACE_WITH_YOUR_USER_ID",
        },
    ],
    "web": {"host": "0.0.0.0", "port": 8080, "log_lines": 5000},
}

DOUYIN_COOKIE_DOMAINS = {"douyin.com", ".douyin.com", "www.douyin.com", ".www.douyin.com"}
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
COOKIE_CRITICAL_NAMES = (
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "sid_guard",
    "sid_ucp_v1",
    "ssid_ucp_v1",
    "ttwid",
    "UIFID",
    "UIFID_TEMP",
    "s_v_web_id",
)
COOKIE_LINE_RE = re.compile(r"^\s*cookie\s*:\s*", re.IGNORECASE)
YAML_KEY_RE = re.compile(r"^\s*[A-Za-z0-9_-]+\s*:\s*")
URL_QUERY_RE = re.compile(r"(https?://[^\s?]+)\?[^\s]+")
COOKIE_ASSIGN_RE = re.compile(r"([A-Za-z0-9_.-]+)=([^;]+)")
SENSITIVE_COOKIE_NAMES = {
    "__ac_nonce",
    "__ac_signature",
    "__security_mc_1_s_sdk_cert_key",
    "__security_mc_1_s_sdk_crypt_sdk",
    "__security_mc_1_s_sdk_sign_data_key_web_protect",
    "__security_server_data_status",
    "_bd_ticket_crypt_cookie",
    "bd_ticket_guard_client_data",
    "bd_ticket_guard_client_data_v2",
    "msToken",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "passport_mfa_token",
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "sid_ucp_v1",
    "ssid_ucp_v1",
    "ttwid",
    "UIFID",
    "UIFID_TEMP",
}
COOKIE_REQUIRED_NAMES = ("sessionid", "ttwid")
COOKIE_YAML_PLACEHOLDER = "__DOUYIN_COOKIE_PLACEHOLDER__"
DOUYIN_CONTENT_ID_RE = re.compile(r"\[(\d{10,})\]")
DOUYIN_SKIP_RE = re.compile(r"\[\s*跳过\s*\]|跳过")
DOUYIN_DONE_RE = re.compile(r"\[\s*完成\s*\]|完成")


@dataclass
class RunResult:
    job: str
    status: str
    returncode: int | None
    started_at: str
    finished_at: str
    message: str


class RingLog:
    def __init__(self, max_lines: int = 400):
        self.max_lines = max_lines
        self._lock = threading.Lock()
        self._lines: list[str] = []

    def write(self, message: str) -> None:
        safe_message = sanitize_log_message(message)
        if not safe_message:
            return
        line = f"[{now_iso()}] {safe_message}"
        print(line, flush=True)
        with self._lock:
            self._lines.append(line)
            self._lines = self._lines[-self.max_lines :]

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return deep_merge(DEFAULT_CONFIG, json.loads(path.read_text(encoding="utf-8-sig")))
        except Exception:
            traceback.print_exc()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def build_f2_runtime_conf(template: dict[str, Any] | None = None) -> dict[str, Any]:
    config = json.loads(json.dumps(template or {}))
    f2_conf = config.setdefault("f2", {})
    f2_conf["enable_bark"] = False
    return config


def load_f2_runtime_conf() -> dict[str, Any]:
    if f2 is None:
        return build_f2_runtime_conf()
    try:
        package_conf = importlib.resources.files("f2").joinpath("conf/conf.yaml")
        template = yaml.safe_load(package_conf.read_text(encoding="utf-8")) or {}
        return build_f2_runtime_conf(template)
    except Exception:
        return build_f2_runtime_conf()


def read_cookie(path_value: str) -> str:
    path = Path(str(path_value or ""))
    if not path.exists() or not path.is_file():
        return ""
    return normalize_cookie_text(path.read_text(encoding="utf-8-sig"))


def parse_cookie_pairs(cookie_text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in str(cookie_text or "").replace("\r", "").split(";"):
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


def extract_cookie_block(text: str) -> str:
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
    return " ".join(item for item in collected if item).strip()


def normalize_cookie_text(text: str) -> str:
    rows: list[tuple[str, str]] = []
    for line in str(text or "").replace("\r", "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 7:
            domain = parts[0].strip().removeprefix("#HttpOnly_")
            if domain not in DOUYIN_COOKIE_DOMAINS:
                continue
            name = parts[5].strip()
            value = parts[6].strip()
            if name:
                rows.append((name, value))
    if rows:
        rows = select_douyin_cookie_pairs(rows)
        return "; ".join(f"{name}={value}" for name, value in rows)
    pairs = select_douyin_cookie_pairs(parse_cookie_pairs(extract_cookie_block(text)))
    return "; ".join(f"{name}={value}" for name, value in pairs)


def render_cookie_block_lines(cookie_text: str, base_indent: str = "") -> list[str]:
    values = {name: value for name, value in parse_cookie_pairs(cookie_text)}
    grouped_parts: list[list[str]] = []
    for group in DOUYIN_REFERENCE_COOKIE_LINE_GROUPS:
        line_parts = [f"{name}={values[name]}" for name in group if name in values]
        if line_parts:
            grouped_parts.append(line_parts)
    # Preserve new security tokens throughout the saved-cookie and f2 YAML path.
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


def render_cookie_block(cookie_text: str, base_indent: str = "") -> str:
    return "\n".join(render_cookie_block_lines(cookie_text, base_indent)) + "\n"


def render_douyin_job_yaml(douyin: dict[str, Any]) -> str:
    payload = {"douyin": dict(douyin, cookie=COOKIE_YAML_PLACEHOLDER)}
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100000)
    placeholder_pattern = re.compile(
        rf"^  cookie:\s+['\"]?{re.escape(COOKIE_YAML_PLACEHOLDER)}['\"]?\s*$"
    )
    output_lines: list[str] = []
    replaced = False
    for line in rendered.splitlines():
        if placeholder_pattern.match(line):
            output_lines.extend(render_cookie_block_lines(str(douyin.get("cookie") or ""), "  "))
            replaced = True
            continue
        output_lines.append(line)
    if not replaced:
        raise RuntimeError("未找到抖音 Cookie 占位符，无法生成参考格式 YAML")
    return "\n".join(output_lines) + "\n"


def f2_network_hint(line: str) -> str:
    lowered = line.lower()
    redirect = re.search(r"redirect response\s+['\"]?(?:301|302|303|307|308)\b", lowered)
    portal = re.search(r"\b(?:gportal|captive[ -]portal)\b", lowered)
    if redirect or portal:
        return "网络请求被重定向，可能需要网络认证、宽带续费或检查代理连接"
    return ""


def build_douyin_job_payload(config: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    cookie = read_cookie(str(config.get("cookie_file") or ""))
    defaults = dict(config.get("defaults") or {})
    return {
        "cookie": cookie,
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


def cookie_summary(cookie_text: str) -> dict[str, Any]:
    pairs = parse_cookie_pairs(cookie_text)
    names = [name for name, _value in pairs]
    available = set(names)
    missing_required = [name for name in COOKIE_REQUIRED_NAMES if name not in available]
    missing_critical = [name for name in COOKIE_CRITICAL_NAMES if name not in available]
    missing_reference = [name for name in DOUYIN_REFERENCE_COOKIE_ORDER if name not in available]
    reference_total = len(DOUYIN_REFERENCE_COOKIE_ORDER)
    reference_present = reference_total - len(missing_reference)
    if not pairs:
        status = "未导入"
        risk = "未导入"
    elif missing_critical:
        status = "高风险"
        risk = "关键字段不完整"
    elif reference_present < reference_total:
        status = "有风险"
        risk = "参考字段不完整"
    else:
        status = "正常"
        risk = "正常"
    return {
        "present": bool(pairs),
        "length": len(cookie_text),
        "fields": len(pairs),
        "names": names,
        "has_sessionid": "sessionid" in available,
        "has_ttwid": "ttwid" in available,
        "missing_required": missing_required,
        "missing_critical": missing_critical,
        "missing_reference": missing_reference,
        "reference_present": reference_present,
        "reference_total": reference_total,
        "risk": risk,
        "status": status,
    }


def cookie_summary_text(cookie_text: str) -> str:
    summary = cookie_summary(cookie_text)
    if not summary["present"]:
        return "未导入"
    base = (
        f"{summary['fields']} 项 / {summary['length']} 字符"
        f"，参考 {summary['reference_present']}/{summary['reference_total']}"
    )
    if summary["missing_critical"]:
        return f"{base}，关键字段缺少 {', '.join(summary['missing_critical'])}"
    if summary["missing_reference"]:
        return f"{base}，缺少 {len(summary['missing_reference'])} 项参考字段"
    return f"{base}，字段完整"


def sanitize_log_message(message: str, max_length: int = 640) -> str:
    text = str(message or "").replace("\r", "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "cookie:" in lowered:
        cookie_text = normalize_cookie_text(text)
        return re.sub(
            r"cookie\s*:.*",
            f"cookie: [redacted {cookie_summary_text(cookie_text)}]",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    if text.count(";") >= 2:
        pairs = parse_cookie_pairs(text)
        sensitive_names = {name for name, _value in pairs if name in SENSITIVE_COOKIE_NAMES}
        if pairs and sensitive_names:
            return f"[cookie redacted {cookie_summary_text(text)}]"
    text = URL_QUERY_RE.sub(r"\1?[redacted]", text)
    text = COOKIE_ASSIGN_RE.sub(
        lambda match: f"{match.group(1)}=[redacted]" if match.group(1) in SENSITIVE_COOKIE_NAMES else match.group(0),
        text,
    )
    if len(text) > max_length:
        extra = len(text) - max_length
        text = f"{text[:max_length]} ... [truncated {extra} chars]"
    return text


def bool_form(form: dict[str, list[str]], key: str) -> bool:
    return (form.get(key) or [""])[0] in {"1", "true", "on", "yes"}


def job_key(job: dict[str, Any], index: int) -> str:
    value = str(job.get("name") or "").strip()
    return value or f"job{index}"


def version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in str(value or "").split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


class F2SkipStopGuard:
    def __init__(self, threshold: int):
        self.threshold = max(0, int(threshold or 0))
        self.current_id = ""
        self.current_skipped = 0
        self.current_done = 0
        self.consecutive_skipped = 0
        self.triggered = False
        self.trigger_id = ""

    def observe(self, line: str) -> bool:
        if self.threshold <= 0 or self.triggered:
            return False
        match = DOUYIN_CONTENT_ID_RE.search(line)
        if match:
            self._finish_current()
            self.current_id = match.group(1)
            self.current_skipped = 0
            self.current_done = 0
        if self.current_id:
            if DOUYIN_DONE_RE.search(line):
                self.current_done += 1
            if DOUYIN_SKIP_RE.search(line):
                self.current_skipped += 1
        return self.triggered

    def finish(self) -> bool:
        self._finish_current()
        return self.triggered

    def _finish_current(self) -> None:
        if not self.current_id or self.triggered:
            return
        if self.current_skipped > 0 and self.current_done == 0:
            self.consecutive_skipped += 1
        elif self.current_done > 0:
            self.consecutive_skipped = 0
        if self.threshold > 0 and self.consecutive_skipped >= self.threshold:
            self.triggered = True
            self.trigger_id = self.current_id
        self.current_id = ""
        self.current_skipped = 0
        self.current_done = 0


class App:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.log = RingLog(int(self.config.get("web", {}).get("log_lines", 5000)))
        self.run_lock = threading.Lock()
        self.run_request_lock = threading.Lock()
        self.running = False
        self.run_pending = False
        self.stop_event = threading.Event()
        self.stop_run_event = threading.Event()
        self.next_run_at = 0.0
        self.current_job = ""
        self.current_proc: subprocess.Popen[str] | None = None
        self.current_proc_lock = threading.Lock()
        self.last_results: list[RunResult] = []
        self.last_run_message = ""
        self.last_notice = ""
        self.f2_installed_version = str(getattr(f2, "__version__", "") or "unknown")
        self.f2_latest_version = ""
        self.f2_version_checked_at = ""
        self.f2_version_message = "尚未检查"
        self._job_config_signature = ""
        self._last_config_mtime = 0.0
        try:
            if self.config_path.exists():
                self._last_config_mtime = self.config_path.stat().st_mtime
        except OSError:
            pass
        self.ensure_dirs()
        self.start_version_check_thread()

    def ensure_dirs(self, sync_configs: bool = True) -> None:
        for key in ["f2_state_dir", "f2_config_dir", "download_dir"]:
            Path(str(self.config.get(key) or "")).mkdir(parents=True, exist_ok=True)
        Path(str(self.config.get("cookie_file") or "")).parent.mkdir(parents=True, exist_ok=True)
        state_dir = Path(str(self.config.get("f2_state_dir") or "/state/douyin/f2"))
        runtime_conf = state_dir / "conf" / "conf.yaml"
        runtime_conf.parent.mkdir(parents=True, exist_ok=True)
        new_conf_text = yaml.safe_dump(load_f2_runtime_conf(), allow_unicode=True, sort_keys=False, width=100000)
        should_write = True
        if runtime_conf.exists():
            try:
                if runtime_conf.read_text(encoding="utf-8", errors="ignore") == new_conf_text:
                    should_write = False
            except OSError:
                pass
        if should_write:
            runtime_conf.write_text(new_conf_text, encoding="utf-8")
        if sync_configs:
            self.sync_job_configs()

    def job_config_signature(self) -> str:
        payload = {
            "cookie": read_cookie(str(self.config.get("cookie_file") or "")),
            "download_dir": self.config.get("download_dir"),
            "defaults": self.config.get("defaults"),
            "f2_config_dir": self.config.get("f2_config_dir"),
            "jobs": self.config.get("jobs"),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def sync_job_configs(self) -> None:
        signature = self.job_config_signature()
        if signature == self._job_config_signature:
            return
        for index, job in enumerate(self.config.get("jobs") or []):
            key = job_key(job, index)
            config_path = Path(str(self.config.get("f2_config_dir") or "/config/douyin/f2")) / f"{key}.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(render_douyin_job_yaml(build_douyin_job_payload(self.config, job)), encoding="utf-8")
        self._job_config_signature = signature

    def reload_config(self, force: bool = False) -> None:
        try:
            mtime = self.config_path.stat().st_mtime if self.config_path.exists() else 0.0
        except OSError:
            mtime = 0.0
        if not force and mtime == self._last_config_mtime:
            return
        self._last_config_mtime = mtime
        self.config = load_config(self.config_path)
        self.log.max_lines = int(self.config.get("web", {}).get("log_lines", 5000))
        self.ensure_dirs()

    def save_config(self, patch: dict[str, Any]) -> None:
        self.config = deep_merge(self.config, patch)
        save_config(self.config_path, self.config)
        self.ensure_dirs()

    def cookie_present(self) -> bool:
        return bool(read_cookie(str(self.config.get("cookie_file") or "")))

    def interval_seconds(self) -> int:
        return max(60, int(float(self.config.get("run_interval_hours") or 12) * 3600))

    def schedule_next_run(self) -> None:
        self.next_run_at = time.time() + self.interval_seconds()

    def set_notice(self, message: str) -> None:
        self.last_notice = message

    def save_cookie(self, text: str) -> str:
        normalized = normalize_cookie_text(text)
        if not normalized:
            raise ValueError("未识别到可保存的抖音 Cookie")
        output = Path(str(self.config.get("cookie_file") or ""))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_cookie_block(normalized), encoding="utf-8")
        self.sync_job_configs()
        summary_text = cookie_summary_text(normalized)
        self.log.write(f"已更新抖音 Cookie：{summary_text}")
        self.set_notice(f"抖音 Cookie 已保存：{summary_text}")
        return normalized

    def request_stop(self) -> bool:
        with self.run_request_lock:
            if not self.running and not self.run_pending:
                message = "当前没有运行中的抖音任务"
                self.set_notice(message)
                self.log.write(message)
                return False
            self.stop_run_event.set()
        proc: subprocess.Popen[str] | None
        with self.current_proc_lock:
            proc = self.current_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            message = "已请求手动停止当前抖音任务"
        else:
            message = "已请求手动停止，当前任务结束后不会继续后续任务"
        self.set_notice(message)
        self.log.write(message)
        return True

    def db_status(self) -> dict[str, Any]:
        state_dir = Path(str(self.config.get("f2_state_dir") or "/state/douyin/f2"))
        result: dict[str, Any] = {}
        for name in ["douyin_users.db", "douyin_videos.db"]:
            path = state_dir / name
            count = None
            if path.exists():
                try:
                    table = "user_info_web" if "users" in name else "video_info"
                    with sqlite3.connect(path) as conn:
                        count = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
                except Exception:
                    count = None
            result[name] = {
                "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else 0,
                "rows": count,
                "required": name == "douyin_users.db",
            }
        return result

    def check_f2_version(self) -> None:
        self.f2_version_message = "检查中"
        try:
            with urllib.request.urlopen("https://pypi.org/pypi/f2/json", timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            latest = str((payload.get("info") or {}).get("version") or "")
            self.f2_latest_version = latest
            self.f2_version_checked_at = now_iso()
            if latest and version_tuple(self.f2_installed_version) < version_tuple(latest):
                self.f2_version_message = f"发现新版本 {latest}"
                self.log.write(f"f2 版本检查：当前 {self.f2_installed_version}，最新 {latest}")
            elif latest:
                self.f2_version_message = "已是最新"
                self.log.write(f"f2 版本检查：当前 {self.f2_installed_version}，最新 {latest}")
            else:
                self.f2_version_message = "无法读取最新版本"
        except Exception as error:
            self.f2_version_checked_at = now_iso()
            self.f2_version_message = f"检查失败：{error}"
            self.log.write(f"f2 版本检查失败：{error}")

    def start_version_check_thread(self) -> None:
        threading.Thread(target=self.check_f2_version, daemon=True).start()

    def f2_config_for_job(self, job: dict[str, Any], index: int) -> Path:
        douyin = build_douyin_job_payload(self.config, job)
        config_path = Path(str(self.config.get("f2_config_dir") or "/config/douyin/f2")) / f"{job_key(job, index)}.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(render_douyin_job_yaml(douyin), encoding="utf-8")
        return config_path

    def run_job(self, job: dict[str, Any], index: int) -> RunResult:
        key = job_key(job, index)
        started_at = now_iso()
        self.current_job = key
        if not str(job.get("url") or "").strip():
            message = "URL 为空，已跳过"
            self.log.write(f"{key}: {message}")
            return RunResult(key, "skipped", None, started_at, now_iso(), message)
        config_path = self.f2_config_for_job(job, index)
        fallback_stop = int(self.config.get("fallback_stop_consecutive_skipped") or 10)
        max_runtime = max(0, int(self.config.get("max_job_runtime_seconds") or 300))
        guard = F2SkipStopGuard(fallback_stop)
        command = [sys.executable, "-m", "f2", "dy", "-c", str(config_path)]
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "COLUMNS": "160"})
        self.log.write(f"{key}: 启动 f2，配置 {config_path}")
        proc = subprocess.Popen(
            command,
            cwd=str(Path(str(self.config.get("f2_state_dir") or "/state/douyin/f2"))),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with self.current_proc_lock:
            self.current_proc = proc
        assert proc.stdout is not None
        stopped_by_guard = False
        stopped_by_timeout = False
        network_hint = ""

        def reader() -> None:
            nonlocal stopped_by_guard, network_hint
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    self.log.write(f"{key}: {line}")
                    hint = f2_network_hint(line)
                    if hint and not network_hint:
                        network_hint = hint
                        self.log.write(f"{key}: {hint}")
                    if guard.observe(line) and proc.poll() is None:
                        stopped_by_guard = True
                        self.log.write(
                            f"{key}: 连续 {guard.consecutive_skipped} 个作品均为跳过，"
                            f"已停止本项目（最后作品 {guard.trigger_id}）"
                        )
                        try:
                            proc.terminate()
                        except Exception:
                            pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            returncode = proc.wait(timeout=max_runtime if max_runtime > 0 else None)
        except subprocess.TimeoutExpired:
            stopped_by_timeout = True
            self.log.write(f"{key}: 达到最大运行时长 {max_runtime} 秒，已停止本项目")
            try:
                proc.terminate()
                returncode = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()
        finally:
            with self.current_proc_lock:
                if self.current_proc is proc:
                    self.current_proc = None
        thread.join(timeout=2)
        if not stopped_by_guard and guard.finish():
            stopped_by_guard = True
        if stopped_by_guard:
            status = "done"
            message = f"连续 {guard.consecutive_skipped} 个作品均为跳过，已停止本项目"
        elif stopped_by_timeout:
            status = "done"
            message = f"达到最大运行时长 {max_runtime} 秒，已停止本项目"
        elif self.stop_run_event.is_set() and returncode not in {0, None}:
            status = "stopped"
            message = "已手动停止"
        elif returncode == 0:
            status = "done"
            message = "ok"
        else:
            status = "failed"
            message = f"f2 exited with {returncode}" + (f"：{network_hint}" if network_hint else "")
        self.log.write(f"{key}: {message}")
        return RunResult(key, status, returncode, started_at, now_iso(), message)

    def run_once(self, only_job: str = "") -> list[RunResult]:
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("已有抖音任务正在运行")
        self.running = True
        self.current_job = ""
        results: list[RunResult] = []
        try:
            self.reload_config()
            if not self.cookie_present():
                raise RuntimeError("未找到抖音 Cookie，请先在抖音页或统一主页导入 Cookie")
            self.log.write("抖音 f2 运行开始")
            jobs = list(self.config.get("jobs") or [])
            for index, job in enumerate(jobs):
                if self.stop_run_event.is_set():
                    self.log.write("收到手动停止请求，剩余任务已跳过")
                    break
                key = job_key(job, index)
                if only_job and key != only_job:
                    continue
                if not bool(job.get("enabled", True)) and not only_job:
                    continue
                results.append(self.run_job(job, index))
            self.last_results = results[-20:]
            failures = [item for item in results if item.status not in {"done", "skipped", "stopped"}]
            self.last_run_message = f"{len(results)} job(s), {len(failures)} failed"
            if self.stop_run_event.is_set():
                self.last_run_message += ", stopped"
            self.log.write(f"抖音 f2 运行结束：{self.last_run_message}")
            return results
        except Exception as error:
            self.last_run_message = str(error)
            self.log.write(f"抖音 f2 运行失败：{error}")
            self.log.write(traceback.format_exc())
            raise
        finally:
            self.current_job = ""
            self.running = False
            self.stop_run_event.clear()
            self.run_lock.release()

    def start_run_thread(self, only_job: str = "") -> bool:
        with self.run_request_lock:
            if self.running or self.run_pending:
                target = only_job or "全部任务"
                message = f"{target}: 已有抖音任务在运行，已阻止重复启动"
                self.log.write(message)
                self.set_notice(message)
                return False
            self.stop_run_event.clear()
            self.run_pending = True
            self.schedule_next_run()
        threading.Thread(target=lambda: self._thread_wrap(self.run_once, only_job), daemon=True).start()
        return True

    def _thread_wrap(self, fn: Any, *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            pass
        finally:
            with self.run_request_lock:
                self.run_pending = False

    def scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            self.reload_config()
            if self.next_run_at <= 0:
                self.schedule_next_run()
            if time.time() >= self.next_run_at and not self.running and not self.run_pending:
                if self.cookie_present():
                    self.start_run_thread()
                else:
                    self.log.write("未找到抖音 Cookie，自动运行暂缓")
                    self.next_run_at = time.time() + 60
            self.stop_event.wait(10)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "stop_requested": self.stop_run_event.is_set(),
            "current_job": self.current_job,
            "next_run_at": datetime.fromtimestamp(self.next_run_at).isoformat() if self.next_run_at else "",
            "cookie_present": self.cookie_present(),
            "cookie_summary": cookie_summary(read_cookie(str(self.config.get("cookie_file") or ""))),
            "config": self.config,
            "db": self.db_status(),
            "f2_version": {
                "installed": self.f2_installed_version,
                "latest": self.f2_latest_version,
                "checked_at": self.f2_version_checked_at,
                "message": self.f2_version_message,
            },
            "last_results": [item.__dict__ for item in self.last_results],
            "last_run_message": self.last_run_message,
            "notice": self.last_notice,
            "logs": self.log.lines(),
        }


def html_page(app: App) -> str:
    data = app.status()
    cfg = data["config"]
    cookie_info = data["cookie_summary"]
    jobs_html = ""
    for index, job in enumerate(cfg.get("jobs") or []):
        key = job_key(job, index)
        checked = " checked" if job.get("enabled", True) else ""
        jobs_html += f"""
        <div class="job">
          <label><input type="checkbox" name="enabled_{index}"{checked}> {html.escape(str(job.get("label") or key))}</label>
          <input type="hidden" name="name_{index}" value="{html.escape(key)}">
          <input type="hidden" name="mode_{index}" value="{html.escape(str(job.get("mode") or ""))}">
          <input name="url_{index}" value="{html.escape(str(job.get("url") or ""))}">
          <button formaction="/run-job?name={html.escape(key)}" type="submit">运行</button>
        </div>"""
    page_style = app_css(
        """
.grid{grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.job{display:grid;grid-template-columns:150px minmax(260px,1fr) 72px;gap:8px;align-items:center;margin:8px 0}
@media(max-width:820px){.job{grid-template-columns:1fr}}
"""
    )
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Douyin F2 Downloader</title>
<style>
{page_style}
</style></head><body>
<header><h1>Douyin F2 Downloader</h1><div><span class="pill" id="runState">运行：{"运行中" if data["running"] else "空闲"}</span> <span class="pill" id="cookieState">Cookie：{html.escape(str(cookie_info["status"]))}</span></div></header>
<main>
{f'<section class="ok" id="noticeBox">{html.escape(str(data["notice"]))}</section>' if data["notice"] else '<section class="ok" id="noticeBox" style="display:none"></section>'}
<section><h2>控制</h2><div class="muted">下一次自动运行：<span id="nextRunAt">{html.escape(data["next_run_at"] or "未排程")}</span>；当前任务：<span id="currentJob">{html.escape(data["current_job"] or "-")}</span></div>
<div class="actions"><form method="post" action="/run"><button type="submit">立即运行全部</button></form><form method="post" action="/stop"><button class="secondary" type="submit">手动停止</button></form><form method="post" action="/reload"><button class="secondary" type="submit">重新读取配置</button></form><form method="post" action="/check-version"><button class="secondary" type="submit">检查 f2 版本</button></form></div></section>
<section><h2>抖音 Cookie</h2><div class="grid">
<div>状态<br><strong id="cookieStatus">{html.escape(str(cookie_info["status"]))}</strong></div>
<div>字段数<br><strong id="cookieFields">{html.escape(str(cookie_info["fields"]))}</strong></div>
<div>长度<br><strong id="cookieLength">{html.escape(str(cookie_info["length"]))}</strong></div>
<div>关键字段<br><strong id="cookieRequired">{html.escape("完整" if not cookie_info["missing_critical"] and cookie_info["present"] else ", ".join(cookie_info["missing_critical"]) or "-")}</strong></div>
<div>参考字段<br><strong id="cookieReference">{html.escape(f'{cookie_info["reference_present"]}/{cookie_info["reference_total"]}')}</strong></div>
<div>风险提示<br><strong id="cookieRisk">{html.escape(str(cookie_info["risk"]))}</strong></div>
</div>
<p class="muted">风险规则：关键字段缺失视为高风险；参考字段未满 {len(DOUYIN_REFERENCE_COOKIE_ORDER)} 项视为有风险；满 {len(DOUYIN_REFERENCE_COOKIE_ORDER)} 项才显示正常。</p>
<p class="muted" id="cookieMissingRef">缺失参考字段：{html.escape(", ".join(cookie_info["missing_reference"][:16]) + (" ..." if len(cookie_info["missing_reference"]) > 16 else "") if cookie_info["missing_reference"] else "无")}</p>
<p class="muted">支持直接粘贴 `app.yaml` 里的 `cookie:` 段或单行 Cookie header。保存时会按本地参考 `app.yaml` 的字段和顺序拼接，其他字段会丢弃，并过滤非 ASCII 值；生成给 f2 的 YAML 会继续按参考 `app.yaml` 的分号换行和缩进输出。页面和日志都不会显示明文。</p>
<form method="post" action="/cookie">
<textarea name="cookie_text" placeholder="cookie: sessionid=...; ttwid=..."></textarea>
<div class="actions"><button type="submit">保存抖音 Cookie</button></div>
</form></section>
<section><h2>f2 版本</h2><div class="grid">
<div>当前版本<br><strong>{html.escape(str(data["f2_version"]["installed"]))}</strong></div>
<div>最新版本<br><strong>{html.escape(str(data["f2_version"]["latest"] or "-"))}</strong></div>
<div>检查时间<br><strong>{html.escape(str(data["f2_version"]["checked_at"] or "-"))}</strong></div>
<div>状态<br><strong>{html.escape(str(data["f2_version"]["message"]))}</strong></div>
</div></section>
<section><h2>配置</h2><form method="post" action="/settings">
<div class="grid"><label>运行间隔（小时）<input name="run_interval_hours" type="number" min="0.1" step="0.1" value="{html.escape(str(cfg.get("run_interval_hours") or 12))}"></label>
<label>连续跳过作品停止数<input name="fallback_stop_consecutive_skipped" type="number" min="1" step="1" value="{html.escape(str(cfg.get("fallback_stop_consecutive_skipped") or 10))}"></label>
<label>最大运行时长（秒）<input name="max_job_runtime_seconds" type="number" min="0" step="1" value="{html.escape(str(cfg.get("max_job_runtime_seconds") or 300))}"></label>
<label>下载目录<input name="download_dir" value="{html.escape(str(cfg.get("download_dir") or ""))}"></label>
<label>f2 数据目录<input name="f2_state_dir" value="{html.escape(str(cfg.get("f2_state_dir") or ""))}"></label></div>
{jobs_html}
<div class="actions"><button type="submit">保存配置</button></div></form></section>
<section><h2>数据库</h2><div class="grid">
<div>douyin_users.db<br><strong>{data["db"]["douyin_users.db"]["rows"] if data["db"]["douyin_users.db"]["rows"] is not None else "-"}</strong><br><span class="muted">点赞/收藏记录</span></div>
<div>douyin_videos.db<br><strong>{data["db"]["douyin_videos.db"]["rows"] if data["db"]["douyin_videos.db"]["rows"] is not None else "-"}</strong><br><span class="muted">没有也可运行</span></div>
</div></section>
<section><h2>最近结果</h2><pre id="resultsBox">{html.escape(json.dumps(data["last_results"], ensure_ascii=False, indent=2))}</pre></section>
<section><h2>日志</h2><pre id="logBox">{html.escape(chr(10).join(data["logs"][-120:]))}</pre></section>
</main>
<script>
const refreshStatus = async () => {{
  try {{
    const resp = await fetch("/api/status", {{ cache: "no-store" }});
    if (!resp.ok) return;
    const data = await resp.json();
    const cookie = data.cookie_summary || {{}};
    document.getElementById("runState").textContent = `运行：${{data.running ? "运行中" : "空闲"}}`;
    document.getElementById("cookieState").textContent = `Cookie：${{cookie.status || "未导入"}}`;
    document.getElementById("nextRunAt").textContent = data.next_run_at || "未排程";
    document.getElementById("currentJob").textContent = data.current_job || "-";
    document.getElementById("cookieStatus").textContent = cookie.status || "未导入";
    document.getElementById("cookieFields").textContent = String(cookie.fields || 0);
    document.getElementById("cookieLength").textContent = String(cookie.length || 0);
    document.getElementById("cookieRequired").textContent = cookie.present ? ((cookie.missing_critical || []).length ? cookie.missing_critical.join(", ") : "完整") : "-";
    document.getElementById("cookieReference").textContent = `${{cookie.reference_present || 0}}/${{cookie.reference_total || 0}}`;
    document.getElementById("cookieRisk").textContent = cookie.risk || "未导入";
    document.getElementById("cookieMissingRef").textContent = "缺失参考字段：" + ((cookie.missing_reference || []).length ? (cookie.missing_reference.length > 16 ? cookie.missing_reference.slice(0, 16).join(", ") + " ..." : cookie.missing_reference.join(", ")) : "无");
    document.getElementById("resultsBox").textContent = JSON.stringify(data.last_results || [], null, 2);
    document.getElementById("logBox").textContent = (data.logs || []).slice(-120).join("\\n");
    const noticeBox = document.getElementById("noticeBox");
    if (data.notice) {{
      noticeBox.style.display = "";
      noticeBox.textContent = data.notice;
    }}
  }} catch (_error) {{
  }}
}};
setInterval(refreshStatus, 5000);
</script>
</body></html>"""
    return body


def redirect(handler: BaseHTTPRequestHandler, location: str = "/") -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.end_headers()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/api/status"):
                body = json.dumps(app.status(), ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = html_page(app).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            if self.path == "/run":
                app.start_run_thread()
                redirect(self)
                return
            if self.path == "/stop":
                app.request_stop()
                redirect(self)
                return
            if self.path.startswith("/run-job"):
                name = self.path.split("name=", 1)[1] if "name=" in self.path else ""
                app.start_run_thread(name)
                redirect(self)
                return
            if self.path == "/cookie":
                try:
                    app.save_cookie((form.get("cookie_text") or [""])[0])
                except ValueError as error:
                    app.set_notice(str(error))
                    app.log.write(str(error))
                redirect(self)
                return
            if self.path == "/reload":
                app.reload_config()
                app.set_notice("已重新读取抖音配置")
                redirect(self)
                return
            if self.path == "/check-version":
                app.start_version_check_thread()
                app.set_notice("已触发 f2 版本检查")
                redirect(self)
                return
            if self.path == "/settings":
                jobs = []
                old_jobs = list(app.config.get("jobs") or [])
                for index, old_job in enumerate(old_jobs):
                    jobs.append(
                        {
                            "name": (form.get(f"name_{index}") or [job_key(old_job, index)])[0],
                            "label": old_job.get("label") or (form.get(f"name_{index}") or [job_key(old_job, index)])[0],
                            "enabled": bool_form(form, f"enabled_{index}"),
                            "mode": (form.get(f"mode_{index}") or [old_job.get("mode") or "like"])[0],
                            "url": (form.get(f"url_{index}") or [old_job.get("url") or ""])[0],
                        }
                    )
                try:
                    hours = max(0.1, float((form.get("run_interval_hours") or ["12"])[0] or "12"))
                except ValueError:
                    hours = 12.0
                try:
                    fallback_stop = max(
                        1,
                        int((form.get("fallback_stop_consecutive_skipped") or ["10"])[0] or "10"),
                    )
                except ValueError:
                    fallback_stop = 10
                try:
                    max_job_runtime = max(
                        0,
                        int((form.get("max_job_runtime_seconds") or ["300"])[0] or "300"),
                    )
                except ValueError:
                    max_job_runtime = 300
                app.save_config(
                    {
                        "run_interval_hours": hours,
                        "fallback_stop_consecutive_skipped": fallback_stop,
                        "max_job_runtime_seconds": max_job_runtime,
                        "download_dir": (form.get("download_dir") or [app.config.get("download_dir")])[0],
                        "f2_state_dir": (form.get("f2_state_dir") or [app.config.get("f2_state_dir")])[0],
                        "jobs": jobs,
                    }
                )
                app.log.write("已从网页端保存抖音配置")
                app.set_notice("抖音配置已保存")
                redirect(self)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()
    app = App(Path(args.config))
    if args.run_once:
        app.run_once()
        return 0

    def shutdown(_signum: int, _frame: Any) -> None:
        app.stop_event.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=app.scheduler_loop, daemon=True).start()
    web_cfg = app.config.get("web", {})
    host = str(web_cfg.get("host", "0.0.0.0"))
    port = int(web_cfg.get("port", 8080))
    app.log.write(f"Web UI listening on {host}:{port}")
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
