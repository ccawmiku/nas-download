from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class Child:
    name: str
    command: list[str]
    cwd: str
    env: dict[str, str]
    process: subprocess.Popen | None = None
    failures: int = 0
    restart_at: float = 0
    started_at: float = 0
    last_exit: int | None = None


class ProcessSupervisor:
    def __init__(self, log: Callable[[str], None], max_failures: int = 10):
        self.log = log
        self.max_failures = max_failures
        self.children: dict[str, Child] = {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()

    def start(self, name: str, command: list[str], cwd: str, env_patch: dict[str, str] | None = None) -> None:
        with self.lock:
            child = Child(name, command, cwd, {**os.environ, **(env_patch or {})})
            self.children[name] = child
            try:
                self._spawn(child)
            except OSError:
                child.failures = 1
                child.restart_at = time.monotonic() + 2
                self.log(f"{name} 启动失败，等待重试")

    def _spawn(self, child: Child) -> None:
        if self.stopping.is_set():
            return
        child.process = subprocess.Popen(child.command, cwd=child.cwd, env=child.env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace")
        child.started_at = time.monotonic()
        child.restart_at = 0
        threading.Thread(target=self._stream, args=(child.name, child.process), daemon=True).start()
        self.log(f"已启动 {child.name}: pid={child.process.pid}")

    def _stream(self, name: str, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        with process.stdout:
            for raw in process.stdout:
                self.log(f"{name}: {raw.rstrip()}")

    def tick(self) -> None:
        with self.lock:
            now = time.monotonic()
            for child in self.children.values():
                if self.stopping.is_set() or child.failures >= self.max_failures:
                    continue
                if child.process is None:
                    if now >= child.restart_at:
                        try:
                            self._spawn(child)
                        except OSError:
                            child.failures += 1
                            child.restart_at = now + min(60, 2 ** child.failures)
                            self.log(f"{child.name} 启动失败，等待重试")
                    continue
                code = child.process.poll()
                if code is None:
                    if now - child.started_at > 60:
                        child.failures = 0
                    continue
                child.last_exit = code
                child.failures += 1
                child.process = None
                child.restart_at = now + min(60, 2 ** child.failures)
                self.log(f"{child.name} 退出码 {code}，退避重启 {child.failures}/{self.max_failures}")

    def monitor(self) -> None:
        while not self.stopping.wait(1):
            self.tick()

    def state(self) -> list[dict]:
        with self.lock:
            return [{"name": c.name, "pid": c.process.pid if c.process else None,
                     "returncode": c.process.poll() if c.process else c.last_exit,
                     "restart_count": c.failures, "quarantined": c.failures >= self.max_failures}
                    for c in self.children.values()]

    def stop(self, timeout: float = 30) -> None:
        self.stopping.set()
        with self.lock:
            processes = [c.process for c in self.children.values() if c.process and c.process.poll() is None]
        for process in processes:
            process.terminate()
        deadline = time.monotonic() + timeout
        for process in processes:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
