from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.config import read_json, write_json
from .client import Client, replay_receipts


def main():
    client = Client()
    owner = os.getenv("WORKER_ID", "engine-" + socket.gethostname())
    platforms = os.getenv("WORKER_PLATFORMS", "x,pixiv,douyin").split(",")
    while True:
        try:
            replay_receipts()
            execution_root = Path(os.getenv("NAS_EXECUTION_DATA", "/state/execution"))
            for outcome_path in execution_root.glob("*.outcome"):
                outcome = read_json(outcome_path)
                if outcome.get("owner") == owner:
                    client.request(
                        "/internal/execution/tasks/" + outcome_path.stem, outcome
                    )
                    outcome_path.unlink()
                    outcome_path.with_suffix(".task").unlink(missing_ok=True)
                    outcome_path.with_suffix(".result").unlink(missing_ok=True)
            if any(platform in platforms for platform in ("x", "pixiv", "douyin")):
                from .bypass import drain_one

                drain_one()
            result = client.request(
                "/internal/claim/tasks", {"owner": owner, "platforms": platforms}
            )
            job = result["job"]
            if not job:
                time.sleep(3)
                continue
            identity = {"owner": owner, "attempt": job["attempt"]}
            payload_path = Path(os.getenv("NAS_EXECUTION_DATA", "/state/execution")) / (
                job["id"] + ".task"
            )
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            payload_path.write_text(
                json.dumps(job, ensure_ascii=False), encoding="utf-8"
            )
            environment = dict(
                os.environ, NAS_TASK_ID=job["id"], NAS_TASK_ATTEMPT=str(job["attempt"])
            )
            command = [sys.executable, "-m", "workers.platform_job", str(payload_path)]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                start_new_session=os.name != "nt",
            )

            def reader(
                proc=process, current_job=dict(job), current_identity=dict(identity)
            ):
                for line in proc.stdout:
                    if line.strip():
                        try:
                            progress = json.loads(line)
                            if isinstance(progress, dict) and any(
                                k in progress
                                for k in (
                                    "discovered",
                                    "downloaded",
                                    "phase",
                                    "completed_bytes",
                                )
                            ):
                                client.request(
                                    "/internal/execution/tasks/" + current_job["id"],
                                    {**current_identity, "progress": progress},
                                )
                                continue
                        except Exception:
                            pass
                        client.log(
                            current_job["platform"], line.rstrip(), current_job["id"]
                        )

            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()
            began = time.monotonic()
            minutes = result["settings"]["max_minutes"].get(job["platform"], 0)
            state, error = "complete", ""
            while process.poll() is None:
                time.sleep(2)
                status = client.request("/internal/task/" + job["id"])["state"]
                if (
                    status == "cancelled"
                    or minutes
                    and time.monotonic() - began >= minutes * 60
                ):
                    state = "cancelled" if status == "cancelled" else "limited"
                    error = (
                        "用户停止"
                        if state == "cancelled"
                        else f"达到 {minutes:g} 分钟上限，本次未完成"
                    )
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        if os.name != "nt":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    break
                client.request("/internal/execution/tasks/" + job["id"], identity)
                client.request(
                    "/internal/heartbeat", {"owner": owner, "platforms": platforms}
                )
            process.wait()
            reader_thread.join(timeout=3)
            if state == "complete" and process.returncode:
                state, error = "failed", "执行失败，请查看此任务的日志"
            receipt = read_json(payload_path.with_suffix(".result"))
            if state == "limited":
                receipt.update(stop_reason="timeout", message=error)
            elif state == "cancelled":
                receipt.update(stop_reason="cancelled", message=error)
            if state == "complete" and receipt.get("failed", 0):
                state, error = "failed", "部分内容下载失败，可重试失败下载"
            outcome = {**identity, "state": state, "error": error, "result": receipt}
            outcome_path = payload_path.with_suffix(".outcome")
            write_json(outcome_path, outcome)
            client.request("/internal/execution/tasks/" + job["id"], outcome)
            outcome_path.unlink(missing_ok=True)
            payload_path.unlink(missing_ok=True)
            payload_path.with_suffix(".result").unlink(missing_ok=True)
        except Exception as error:
            # Kill a child before retrying a poll; a disconnected controller never creates an orphan executor.
            if "process" in locals() and process.poll() is None:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
            print("worker disconnected: " + type(error).__name__, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
