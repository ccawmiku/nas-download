from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from .config import (
    CONFIG_PATHS,
    DEFAULTS,
    THRESHOLDS,
    apply_fields,
    public_config,
    read_json,
    sanitize,
    write_json,
)
from .store import PLATFORMS, Store
from .updates import check_updates

DATA = Path(os.getenv("NAS_CORE_DATA", ".runtime/core"))
store = Store(DATA / "state.sqlite3")
TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
STATIC = Path(os.getenv("NAS_FRONTEND_DIST", "frontend/dist"))
VERSION = os.getenv("APP_VERSION", "4.0.0")


def settings():
    return {**DEFAULTS, **store.get("preferences", {})}


def session_secret():
    secret = store.get("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        store.set("session_secret", secret)
    return secret.encode()


def valid_session(token):
    try:
        stamp, signature = token.split(".")
        expected = hmac.new(
            session_secret(), stamp.encode(), hashlib.sha256
        ).hexdigest()
        return 0 <= time.time() - int(stamp) < 7 * 86400 and hmac.compare_digest(
            signature, expected
        )
    except (ValueError, TypeError, AttributeError):
        return False


async def scheduler():
    last_update = 0
    while True:
        try:
            now = time.time()
            if now - last_update > 21600:
                await asyncio.to_thread(check_updates, store)
                last_update = now
            for platform, schedule in settings()["schedule"].items():
                if not schedule["enabled"]:
                    continue
                due = store.get("due:" + platform, now)
                if due <= now:
                    store.enqueue(platform)
                    store.set("due:" + platform, now + schedule["hours"] * 3600)
            schedule_xhs_retries(now)
        except Exception as error:
            store.log("system", sanitize(str(error)), "error")
        await asyncio.sleep(10)


def schedule_xhs_retries(now):
    config = read_json(CONFIG_PATHS["xhs"])
    if not config.get("retry_failed", True):
        return
    with store.connect() as db:
        if db.execute(
            "SELECT 1 FROM tasks WHERE platform='xhs' AND state IN ('queued','running')"
        ).fetchone():
            return
        rows = db.execute(
            "SELECT * FROM records WHERE platform='xhs' AND state='failed'"
        ).fetchall()
    maximum = int(config.get("max_download_attempts", 0) or 0)
    delay = max(10, float(config.get("network_retry_delay_seconds", 300)))
    urls = []
    for row in rows:
        meta = json.loads(row["metadata"])
        if meta.get("imported") or not meta.get("url") or row["updated"] + delay > now:
            continue
        if maximum and int(meta.get("attempts", 0)) >= maximum:
            continue
        urls.append(meta["url"])
    if urls:
        store.enqueue(
            "xhs", "retry", {"urls": list(dict.fromkeys(urls))[:500], "automatic": True}
        )


@asynccontextmanager
async def lifespan(app):
    if os.getenv("NAS_INITIALIZE_CONFIGS", "true") == "true":
        from .config import initialize_configs

        initialize_configs()
    if os.getenv("NAS_IMPORT_LEGACY", "false") == "true":
        from .migrate import import_legacy

        await asyncio.to_thread(import_legacy, store)
    task = (
        asyncio.create_task(scheduler())
        if os.getenv("NAS_SCHEDULER", "true") == "true"
        else None
    )
    yield
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="NAS Download", lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/internal/"):
        if not TOKEN or not hmac.compare_digest(
            request.headers.get("X-NAS-Download-Token", ""), TOKEN
        ):
            return Response(status_code=401)
    elif path.startswith(("/api/", "/media/")) and path not in {
        "/api/auth/status",
        "/api/auth/login",
    }:
        submit_token = store.get("browser_submit_token", "")
        script_auth = (
            path == "/api/xhs/links"
            and submit_token
            and hmac.compare_digest(
                request.headers.get("X-NAS-Submit-Token", ""), submit_token
            )
        )
        if settings()["auth_enabled"] and not (
            valid_session(request.cookies.get("nas_session")) or script_auth
        ):
            return Response(status_code=401)
    if not path.startswith("/internal/") and request.method in {
        "POST",
        "PATCH",
        "DELETE",
        "PUT",
    }:
        origin = request.headers.get("origin")
        script_origin = (
            path == "/api/xhs/links" and origin == "https://www.xiaohongshu.com"
        )
        if (
            origin
            and not script_origin
            and origin.rstrip("/") != str(request.base_url).rstrip("/")
        ):
            return Response("来源不匹配", status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/healthz")
def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {
        "enabled": settings()["auth_enabled"],
        "authenticated": not settings()["auth_enabled"]
        or valid_session(request.cookies.get("nas_session")),
    }


class Login(BaseModel):
    password: str = Field(max_length=1024)


@app.post("/api/auth/login")
def login(payload: Login, request: Request, response: Response):
    address = request.client.host if request.client else "unknown"
    key = "login:" + address
    attempts = [t for t in store.get(key, []) if time.time() - t < 300]
    if len(attempts) >= 10:
        raise HTTPException(429, "请稍后再试")
    credential = store.get("password", {})
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        payload.password.encode(),
        bytes.fromhex(credential.get("salt", "")),
        260000,
    ).hex()
    if not credential or not hmac.compare_digest(digest, credential.get("digest", "")):
        store.set(key, attempts + [time.time()])
        raise HTTPException(401, "密码不正确")
    store.set(key, [])
    stamp = str(int(time.time()))
    signature = hmac.new(session_secret(), stamp.encode(), hashlib.sha256).hexdigest()
    response.set_cookie(
        "nas_session",
        stamp + "." + signature,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=604800,
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("nas_session")
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return {**settings(), "password_set": bool(store.get("password"))}


@app.patch("/api/settings")
def save_settings(payload: dict):
    current = settings()
    for key in DEFAULTS:
        if key in payload:
            if key in {"workspace", "max_minutes", "schedule"}:
                if not isinstance(payload[key], dict):
                    raise HTTPException(422, "设置格式无效")
                current[key] = {**current[key], **payload[key]}
            else:
                current[key] = payload[key]
    if (
        not isinstance(current["auth_enabled"], bool)
        or current["auth_enabled"]
        and not (store.get("password") or payload.get("password"))
    ):
        raise HTTPException(422, "启用登录前请设置密码")
    if set(current["workspace"]) != set(PLATFORMS) or any(
        type(v) is not bool for v in current["workspace"].values()
    ):
        raise HTTPException(422, "工作区开关无效")
    if set(current["max_minutes"]) != {"x", "pixiv", "douyin"} or set(
        current["schedule"]
    ) != {"x", "pixiv", "douyin"}:
        raise HTTPException(422, "平台设置无效")
    if any(
        not isinstance(v, (int, float)) or not 1 <= v <= 1440
        for v in current["max_minutes"].values()
    ):
        raise HTTPException(422, "最大时长须为 1–1440 分钟")
    if (
        type(current["log_preview_lines"]) is not int
        or not 5 <= current["log_preview_lines"] <= 100
        or current["conversion_retries"] != 1
    ):
        raise HTTPException(422, "日志预览须为 5–100 行，转换重试固定一次")
    for value in current["schedule"].values():
        if (
            not isinstance(value, dict)
            or type(value.get("enabled")) is not bool
            or type(value.get("hours")) not in {int, float}
            or not 0.1 <= value["hours"] <= 720
        ):
            raise HTTPException(422, "同步周期无效")
    if str(current["workspace_root"]) != str(settings()["workspace_root"]):
        raise HTTPException(422, "工作区目录由部署配置指定，迁移目录须先完成队列")
    if payload.get("password"):
        salt = secrets.token_bytes(16)
        store.set(
            "password",
            {
                "salt": salt.hex(),
                "digest": hashlib.pbkdf2_hmac(
                    "sha256", str(payload["password"]).encode(), salt, 260000
                ).hex(),
            },
        )
        store.set("session_secret", secrets.token_hex(32))
    store.set("preferences", current)
    return get_settings()


@app.get("/api/overview")
def overview():
    with store.connect() as db:
        workers = [store.decode(row) for row in db.execute("SELECT * FROM workers")]
        counts = {
            table: dict(
                db.execute(
                    f"SELECT state,count(*) FROM {table} GROUP BY state"
                ).fetchall()
            )
            for table in ("tasks", "assets")
        }
        latest = {
            key: store.decode(row)
            if (
                row := db.execute(
                    "SELECT * FROM tasks WHERE platform=? ORDER BY created DESC LIMIT 1",
                    (key,),
                ).fetchone()
            )
            else None
            for key in PLATFORMS
        }
    return {
        "version": VERSION,
        "counts": counts,
        "workers": workers,
        "platforms": [
            {
                "key": key,
                "name": name,
                "configured": public_config(key)["configured"],
                "workspace": settings()["workspace"][key],
                "latest": latest[key],
                "next_run": store.get("due:" + key),
                "max_minutes": settings()["max_minutes"].get(key),
            }
            for key, name in PLATFORMS.items()
        ],
        "updates": store.get("upstreams", {}),
        "recent": store.page("tasks", limit=5)["items"],
    }


@app.get("/api/platforms/{platform}/settings")
def platform_config(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(404)
    return public_config(platform)


@app.patch("/api/platforms/{platform}/settings")
async def save_platform_config(platform: str, payload: dict):
    if platform not in PLATFORMS:
        raise HTTPException(404)
    config = read_json(CONFIG_PATHS[platform])
    fields = payload.get("fields", {})
    if not isinstance(fields, dict) or len(fields) > 200:
        raise HTTPException(422, "设置格式无效")
    # Hash-only credentials are never accepted as arbitrary input.
    if any(k.split(".")[-1] == "admin_password_hash" for k in fields):
        raise HTTPException(422)
    if platform in THRESHOLDS:
        key = THRESHOLDS[platform][0]
        if key in fields and (
            type(fields[key]) is not int or not 1 <= fields[key] <= 1000
        ):
            raise HTTPException(422, "连续已下载停止数须为 1–1000 的整数")
    if platform == "telegram":
        async with httpx.AsyncClient(timeout=90) as client:
            try:
                response = await client.post(
                    os.getenv("TELEGRAM_URL", "http://telegram-worker:8000")
                    + "/api/settings",
                    json=fields,
                    headers={"X-NAS-Download-Token": TOKEN},
                )
                if response.is_error:
                    raise HTTPException(response.status_code, sanitize(response.text))
            except httpx.HTTPError:
                raise HTTPException(503, "Telegram 服务不可用，设置未保存")
        return public_config(platform)
    if platform == "xhs":
        upstream_path = Path(
            os.getenv("XHS_SETTINGS_PATH", "/xhs-volume/settings.json")
        )
        value = read_json(upstream_path)
        apply_fields(
            value,
            {
                k.removeprefix("upstream."): v
                for k, v in fields.items()
                if k.startswith("upstream.")
            },
        )
        write_json(upstream_path, value)
        fields = {k: v for k, v in fields.items() if not k.startswith("upstream.")}
    apply_fields(config, fields)
    write_json(CONFIG_PATHS[platform], config)
    credential = payload.get("credential", "")
    if credential:
        if platform == "xhs":
            upstream = Path(os.getenv("XHS_SETTINGS_PATH", "/xhs-volume/settings.json"))
            value = read_json(upstream)
            value["cookie"] = str(credential)
            write_json(upstream, value)
        elif platform in {"x", "douyin", "pixiv"}:
            key = "refresh_token_file" if platform == "pixiv" else "cookie_file"
            target = Path(config.get(key) or f"/config/{platform}/{key}.txt")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(credential), encoding="utf-8")
            os.chmod(target, 0o600)
    store.log(platform, "平台设置已更新")
    return public_config(platform)


@app.get("/api/{table}")
def list_rows(
    table: str,
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    platform: str = "",
    state: str = "",
    q: str = "",
):
    if table == "workspace":
        return workspace_status()
    if table not in {"tasks", "assets", "records", "logs"}:
        raise HTTPException(404)
    return store.page(table, page, limit, platform, state, q)


@app.get("/api/logs/export/all")
def export_logs(platform: str = "", q: str = ""):
    clauses, params = ["id <= ?"], []
    with store.connect() as db:
        upper = db.execute("SELECT COALESCE(MAX(id),0) FROM logs").fetchone()[0]
    params.append(upper)
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if q:
        clauses.append("message LIKE ?")
        params.append("%" + q + "%")

    def stream():
        cursor = upper + 1
        while True:
            with store.connect() as db:
                rows = db.execute(
                    "SELECT * FROM logs WHERE "
                    + " AND ".join(clauses)
                    + " AND id < ? ORDER BY id DESC LIMIT 100",
                    [*params, cursor],
                ).fetchall()
            for row in rows:
                yield f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['created']))} [{row['platform']}] {row['level']} {row['message']}\n"
            if not rows:
                break
            cursor = rows[-1]["id"]

    return StreamingResponse(
        stream(),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="nas-download.log"'},
    )


@app.get("/api/detail/{table}/{identifier}")
def row_detail(table: str, identifier: str):
    if table not in {"tasks", "assets"}:
        raise HTTPException(404)
    with store.connect() as db:
        value = db.execute(
            f"SELECT * FROM {table} WHERE id=?", (identifier,)
        ).fetchone()
    if not value:
        raise HTTPException(404)
    return store.decode(value)


@app.post("/api/tasks")
def add_task(payload: dict):
    platform = payload.get("platform")
    kind = payload.get("kind", "sync")
    if (
        platform not in {"x", "pixiv", "douyin", "xhs"}
        or kind not in {"sync", "links", "retry", "test", "oauth-start", "oauth-finish"}
        or kind.startswith("oauth-")
        and platform != "pixiv"
        or kind == "test"
        and platform == "xhs"
    ):
        raise HTTPException(422, "任务无效")
    task_payload = payload.get("payload", {})
    if not isinstance(task_payload, dict):
        raise HTTPException(422, "任务参数无效")
    if payload.get("kind") == "links":
        urls = task_payload.get("urls", [])
        from urllib.parse import urlparse

        domains = {
            "x": {"x.com", "twitter.com"},
            "pixiv": {"www.pixiv.net", "pixiv.net"},
            "douyin": {"www.douyin.com", "v.douyin.com", "douyin.com"},
            "xhs": {"www.xiaohongshu.com", "xiaohongshu.com", "xhslink.com"},
        }
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 500
            or any(
                not isinstance(u, str)
                or urlparse(u).hostname not in domains.get(platform, set())
                for u in urls
            )
        ):
            raise HTTPException(422, "请提交所选平台的有效链接，最多 500 条")
        task_payload["urls"] = list(dict.fromkeys(urls))
    identifier, created = store.enqueue(
        platform, payload.get("kind", "sync"), task_payload
    )
    return {"id": identifier, "created": created}


@app.get("/api/browser-push/token")
def browser_push_token():
    value = store.get("browser_submit_token")
    if not value:
        value = secrets.token_urlsafe(32)
        store.set("browser_submit_token", value)
    return {"token": value}


@app.post("/api/xhs/links")
def browser_links(payload: dict):
    urls = payload.get("urls", [])
    result = add_task({"platform": "xhs", "kind": "links", "payload": {"urls": urls}})
    return {
        "ok": True,
        "submitted": len(urls),
        "valid": len(set(urls)),
        "accepted": len(set(urls)) if result["created"] else 0,
        "skipped": 0 if result["created"] else len(set(urls)),
        "invalid": [],
        "queue_file": "持久任务队列",
        "triggered": {"ok": True},
        "task_id": result["id"],
    }


@app.post("/api/tasks/{identifier}/{action}")
def task_action(identifier: str, action: str):
    with store.connect() as db:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise HTTPException(404)
        if action == "retry" and row["state"] in {
            "failed",
            "interrupted",
            "limited",
            "cancelled",
        }:
            # Interrupted execution must have an expired lease before being restarted.
            if row["lease"] and row["lease"] > time.time():
                raise HTTPException(409, "执行仍在结束中")
            db.execute(
                "UPDATE tasks SET state='queued',owner=NULL,error='',updated=? WHERE id=?",
                (time.time(), identifier),
            )
        elif action == "cancel" and row["state"] in {"queued", "running"}:
            db.execute(
                "UPDATE tasks SET state='cancelled',updated=? WHERE id=?",
                (time.time(), identifier),
            )
        else:
            raise HTTPException(409, "当前任务不能执行该操作")
    return {"ok": True}


@app.post("/api/updates/check")
async def updates():
    return await asyncio.to_thread(check_updates, store, True)


@app.post("/internal/claim/{table}")
def claim(table: str, payload: dict):
    owner = str(payload["owner"])
    with store.connect() as db:
        db.execute(
            "INSERT INTO workers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,details=excluded.details",
            (
                owner,
                json.dumps(payload.get("platforms", [])),
                time.time(),
                json.dumps(payload.get("details", {})),
            ),
        )
    return {
        "job": store.claim(
            table, owner, payload.get("platforms") if table == "tasks" else None
        ),
        "settings": settings(),
    }


@app.post("/internal/heartbeat")
def worker_heartbeat(payload: dict):
    with store.connect() as db:
        db.execute(
            "INSERT INTO workers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,details=excluded.details",
            (
                str(payload["owner"]),
                json.dumps(payload.get("platforms", [])),
                time.time(),
                json.dumps(payload.get("details", {})),
            ),
        )
    return {"settings": settings()}


@app.post("/internal/bypass/claim")
def bypass_claim(payload: dict):
    job = (
        store.claim("assets", str(payload["owner"]))
        if not workspace_status()["available"]
        else None
    )
    return {"job": job, "settings": settings()}


@app.get("/internal/failed/{platform}")
def failed_records(platform: str, page: int = 1):
    return store.page("records", page, 100, platform, "failed")


@app.post("/internal/execution/{table}/{identifier}")
def report(table: str, identifier: str, payload: dict):
    owner, attempt = payload.pop("owner"), payload.pop("attempt")
    payload["lease"] = (
        time.time()
        if payload.get("state") in {"complete", "failed", "limited", "cancelled"}
        else time.time() + 90
    )
    if not store.update_execution(table, identifier, owner, attempt, **payload):
        raise HTTPException(409, "执行已取消或过期")
    if table == "assets" and payload.get("state") in {"complete", "failed"}:
        reconcile_asset(identifier)
    return {"ok": True}


def reconcile_asset(identifier):
    with store.connect() as db:
        asset = db.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone()
        details = json.loads(asset["details"])
        source_id = str(details.get("source_id", identifier))
        record = db.execute(
            "SELECT * FROM records WHERE platform=? AND source_id=?",
            (asset["platform"], source_id),
        ).fetchone()
        files = json.loads(record["files"]) if record else [asset["source"]]
        if asset["state"] == "complete":
            files = [
                asset["target"] if item == asset["source"] else item for item in files
            ]
        siblings = db.execute(
            "SELECT source,target,state,reason FROM assets WHERE platform=? AND json_extract(details,'$.source_id')=?",
            (asset["platform"], source_id),
        ).fetchall()
        relevant = [
            row for row in siblings if row["source"] in files or row["target"] in files
        ]
        pending = any(row["state"] not in {"complete", "deleted"} for row in relevant)
        failed = next((row for row in relevant if row["state"] == "failed"), None)
        deleted = any(row["state"] == "deleted" for row in relevant)
        metadata = (
            json.loads(record["metadata"]) if record else details.get("metadata", {})
        )
        if failed:
            metadata["error"] = sanitize(failed["reason"] or "处理失败")
        state = (
            "deleted"
            if deleted
            else "failed"
            if failed or metadata.get("download_incomplete")
            else "processing"
            if pending
            else "complete"
        )
        db.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?) ON CONFLICT(platform,source_id) DO UPDATE SET state=excluded.state,files=excluded.files,metadata=excluded.metadata,updated=excluded.updated",
            (
                asset["platform"],
                source_id,
                state,
                json.dumps(files),
                json.dumps(metadata, ensure_ascii=False),
                time.time(),
            ),
        )


@app.get("/internal/workspace/status")
def workspace_status():
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM workers WHERE updated>?", (time.time() - 45,)
        ).fetchall()
        saved = db.execute(
            "SELECT COALESCE(SUM(MAX(source_bytes-output_bytes,0)),0) FROM assets "
            "WHERE state='complete' AND output_bytes>0"
        ).fetchone()[0]
    worker_ready = any(
        "media" in json.loads(row["roles"]) and json.loads(row["details"]).get("ready")
        for row in rows
    )
    root = Path(settings()["workspace_root"])
    available = worker_ready and root.is_dir() and os.access(root, os.W_OK)
    return {
        "settings": settings(),
        "available": available,
        "saved_bytes_total": saved,
        "reason": "" if available else "工作区或硬件处理服务不可用，原件直接入库",
    }


@app.get("/internal/task/{identifier}")
def execution_state(identifier: str):
    with store.connect() as db:
        row = db.execute("SELECT state FROM tasks WHERE id=?", (identifier,)).fetchone()
    return {"state": row[0] if row else "missing"}


@app.post("/internal/logs")
def log(payload: dict):
    store.log(
        payload.get("platform", "system"),
        sanitize(payload.get("message", "")),
        payload.get("level", "info"),
        payload.get("task_id", ""),
        payload.get("event_id", ""),
    )
    return {"ok": True}


@app.api_route("/internal/checkpoint/{platform}/{source}", methods=["GET", "POST"])
async def checkpoint(platform: str, source: str, request: Request):
    with store.connect() as db:
        if request.method == "POST":
            payload = await request.json()
            db.execute(
                "INSERT INTO checkpoints VALUES(?,?,?,?) ON CONFLICT(platform,source) DO UPDATE SET head=excluded.head,updated=excluded.updated",
                (platform, source, str(payload["head"]), time.time()),
            )
        row = db.execute(
            "SELECT head FROM checkpoints WHERE platform=? AND source=?",
            (platform, source),
        ).fetchone()
    return {"head": row[0] if row else ""}


@app.post("/internal/records")
def record(payload: dict):
    with store.connect() as db:
        metadata = payload.get("metadata", {})
        previous = metadata.get("replaces_source_id")
        if previous and str(previous).startswith("url-"):
            db.execute(
                "DELETE FROM records WHERE platform=? AND source_id=? AND state='failed' AND json_extract(metadata,'$.url')=?",
                (payload["platform"], str(previous), metadata.get("url", "")),
            )
        db.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?) ON CONFLICT(platform,source_id) DO UPDATE SET state=excluded.state,files=excluded.files,metadata=excluded.metadata,updated=excluded.updated",
            (
                payload["platform"],
                str(payload["source_id"]),
                payload["state"],
                json.dumps(payload.get("files", [])),
                json.dumps(payload.get("metadata", {}), ensure_ascii=False),
                time.time(),
            ),
        )
        ids = [
            row[0]
            for row in db.execute(
                "SELECT id FROM assets WHERE platform=? AND state IN ('complete','failed','deleted') AND json_extract(details,'$.source_id')=?",
                (payload["platform"], str(payload["source_id"])),
            )
        ]
    for identifier in ids:
        reconcile_asset(identifier)
    return {"ok": True}


@app.get("/internal/record/{platform}/{identifier}")
def get_record(platform: str, identifier: str):
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM records WHERE platform=? AND source_id=?",
            (platform, identifier),
        ).fetchone()
    if not row:
        raise HTTPException(404)
    return store.decode(row)


@app.post("/internal/records/lookup")
def lookup_records(payload: dict):
    platform, identifiers = payload.get("platform"), payload.get("source_ids")
    if (
        platform not in PLATFORMS
        or not isinstance(identifiers, list)
        or len(identifiers) > 100
        or any(not isinstance(i, str) or len(i) > 200 for i in identifiers)
    ):
        raise HTTPException(422, "记录查询无效")
    if not identifiers:
        return {"records": {}}
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM records WHERE platform=? AND source_id IN ("
            + ",".join("?" for _ in identifiers)
            + ")",
            [platform, *identifiers],
        ).fetchall()
    return {"records": {row["source_id"]: store.decode(row) for row in rows}}


@app.get("/api/records/{platform}/{identifier}/file")
def record_file(platform: str, identifier: str, index: int = Query(0, ge=0)):
    value = get_record(platform, identifier)
    if index >= len(value["files"]):
        raise HTTPException(404)
    path = allowed_path(value["files"][index])
    if not path.is_file():
        raise HTTPException(404, "文件已被移动或删除")
    return FileResponse(path)


def allowed_path(value):
    path = Path(value).resolve()
    roots = [
        Path(p).resolve()
        for p in os.getenv("NAS_MEDIA_ROOTS", "/media,/downloads,/xhs,/douyin").split(
            ","
        )
    ]
    if not any(path.is_relative_to(root) for root in roots):
        raise HTTPException(422, "文件路径不在媒体目录中")
    return path


@app.post("/internal/intake")
def intake(payload: dict):
    platform = payload["platform"]
    if platform not in PLATFORMS:
        raise HTTPException(422)
    source, target = allowed_path(payload["source"]), allowed_path(payload["target"])
    if not source.is_file() or source.stat().st_size <= 0:
        raise HTTPException(422, "下载文件不存在或为空")
    # The source must be new staged output, never an existing archive file.
    if not source.is_relative_to(Path(settings()["workspace_root"]).resolve()):
        raise HTTPException(422, "只能处理工作区中新下载的文件")
    now = time.time()
    with store.connect() as db:
        identifier = hashlib.sha256(
            (platform + "\0" + str(source)).encode()
        ).hexdigest()[:24]
        db.execute(
            "INSERT OR IGNORE INTO assets(id,platform,source,target,original_target,state,created,updated,source_bytes,details) VALUES(?,?,?,?,?,'queued',?,?,?,?)",
            (
                identifier,
                platform,
                str(source),
                str(target),
                str(target),
                now,
                now,
                source.stat().st_size,
                json.dumps(payload.get("details", {}), ensure_ascii=False),
            ),
        )
        row = db.execute(
            "SELECT id FROM assets WHERE platform=? AND source=?",
            (platform, str(source)),
        ).fetchone()
    return {"id": row[0]}


@app.get("/api/assets/{identifier}/file")
def asset_file(identifier: str):
    with store.connect() as db:
        row = db.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404)
    path = allowed_path(row["target"] if row["state"] == "complete" else row["source"])
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.post("/api/assets/{identifier}/{action}")
def asset_action(identifier: str, action: str):
    with store.connect() as db:
        row = db.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise HTTPException(404)
        source = allowed_path(row["source"])
        if source.with_name(".archive-" + identifier + ".json").is_file():
            raise HTTPException(409, "文件正在恢复入库记录，请先恢复处理服务")
        if action == "retry" and row["state"] in {"failed", "interrupted"}:
            if row["lease"] and row["lease"] > time.time():
                raise HTTPException(409, "执行仍在结束中")
            db.execute(
                "UPDATE assets SET state='queued',reason='',updated=? WHERE id=?",
                (time.time(), identifier),
            )
        elif action == "delete" and row["state"] not in {
            "processing",
            "publishing",
            "complete",
        }:
            allowed_path(row["source"]).unlink(missing_ok=True)
            for candidate in source.parent.glob("." + source.stem + ".converted.*"):
                candidate.unlink(missing_ok=True)
            db.execute(
                "UPDATE assets SET state='deleted',updated=? WHERE id=?",
                (time.time(), identifier),
            )
            source_id = json.loads(row["details"]).get("source_id")
            if source_id:
                db.execute(
                    "UPDATE records SET state='deleted',metadata=json_set(metadata,'$.error','工作区文件已删除'),updated=? WHERE platform=? AND source_id=? AND EXISTS (SELECT 1 FROM json_each(records.files) WHERE value=?)",
                    (time.time(), row["platform"], str(source_id), row["source"]),
                )
        else:
            raise HTTPException(409, "处理中的文件不可删除；已入库文件请在归档目录管理")
    return {"ok": True}


@app.api_route("/api/telegram/{path:path}", methods=["GET", "POST", "HEAD"])
async def telegram_proxy(path: str, request: Request):
    # JSON and media only. Legacy HTML is deliberately absent from the new UI.
    if not path.startswith(("api/", "files/", "previews/")):
        raise HTTPException(404)
    url = os.getenv("TELEGRAM_URL", "http://telegram-worker:8000") + "/" + path
    headers = {"X-NAS-Download-Token": TOKEN}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    client = httpx.AsyncClient(timeout=30)
    try:
        result = await client.send(
            client.build_request(
                request.method,
                url,
                params=request.query_params,
                content=await request.body(),
                headers=headers,
            ),
            stream=True,
        )
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(503, "Telegram 服务尚未连接")

    async def close():
        await result.aclose()
        await client.aclose()

    return StreamingResponse(
        result.aiter_raw(),
        status_code=result.status_code,
        headers={
            k: v
            for k, v in result.headers.items()
            if k.lower()
            in {"content-type", "content-range", "accept-ranges", "content-length"}
        },
        background=BackgroundTask(close),
    )


if STATIC.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith(("api/", "internal/")) or not (STATIC / "index.html").is_file():
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html")
