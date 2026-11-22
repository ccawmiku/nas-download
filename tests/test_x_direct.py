import asyncio
import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import app as server
from core.config import DEFAULTS, write_json
from core.store import Store
from core.x_direct import (
    MAX_DIRECT_TWEETS_PER_BATCH,
    download_cdn_file,
    normalize_direct_payload,
    validate_media_url,
)
from core.x_errors import MAX_ATTEMPTS
from workers import platform_job as jobs
from workers import xhs_job

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_src/x-auto-download-nas-main"))
import x_auto_worker as x


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "store", Store(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "TOKEN", "test-key")
    monkeypatch.setenv("NAS_SCHEDULER", "false")
    monkeypatch.setenv("NAS_INITIALIZE_CONFIGS", "false")
    monkeypatch.setenv("NAS_MEDIA_ROOTS", str(tmp_path))
    monkeypatch.setitem(server.CONFIG_PATHS, "x", tmp_path / "x-config.json")
    monkeypatch.setitem(server.CONFIG_PATHS, "xhs", tmp_path / "xhs-config.json")
    server.store.set("preferences", {**DEFAULTS, "workspace_root": str(tmp_path)})
    with TestClient(server.app) as c:
        yield c


def _sample_direct_payload():
    return {
        "batch_id": "batch-x-1",
        "source_mode": "bg",
        "account": "alice",
        "discovered": 3,
        "items": [
            {
                "tweet_id": "1001",
                "author": "outer_user",
                "url": "https://x.com/outer_user/status/1001",
                "title": "outer photo tweet",
                "owner_role": "outer",
                "media": [
                    {
                        "media_key": "3_1001",
                        "type": "photo",
                        "url": "https://pbs.twimg.com/media/AbCdEf1?format=jpg&name=orig",
                    }
                ],
            },
            {
                "tweet_id": "2002",
                "author": "quoted_user",
                "url": "https://x.com/quoted_user/status/2002",
                "title": "quoted video tweet",
                "owner_role": "quoted",
                "quoted_from": "1001",
                "media": [
                    {
                        "media_key": "7_2002",
                        "type": "video",
                        "url": "https://video.twimg.com/ext_tw_video/2002/pu/vid/avc1/720x1280/clip.mp4?tag=12",
                    }
                ],
            },
        ],
    }


def test_direct_url_validation_rejects_ssrf_and_non_cdn_hosts():
    assert (
        validate_media_url("https://pbs.twimg.com/media/abc?format=jpg&name=orig", "photo")
        == "https://pbs.twimg.com/media/abc?format=jpg&name=orig"
    )
    assert (
        validate_media_url("https://video.twimg.com/tweet_video/abc.mp4", "animated_gif")
        == "https://video.twimg.com/tweet_video/abc.mp4"
    )
    bad_cases = [
        ("http://pbs.twimg.com/media/abc.jpg", "photo"),
        ("https://evil.com/pbs.twimg.com/abc.jpg", "photo"),
        ("https://pbs.twimg.com.evil.com/media/abc.jpg", "photo"),
        ("https://127.0.0.1/media/abc.jpg", "photo"),
        ("https://user:pass@pbs.twimg.com/media/abc.jpg", "photo"),
        ("https://pbs.twimg.com:8443/media/abc.jpg", "photo"),
        ("https://video.twimg.com/tweet_video/abc.mp4", "photo"),
        ("https://pbs.twimg.com/media/abc.jpg", "video"),
    ]
    for url, mtype in bad_cases:
        with pytest.raises(ValueError):
            validate_media_url(url, mtype)

    oversized = {
        "items": [
            {
                "tweet_id": str(i + 1),
                "media": [{"type": "photo", "url": f"https://pbs.twimg.com/media/{i}.jpg"}],
            }
            for i in range(MAX_DIRECT_TWEETS_PER_BATCH + 1)
        ]
    }
    with pytest.raises(ValueError):
        normalize_direct_payload(oversized)


def test_cdn_downloader_blocks_redirects_html_and_oversized_streams(tmp_path):
    class FakeResponse:
        def __init__(self, status_code, headers, chunks):
            self.status_code = status_code
            self.headers = headers
            self._chunks = chunks

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, chunk_size=0):
            return iter(self._chunks)

    class FakeSession:
        def __init__(self, response):
            self._response = response
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(kwargs)
            assert kwargs.get("allow_redirects") is False
            return self._response

    with pytest.raises(RuntimeError, match="重定向"):
        download_cdn_file(
            "https://pbs.twimg.com/media/a?format=jpg&name=orig",
            "photo",
            tmp_path,
            "1_a",
            session=FakeSession(FakeResponse(302, {"location": "http://127.0.0.1/secret"}, [])),
        )

    with pytest.raises(RuntimeError, match="类型不符"):
        download_cdn_file(
            "https://pbs.twimg.com/media/a?format=jpg&name=orig",
            "photo",
            tmp_path,
            "1_a",
            session=FakeSession(FakeResponse(200, {"content-type": "text/html"}, [b"<html>bad</html>"])),
        )

    with pytest.raises(RuntimeError, match="体积上限"):
        download_cdn_file(
            "https://pbs.twimg.com/media/a?format=jpg&name=orig",
            "photo",
            tmp_path,
            "1_a",
            max_bytes=5,
            session=FakeSession(FakeResponse(200, {"content-type": "image/jpeg"}, [b"123", b"456"])),
        )

    ok_path = download_cdn_file(
        "https://pbs.twimg.com/media/a?format=png&name=orig",
        "photo",
        tmp_path,
        "1_a",
        max_bytes=100,
        session=FakeSession(FakeResponse(200, {"content-type": "image/png"}, [b"pngdata"])),
    )
    assert ok_path.name == "1_a.png" and ok_path.read_bytes() == b"pngdata"


def test_x_direct_intake_auth_origin_dedupe_idempotency_and_telemetry(client):
    payload = _sample_direct_payload()
    # Unauthenticated calls fail with 401 even when auth_enabled is False and emit auth_error telemetry.
    assert client.post("/api/x/direct", headers={"Origin": "https://x.com"}, json=payload).status_code == 401
    status_after_401 = client.get("/api/browser-push/status").json()
    assert status_after_401["summary"]["x"]["last_push"]["status"] == "auth_error"

    token = client.get("/api/browser-push/token").json()["token"]
    # Wrong Origin rejected with 403.
    assert (
        client.post(
            "/api/x/direct",
            headers={"X-NAS-Submit-Token": token, "Origin": "https://evil.example"},
            json=payload,
        ).status_code
        == 403
    )

    headers = {"X-NAS-Submit-Token": token, "Origin": "https://x.com"}
    first = client.post("/api/x/direct", headers=headers, json=payload)
    assert first.status_code == 200
    body = first.json()
    assert body["accepted"] == 2 and body["skipped"] == 0 and body["task_id"]

    # Idempotent replay returns exact original receipt even if task finished.
    with server.store.connect() as db:
        db.execute("UPDATE tasks SET state='complete' WHERE id=?", (body["task_id"],))
    replay = client.post("/api/x/direct", headers=headers, json=payload).json()
    assert replay["idempotent_replay"] is True and replay["task_id"] == body["task_id"]

    # Conflicting payload under the same batch_id returns 409.
    conflict = client.post(
        "/api/x/direct",
        headers=headers,
        json={**payload, "items": [payload["items"][0]]},
    )
    assert conflict.status_code == 409

    # Completed records are skipped when submitted in a fresh batch; unresolvable entries are logged as terminal failures.
    server.record(
        {
            "platform": "x",
            "source_id": "1001",
            "state": "complete",
            "files": ["/downloads/x/images/1001.jpg"],
            "metadata": {"source_mode": "direct"},
        }
    )
    second = client.post(
        "/api/x/direct",
        headers=headers,
        json={
            "batch_id": "batch-x-2",
            "source_mode": "workbench",
            "items": [
                payload["items"][0],
                {
                    "tweet_id": "3003",
                    "author": "vid_user",
                    "url": "https://x.com/vid_user/status/3003",
                    "media": [],
                    "unresolvable_reason": "missing_video_variant",
                },
            ],
        },
    ).json()
    assert second["skipped"] == 1 and second["accepted"] == 0 and second["unresolvable"] == 1
    err_rec = server.get_record("x", "3003")
    assert err_rec["state"] == "failed" and err_rec["metadata"]["retry_exhausted"] is True

    # Browser empty-check report is also surfaced in push summary & overview.
    rep = client.post(
        "/api/browser-push/report",
        headers=headers,
        json={
            "platform": "x",
            "source_mode": "bg",
            "account": "alice",
            "status": "empty",
            "discovered": 20,
            "message": "今日检查 20 条赞过推文，无新增媒体",
        },
    )
    assert rep.status_code == 200
    overview = client.get("/api/overview").json()
    assert overview["browser_push"]["x"]["last_push"]["status"] == "empty"


def test_x_direct_worker_and_retry_never_fallback_to_browser_collector(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    monkeypatch.setattr(server, "store", store)
    x_cfg_path = tmp_path / "x.json"
    download_dir = tmp_path / "x_downloads"
    write_json(
        x_cfg_path,
        {
            "database": str(tmp_path / "x_auto.sqlite3"),
            "cookie_file": str(tmp_path / "missing_cookies.txt"),
            "download_dir": str(download_dir),
            "retry_failed": True,
            "max_download_attempts": MAX_ATTEMPTS,
            "request_delay_seconds": 0,
            "media": {"convert_gif": False},
        },
    )
    monkeypatch.setitem(jobs.CONFIG_PATHS, "x", x_cfg_path)

    class LocalClient:
        def request(self, path, payload=None):
            if path.startswith("/internal/record/x/"):
                return server.get_record("x", path.rsplit("/", 1)[-1])
            if path == "/internal/records/lookup":
                return server.lookup_records(payload)
            if path.startswith("/internal/failed/"):
                return server.failed_records("x")
            if path.startswith("/internal/checkpoint/"):
                return {"head": ""}
            raise AssertionError(path)

    monkeypatch.setattr(jobs, "Client", LocalClient)
    monkeypatch.setattr("workers.stopping.Client", LocalClient)
    monkeypatch.setattr(jobs, "prepare", lambda *a: (tmp_path / "unused", False))
    monkeypatch.setattr(
        jobs,
        "record",
        lambda platform, identifier, state, files, metadata: server.record(
            {
                "platform": platform,
                "source_id": identifier,
                "state": state,
                "files": files,
                "metadata": metadata,
            }
        ),
    )

    class ForbidBrowser:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Direct X flow must never launch BrowserCollector or read cookies!")

    monkeypatch.setattr(x, "BrowserCollector", ForbidBrowser)

    # First attempt: CDN fails -> record saved as failed WITH direct_media persisted.
    with patch("core.x_direct.download_cdn_file", side_effect=RuntimeError("CDN HTTP 503")):
        stats = jobs.x_job({"id": "job-1", "kind": "direct", "payload": {"items": _sample_direct_payload()["items"]}})
    assert stats["failed"] == 2 and stats["downloaded"] == 0
    failed_1001 = server.get_record("x", "1001")
    assert failed_1001["state"] == "failed"
    assert failed_1001["metadata"]["source_mode"] == "direct"
    assert len(failed_1001["metadata"]["direct_media"]) == 1

    # Retry attempt: uses persisted direct_media and succeeds WITHOUT BrowserCollector!
    def ok_download(url, media_type, target_dir, stem, **kwargs):
        ext = ".jpg" if media_type == "photo" else ".mp4"
        out = target_dir / f"{stem}{ext}"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ok-media-bytes")
        return out

    with patch("workers.platform_job.download_direct_item") as mock_dl:
        from core.x_direct import download_direct_item as real_dl
        mock_dl.side_effect = real_dl
        with patch("core.x_direct.download_cdn_file", side_effect=ok_download):
            retry_stats = jobs.x_job({"id": "job-2", "kind": "retry", "payload": {}})
    assert retry_stats["downloaded"] == 2 and retry_stats["failed"] == 0
    assert server.get_record("x", "1001")["state"] == "complete"
    assert server.get_record("x", "2002")["state"] == "complete"


def test_xhs_batch_idempotency_and_failure_classification_stops_infinite_retries(client, tmp_path, monkeypatch):
    token = client.get("/api/browser-push/token").json()["token"]
    headers = {"X-NAS-Submit-Token": token, "Origin": "https://www.xiaohongshu.com"}
    url = "https://www.xiaohongshu.com/explore/1234567890abcdef12345678?xsec_token=tok1"

    r1 = client.post(
        "/api/xhs/links",
        headers=headers,
        json={"urls": [url], "batch_id": "xhs-b1", "source_mode": "bg"},
    ).json()
    with server.store.connect() as db:
        db.execute("UPDATE tasks SET state='complete' WHERE id=?", (r1["task_id"],))
    r2 = client.post(
        "/api/xhs/links",
        headers=headers,
        json={"urls": [url], "batch_id": "xhs-b1", "source_mode": "bg"},
    ).json()
    assert r2["idempotent_replay"] is True and r2["task_id"] == r1["task_id"]

    assert xhs_job.classify_xhs_failure("HTTP 429 Too Many Requests retry_after=900")["kind"] == "rate_limit"
    assert xhs_job.classify_xhs_failure("xsec_token 已过期 404")["needs_fresh_token"] is True
    assert xhs_job.classify_xhs_failure("触发风控验证码")["retry_paused"] is True

    # Even with max_download_attempts=0, token_expired / auth_required / cap=5 will not schedule infinite retries.
    write_json(tmp_path / "xhs-config.json", {"retry_failed": True, "max_download_attempts": 0, "network_retry_delay_seconds": 60})
    now = time.time()
    with server.store.connect() as db:
        for sid, meta in [
            ("tok-expired", {"url": "https://www.xiaohongshu.com/explore/111111111111111111111111?xsec_token=old", "needs_fresh_token": True, "retry_exhausted": True, "attempts": 1}),
            ("net-capped", {"url": "https://www.xiaohongshu.com/explore/222222222222222222222222?xsec_token=ok", "attempts": 5}),
            ("net-backoff", {"url": "https://www.xiaohongshu.com/explore/333333333333333333333333?xsec_token=ok", "attempts": 2}),
        ]:
            db.execute(
                "INSERT INTO records VALUES('xhs',?,'failed','[]',?,?)",
                (sid, json.dumps(meta), now - 90),
            )
    server.schedule_xhs_retries(now)
    # net-backoff has attempts=2 -> backoff = 60*2 = 120s > 90s elapsed, so nothing queued yet;
    # tok-expired and net-capped are both excluded permanently!
    assert server.store.page("tasks", platform="xhs", state="queued")["total"] == 0
    server.schedule_xhs_retries(now + 40)
    tasks = server.store.page("tasks", platform="xhs", state="queued")["items"]
    assert len(tasks) == 1 and tasks[0]["payload"]["urls"] == [
        "https://www.xiaohongshu.com/explore/333333333333333333333333?xsec_token=ok"
    ]


def test_browser_push_report_accepts_xhs_and_aliases(client):
    token = client.get("/api/browser-push/token").json()["token"]
    headers = {"X-NAS-Submit-Token": token, "Origin": "https://www.xiaohongshu.com"}
    for reported_platform in ("xhs", "xiaohongshu"):
        resp = client.post(
            "/api/browser-push/report",
            headers=headers,
            json={
                "platform": reported_platform,
                "source_mode": "workbench",
                "account": "u1",
                "status": "ok",
                "discovered": 5,
                "message": f"test-{reported_platform}",
            },
        )
        assert resp.status_code == 200 and resp.json()["ok"] is True
    pushes = client.get("/api/browser-push/status?platform=xhs").json()["history"]["items"]
    assert len(pushes) == 2 and {p["message"] for p in pushes} == {"test-xhs", "test-xiaohongshu"}


def test_bg_run_receipt_endpoint_requires_token_and_returns_reported_status(client):
    token = client.get("/api/browser-push/token").json()["token"]
    assert client.get("/api/browser-push/runs/x_run_1").status_code == 401
    assert client.get("/api/browser-push/runs/x_run_1", headers={"X-NAS-Submit-Token": "wrong"}).status_code == 401

    auth = {"X-NAS-Submit-Token": token}
    assert client.get("/api/browser-push/runs/x_run_1", headers=auth).json() == {"found": False, "run": None}

    client.post(
        "/api/browser-push/report",
        headers={**auth, "Origin": "https://x.com"},
        json={
            "platform": "x",
            "source_mode": "bg",
            "run_id": "x_run_1",
            "account": "CCCW",
            "status": "empty",
            "discovered": 20,
            "message": "命中锚点",
        },
    )
    res = client.get("/api/browser-push/runs/x_run_1", headers=auth).json()
    assert res["found"] is True
    assert res["run"]["status"] == "ok" and res["run"]["reason"] == "no_new_items"

    url = "https://www.xiaohongshu.com/explore/abcdefabcdefabcdefabcdef?xsec_token=tok2"
    client.post(
        "/api/xhs/links",
        headers={**auth, "Origin": "https://www.xiaohongshu.com"},
        json={"urls": [url], "category": "saved", "run_id": "xhs_run_2", "source_mode": "bg"},
    )
    res_xhs = client.get("/api/browser-push/runs/xhs_run_2", headers=auth).json()
    assert res_xhs["found"] is True and res_xhs["run"]["category"] == "saved" and res_xhs["run"]["status"] == "ok"


def test_x_reconcile_and_baseline_api(client, tmp_path):
    assert client.post("/api/x/reconcile", json={"ids": ["1001"]}).status_code == 401
    assert client.post("/api/x/baseline", json={"ids": ["1001"]}).status_code == 401

    token = client.get("/api/browser-push/token").json()["token"]
    bad_origin = {"X-NAS-Submit-Token": token, "Origin": "https://evil.example"}
    assert client.post("/api/x/reconcile", headers=bad_origin, json={"ids": ["1001"]}).status_code == 403
    assert client.post("/api/x/baseline", headers=bad_origin, json={"ids": ["1001"]}).status_code == 403

    headers = {"X-NAS-Submit-Token": token, "Origin": "https://x.com"}
    assert client.post("/api/x/reconcile", headers=headers, json={"ids": []}).status_code == 422
    assert client.post("/api/x/reconcile", headers=headers, json={"ids": ["not-id"]}).status_code == 422
    assert client.post("/api/x/baseline", headers=headers, json={"ids": ["bad!"]}).status_code == 422

    # Populate diverse X states across baseline, records, and active direct tasks.
    base_res = client.post(
        "/api/x/baseline",
        headers=headers,
        json={"ids": ["1001", "1001", "1002"], "account": "alice", "source_mode": "workbench"},
    )
    assert base_res.status_code == 200
    assert base_res.json()["added"] == 2 and base_res.json()["baseline_total"] == 2

    # Second import is idempotent.
    base_res2 = client.post("/api/x/baseline", headers=headers, json={"ids": ["1001", "1003"]}).json()
    assert base_res2["added"] == 1 and base_res2["existing"] == 1 and base_res2["baseline_total"] == 3

    existing_file = tmp_path / "ok.jpg"
    existing_file.write_bytes(b"img")
    server.record({"platform": "x", "source_id": "2001", "state": "complete", "files": [str(existing_file)], "metadata": {}})
    server.record({"platform": "x", "source_id": "2002", "state": "processing", "files": [], "metadata": {}})
    server.record(
        {
            "platform": "x",
            "source_id": "2003",
            "state": "failed",
            "files": [],
            "metadata": {"retry_exhausted": True, "error": "terminal"},
        }
    )
    server.record(
        {
            "platform": "x",
            "source_id": "2004",
            "state": "failed",
            "files": [],
            "metadata": {"source_mode": "direct", "attempts": 1},
        }
    )

    # Active direct task item -> pending; baseline item 1001 -> skipped by /api/x/direct.
    direct_resp = client.post(
        "/api/x/direct",
        headers=headers,
        json={
            "batch_id": "x-rec-1",
            "items": [
                {
                    "tweet_id": "1001",
                    "author": "alice",
                    "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg"}],
                },
                {
                    "tweet_id": "2005",
                    "author": "bob",
                    "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/b.jpg"}],
                },
            ],
        },
    ).json()
    assert direct_resp["skipped"] == 1 and direct_resp["accepted"] == 1

    rec = client.post(
        "/api/x/reconcile",
        headers=headers,
        json={"ids": ["1001", "2001", "2002", "2003", "2004", "2005", "9999"]},
    ).json()["states"]
    assert rec == {
        "1001": "archived",
        "2001": "archived",
        "2002": "pending",
        "2003": "deferred",
        "2004": "missing",
        "2005": "pending",
        "9999": "missing",
    }

    # redownload_missing_files=True treats missing archived file on disk as missing.
    server.record(
        {
            "platform": "x",
            "source_id": "2006",
            "state": "complete",
            "files": [str(tmp_path / "gone.jpg")],
            "metadata": {},
        }
    )
    write_json(server.CONFIG_PATHS["x"], {"redownload_missing_files": True})
    rec2 = client.post("/api/x/reconcile", headers=headers, json={"ids": ["2001", "2006"]}).json()["states"]
    assert rec2 == {"2001": "archived", "2006": "missing"}
    assert client.get("/api/browser-push/status").json()["x_baseline_count"] == 3


def test_import_legacy_keeps_x_schedule_disabled(client, tmp_path, monkeypatch):
    from core import config, migrate

    for key in ("x", "pixiv", "douyin", "xhs"):
        monkeypatch.setitem(config.CONFIG_PATHS, key, tmp_path / key / "config.json")
    write_json(config.CONFIG_PATHS["x"], {"database": str(tmp_path / "missing_x.sqlite3"), "run_interval_hours": 6})
    write_json(config.CONFIG_PATHS["xhs"], {"database": str(tmp_path / "missing_xhs.sqlite3"), "run_interval_seconds": 1800})
    migrate.import_legacy(server.store)
    prefs = server.settings()
    assert prefs["schedule"]["x"]["enabled"] is False
    assert prefs["schedule"]["xhs"] == {"enabled": True, "hours": 0.5}


def test_x_retry_direct_only_skips_legacy_records_without_browser(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    monkeypatch.setattr(server, "store", store)
    x_cfg_path = tmp_path / "x.json"
    write_json(
        x_cfg_path,
        {
            "database": str(tmp_path / "x_auto.sqlite3"),
            "cookie_file": str(tmp_path / "missing_cookies.txt"),
            "download_dir": str(tmp_path / "x_downloads"),
            "retry_failed": True,
            "max_download_attempts": MAX_ATTEMPTS,
            "request_delay_seconds": 0,
            "media": {"convert_gif": False},
        },
    )
    monkeypatch.setitem(jobs.CONFIG_PATHS, "x", x_cfg_path)

    server.record(
        {
            "platform": "x",
            "source_id": "5001",
            "state": "failed",
            "files": [],
            "metadata": {"url": "https://x.com/legacy/status/5001", "error": "old"},
        }
    )
    server.record(
        {
            "platform": "x",
            "source_id": "5002",
            "state": "failed",
            "files": [],
            "metadata": {
                "url": "https://x.com/direct_u/status/5002",
                "author": "direct_u",
                "source_mode": "direct",
                "direct_media": [{"media_key": "m1", "type": "photo", "url": "https://pbs.twimg.com/media/d.jpg"}],
            },
        }
    )

    class LocalClient:
        def request(self, path, payload=None):
            if path.startswith("/internal/record/x/"):
                return server.get_record("x", path.rsplit("/", 1)[-1])
            if path.startswith("/internal/failed/"):
                return server.failed_records("x")
            if path.startswith("/internal/checkpoint/"):
                return {"head": ""}
            raise AssertionError(path)

    monkeypatch.setattr(jobs, "Client", LocalClient)
    monkeypatch.setattr(jobs, "prepare", lambda *a: (tmp_path / "unused", False))
    monkeypatch.setattr(
        jobs,
        "record",
        lambda platform, identifier, state, files, metadata: server.record(
            {"platform": platform, "source_id": identifier, "state": state, "files": files, "metadata": metadata}
        ),
    )

    class ForbidBrowser:
        def __init__(self, *args, **kwargs):
            raise AssertionError("direct_only retry must never instantiate BrowserCollector!")

    monkeypatch.setattr(x, "BrowserCollector", ForbidBrowser)

    def ok_download(url, media_type, target_dir, stem, **kwargs):
        out = target_dir / f"{stem}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ok")
        return out

    with patch("core.x_direct.download_cdn_file", side_effect=ok_download):
        stats = jobs.x_job({"id": "job-direct-only", "kind": "retry", "payload": {"direct_only": True}})
    assert stats["skipped"] == 1 and stats["downloaded"] == 1 and stats["failed"] == 0
    assert server.get_record("x", "5001")["state"] == "failed"
    assert server.get_record("x", "5002")["state"] == "complete"


def test_import_x_baseline_cli_tool(tmp_path):
    import sqlite3
    from tools import import_x_baseline as tool

    assert tool.extract_tweet_id_from_filename("1888123456789012345_alice_1.jpg") == "1888123456789012345"
    assert tool.extract_tweet_id_from_filename("alice_1888123456789012346.mp4") == "1888123456789012346"
    assert tool.extract_tweet_id_from_filename("random_note.txt") == ""

    img_dir = tmp_path / "images"
    meta_dir = tmp_path / "_metadata"
    img_dir.mkdir()
    meta_dir.mkdir()
    (img_dir / "1888123456789012345_alice_1.jpg").write_bytes(b"jpg")
    (img_dir / "1888123456789012345_alice.mp4.part").write_bytes(b"partial")
    (meta_dir / "sidecar.json").write_text(json.dumps({"tweet_id": "1888123456789012347"}), encoding="utf-8")

    legacy_db = tmp_path / "x_auto.sqlite3"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE tweets(tweet_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO tweets VALUES('1888123456789012348', 'done')")
        conn.execute("INSERT INTO tweets VALUES('1888123456789012349', 'failed')")

    core_db = tmp_path / "state.sqlite3"
    assert tool.main(["--db", str(core_db), "--dirs", str(img_dir), str(meta_dir), "--legacy-db", str(legacy_db), "--dry-run"]) == 0
    assert not core_db.exists()

    # Default mode scans media files + legacy DB only, ignoring JSON-only metadata files.
    assert tool.main(["--db", str(core_db), "--dirs", str(img_dir), str(meta_dir), "--legacy-db", str(legacy_db)]) == 0
    s = Store(core_db)
    assert s.x_baseline_count() == 2
    assert s.x_download_states(["1888123456789012345", "1888123456789012347", "1888123456789012348", "1888123456789012349"]) == {
        "1888123456789012345": "archived",
        "1888123456789012347": "missing",
        "1888123456789012348": "archived",
        "1888123456789012349": "missing",
    }

    # Explicit --include-metadata picks up JSON metadata sidecars.
    assert tool.main(["--db", str(core_db), "--dirs", str(img_dir), str(meta_dir), "--legacy-db", str(legacy_db), "--include-metadata"]) == 0
    assert s.x_baseline_count() == 3
    states = s.x_download_states(["1888123456789012345", "1888123456789012347", "1888123456789012348", "1888123456789012349"])
    assert states == {
        "1888123456789012345": "archived",
        "1888123456789012347": "archived",
        "1888123456789012348": "archived",
        "1888123456789012349": "missing",
    }


def test_x_direct_skips_queued_tasks_allows_deferred_and_summarizes_multibatch_bg_run(client):
    token = client.get("/api/browser-push/token").json()["token"]
    x_headers = {"X-NAS-Submit-Token": token, "Origin": "https://x.com"}
    xhs_headers = {"X-NAS-Submit-Token": token, "Origin": "https://www.xiaohongshu.com"}

    # Queued direct item is skipped on duplicate direct submission; deferred record can still be manually re-pushed.
    server.record(
        {
            "platform": "x",
            "source_id": "7002",
            "state": "failed",
            "files": [],
            "metadata": {"retry_exhausted": True, "direct_unresolvable": True},
        }
    )
    first = client.post(
        "/api/x/direct",
        headers=x_headers,
        json={
            "batch_id": "dup-check-1",
            "items": [{"tweet_id": "7001", "author": "a", "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/1.jpg"}]}],
        },
    ).json()
    assert first["accepted"] == 1

    second = client.post(
        "/api/x/direct",
        headers=x_headers,
        json={
            "batch_id": "dup-check-2",
            "source_mode": "single",
            "items": [
                {"tweet_id": "7001", "author": "a", "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/1.jpg"}]},
                {"tweet_id": "7002", "author": "b", "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/2.jpg"}]},
            ],
        },
    ).json()
    assert second["skipped"] == 1 and second["accepted"] == 1

    # Multi-batch summary report preserves total item_count without double-counting today_accepted.
    client.post(
        "/api/xhs/links",
        headers=xhs_headers,
        json={
            "urls": [f"https://www.xiaohongshu.com/explore/{i:024x}?xsec_token=t" for i in range(1, 51)],
            "category": "saved",
            "source_mode": "bg",
            "batch_id": "xhs-multi-1",
        },
    )
    client.post(
        "/api/xhs/links",
        headers=xhs_headers,
        json={
            "urls": [f"https://www.xiaohongshu.com/explore/{51:024x}?xsec_token=t"],
            "category": "saved",
            "source_mode": "bg",
            "batch_id": "xhs-multi-2",
        },
    )
    rep = client.post(
        "/api/browser-push/report",
        headers=xhs_headers,
        json={
            "platform": "xhs",
            "source_mode": "bg",
            "run_id": "xhs_multi_run",
            "account": "u1:saved",
            "category": "saved",
            "status": "ok",
            "discovered": 51,
            "item_count": 51,
            "submitted": 51,
            "accepted": 51,
            "skipped": 0,
            "task_id": "t-last",
            "message": "流式汇总",
        },
    )
    assert rep.status_code == 200
    run = client.get("/api/browser-push/runs/xhs_multi_run", headers=xhs_headers).json()["run"]
    assert run["itemCount"] == 51 and run["category"] == "saved" and run["taskId"] == "t-last"
    summary = client.get("/api/browser-push/status?platform=xhs").json()["summary"]["xhs"]
    assert summary["today_accepted"] == 51
