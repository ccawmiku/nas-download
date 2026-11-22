import asyncio
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import app as server
from core.config import DEFAULTS, write_json
from core.store import Store
from workers import xhs_job

# Valid 1x1 PNG (zero external deps required)
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7+X2kAAAAASUVORK5CYII="
)
NOTE_A = "a" * 24
NOTE_B = "b" * 24
NOTE_C = "c" * 24


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


def test_record_thumb_caches_webp_rejects_video_and_blocks_traversal(client, tmp_path, monkeypatch):
    media_root = tmp_path / "downloads"
    media_dir = media_root / "xhs"
    media_dir.mkdir(parents=True)
    monkeypatch.setenv("NAS_MEDIA_ROOTS", str(media_root))
    img_path = media_dir / "sample.png"
    img_path.write_bytes(_TINY_PNG)
    vid_path = media_dir / "clip.mp4"
    vid_path.write_bytes(b"ftypmp42")

    outside = tmp_path / "outside.png"
    outside.write_bytes(_TINY_PNG)
    server.record(
        {
            "platform": "xhs",
            "source_id": NOTE_A,
            "state": "complete",
            "files": [str(img_path), str(vid_path)],
            "metadata": {"title": "图集测试"},
        }
    )
    server.record(
        {
            "platform": "xhs",
            "source_id": NOTE_B,
            "state": "complete",
            "files": [str(outside)],
            "metadata": {"title": "越权测试"},
        }
    )

    resp1 = client.get(f"/api/records/xhs/{NOTE_A}/thumb?index=0&w=320")
    assert resp1.status_code == 200
    assert resp1.content

    thumbs = list((tmp_path / "thumbs").glob("*.webp"))
    # If Pillow is available, WEBP cache file is created and reused on second hit
    if thumbs:
        assert resp1.headers["content-type"].startswith("image/webp")
        mtime_before = thumbs[0].stat().st_mtime_ns
        resp2 = client.get(f"/api/records/xhs/{NOTE_A}/thumb?index=0&w=320")
        assert resp2.status_code == 200
        assert thumbs[0].stat().st_mtime_ns == mtime_before

    vid_resp = client.get(f"/api/records/xhs/{NOTE_A}/thumb?index=1")
    assert vid_resp.status_code == 404

    bad_resp = client.get(f"/api/records/xhs/{NOTE_B}/thumb?index=0")
    assert bad_resp.status_code == 422


def test_enqueue_xhs_links_unlocks_deferred_when_token_refreshed(client):
    old_url = f"https://www.xiaohongshu.com/explore/{NOTE_A}?xsec_token=old_tok&xsec_source=pc_user"
    fresh_url = f"https://www.xiaohongshu.com/explore/{NOTE_A}?xsec_token=new_tok&xsec_source=pc_user"
    server.record(
        {
            "platform": "xhs",
            "source_id": NOTE_A,
            "state": "failed",
            "files": [],
            "metadata": {
                "url": old_url,
                "category": "liked",
                "retry_paused": True,
                "retry_exhausted": True,
                "needs_fresh_token": True,
                "error": "xsec_token 已失效",
            },
        }
    )

    # Identical stale link must still be skipped
    tid1, acc1 = server.store.enqueue_xhs_links(
        {"urls": [old_url], "destination": "batch", "category": "liked"}
    )
    assert tid1 == "" and acc1 == 0

    # Refreshed URL with updated xsec_token unlocks retry automatically
    tid2, acc2 = server.store.enqueue_xhs_links(
        {"urls": [fresh_url], "destination": "batch", "category": "liked"}
    )
    assert tid2 and acc2 == 1


def test_browser_errors_sync_and_dismiss_and_x_force_retry(client):
    token = client.get("/api/browser-push/token").json()["token"]

    server.record(
        {
            "platform": "xhs",
            "source_id": NOTE_A,
            "state": "failed",
            "files": [],
            "metadata": {
                "url": f"https://www.xiaohongshu.com/explore/{NOTE_A}",
                "title": "失败笔记",
                "category": "liked",
                "error": "上游未确认所有文件完成",
            },
        }
    )
    server.record(
        {
            "platform": "x",
            "source_id": "99001",
            "state": "failed",
            "files": [],
            "metadata": {
                "url": "https://x.com/alice/status/99001",
                "author": "alice",
                "retry_exhausted": True,
                "error": "抓取失败",
            },
        }
    )
    server.record(
        {
            "platform": "x",
            "source_id": "99002",
            "state": "failed",
            "files": [],
            "metadata": {
                "url": "https://x.com/bob/status/99002",
                "retry_exhausted": False,
                "error": "不应出现在浏览器错误清单",
            },
        }
    )

    assert client.get("/api/browser-push/errors?platform=xhs").status_code == 401
    assert client.get("/api/browser-push/errors?platform=other", headers={"X-NAS-Submit-Token": token}).status_code == 422

    r_xhs = client.get("/api/browser-push/errors?platform=xhs", headers={"X-NAS-Submit-Token": token})
    assert r_xhs.status_code == 200
    assert [it["source_id"] for it in r_xhs.json()["items"]] == [NOTE_A]

    r_x = client.get("/api/browser-push/errors?platform=x", headers={"X-NAS-Submit-Token": token})
    assert r_x.status_code == 200
    assert [it["source_id"] for it in r_x.json()["items"]] == ["99001"]

    # Forced error retry via /api/x/direct sets force=True in queued task payload
    x_force = client.post(
        "/api/x/direct",
        headers={"Origin": "https://x.com", "X-NAS-Submit-Token": token},
        json={
            "source_mode": "error_retry",
            "items": [
                {
                    "tweet_id": "99001",
                    "author": "alice",
                    "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg"}],
                }
            ],
        },
    )
    assert x_force.status_code == 200
    assert x_force.json()["accepted"] == 1
    with server.store.connect() as db:
        row = db.execute("SELECT payload FROM tasks WHERE id=?", (x_force.json()["task_id"],)).fetchone()
    assert json.loads(row["payload"]).get("force") is True

    # Dismiss via browser token requires allowed origin
    bad_origin = client.post(
        "/api/browser-push/errors/dismiss",
        headers={"Origin": "https://evil.example", "X-NAS-Submit-Token": token},
        json={"platform": "xhs", "source_id": NOTE_A},
    )
    assert bad_origin.status_code == 403

    ok_dismiss = client.post(
        "/api/browser-push/errors/dismiss",
        headers={"Origin": "https://www.xiaohongshu.com", "X-NAS-Submit-Token": token},
        json={"platform": "xhs", "source_id": NOTE_A},
    )
    assert ok_dismiss.status_code == 200
    after_xhs = client.get("/api/browser-push/errors?platform=xhs", headers={"X-NAS-Submit-Token": token}).json()
    assert after_xhs["items"] == []


def test_xhs_direct_api_validation_and_requeue_failed(client):
    token = client.get("/api/browser-push/token").json()["token"]
    headers = {"Origin": "https://www.xiaohongshu.com", "X-NAS-Submit-Token": token}

    server.record({"platform": "xhs", "source_id": NOTE_A, "state": "complete", "files": ["/xhs/ok.jpg"], "metadata": {}})
    server.record(
        {
            "platform": "xhs",
            "source_id": NOTE_B,
            "state": "failed",
            "files": [],
            "metadata": {"retry_paused": True, "retry_exhausted": True, "error": "expired"},
        }
    )

    bad_ssrf = client.post(
        "/api/xhs/direct",
        headers=headers,
        json={
            "items": [
                {
                    "note_id": NOTE_C,
                    "media": [{"type": "image", "url": "https://evil.example/p.jpg"}],
                }
            ]
        },
    )
    assert bad_ssrf.status_code == 422

    bad_ip = client.post(
        "/api/xhs/direct",
        headers=headers,
        json={
            "items": [
                {
                    "note_id": NOTE_C,
                    "media": [{"type": "image", "url": "https://127.0.0.1/p.jpg"}],
                }
            ]
        },
    )
    assert bad_ip.status_code == 422

    resp = client.post(
        "/api/xhs/direct",
        headers=headers,
        json={
            "batch_id": "xhs-direct-1",
            "category": "liked",
            "source_mode": "error_retry",
            "items": [
                {
                    "note_id": NOTE_A,
                    "title": "已存在",
                    "media": [{"type": "image", "url": "http://sns-webpic-qc.xhscdn.com/test1.jpg"}],
                },
                {
                    "note_id": NOTE_B,
                    "title": "失败待补",
                    "media": [{"type": "image", "url": "http://sns-webpic-qc.xhscdn.com/test2.jpg"}],
                },
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert body["skipped"] == 1
    assert body["accepted"] == 1
    assert body["task_id"]


def test_xhs_run_direct_worker_clears_failure_metadata(client, tmp_path, monkeypatch):
    monkeypatch.setitem(xhs_job.CONFIG_PATHS, "xhs", tmp_path / "xhs-config.json")
    write_json(tmp_path / "xhs-config.json", {"request_delay_seconds": 0})
    settings_path = tmp_path / "xhs-settings.json"
    archive_dir = tmp_path / "xhs_archive"
    write_json(settings_path, {"work_path": str(archive_dir), "folder_name": "Download"})
    monkeypatch.setenv("XHS_SETTINGS_PATH", str(settings_path))

    server.record(
        {
            "platform": "xhs",
            "source_id": NOTE_B,
            "state": "failed",
            "files": [],
            "metadata": {
                "url": f"https://www.xiaohongshu.com/explore/{NOTE_B}",
                "category": "liked",
                "retry_paused": True,
                "retry_exhausted": True,
                "needs_fresh_token": True,
                "error_dismissed": True,
                "error": "xsec_token 已失效",
            },
        }
    )

    class _FakeClient:
        def request(self, path, payload=None):
            if path.startswith("/internal/record/xhs/"):
                sid = path.rsplit("/", 1)[-1]
                return server.get_record("xhs", sid)
            return {}

    recorded = []

    def _fake_download(entry, dest_dir, **_kwargs):
        out_dir = Path(dest_dir) / f"{entry['note_id']}_ok"
        out_dir.mkdir(parents=True, exist_ok=True)
        img = out_dir / f"{entry['note_id']}_01.png"
        img.write_bytes(_TINY_PNG)
        return "done", [str(img)], ""

    monkeypatch.setattr(xhs_job, "Client", _FakeClient)
    monkeypatch.setattr(xhs_job, "prepare", lambda _plat, _inc, _jid: (archive_dir / "incoming", False))
    monkeypatch.setattr(xhs_job, "download_xhs_direct_item", _fake_download)
    monkeypatch.setattr(xhs_job, "record", lambda *args: (recorded.append(args), server.record({
        "platform": args[0],
        "source_id": args[1],
        "state": args[2],
        "files": args[3],
        "metadata": args[4],
    })))

    job = {
        "id": "job_xhs_direct_1",
        "platform": "xhs",
        "kind": "direct",
        "payload": {
            "destination": "batch",
            "items": [
                {
                    "note_id": NOTE_B,
                    "url": f"https://www.xiaohongshu.com/explore/{NOTE_B}",
                    "title": "成功恢复的作品",
                    "author": "alice",
                    "media": [{"type": "image", "url": "https://sns-webpic-qc.xhscdn.com/a.jpg"}],
                }
            ],
        },
    }
    stats = asyncio.run(xhs_job.run(job))
    assert stats["downloaded"] == 1
    assert stats["failed"] == 0

    updated = server.get_record("xhs", NOTE_B)
    assert updated["state"] == "complete"
    assert updated["metadata"]["source_mode"] == "direct"
    assert updated["metadata"]["category"] == "liked"
    assert "error" not in updated["metadata"]
    assert "retry_paused" not in updated["metadata"]
    assert "retry_exhausted" not in updated["metadata"]
    assert "needs_fresh_token" not in updated["metadata"]
    assert len(updated["files"]) == 1
    assert Path(updated["files"][0]).is_file()
