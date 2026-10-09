from __future__ import annotations
import asyncio
import os
import json
import hashlib
from pathlib import Path
from core.config import CONFIG_PATHS, read_json
from core.media import publish
from .client import Client, prepare, record, submit


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
    final = Path(settings.get("work_path") or "/xhs")
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
                target = final / source.relative_to(staging)
                if enabled:
                    submit(
                        "xhs",
                        source,
                        target,
                        {"source_id": identifier, "metadata": {"url": current["url"]}},
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
                {"url": current["url"]},
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
            input_id = (
                getattr(app, "extract_link_id", lambda value: None)(url)
                or "url-" + hashlib.sha256(url.encode()).hexdigest()[:24]
            )
            try:
                previous = Client().request("/internal/record/xhs/" + input_id)
            except Exception:
                previous = {}
            values = await app.extract(
                url,
                download=True,
                check_record=job["kind"] != "retry",
                task_id=job["id"],
                result_callback=lambda value: count.update(value),
            )
            identifier = current.get("identifier") or next(
                (
                    str(value["作品ID"])
                    for value in values
                    if isinstance(value, dict) and value.get("作品ID")
                ),
                input_id,
            )
            if count.get("skip"):
                stats["skipped"] += 1
                continue
            await deliver(identifier)
            delivered, processing = current["delivered"], current["processing"]
            failed = bool(count.get("fail") or not delivered)
            metadata = {
                "url": url,
                "attempts": int(previous.get("metadata", {}).get("attempts", 0)) + 1,
                "replaces_source_id": input_id if input_id != identifier else "",
                "title": next(
                    (v.get("作品标题", "") for v in values if isinstance(v, dict)), ""
                ),
            }
            if failed:
                metadata.update(
                    error="上游未确认所有文件完成", download_incomplete=True
                )
            record(
                "xhs",
                identifier,
                "failed" if failed else "processing" if processing else "complete",
                delivered,
                metadata,
            )
            stats["failed" if failed else "downloaded"] += 1
            print(__import__("json").dumps(stats), flush=True)
            await asyncio.sleep(float(config.get("request_delay_seconds", 1)))
    activity.unlink(missing_ok=True)
    for key, seen in checkpoints:
        Client().request("/internal/checkpoint/xhs/" + key, {"head": json.dumps(seen)})
    return stats
