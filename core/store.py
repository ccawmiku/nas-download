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
            where, args = "state='queued'", []
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

    def page(self, table, page=1, limit=30, platform="", state="", query=""):
        if table not in {"tasks", "assets", "logs", "records"}:
            raise ValueError("invalid table")
        conditions, args = [], []
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
