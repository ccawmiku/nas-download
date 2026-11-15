from __future__ import annotations
import asyncio
import os
import hashlib
import json
import os
import re
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from core.config import CONFIG_PATHS, read_json, sanitize
from core.media import publish
from .client import Client, prepare, record, submit


def classify_xhs_failure(detail: str) -> dict:
    raw = str(detail or "").strip()
    lower = raw.lower()
    retry_after = 0
    match = re.search(r"retry[-_\s]?after[:=\s]+(\d+)", lower)
    if match:
        retry_after = max(60, min(int(match.group(1)), 86400))
    if (
        "429" in lower
        or "too many requests" in lower
        or "频繁" in raw
        or "限流" in raw
        or "rate limit" in lower
    ):
        wait = retry_after or 1800
        return {
            "kind": "rate_limit",
            "retry_paused": False,
            "retry_exhausted": False,
            "needs_fresh_token": False,
            "cooldown_seconds": wait,
            "message": f"触发上游限流 (429)，已暂停并延后 {wait} 秒后重试",
        }
    if (
        "401" in lower
        or "403" in lower
        or "captcha" in lower
        or "验证码" in raw
        or "风控" in raw
        or "未登录" in raw
        or "登录" in raw
    ):
        return {
            "kind": "auth_required",
            "retry_paused": True,
            "retry_exhausted": True,
            "needs_fresh_token": False,
            "cooldown_seconds": 0,
            "message": "小红书要求登录或触发风控验证码，已暂停自动重试",
        }
    if (
        "xsec_token" in lower
        or "token" in lower
        or "404" in lower
        or "已过期" in raw
        or "参数无效" in raw
        or "作品不存在" in raw
        or "无法获取" in raw
        or "已删除" in raw
    ):
        return {
            "kind": "token_expired",
            "retry_paused": True,
            "retry_exhausted": True,
            "needs_fresh_token": True,
            "cooldown_seconds": 0,
            "message": "xsec_token 已失效或作品不可访问，等待浏览器提交新链接后重试",
        }
    return {
        "kind": "network",
        "retry_paused": False,
        "retry_exhausted": False,
        "needs_fresh_token": False,
        "cooldown_seconds": 0,
        "message": sanitize(raw) or "上游未确认所有文件完成",
    }


def queue_urls(config):
    urls, checkpoints = [], []
    for raw in config.get("queue_files", []):
        path = Path(raw)
        if not path.is_file():
            continue
        import re

        current = list(
            dict.fromkeys(
                re.findall(r"https?://[^\s<>]+", path.read_text(encoding="utf-8-sig"))
            )
        )
        key = "queue-" + hashlib.sha256(str(path).encode()).hexdigest()[:20]
        head = Client().request("/internal/checkpoint/xhs/" + key)["head"]
        if not head:
            Client().request(
                "/internal/checkpoint/xhs/" + key, {"head": json.dumps(current)}
            )
            continue
        seen = json.loads(head)
        urls.extend(url for url in current if url not in set(seen))
        checkpoints.append((key, list(dict.fromkeys(seen + current))))
    return urls, checkpoints


def archive_root(settings, config, destination="batch"):
    raw = str(settings.get("work_path") or "/xhs")
    root = Path(raw)
    if destination == "single":
        custom = str(config.get("single_work_path") or "").strip()
        raw = custom if custom else f"{raw.rstrip('/\\')}/single"
        root = Path(raw)
        if not (
            root.is_absolute()
            or PurePosixPath(str(raw).replace("\\", "/")).is_absolute()
        ):
            raise ValueError("单个作品保存目录必须是容器绝对路径")
    return root


async def run(job):
    from source.application.app import XHS

    config = read_json(CONFIG_PATHS["xhs"])
    checkpoints = []
    if job["kind"] == "sync":
        job["payload"]["urls"], checkpoints = queue_urls(config)
        if not job["payload"]["urls"]:
            return {
                "discovered": 0,
                "downloaded": 0,
                "failed": 0,
                "skipped": 0,
                "stop_reason": "baseline",
                "message": "没有新队列链接；不回查历史",
            }
    settings = read_json(os.getenv("XHS_SETTINGS_PATH", "/app/Volume/settings.json"))
    settings_original = dict(settings)
    final = archive_root(settings, config)
    destination, enabled = prepare("xhs", final / "incoming", job["id"])
    # Temporary downloads are scoped to this task even when processing is bypassed.
    staging = destination.parent if enabled else final / ".incoming" / job["id"]
    staging.mkdir(parents=True, exist_ok=True)
    activity = staging / ".downloading"
    activity.touch()
    settings["work_path"] = str(staging)
    settings["download_record"] = True
    stats = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    urls = job["payload"].get("urls", [])
    if job["kind"] == "retry" and not urls:
        page = 1
        while True:
            result = Client().request(f"/internal/failed/xhs?page={page}")
            urls.extend(r["metadata"].get("url", "") for r in result["items"])
            if page * 100 >= result["total"]:
                break
            page += 1
    urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))
    async with XHS(**settings) as app:
        current = {}

        async def deliver(identifier):
            files = [
                p
                for p in staging.rglob("*")
                if p.is_file()
                and p not in current["before"]
                and p not in current["owned"]
                and p.stat().st_size
                and not p.name.startswith(".")
            ]
            for source in files:
                target = current["final"] / source.relative_to(staging)
                if enabled:
                    submit(
                        "xhs",
                        source,
                        target,
                        {
                            "source_id": identifier,
                            "metadata": {"url": current["url"], "destination": current["destination"]},
                        },
                    )
                    current["delivered"].append(str(source))
                    current["processing"] = True
                else:
                    saved = publish(source, source, target)
                    current["delivered"].append(str(saved))
                current["owned"].add(source)
            # record() keeps a durable receipt if the controller is offline.
            record(
                "xhs",
                identifier,
                "processing" if current["processing"] else "complete",
                current["delivered"],
                {"url": current["url"], "destination": current["destination"]},
            )
            if not enabled:
                for source in files:
                    source.unlink(missing_ok=True)

        native_add = app.id_recorder.add

        async def add_after_delivery(identifier, *args, **kwargs):
            # Commit our handoff before upstream's downloaded-ID marker. A killed
            # wrapper cannot leave an upstream success with unregistered media.
            await deliver(str(identifier))
            current["identifier"] = str(identifier)
            await native_add(identifier, *args, **kwargs)

        app.id_recorder.add = add_after_delivery
        for url in urls:
            count = {}
            stats["discovered"] += 1
            current = {
                "url": url,
                "before": set(staging.rglob("*")),
                "owned": set(),
                "delivered": [],
                "processing": False,
            }
            match = re.search(r"/(?:explore|discovery/item)/([^/?#]+)", urlparse(url).path)
            input_id = (
                match.group(1) if match
                else "url-" + hashlib.sha256(url.encode()).hexdigest()[:24]
            )
            try:
                previous = Client().request("/internal/record/xhs/" + input_id)
            except Exception:
                if job["kind"] == "retry":
                    # Never route a single retry into batch when its receipt is unavailable.
                    raise
                previous = {}
            prev_meta = previous.get("metadata", {}) if isinstance(previous, dict) else {}
            destination_kind = job["payload"].get("destination", "batch")
            if job["kind"] == "retry":
                destination_kind = prev_meta.get("destination", destination_kind)
            current["destination"] = destination_kind
            current["final"] = archive_root(settings_original, config, destination_kind)
            extracted_error = ""
            values = []
            try:
                values = await app.extract(
                    url,
                    download=True,
                    check_record=job["kind"] != "retry" and destination_kind != "single",
                    task_id=job["id"],
                    result_callback=lambda value: count.update(value),
                )
            except Exception as error:
                extracted_error = str(error)
            if count.get("error"):
                extracted_error = extracted_error or str(count.get("error"))
            values_list = values if isinstance(values, list) else []
            identifier = current.get("identifier") or next(
                (
                    str(value["作品ID"])
                    for value in values_list
                    if isinstance(value, dict) and value.get("作品ID")
                ),
                input_id,
            )
            if count.get("skip"):
                stats["skipped"] += 1
                continue
            await deliver(identifier)
            delivered, processing = current["delivered"], current["processing"]
            failed = bool(extracted_error or count.get("fail") or not delivered)
            fresh_submission = job["kind"] == "links" and bool(prev_meta.get("url")) and url != prev_meta.get("url")
            attempts = 1 if fresh_submission else int(prev_meta.get("attempts", 0) or 0) + 1
            metadata = {
                "url": url,
                "destination": destination_kind,
                "attempts": attempts,
                "replaces_source_id": input_id if input_id != identifier else "",
                "title": next(
                    (v.get("作品标题", "") for v in values_list if isinstance(v, dict)),
                    prev_meta.get("title", ""),
                ),
            }
            rate_limited = False
            if failed:
                classified = classify_xhs_failure(extracted_error)
                rate_limited = classified["kind"] == "rate_limit"
                metadata.update(
                    error=classified["message"],
                    download_incomplete=True,
                    failure_kind=classified["kind"],
                    retry_paused=classified["retry_paused"],
                    retry_exhausted=classified["retry_exhausted"],
                    needs_fresh_token=classified["needs_fresh_token"],
                    cooldown_until=(
                        time.time() + classified["cooldown_seconds"]
                        if classified["cooldown_seconds"] > 0
                        else 0
                    ),
                )
            record(
                "xhs",
                identifier,
                "failed" if failed else "processing" if processing else "complete",
                delivered,
                metadata,
            )
            stats["failed" if failed else "downloaded"] += 1
            print(json.dumps(stats), flush=True)
            if rate_limited:
                stats["stop_reason"] = "rate_limit"
                break
            await asyncio.sleep(float(config.get("request_delay_seconds", 1)))
    activity.unlink(missing_ok=True)
    for key, seen in checkpoints:
        Client().request("/internal/checkpoint/xhs/" + key, {"head": json.dumps(seen)})
    return stats
