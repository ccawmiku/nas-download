"""Import existing records only. Never enumerate or probe archived media."""

from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from .config import CONFIG_PATHS, DEFAULTS, read_json


def import_legacy(store):
    if store.get("legacy_imported"):
        return
    count = 0
    for platform, table, id_column in [
        ("x", "tweets", "tweet_id"),
        ("pixiv", "artworks", "artwork_id"),
        ("xhs", "notes", "note_id"),
    ]:
        config = read_json(CONFIG_PATHS[platform])
        path = Path(config.get("database", "/missing"))
        if not path.is_file():
            continue
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            for row in source.execute(f"SELECT * FROM {table}"):
                value = dict(row)
                files = json.loads(value.get("files_json") or "[]")
                state = {
                    "done": "complete",
                    "skipped": "complete",
                    "pending": "queued",
                    "retry": "failed",
                }.get(value.get("status"), value.get("status", "failed"))
                metadata = {
                    "title": value.get("title") or value.get("text") or "",
                    "author": value.get("user_name") or value.get("author") or "",
                    "url": value.get("url")
                    or (
                        f"https://www.pixiv.net/artworks/{value[id_column]}"
                        if platform == "pixiv"
                        else ""
                    ),
                    "error": value.get("last_error") or value.get("error") or "",
                    "imported": True,
                    "legacy_status": value.get("status", ""),
                }
                with store.connect() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO records VALUES(?,?,?,?,?,?)",
                        (
                            platform,
                            str(value[id_column]),
                            state,
                            json.dumps(files),
                            json.dumps(metadata, ensure_ascii=False),
                            time.time(),
                        ),
                    )
                count += 1
        finally:
            source.close()
    preferences = store.get("preferences", json.loads(json.dumps(DEFAULTS)))
    for key in ("x", "pixiv", "douyin", "xhs"):
        config = read_json(CONFIG_PATHS[key])
        seconds = (
            config.get("run_interval_seconds")
            or float(config.get("run_interval_hours", 12)) * 3600
        )
        if key == "douyin" and config.get("max_job_runtime_seconds"):
            preferences["max_minutes"][key] = max(
                1, min(1440, float(config["max_job_runtime_seconds"]) / 60)
            )
        preferences["schedule"][key] = {
            "enabled": bool(config),
            "hours": max(0.1, min(720, float(seconds) / 3600)),
        }
    store.set("preferences", preferences)
    store.set("legacy_imported", {"count": count, "at": time.time()})
    store.log(
        "system",
        f"已迁移 {count} 条下载记录；不扫描历史媒体。首次同步按连续已下载记录结束。",
    )
