import json
import random
import time
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core import app as server
from core.config import DEFAULTS, write_json
from core.store import Store
from core import media
from workers.media_worker import finish_receipt


def test_telegram_gateway_forwards_json_content_type(client, monkeypatch):
    import httpx

    async_client = httpx.AsyncClient

    def accept(request):
        assert request.headers['content-type'] == 'application/json'
        assert json.loads(request.content) == {'megabytes_per_second': 2}
        assert request.headers['X-NAS-Download-Token'] == 'private-test-key'
        return httpx.Response(200, json={'ok': True})

    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kwargs: async_client(transport=httpx.MockTransport(accept), **kwargs))
    response = client.post('/api/telegram/api/controls/limit', json={'megabytes_per_second': 2})
    assert response.status_code == 200 and response.json() == {'ok': True}


def test_xhs_legacy_schedule_migrates_can_be_saved_and_enqueues(client, tmp_path, monkeypatch):
    import asyncio
    from core import config, migrate

    for key in ("x", "pixiv", "douyin", "xhs"):
        monkeypatch.setitem(config.CONFIG_PATHS, key, tmp_path / key / "config.json")
    write_json(config.CONFIG_PATHS['xhs'], {'database': str(tmp_path / 'missing.sqlite3'), 'run_interval_seconds': 1800})
    migrate.import_legacy(server.store)
    prefs = server.settings()
    assert prefs['schedule']['xhs'] == {'enabled': True, 'hours': 0.5}
    assert client.patch('/api/settings', json={'schedule': prefs['schedule']}).status_code == 200
    server.store.set('due:xhs', time.time() - 1)
    monkeypatch.setattr(server, 'check_updates', lambda store: None)
    monkeypatch.setattr(server, 'schedule_xhs_retries', lambda now: None)

    async def stop_after_cycle(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(server.asyncio, 'sleep', stop_after_cycle)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server.scheduler())
    with server.store.connect() as db:
        row = db.execute('SELECT platform,kind,state FROM tasks').fetchone()
    assert tuple(row) == ('xhs', 'sync', 'queued')
    assert server.store.get('due:xhs') > time.time() + 1700


def test_console_standalone_starts_and_saves_threshold_without_worker_package(tmp_path):
    isolated = tmp_path / "console-only"
    shutil.copytree(
        Path(server.__file__).parent,
        isolated / "core",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    environment = dict(
        os.environ,
        NAS_CORE_DATA=str(isolated / "state"),
        NAS_SCHEDULER="false",
        NAS_IMPORT_LEGACY="false",
        NAS_INITIALIZE_CONFIGS="true",
        INTERNAL_API_TOKEN="standalone-test",
        PYTHONPATH=str(isolated),
    )
    for platform in ("x", "pixiv", "douyin", "xhs", "telegram"):
        environment[platform.upper() + "_CONFIG_PATH"] = str(
            isolated / "config" / platform / "config.json"
        )
    code = """from core.app import app
from fastapi.testclient import TestClient
with TestClient(app) as client:
    assert client.get('/healthz').status_code == 200
    response = client.patch('/api/platforms/x/settings',json={'fields':{'known_stop_consecutive':7}})
    assert response.status_code == 200, response.text
    assert response.json()['fields']['known_stop_consecutive'] == 7
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=isolated,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_cumulative_savings_counts_only_confirmed_assets_once_and_survives_restart(
    client, tmp_path
):
    headers = {"X-NAS-Download-Token": "private-test-key"}
    for index, (state, output) in enumerate(
        [("complete", 60), ("complete", 100), ("complete", 140), ("failed", 20)]
    ):
        source = tmp_path / "workspace" / f"{index}.png"
        source.write_bytes(b"x" * 100)
        identifier = client.post(
            "/internal/intake",
            headers=headers,
            json={
                "platform": "xhs",
                "source": str(source),
                "target": str(tmp_path / "archive" / source.name),
            },
        ).json()["id"]
        job = server.store.claim("assets", "media")
        payload = {
            "owner": "media",
            "attempt": job["attempt"],
            "state": state,
            "output_bytes": output,
        }
        for _ in range(2):
            assert (
                client.post(
                    "/internal/execution/assets/" + identifier,
                    headers=headers,
                    json=payload,
                ).status_code
                == 200
            )
    assert client.get("/api/workspace").json()["saved_bytes_total"] == 40
    server.store = Store(server.store.path)
    assert client.get("/api/workspace").json()["saved_bytes_total"] == 40
    assert (
        client.post("/api/assets/" + identifier + "/retry", json={}).status_code == 200
    )
    rerun = server.store.claim("assets", "media")
    assert (
        client.post(
            "/internal/execution/assets/" + identifier,
            headers=headers,
            json={
                "owner": "media",
                "attempt": rerun["attempt"],
                "state": "complete",
                "output_bytes": 90,
            },
        ).status_code
        == 200
    )
    assert client.get("/api/workspace").json()["saved_bytes_total"] == 50


def test_stopping_threshold_validation_and_batch_record_lookup(
    client, tmp_path, monkeypatch
):
    path = tmp_path / "x-config.json"
    monkeypatch.setitem(server.CONFIG_PATHS, "x", path)
    for value in (0, -1, 1.5, True, 1001):
        assert (
            client.patch(
                "/api/platforms/x/settings",
                json={"fields": {"known_stop_consecutive": value}},
            ).status_code
            == 422
        )
    assert not path.exists()
    assert (
        client.patch(
            "/api/platforms/x/settings", json={"fields": {"known_stop_consecutive": 10}}
        ).status_code
        == 200
    )
    headers = {"X-NAS-Download-Token": "private-test-key"}
    client.post(
        "/internal/records",
        headers=headers,
        json={
            "platform": "x",
            "source_id": "a",
            "state": "complete",
            "files": ["one.jpg"],
        },
    )
    response = client.post(
        "/internal/records/lookup",
        headers=headers,
        json={"platform": "x", "source_ids": ["a", "missing"]},
    )
    assert list(response.json()["records"]) == ["a"]
    assert (
        client.post(
            "/internal/records/lookup",
            headers=headers,
            json={"platform": "x", "source_ids": ["a"] * 101},
        ).status_code
        == 422
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "store", Store(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(server, "TOKEN", "private-test-key")
    monkeypatch.setenv("NAS_SCHEDULER", "false")
    monkeypatch.setenv("NAS_INITIALIZE_CONFIGS", "false")
    monkeypatch.setenv("NAS_MEDIA_ROOTS", str(tmp_path))
    root = tmp_path / "workspace"
    root.mkdir()
    server.store.set("preferences", {**DEFAULTS, "workspace_root": str(root)})
    with TestClient(server.app) as http:
        yield http


def test_password_is_opt_in_and_bad_settings_cannot_set_password(client):
    assert client.get("/api/auth/status").json()["authenticated"]
    assert client.patch("/api/settings", json={"auth_enabled": True}).status_code == 422
    assert (
        client.patch(
            "/api/settings", json={"password": "secret", "log_preview_lines": 2}
        ).status_code
        == 422
    )
    assert not server.store.get("password")
    assert (
        client.patch(
            "/api/settings", json={"password": "test-password", "auth_enabled": True}
        ).status_code
        == 200
    )
    assert client.get("/api/overview").status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert (
        client.post("/api/auth/login", json={"password": "test-password"}).status_code
        == 200
    )
    assert client.get("/api/overview").status_code == 200


def test_task_dedup_fencing_and_minute_limit_result(client):
    task = client.post("/api/tasks", json={"platform": "x"}).json()["id"]
    assert not client.post("/api/tasks", json={"platform": "x"}).json()["created"]
    job = server.store.claim("tasks", "worker", ["x"])
    assert not server.store.claim("tasks", "second-worker", ["x"])
    assert not server.store.update_execution(
        "tasks", task, "wrong-worker", 1, state="complete"
    )
    headers = {"X-NAS-Download-Token": "private-test-key"}
    assert (
        client.post(
            "/internal/execution/tasks/" + task,
            headers=headers,
            json={
                "owner": "worker",
                "attempt": job["attempt"],
                "state": "limited",
                "error": "上限，本次未完成",
            },
        ).status_code
        == 200
    )
    assert client.post("/api/tasks/" + task + "/retry", json={}).status_code == 200
    rerun = server.store.claim("tasks", "second-worker", ["x"])
    assert rerun["attempt"] == 2
    assert not server.store.update_execution(
        "tasks", task, "worker", 1, state="complete"
    )


def test_workspace_bypass_health_and_record_reconciliation(client, tmp_path):
    assert not client.get("/api/workspace").json()["available"]
    headers = {"X-NAS-Download-Token": "private-test-key"}
    assert (
        client.post(
            "/internal/heartbeat",
            headers=headers,
            json={"owner": "media", "platforms": ["media"], "details": {"ready": True}},
        ).status_code
        == 200
    )
    assert client.get("/api/workspace").json()["available"]
    source = tmp_path / "workspace" / "one.png"
    Image.new("RGB", (20, 30)).save(source)
    target = tmp_path / "archive" / "one.png"
    body = {
        "platform": "telegram",
        "source": str(source),
        "target": str(target),
        "details": {"source_id": "message-1"},
    }
    identifier = client.post("/internal/intake", headers=headers, json=body).json()[
        "id"
    ]
    assert (
        client.post("/internal/intake", headers=headers, json=body).json()["id"]
        == identifier
    )
    job = client.post(
        "/internal/claim/assets",
        headers=headers,
        json={"owner": "media", "platforms": ["media"], "details": {"ready": True}},
    ).json()["job"]
    target.parent.mkdir()
    target.write_bytes(source.read_bytes())
    payload = {
        "owner": "media",
        "attempt": job["attempt"],
        "state": "complete",
        "target": str(target),
        "output_bytes": target.stat().st_size,
        "details": job["details"],
    }
    assert (
        client.post(
            "/internal/execution/assets/" + identifier, headers=headers, json=payload
        ).status_code
        == 200
    )
    # Downloader's record may arrive after media has already finished.
    assert (
        client.post(
            "/internal/records",
            headers=headers,
            json={
                "platform": "telegram",
                "source_id": "message-1",
                "state": "processing",
                "files": [str(source)],
            },
        ).status_code
        == 200
    )
    value = client.get("/internal/record/telegram/message-1", headers=headers).json()
    assert value["state"] == "complete" and value["files"] == [str(target)]
    assert (
        client.post(
            "/internal/execution/assets/" + identifier, headers=headers, json=payload
        ).status_code
        == 200
    )


def test_png_transparency_inflation_and_resolution(tmp_path):
    source = tmp_path / "alpha.png"
    Image.new("RGBA", (100, 80), (200, 100, 40, 100)).save(source)
    chosen, reason, _ = media.convert(source)
    assert chosen == source and "透明" in reason
    Image.new("RGB", (100, 80), (200, 100, 40)).save(source)
    chosen, reason, _ = media.convert(source)
    assert chosen == source and "不更小" in reason
    noise = random.Random(12).randbytes(300 * 200 * 3)
    Image.frombytes("RGB", (300, 200), noise).save(source)
    chosen, reason, _ = media.convert(source)
    assert chosen.suffix == ".jpg" and chosen.stat().st_size < source.stat().st_size
    with Image.open(chosen) as image:
        assert image.size == (300, 200)


def test_retry_once_then_keep_valid_original(tmp_path, monkeypatch):
    source = tmp_path / "original.png"
    Image.new("RGB", (30, 40)).save(source)
    calls = []

    def fail(*args):
        calls.append(1)
        raise RuntimeError("synthetic conversion failure")

    monkeypatch.setattr(media, "png_candidate", fail)
    monkeypatch.setattr(media.time, "sleep", lambda value: None)
    chosen, reason, _ = media.convert(source)
    assert len(calls) == 2 and chosen == source and "重试仍失败" in reason
    source.write_bytes(b"not-a-png")
    with pytest.raises(Exception):
        media.convert(source)


def test_publish_resume_and_delete_only_after_core_ack(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"original-file")
    chosen = tmp_path / "converted.jpg"
    chosen.write_bytes(b"converted")
    receipt_path = tmp_path / ".archive-one.json"
    receipt = {
        "id": "one",
        "source": str(source),
        "chosen": str(chosen),
        "archive_target": str(tmp_path / "archive" / "media.png"),
        "report": {"owner": "media", "attempt": 1, "state": "complete"},
    }
    write_json(receipt_path, receipt)

    class Client:
        failed = True

        def request(self, path, payload):
            if self.failed:
                raise RuntimeError("core disconnected")
            return {"ok": True}

    client = Client()
    with pytest.raises(RuntimeError):
        finish_receipt(client, receipt, receipt_path)
    assert source.exists() and chosen.exists()
    destination = Path(json.loads(receipt_path.read_text())["destination"])
    assert destination.read_bytes() == b"converted"
    client.failed = False
    finish_receipt(client, json.loads(receipt_path.read_text()), receipt_path)
    assert not source.exists() and not chosen.exists() and not receipt_path.exists()
    assert list(destination.parent.iterdir()) == [destination]


def test_hardware_command_has_no_resize_and_limits_pacing():
    command = media.hevc_command(Path("source.mp4"), Path("result.mp4"), {"color": {}})
    assert command[command.index("-c:v") + 1] == "hevc_qsv"
    assert command[command.index("-readrate") + 1] == "0.5"
    assert "-s" not in command and "-vf" not in command
    assert command[command.index("-fps_mode") + 1] == "passthrough"


def test_expired_media_can_ack_receipt_but_superseded_attempt_cannot(client, tmp_path):
    source = tmp_path / "workspace" / "sample.png"
    source.write_bytes(b"data")
    headers = {"X-NAS-Download-Token": "private-test-key"}
    identifier = client.post(
        "/internal/intake",
        headers=headers,
        json={
            "platform": "xhs",
            "source": str(source),
            "target": str(tmp_path / "final.png"),
        },
    ).json()["id"]
    job = server.store.claim("assets", "worker")
    with server.store.connect() as db:
        db.execute(
            "UPDATE assets SET lease=? WHERE id=?", (time.time() - 1, identifier)
        )
    server.store.claim("assets", "second")
    assert server.store.update_execution(
        "assets", identifier, "worker", job["attempt"], state="complete"
    )


def test_logs_preview_and_full_export(client):
    for number in range(135):
        server.store.log("x", str(number))
    assert len(client.get("/api/logs").json()["items"]) == 30
    assert len(client.get("/api/logs/export/all").text.splitlines()) == 135
    assert client.get("/internal/workspace/status").status_code == 401


def test_invalid_preference_types_return_validation_errors(client):
    for payload in (
        {"workspace": []},
        {"schedule": {"x": 3}},
        {"log_preview_lines": "oops"},
    ):
        assert client.patch("/api/settings", json=payload).status_code == 422
    assert client.post("/api/tasks", json={"platform": "telegram"}).status_code == 422


def test_retry_file_set_is_independent_of_previous_failed_asset(client, tmp_path):
    headers = {"X-NAS-Download-Token": "private-test-key"}
    sources = [tmp_path / "workspace" / name for name in ["old.mp4", "new.mp4"]]
    for source in sources:
        source.write_bytes(b"synthetic")
    ids = []
    for source in sources:
        ids.append(
            client.post(
                "/internal/intake",
                headers=headers,
                json={
                    "platform": "xhs",
                    "source": str(source),
                    "target": str(tmp_path / source.name),
                    "details": {"source_id": "same-note"},
                },
            ).json()["id"]
        )
    job = server.store.claim("assets", "media")
    assert job["id"] == ids[0]
    assert (
        client.post(
            "/internal/execution/assets/" + ids[0],
            headers=headers,
            json={
                "owner": "media",
                "attempt": job["attempt"],
                "state": "failed",
                "reason": "bad original",
            },
        ).status_code
        == 200
    )
    # Failure can arrive before the downloader sends its catalog record.
    payload = {
        "platform": "xhs",
        "source_id": "same-note",
        "state": "processing",
        "files": [str(sources[0])],
    }
    client.post("/internal/records", headers=headers, json=payload)
    assert (
        client.get("/internal/record/xhs/same-note", headers=headers).json()["state"]
        == "failed"
    )
    payload["files"] = [str(sources[1])]
    client.post("/internal/records", headers=headers, json=payload)
    job = server.store.claim("assets", "media")
    assert job["id"] == ids[1]
    client.post(
        "/internal/execution/assets/" + ids[1],
        headers=headers,
        json={
            "owner": "media",
            "attempt": job["attempt"],
            "state": "complete",
            "target": str(tmp_path / "new.mp4"),
        },
    )
    assert (
        client.get("/internal/record/xhs/same-note", headers=headers).json()["state"]
        == "complete"
    )
    client.post("/api/assets/" + ids[0] + "/delete", json={})
    assert (
        client.get("/internal/record/xhs/same-note", headers=headers).json()["state"]
        == "complete"
    )


def test_telegram_replays_only_completed_workspace_handoffs(tmp_path, monkeypatch):
    from services.telegram.app.history import DownloadHistory, DownloadRecord
    from workers import telegram_delivery as delivery

    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "new.png"
    source.write_bytes(b"completed")
    archived = tmp_path / "archive" / "new.png"
    history = DownloadHistory(tmp_path / "history.json")
    history.add(
        DownloadRecord(
            "new",
            1,
            1,
            source.name,
            str(source),
            status="verifying",
            total_bytes=9,
            archive_path=str(archived),
        )
    )
    history.add(
        DownloadRecord(
            "old", 2, 1, "old.png", str(tmp_path / "old.png"), status="complete"
        )
    )
    assert history.recover_incomplete()["recovered"] == 1
    calls = []
    monkeypatch.setattr(
        delivery.Client,
        "request",
        lambda *a, **k: {"settings": {"workspace_root": str(root)}},
    )
    monkeypatch.setattr(
        delivery,
        "submit",
        lambda *a, **k: calls.append(("submit", a[1])) or "asset-new",
    )
    monkeypatch.setattr(
        delivery, "record", lambda *a, **k: calls.append(("record", a[1]))
    )
    delivery.recover_handoffs(history)
    assert history.find("new")["workspace_asset_id"] == "asset-new"
    assert history.find("new")["status"] == "processing"
    assert history.find("old")["status"] == "complete"
    assert source.exists() and len(calls) == 2
    delivery.recover_handoffs(history)
    assert len(calls) == 2


def test_xhs_handoff_precedes_upstream_download_marker(tmp_path, monkeypatch):
    import asyncio, sys, types
    from workers import xhs_job

    root = tmp_path / "workspace"
    root.mkdir()
    config = tmp_path / "config.json"
    write_json(config, {})
    monkeypatch.setitem(xhs_job.CONFIG_PATHS, "xhs", config)
    settings = tmp_path / "settings.json"
    write_json(settings, {"work_path": str(tmp_path / "archive")})
    monkeypatch.setenv("XHS_SETTINGS_PATH", str(settings))
    monkeypatch.setattr(xhs_job, "prepare", lambda *a: (root / "incoming", True))
    events = []

    async def add(identifier, *a, **kw):
        events.append("upstream-marker")

    class XHS:
        def __init__(self, **kw):
            self.id_recorder = types.SimpleNamespace(add=add)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def extract(self, url, **kw):
            assert url == "https://www.xiaohongshu.com/explore/note"
            (root / "new.png").write_bytes(b"image")
            await self.id_recorder.add("note")
            (root / "note.json").write_text("{}")
            kw["result_callback"]({"success": 1})
            return [{"作品ID": "note", "作品标题": "one"}]

    for name in ["source", "source.application", "source.application.app"]:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["source.application.app"].XHS = XHS
    monkeypatch.setattr(
        xhs_job,
        "submit",
        lambda *a, **kw: events.append("intake:" + a[1].name) or "asset",
    )
    monkeypatch.setattr(xhs_job, "record", lambda *a, **kw: events.append("record"))
    monkeypatch.setattr(xhs_job.Client, "request", lambda *a, **kw: {"metadata": {}})
    stats = asyncio.run(
        xhs_job.run(
            {
                "id": "task",
                "kind": "links",
                "payload": {"urls": ["https://www.xiaohongshu.com/explore/note"]},
            }
        )
    )
    assert (
        events.index("intake:new.png")
        < events.index("record")
        < events.index("upstream-marker")
    )
    assert "intake:note.json" in events and stats["downloaded"] == 1


def test_xhs_file_queue_establishes_baseline_then_only_new_urls(tmp_path, monkeypatch):
    from workers import xhs_job

    path = tmp_path / "links.txt"
    old = "https://www.xiaohongshu.com/explore/old"
    new = "https://www.xiaohongshu.com/explore/new"
    path.write_text(old + "\n")
    heads = {}

    def request(self, key, payload=None):
        if payload:
            heads[key] = payload["head"]
        return {"head": heads.get(key, "")}

    monkeypatch.setattr(xhs_job.Client, "request", request)
    assert xhs_job.queue_urls({"queue_files": [str(path)]})[0] == []
    path.write_text(new + "\n" + old + "\n")
    assert xhs_job.queue_urls({"queue_files": [str(path)]})[0] == [new]


def test_xhs_auto_retry_excludes_imported_history(client):
    with server.store.connect() as db:
        for identifier, metadata in [
            (
                "old",
                {"url": "https://www.xiaohongshu.com/explore/old", "imported": True},
            ),
            ("new", {"url": "https://www.xiaohongshu.com/explore/new", "attempts": 1}),
        ]:
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (
                    "xhs",
                    identifier,
                    "failed",
                    "[]",
                    json.dumps(metadata),
                    time.time() - 400,
                ),
            )
    server.schedule_xhs_retries(time.time())
    tasks = client.get("/api/tasks").json()["items"]
    assert len(tasks) == 1 and tasks[0]["payload"]["urls"] == [
        "https://www.xiaohongshu.com/explore/new"
    ]


def test_upstream_date_versions_ignore_zero_padding(monkeypatch):
    from core import updates

    monkeypatch.setattr(
        updates,
        "github",
        lambda path: {
            "tag_name": "2026.08.19",
            "html_url": "https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19",
        },
    )
    assert (
        updates.check_one("yt-dlp", updates.SOURCES["yt-dlp"], {})["status"]
        == "current"
    )
