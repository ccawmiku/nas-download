from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path


class Client:
    def __init__(self):
        self.url = os.getenv("NAS_CORE_URL", "http://console:14001")
        self.token = os.getenv("INTERNAL_API_TOKEN", "")

    def request(self, path, payload=None, timeout=10):
        body = (
            json.dumps(payload, ensure_ascii=False).encode()
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            self.url + path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-NAS-Download-Token": self.token,
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def log(self, platform, message, task_id="", level="info"):
        try:
            self.request(
                "/internal/logs",
                {
                    "platform": platform,
                    "message": str(message),
                    "task_id": task_id,
                    "level": level,
                },
            )
        except Exception:
            # Process supervisor also captures these logs; do not lose the diagnostic.
            print(f"[{platform}] log delivery unavailable", flush=True)


class ExecutionLease:
    def __init__(self, table, identifier, identity):
        self.path = "/internal/execution/" + table + "/" + identifier
        self.identity = dict(identity)
        self.stop = threading.Event()

    def __enter__(self):
        def beat():
            while not self.stop.wait(15):
                try:
                    Client().request(self.path, self.identity)
                except Exception:
                    pass

        self.thread = threading.Thread(target=beat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=1)


def prepare(platform, target, identifier):
    target = Path(target)
    if not os.getenv("NAS_CORE_URL"):
        return target, False
    try:
        result = Client().request("/internal/workspace/status")
        if not result["settings"]["workspace"].get(platform) or not result["available"]:
            if result["settings"]["workspace"].get(platform):
                Client().log(
                    platform, result.get("reason", "工作区不可用，原件直接入库")
                )
            return target, False
        import secrets

        execution = (
            str(identifier) + "-a" + os.getenv("NAS_TASK_ATTEMPT", "1")
            if os.getenv("NAS_TASK_ID") == str(identifier)
            else str(identifier) + "-" + secrets.token_hex(4)
        )
        folder = Path(result["settings"]["workspace_root"]) / platform / execution
        folder.mkdir(parents=True, exist_ok=True)
        return folder / target.name, True
    except Exception:
        return target, False


def submit(platform, source, target, details=None):
    source, target = Path(source), Path(target)
    import hashlib
    from core.config import write_json

    identifier = hashlib.sha256(
        (platform + "\0" + str(source.resolve())).encode()
    ).hexdigest()[:24]
    payload = {
        "platform": platform,
        "source": str(source),
        "target": str(target),
        "details": details or {},
    }
    root = Path(os.getenv("NAS_EXECUTION_DATA", "/state/execution"))
    receipt = root / (identifier + ".intake")
    root.mkdir(parents=True, exist_ok=True)
    write_json(receipt, payload)
    try:
        value = Client().request("/internal/intake", payload)["id"]
        receipt.unlink(missing_ok=True)
        return value
    except urllib.error.HTTPError as error:
        if error.code not in {500, 502, 503, 504}:
            raise RuntimeError("工作区交付被拒绝，原文件已保留") from error
        return identifier
    except (urllib.error.URLError, TimeoutError, OSError):
        # An acknowledgement can be lost after the server commits. Keep the owned source
        # and replay the same deterministic intake instead of publishing a second copy.
        return identifier


def record(platform, identifier, state, files, metadata=None):
    client = Client()
    payload = {
        "platform": platform,
        "source_id": str(identifier),
        "state": state,
        "files": files,
        "metadata": metadata or {},
    }
    try:
        client.request("/internal/records", payload)
    except Exception:
        # Retain a replayable receipt beside new output. No record is silently lost.
        root = Path(os.getenv("NAS_EXECUTION_DATA", "/state/execution"))
        root.mkdir(parents=True, exist_ok=True)
        import hashlib

        path = root / (
            platform
            + "-"
            + hashlib.sha256(str(identifier).encode()).hexdigest()
            + ".json"
        )
        from core.config import write_json

        write_json(path, payload)


def replay_receipts():
    root = Path(os.getenv("NAS_EXECUTION_DATA", "/state/execution"))
    for path in root.glob("*.intake"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if Path(payload["source"]).is_file():
                Client().request("/internal/intake", payload)
            path.unlink()
        except Exception:
            break
    for path in root.glob("*.json"):
        try:
            Client().request(
                "/internal/records", json.loads(path.read_text(encoding="utf-8"))
            )
            path.unlink()
        except Exception:
            break
