"""Replay completed Telegram downloads from history, never enumerate archives."""

from pathlib import Path
from .client import Client, submit, record


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
