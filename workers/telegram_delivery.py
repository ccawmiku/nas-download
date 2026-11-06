"""Replay completed Telegram downloads from history, never enumerate archives."""

from pathlib import Path
from datetime import datetime, timedelta, timezone
from core.config import read_json, write_json
from .client import Client, submit, record


def sync_history(history):
    path = history.path.parent / 'core-history-cursor.json'
    cursor = read_json(path).get('cursor', ['', ''])
    # History stamps have second precision. Only consume closed seconds so a
    # retry within the same second cannot be lost behind the pagination cursor.
    through = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec='seconds')
    rows = history.core_page(cursor, through)
    if rows:
        Client().request('/internal/telegram/history', {'items': rows})
        write_json(path, {'cursor': [rows[-1]['updated_at'], rows[-1]['id']]})


def recover_handoffs(history):
    result = Client().request("/internal/workspace/status")
    root = Path(result["settings"]["workspace_root"]).resolve()
    for row in history.pending_handoffs():
        source, target = Path(row["path"]), Path(row["archive_path"])
        if not source.resolve().is_relative_to(root) or not source.is_file():
            continue
        if source.stat().st_size <= 0:
            continue
        identifier = submit(
            "telegram",
            source,
            target,
            {"source_id": row["id"], "metadata": {"title": target.name}},
        )
        record(
            "telegram", row["id"], "processing", [str(source)], {"title": target.name}
        )
        history.update(row["id"], status="processing", workspace_asset_id=identifier)
