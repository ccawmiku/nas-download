from __future__ import annotations

import logging
import sqlite3
import contextlib
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass
class LogEntry:
    time: str
    level: str
    logger: str
    message: str


class MemoryLogHandler(logging.Handler):
    def __init__(self, limit: int = 300, path=None):
        super().__init__()
        self.records: deque[LogEntry] = deque(maxlen=limit)
        self._records_lock = Lock()
        self.path=path
        if path:
            path.parent.mkdir(parents=True,exist_ok=True)
            with self.connect() as db:
                db.executescript('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,message TEXT,level TEXT);')

    @contextlib.contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        if self.path:
            from core.config import sanitize
            message=sanitize(message)
            with self.connect() as db:
                db.execute('INSERT INTO logs(message,level) VALUES(?,?)',(message,record.levelname.lower()))
        entry = LogEntry(
            time=datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            level=record.levelname,
            logger=record.name,
            message=message,
        )
        with self._records_lock:
            self.records.append(entry)

    def list(self) -> list[dict[str, Any]]:
        with self._records_lock:
            return [asdict(record) for record in reversed(self.records)]

    def pending(self,after):
        with self.connect() as db:
            return [{'id':r[0],'message':r[1],'level':r[2]} for r in db.execute('SELECT * FROM logs WHERE id>? ORDER BY id LIMIT 100',(after,))]
