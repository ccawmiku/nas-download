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
