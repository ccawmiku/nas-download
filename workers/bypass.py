"""Publish waiting originals when the dedicated workspace service is unavailable."""

import socket
from pathlib import Path
from core.config import read_json, write_json
from core.media import VIDEO_EXTENSIONS, probe, validate_video
from .client import Client, ExecutionLease
from .media_worker import finish_receipt


def drain_one():
    client = Client()
    owner = "bypass-" + socket.gethostname()
    # Also finish acknowledged-or-published originals while the media service
    # is absent. These receipts belong to this fallback worker only.
    status = client.request("/internal/workspace/status")
    root = Path(status["settings"]["workspace_root"])
    for path in root.rglob(".archive-*.json"):
        receipt = read_json(path)
        if receipt.get("report", {}).get("owner") == owner:
            finish_receipt(client, receipt, path)
    result = client.request("/internal/bypass/claim", {"owner": owner})
    job = result["job"]
    if not job:
        return
    identity = {"owner": owner, "attempt": job["attempt"]}
    source = Path(job["source"])
    path = source.parent / (".archive-" + job["id"] + ".json")
    lease = ExecutionLease("assets", job["id"], identity)
    lease.__enter__()
    try:
        if source.suffix.lower() in VIDEO_EXTENSIONS:
            validate_video(source, probe(source))
        client.request(
            "/internal/execution/assets/" + job["id"],
            {**identity, "state": "publishing"},
        )
        receipt = {
            "id": job["id"],
            "source": str(source),
            "chosen": str(source),
            "archive_target": job["original_target"],
            "workspace_root": result["settings"]["workspace_root"],
            "report": {
                **identity,
                "state": "complete",
                "reason": "工作区不可用，原件直接入库",
                "details": job["details"],
            },
        }
        write_json(path, receipt)
        finish_receipt(client, receipt, path)
    except Exception as error:
        if not path.exists():
            client.request(
                "/internal/execution/assets/" + job["id"],
                {**identity, "state": "failed", "reason": str(error)},
            )
        else:
            raise
    finally:
        lease.__exit__()
