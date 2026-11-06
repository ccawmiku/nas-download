"""Private Bark configuration and persistent, low-frequency incident digests."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import read_json, write_json
from .store import PLATFORMS

DAY = 86400
GRACE = 600
REMINDER = 3 * DAY
STATE_KEY = "bark_notification_state"


def validate_url(value):
    if not isinstance(value, str):
        raise ValueError("Bark 地址格式无效")
    try:
        parsed = urlsplit(value.strip())
        valid = (
            parsed.scheme == "https" and parsed.hostname == "api.day.app"
            and parsed.port in {None, 443} and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/[A-Za-z0-9_-]{8,128}/?", parsed.path)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("请输入完整的 https://api.day.app/密钥/ 地址")
    return "https://api.day.app/" + parsed.path.strip("/") + "/"


def save_settings(path, payload):
    current = read_json(path)
    enabled = payload.get("enabled", current.get("enabled", False))
    if type(enabled) is not bool:
        raise ValueError("通知开关无效")
    url = current.get("url", "")
    if payload.get("url"):
        url = validate_url(payload["url"])
    if enabled and not url:
        raise ValueError("开启通知前请填写 Bark 地址")
    enabled_at = current.get("enabled_at", time.time())
    if enabled and (not current.get("enabled") or url != current.get("url")):
        enabled_at = time.time()
    write_json(path, {"enabled": enabled, "url": url, "enabled_at": enabled_at})


def public_settings(store, path):
    config = read_json(path)
    state = store.get(STATE_KEY, {})
    return {
        "enabled": bool(config.get("enabled")), "configured": bool(config.get("url")),
        "last_sent": state.get("last_sent"), "last_error": state.get("last_error", ""),
        "daily_limit": 1, "reminder_hours": 72, "grace_minutes": 10,
    }


def incidents(store, config, preferences, now):
    """Collect counts and service health only; never push URLs, titles or logs."""
    issues = {}
    enabled_at = config.get("enabled_at", now)
    with store.connect() as db:
        for key, name in PLATFORMS.items():
            recent = db.execute(
                "SELECT state,updated FROM tasks WHERE platform=? AND kind IN ('sync','links','retry') "
                "AND state IN ('complete','failed','limited','interrupted') ORDER BY updated DESC LIMIT 3",
                (key,),
            ).fetchall()
            if len(recent) == 3 and recent[0]["updated"] >= enabled_at and all(row["state"] != "complete" for row in recent):
                issues["execution:" + key] = f"{name} 连续 3 次下载执行未完成，请查看任务与凭证状态"
            count = db.execute(
                "SELECT COUNT(*) FROM records WHERE platform=? AND state IN ('failed','interrupted') "
                "AND updated>=? AND COALESCE(json_extract(metadata,'$.error_dismissed'),0)=0 "
                "AND (platform!='x' OR COALESCE(json_extract(metadata,'$.retry_exhausted'),0)=1)",
                (key, enabled_at),
            ).fetchone()[0]
            if count:
                issues["errors:" + key] = f"{name} 有 {count} 条新增错误尚未处理，请查看错误列表"
        workers = [store.decode(row) for row in db.execute("SELECT * FROM workers")]
    # Containers acquire new worker IDs after upgrades. An old worker row is
    # harmless when another live worker has taken over the same role.
    latest = {}
    for worker in workers:
        for role in worker["roles"]:
            if role not in latest or worker["updated"] > latest[role]["updated"]:
                latest[role] = worker
    for role, worker in latest.items():
        if role not in {*PLATFORMS, "media"}:
            continue
        if role == "media" and not any(preferences.get("workspace", {}).values()):
            continue
        details = worker["details"]
        if role == "telegram" and details.get("connection_status") == "waiting_config":
            continue
        if worker["updated"] < now - 60:
            issues["service:" + role] = PLATFORMS.get(role, "工作区") + " 服务心跳中断"
        elif role == "media" and not details.get("ready"):
            issues["service:media"] = "工作区处理服务持续不可用，新媒体将保留有效原件"
        elif role == "telegram" and details.get("connection_status") == "credential_error":
            issues["telegram:credentials"] = "Telegram 凭证异常，请检查账号配置"
        elif role == "telegram" and details.get("connection_status") in {"retry_wait", "connecting"}:
            issues["telegram:connection"] = "Telegram 持续未连接，请检查网络或账号状态"
    paths = [store.path.parent, Path(preferences.get("workspace_root", "/media/.nas-workspace"))]
    paths.extend(Path(value) for value in os.getenv("NAS_MEDIA_ROOTS", "/media,/downloads,/xhs,/douyin").split(",") if value)
    seen = set()
    for path in paths:
        try:
            device = path.stat().st_dev
            if device in seen:
                continue
            seen.add(device)
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if usage.total and usage.free < 5 * 1024**3 and usage.free / usage.total < 0.05:
            issues["disk:" + str(device)] = f"媒体或状态磁盘空间不足，剩余 {usage.free / 1024**3:.1f} GB（不足 5%）"
    return issues


def send(url, body):
    key = urlsplit(validate_url(url)).path.strip("/")
    # The private key travels in the JSON body, never in a request URL or log.
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.post("https://api.day.app/push", json={
            "device_key": key, "title": "NAS自动下载 · 异常汇总", "body": body,
            "group": "NAS自动下载", "level": "passive", "isArchive": "0",
        })
        response.raise_for_status()
        if response.json().get("code") != 200:
            raise RuntimeError("Bark 未确认接收")


def tick(store, path, preferences, now=None, sender=None):
    now = time.time() if now is None else now
    config = read_json(path)
    if not config.get("enabled") or not config.get("url"):
        return
    current = incidents(store, config, preferences, now)
    state = store.get(STATE_KEY, {})
    observed = state.get("observed", {})
    state["observed"] = {key: observed.get(key, now) for key in current}
    sent = state.setdefault("sent", {})
    due = [key for key in current if now - state["observed"][key] >= GRACE and now - sent.get(key, -REMINDER) >= REMINDER]
    daily_ready = now - state.get("last_sent", -DAY) >= DAY
    retry_ready = now - state.get("last_attempt", -3600) >= 3600
    if due and daily_ready and retry_ready:
        state["last_attempt"] = now
        # Persist the attempt before network I/O so repeated crashes do not spam.
        store.set(STATE_KEY, state)
        try:
            (sender or send)(config["url"], "\n".join(current[key] for key in sorted(due)))
        except Exception:
            state["last_error"] = "通知发送未成功，将限频重试；请检查 Bark 配置或网络"
        else:
            state["last_sent"] = now
            state["last_error"] = ""
            for key in due:
                sent[key] = now
    # Resolved incidents silently disappear; their cooldown survives recovery/restart.
    store.set(STATE_KEY, state)
