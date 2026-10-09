from __future__ import annotations
import os
import socket
import threading
import time
from pathlib import Path
from core.config import read_json, write_json
from core.media import convert, publish
from .client import Client


def finish_receipt(client, receipt, path):
    source, chosen = Path(receipt["source"]), Path(receipt["chosen"])
    destination = publish(source, chosen, Path(receipt["archive_target"]), path)
    receipt = read_json(path)
    report = {
        **receipt["report"],
        "target": str(destination),
        "output_bytes": destination.stat().st_size,
    }
    client.request("/internal/execution/assets/" + receipt["id"], report)
    source.unlink(missing_ok=True)
    if chosen != source:
        chosen.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    root = Path(
        receipt.get(
            "workspace_root", os.getenv("NAS_WORKSPACE_ROOT", "/media/.nas-workspace")
        )
    )
    parent = source.parent
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def main():
    if os.name != "nt":
        import subprocess

        os.nice(19)
        if hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(0, {max(os.sched_getaffinity(0))})
        subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], check=True)
    client = Client()
    owner = "media-" + socket.gethostname()
    while True:
        try:
            ready = (
                Path(os.getenv("FFMPEG", "/usr/lib/jellyfin-ffmpeg/ffmpeg")).is_file()
                and Path("/dev/dri/renderD128").exists()
            )
            status = {
                "owner": owner,
                "platforms": ["media"],
                "details": {"device": "/dev/dri/renderD128", "ready": ready},
            }
            result = client.request("/internal/heartbeat", status)
            root = Path(result["settings"]["workspace_root"])
            root.mkdir(parents=True, exist_ok=True)
            for path in root.rglob(".archive-*.json"):
                finish_receipt(client, read_json(path), path)
            if not ready:
                time.sleep(10)
                continue
            result = client.request("/internal/claim/assets", status)
            job = result["job"]
            if not job:
                time.sleep(3)
                continue
            identity = {"owner": owner, "attempt": job["attempt"]}
            stopping = threading.Event()

            def heartbeat(
                current_id=job["id"],
                current_identity=dict(identity),
                stop=stopping,
                current_status=dict(status),
            ):
                while not stop.wait(15):
                    try:
                        client.request("/internal/heartbeat", current_status)
                        client.request(
                            "/internal/execution/assets/" + current_id, current_identity
                        )
                    except Exception:
                        pass

            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            receipt_path = Path(job["source"]).parent / (
                ".archive-" + job["id"] + ".json"
            )
            try:
                source = Path(job["source"])
                chosen, reason, info = convert(
                    source,
                    os.getenv("FFMPEG", "ffmpeg"),
                    os.getenv("FFPROBE", "ffprobe"),
                )
                client.request(
                    "/internal/execution/assets/" + job["id"],
                    {**identity, "state": "publishing", "reason": reason},
                )
                receipt = {
                    "id": job["id"],
                    "source": str(source),
                    "chosen": str(chosen),
                    "archive_target": job["original_target"],
                    "workspace_root": str(root),
                    "report": {
                        **identity,
                        "state": "complete",
                        "reason": reason,
                        "details": {**job["details"], "media": info},
                    },
                }
                write_json(receipt_path, receipt)
                finish_receipt(client, receipt, receipt_path)
                client.log(
                    job["platform"],
                    reason + " · " + Path(job["original_target"]).name,
                    job["id"],
                )
                if info.get("processing_error"):
                    client.log(
                        job["platform"], info["processing_error"], job["id"], "warning"
                    )
            except Exception as error:
                # A publication receipt is retried on reconnect, never marked as a failed conversion.
                if not receipt_path.exists():
                    try:
                        client.request(
                            "/internal/execution/assets/" + job["id"],
                            {
                                **identity,
                                "state": "failed",
                                "reason": "处理失败，原件已保留",
                                "details": {
                                    **job["details"],
                                    "processing_error": str(error),
                                },
                            },
                        )
                    except Exception:
                        pass
                client.log(job["platform"], str(error), job["id"], "error")
            finally:
                stopping.set()
                thread.join(timeout=1)
        except Exception:
            time.sleep(5)


if __name__ == "__main__":
    main()
