"""X download policy: initial attempt plus three retries, then manual handling."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

MAX_ATTEMPTS = 4


def terminal(metadata):
    return bool(metadata.get("error_dismissed") or metadata.get("retry_exhausted")
                or int(metadata.get("attempts", 0)) >= MAX_ATTEMPTS)


def migrate_attempts(store, config):
    """Backfill existing failures from the download DB; never inspect media files."""
    if store.get("x_error_attempts_migrated"):
        return
    path = Path(config.get("database", "/missing"))
    if not path.is_file():
        return
    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = source.execute("SELECT tweet_id,attempts FROM tweets WHERE status='failed'").fetchall()
    finally:
        source.close()
    with store.connect() as db:
        for identifier, attempts in rows:
            row = db.execute("SELECT metadata FROM records WHERE platform='x' AND source_id=? AND state='failed'", (identifier,)).fetchone()
            if not row:
                continue
            metadata = json.loads(row[0])
            metadata.update(attempts=attempts, retry_exhausted=attempts >= MAX_ATTEMPTS)
            db.execute("UPDATE records SET metadata=? WHERE platform='x' AND source_id=?", (json.dumps(metadata, ensure_ascii=False), identifier))
    store.set("x_error_attempts_migrated", True)
