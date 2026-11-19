from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

PLATFORMS = {
    "telegram": "Telegram",
    "xhs": "小红书",
    "x": "X",
    "pixiv": "Pixiv",
    "douyin": "抖音",
}


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,platform TEXT NOT NULL,
                  kind TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,
                  updated REAL NOT NULL,owner TEXT,lease REAL,progress TEXT NOT NULL DEFAULT '{}',
                  result TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',attempt INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(state,platform,created);
                CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,platform TEXT NOT NULL,
                  source TEXT NOT NULL,target TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,
                  updated REAL NOT NULL,source_bytes INTEGER NOT NULL,output_bytes INTEGER DEFAULT 0,
                  owner TEXT,lease REAL,attempt INTEGER DEFAULT 0,reason TEXT DEFAULT '',
                  details TEXT NOT NULL DEFAULT '{}',original_target TEXT NOT NULL,
                  UNIQUE(platform,source));
                CREATE INDEX IF NOT EXISTS assets_queue ON assets(state,created);
                CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,
                  platform TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL,task_id TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS checkpoints(platform TEXT NOT NULL,source TEXT NOT NULL,
                  head TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(platform,source));
                CREATE TABLE IF NOT EXISTS records(platform TEXT NOT NULL,source_id TEXT NOT NULL,
                  state TEXT NOT NULL,files TEXT NOT NULL DEFAULT '[]',metadata TEXT DEFAULT '{}',
                  updated REAL NOT NULL,PRIMARY KEY(platform,source_id));
                CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,roles TEXT NOT NULL,
                  updated REAL NOT NULL,details TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS log_events(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS browser_pushes(id TEXT PRIMARY KEY,
                  platform TEXT NOT NULL,source_mode TEXT NOT NULL,batch_id TEXT DEFAULT '',
                  account TEXT DEFAULT '',status TEXT NOT NULL,submitted INTEGER DEFAULT 0,
                  accepted INTEGER DEFAULT 0,skipped INTEGER DEFAULT 0,invalid INTEGER DEFAULT 0,
                  unresolvable INTEGER DEFAULT 0,discovered INTEGER DEFAULT 0,
                  task_id TEXT DEFAULT '',message TEXT DEFAULT '',client_ip TEXT DEFAULT '',
                  created REAL NOT NULL,updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS browser_pushes_platform_created ON browser_pushes(platform,created DESC);
                CREATE TABLE IF NOT EXISTS push_batches(platform TEXT NOT NULL,
                  batch_id TEXT NOT NULL,payload_hash TEXT NOT NULL,response TEXT NOT NULL,
                  created REAL NOT NULL,PRIMARY KEY(platform,batch_id));
                CREATE TABLE IF NOT EXISTS bg_runs(run_id TEXT PRIMARY KEY,
                  platform TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,
                  created REAL NOT NULL);
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def log(self, platform, message, level="info", task_id="", event_id=""):
        with self.connect() as db:
            if (
                event_id
                and not db.execute(
                    "INSERT OR IGNORE INTO log_events VALUES(?)", (event_id,)
                ).rowcount
            ):
                return
            db.execute(
                "INSERT INTO logs(created,platform,level,message,task_id) VALUES(?,?,?,?,?)",
                (time.time(), platform, level, message, task_id),
            )

    def enqueue(self, platform, kind="sync", payload=None):
        if platform not in PLATFORMS:
            raise ValueError("未知平台")
        now = time.time()
        encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM tasks WHERE platform=? AND kind=? AND payload=? AND state IN ('queued','running')",
                (platform, kind, encoded),
            ).fetchone()
            if row:
                return row[0], False
            identifier = secrets.token_hex(12)
            db.execute(
                "INSERT INTO tasks(id,platform,kind,payload,state,created,updated) VALUES(?,?,?,?,'queued',?,?)",
                (identifier, platform, kind, encoded, now, now),
            )
        return identifier, True

    def claim(self, table, owner, platforms=None):
        if table not in {"tasks", "assets"}:
            raise ValueError("invalid queue")
        if table == "tasks":
            platforms = [key for key in (PLATFORMS if platforms is None else platforms) if key in PLATFORMS]
            if not platforms:
                return None
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A disconnected execution is visible, never blindly duplicated.
            db.execute(
                f"UPDATE {table} SET state='interrupted',reason='执行连接中断',updated=? WHERE state IN ('processing','publishing') AND lease<?"
                if table == "assets"
                else "UPDATE tasks SET state='interrupted',error='执行连接中断',updated=? WHERE state='running' AND lease<?",
                (now, now),
            )
            where = "state='queued' AND platform IN (" + ",".join("?" for _ in PLATFORMS) + ")"
            args = list(PLATFORMS)
            if platforms:
                where += " AND platform IN (" + ",".join("?" for _ in platforms) + ")"
                args.extend(platforms)
            if table == "assets":
                if db.execute(
                    "SELECT 1 FROM assets WHERE state IN ('processing','publishing')"
                ).fetchone():
                    return None
            else:
                where += " AND platform NOT IN (SELECT platform FROM tasks WHERE state='running')"
            row = db.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY created LIMIT 1", args
            ).fetchone()
            if not row:
                return None
            state = "processing" if table == "assets" else "running"
            db.execute(
                f"UPDATE {table} SET state=?,owner=?,lease=?,attempt=attempt+1,updated=? WHERE id=?",
                (state, owner, now + 90, now, row["id"]),
            )
            result = dict(row)
            result.update(state=state, owner=owner, attempt=row["attempt"] + 1)
        return self.decode(result)

    @staticmethod
    def decode(row):
        result = dict(row)
        for key in (
            "payload",
            "progress",
            "result",
            "details",
            "files",
            "metadata",
            "roles",
        ):
            if key in result:
                result[key] = json.loads(result[key] or "{}")
        return result

    def update_execution(self, table, identifier, owner, attempt, **patch):
        allowed = {
            "state",
            "progress",
            "result",
            "error",
            "lease",
            "reason",
            "output_bytes",
            "details",
            "target",
        }
        if table not in {"tasks", "assets"} or not patch or not patch.keys() <= allowed:
            raise ValueError("invalid update")
        patch["updated"] = time.time()
        for key in ("progress", "result", "details"):
            if key in patch:
                patch[key] = json.dumps(patch[key], ensure_ascii=False)
        with self.connect() as db:
            row = db.execute(
                f"SELECT * FROM {table} WHERE id=? AND owner=? AND attempt=?",
                (identifier, owner, attempt),
            ).fetchone()
            if not row:
                return False
            if row["state"] == patch.get("state") and row["state"] in {
                "complete",
                "failed",
                "limited",
            }:
                return True
            recover = (
                row["state"] == "interrupted"
                and patch.get("state") in {"complete", "failed", "limited"}
                or row["state"] == "cancelled"
                and patch.get("state") == "cancelled"
            )
            if (
                row["state"] not in {"running", "processing", "publishing"}
                and not recover
            ):
                return False
            assignments = ",".join(f"{key}=?" for key in patch)
            cur = db.execute(
                f"UPDATE {table} SET {assignments} WHERE id=? AND owner=? AND attempt=?",
                (*patch.values(), identifier, owner, attempt),
            )
            return cur.rowcount == 1

    @staticmethod
    def _error_where_clauses():
        return [
            "state IN ('failed','interrupted')",
            "(platform!='x' OR COALESCE(json_extract(metadata,'$.retry_exhausted'),0)=1)",
            "COALESCE(json_extract(metadata,'$.error_dismissed'),0)=0",
        ]

    def active_error_count(self):
        conditions = [
            "platform IN (" + ",".join("?" for _ in PLATFORMS) + ")",
            *self._error_where_clauses(),
        ]
        where = " WHERE " + " AND ".join(conditions)
        with self.connect() as db:
            return int(db.execute(f"SELECT count(*) FROM records{where}", list(PLATFORMS)).fetchone()[0] or 0)

    def page(self, table, page=1, limit=30, platform="", state="", query="", errors=False):
        if table not in {"tasks", "assets", "logs", "records"}:
            raise ValueError("invalid table")
        conditions, args = [], []
        if table != "logs":
            conditions.append("platform IN (" + ",".join("?" for _ in PLATFORMS) + ")")
            args.extend(PLATFORMS)
        if errors:
            conditions.extend(self._error_where_clauses())
        for key, value in (("platform", platform), ("state", state)):
            if value and not (key == "state" and table == "logs"):
                conditions.append(f"{key}=?")
                args.append(value)
        if query:
            column = {
                "assets": "source",
                "logs": "message",
                "tasks": "payload",
                "records": "metadata",
            }[table]
            conditions.append(f"{column} LIKE ?")
            args.append("%" + query + "%")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        order = "updated" if table == "records" else "created"
        with self.connect() as db:
            count = db.execute(f"SELECT count(*) FROM {table}{where}", args).fetchone()[
                0
            ]
            rows = db.execute(
                f"SELECT * FROM {table}{where} ORDER BY {order} DESC LIMIT ? OFFSET ?",
                (*args, limit, (page - 1) * limit),
            ).fetchall()
        return {
            "items": [self.decode(row) for row in rows],
            "total": count,
            "page": page,
            "limit": limit,
        }

    def get_push_batch(self, platform, batch_id):
        if not batch_id:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT payload_hash,response FROM push_batches WHERE platform=? AND batch_id=?",
                (platform, batch_id),
            ).fetchone()
        if not row:
            return None
        return {
            "payload_hash": row["payload_hash"],
            "response": json.loads(row["response"]),
        }

    def save_push_batch(self, platform, batch_id, payload_hash, response_data):
        if not batch_id:
            return
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO push_batches(platform,batch_id,payload_hash,response,created) VALUES(?,?,?,?,?) "
                "ON CONFLICT(platform,batch_id) DO UPDATE SET payload_hash=excluded.payload_hash,response=excluded.response,created=excluded.created",
                (
                    platform,
                    batch_id,
                    payload_hash,
                    json.dumps(response_data, ensure_ascii=False),
                    now,
                ),
            )
            db.execute("DELETE FROM push_batches WHERE created < ?", (now - 7 * 86400,))

    def can_record_auth_error(self, platform, cooldown_seconds=30):
        with self.connect() as db:
            row = db.execute(
                "SELECT MAX(created) FROM browser_pushes WHERE platform=? AND status='auth_error'",
                (platform,),
            ).fetchone()
        latest = row[0] if row and row[0] else 0
        return (time.time() - latest) >= cooldown_seconds

    def record_browser_push(
        self,
        platform,
        source_mode,
        status,
        *,
        batch_id="",
        account="",
        submitted=0,
        accepted=0,
        skipped=0,
        invalid=0,
        unresolvable=0,
        discovered=0,
        task_id="",
        message="",
        client_ip="",
    ):
        now = time.time()
        identifier = secrets.token_hex(10)
        with self.connect() as db:
            db.execute(
                "INSERT INTO browser_pushes(id,platform,source_mode,batch_id,account,status,submitted,accepted,skipped,invalid,unresolvable,discovered,task_id,message,client_ip,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    platform,
                    str(source_mode or "unknown")[:32],
                    str(batch_id or "")[:80],
                    str(account or "")[:100],
                    str(status or "queued")[:32],
                    int(submitted or 0),
                    int(accepted or 0),
                    int(skipped or 0),
                    int(invalid or 0),
                    int(unresolvable or 0),
                    int(discovered or 0),
                    str(task_id or "")[:64],
                    str(message or "")[:500],
                    str(client_ip or "")[:64],
                    now,
                    now,
                ),
            )
            db.execute(
                "DELETE FROM browser_pushes WHERE id NOT IN (SELECT id FROM browser_pushes ORDER BY created DESC LIMIT 200)"
            )
        return identifier

    def _enrich_push_row(self, db, row):
        item = dict(row)
        task_id = item.get("task_id") or ""
        if task_id:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task:
                decoded = self.decode(task)
                item["task_state"] = decoded["state"]
                item["task_progress"] = decoded["progress"]
                item["task_result"] = decoded["result"]
                item["task_error"] = decoded["error"]
        return item

    def list_browser_pushes(self, page=1, limit=30, platform=""):
        clauses, args = [], []
        if platform:
            clauses.append("platform=?")
            args.append(platform)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connect() as db:
            total = db.execute(
                f"SELECT count(*) FROM browser_pushes{where}", args
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM browser_pushes{where} ORDER BY created DESC LIMIT ? OFFSET ?",
                (*args, limit, (page - 1) * limit),
            ).fetchall()
            items = [self._enrich_push_row(db, row) for row in rows]
        return {"items": items, "total": total, "page": page, "limit": limit}

    def browser_push_summary(self):
        local_day = time.localtime()
        day_start = time.mktime(
            (local_day.tm_year, local_day.tm_mon, local_day.tm_mday, 0, 0, 0, 0, 0, -1)
        )
        summary = {}
        with self.connect() as db:
            for key in ("x", "xhs"):
                latest = db.execute(
                    "SELECT * FROM browser_pushes WHERE platform=? ORDER BY created DESC LIMIT 1",
                    (key,),
                ).fetchone()
                last_ok = db.execute(
                    "SELECT * FROM browser_pushes WHERE platform=? AND status IN ('queued','empty','duplicate','ok') ORDER BY created DESC LIMIT 1",
                    (key,),
                ).fetchone()
                stats = db.execute(
                    "SELECT count(*), COALESCE(SUM(accepted),0), "
                    "COALESCE(SUM(CASE WHEN status IN ('queued','duplicate','empty','ok') THEN 1 ELSE 0 END),0), "
                    "COALESCE(SUM(CASE WHEN status IN ('error','auth_error') THEN 1 ELSE 0 END),0) "
                    "FROM browser_pushes WHERE platform=? AND created>=?",
                    (key, day_start),
                ).fetchone()
                summary[key] = {
                    "last_push": self._enrich_push_row(db, latest) if latest else None,
                    "last_success_push": self._enrich_push_row(db, last_ok) if last_ok else None,
                    "today_pushes": int(stats[0] or 0),
                    "today_accepted": int(stats[1] or 0),
                    "today_success_count": int(stats[2] or 0),
                    "today_error_count": int(stats[3] or 0),
                }
        return summary

    def save_bg_run(self, run_id, platform, status, payload):
        clean_id = str(run_id or "").strip()[:100]
        if not clean_id:
            return
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO bg_runs(run_id,platform,status,payload,created) VALUES(?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET platform=excluded.platform,status=excluded.status,payload=excluded.payload,created=excluded.created",
                (
                    clean_id,
                    str(platform or "")[:16],
                    str(status or "ok")[:32],
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                ),
            )
            db.execute("DELETE FROM bg_runs WHERE created < ?", (now - 2 * 86400,))

    def get_bg_run(self, run_id):
        clean_id = str(run_id or "").strip()[:100]
        if not clean_id:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT run_id,platform,status,payload,created FROM bg_runs WHERE run_id=?",
                (clean_id,),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row["payload"] or "{}")
        return {
            **data,
            "runId": row["run_id"],
            "platform": row["platform"],
            "status": row["status"],
            "created": row["created"],
        }
