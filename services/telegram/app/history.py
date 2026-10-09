from __future__ import annotations

import json
import os
import secrets
import sqlite3
import contextlib
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


ACTIVE_STATUSES = {"queued", "downloading", "paused", "retrying", "verifying"}
RETRYABLE_STATUSES = {"failed", "interrupted", "cancelled"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DownloadRecord:
    id: str
    message_id: int
    chat_id: int | None
    file_name: str
    path: str
    status: str = "queued"
    progress: int = 0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    size_bytes: int = 0
    speed_bytes_per_second: float = 0.0
    eta_seconds: int | None = None
    speed_limit_bytes_per_second: int | None = None
    retry_count: int = 0
    max_retries: int = 3
    error: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    notification_chat_id: int | None = None
    archive_path: str = ''
    workspace_asset_id: str = ''


class DownloadHistory:
    """SQLite history: display limits never prune durable download records."""
    def __init__(self, path, limit=200, flush_interval=2.0):
        self.path, self.limit = Path(path), limit
        self.flush_interval = max(.5, flush_interval)
        self.database = self.path.with_suffix(".sqlite3")
        self._lock, self._pending, self._last_saved = Lock(), {}, time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS downloads(id TEXT PRIMARY KEY,status TEXT,created TEXT,data TEXT); CREATE INDEX IF NOT EXISTS status_idx ON downloads(status,created); CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY);")
            imported = db.execute("SELECT 1 FROM migrations WHERE name='legacy-json'").fetchone()
        if not imported:
            self._import_legacy()

    @contextlib.contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _write(db, row):
        db.execute("INSERT INTO downloads VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                   (row["id"],row["status"],row["created_at"],json.dumps(row,ensure_ascii=False)))

    def _import_legacy(self):
        records = []
        if self.path.exists():
            try:
                rows = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if not isinstance(rows,list):
                    raise ValueError("history root must be a list")
                allowed = {f.name for f in fields(DownloadRecord)}
                for row in rows:
                    if isinstance(row,dict):
                        value = {k:v for k,v in row.items() if k in allowed}
                        value.setdefault("notification_chat_id",value.get("chat_id"))
                        records.append(asdict(DownloadRecord(**value)))
            except (OSError,TypeError,ValueError) as error:
                backup = self.path.with_suffix(".json.corrupt")
                if backup.exists():
                    backup = self.path.with_suffix("."+secrets.token_hex(4)+".corrupt")
                os.replace(self.path,backup)
                records.append(asdict(DownloadRecord("recovery-"+secrets.token_hex(6),0,None,backup.name,str(backup),status="failed",error=f"历史文件损坏，已备份：{type(error).__name__}")))
        with self._connect() as db:
            for row in records:
                self._write(db,row)
            db.execute("INSERT OR IGNORE INTO migrations VALUES('legacy-json')")

    def _flush_locked(self):
        if self._pending:
            with self._connect() as db:
                for row in self._pending.values():
                    self._write(db,row)
            self._pending.clear()
        self._last_saved = time.monotonic()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def pending_handoffs(self):
        self.flush()
        with self._connect() as db:
            rows = db.execute("SELECT data FROM downloads WHERE status IN ('complete','processing') AND json_extract(data,'$.archive_path') != '' AND json_extract(data,'$.path') != json_extract(data,'$.archive_path') AND COALESCE(json_extract(data,'$.workspace_asset_id'),'') = ''").fetchall()
        return [json.loads(row['data']) for row in rows]

    def add(self, record):
        with self._lock:
            with self._connect() as db:
                self._write(db,asdict(record))

    def _find_locked(self,prefix):
        with self._connect() as db:
            rows = db.execute("SELECT data FROM downloads WHERE substr(id,1,?)=?",(len(prefix),prefix)).fetchall()
        if len(rows)!=1:
            return None
        row=json.loads(rows[0][0])
        return dict(self._pending.get(row["id"],row))

    def find(self,prefix):
        with self._lock:
            return self._find_locked(prefix)

    def update(self,record_id,*,persist=True,**updates):
        with self._lock:
            row=self._find_locked(record_id)
            if not row:
                return False
            row.update({k:v for k,v in updates.items() if k in row})
            row["updated_at"]=now_iso()
            self._pending[row["id"]]=row
            if persist or time.monotonic()-self._last_saved>=self.flush_interval:
                self._flush_locked()
            return True

    def _query(self,statuses=None,limit=None):
        with self._lock:
            self._flush_locked()
            where,args="",[]
            if statuses is not None:
                if not statuses:
                    return []
                where=" WHERE status IN ("+",".join("?" for _ in statuses)+")"
                args=list(statuses)
            query="SELECT data FROM downloads"+where+" ORDER BY created DESC,rowid DESC"
            if limit is not None:
                query+=" LIMIT ?"
                args.append(max(0,limit))
            with self._connect() as db:
                return [json.loads(r[0]) for r in db.execute(query,args)]

    def list(self,limit=None):
        return self._query(limit=limit)

    def list_statuses(self,statuses,limit=10):
        return self._query(statuses,limit)

    def page(self,page=1,limit=30):
        self.flush()
        with self._connect() as db:
            total=db.execute('SELECT count(*) FROM downloads').fetchone()[0]
            rows=db.execute('SELECT data FROM downloads ORDER BY created DESC,rowid DESC LIMIT ? OFFSET ?',(limit,(page-1)*limit)).fetchall()
        return {'items':[json.loads(r[0]) for r in rows],'total':total,'page':page,'limit':limit}

    def counts(self):
        self.flush()
        with self._connect() as db:
            return dict(db.execute('SELECT status,count(*) FROM downloads GROUP BY status'))

    def remove_statuses(self,statuses):
        if not statuses:
            return 0
        with self._lock:
            self._flush_locked()
            with self._connect() as db:
                return db.execute("DELETE FROM downloads WHERE status IN ("+",".join("?" for _ in statuses)+")",list(statuses)).rowcount

    def recover_incomplete(self):
        recovered,interrupted=0,0
        for row in self.list_statuses(ACTIVE_STATUSES,limit=None):
            path=Path(row["path"])
            actual=path.stat().st_size if path.is_file() else 0
            if row["total_bytes"]>0 and actual==row["total_bytes"]:
                self.update(row["id"],status="complete",progress=100,downloaded_bytes=actual,size_bytes=actual,error="",speed_bytes_per_second=0,eta_seconds=None)
                recovered+=1
            else:
                self.update(row["id"],status="interrupted",downloaded_bytes=actual,size_bytes=actual,error="服务重启或任务中断，可使用 /retry 重试",speed_bytes_per_second=0,eta_seconds=None)
                path.with_name(f".{path.name}.{row['id'][:8]}.part").unlink(missing_ok=True)
                if actual==0:
                    path.unlink(missing_ok=True)
                interrupted+=1
        return {"recovered":recovered,"interrupted":interrupted}
