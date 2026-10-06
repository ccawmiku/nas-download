import importlib
import sys
from unittest.mock import AsyncMock
import os

from fastapi.testclient import TestClient
from PIL import Image

from app.history import DownloadRecord


def load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("IMAGE_DOWNLOAD_DIR", str(tmp_path / "downloads" / "images"))
    monkeypatch.setenv("VIDEO_DOWNLOAD_DIR", str(tmp_path / "downloads" / "videos"))
    monkeypatch.setenv("FILE_DOWNLOAD_DIR", str(tmp_path / "downloads" / "files"))
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "bootstrap-test-token")
    monkeypatch.setenv("AUTO_START_BOT", "false")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def test_file_listing_keeps_newest_200_across_categories(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    dirs = main.configured_download_dirs(main.settings_store.settings)
    for category, directory in dirs.items():
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(110):
            path = directory / f"测试 {category}-{index}.bin"
            path.write_bytes(b"data")
            os.utime(path, (1000 + index, 1000 + index))
        (directory / "empty").touch()
        (directory / ".partial").write_bytes(b"hidden")
        (directory / "subdirectory").mkdir()
    reference = []
    for category, directory in dirs.items():
        for path in directory.iterdir():
            if path.is_file() and not path.name.startswith(".") and path.stat().st_size:
                stat = path.stat()
                reference.append({
                    "category": category, "name": path.name, "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                    **main._media_urls(category, path.name, stat.st_mtime_ns),
                })
    reference.sort(key=lambda row: row["modified_at"], reverse=True)
    original_urls = main._media_urls
    calls = []

    def counted_urls(*args):
        calls.append(args)
        return original_urls(*args)

    monkeypatch.setattr(main, "_media_urls", counted_urls)
    assert main.list_downloaded_files(dirs) == reference[:200]
    assert len(calls) == 200


def login(client, main):
    response = client.post("/api/auth/login", json={"password": main.BOOTSTRAP_TOKEN})
    assert response.status_code == 200


def test_file_listing_cache_expires_and_does_not_expose_mutable_rows(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    dirs = main.configured_download_dirs(main.settings_store.settings)
    dirs['images'].mkdir(parents=True)
    (dirs['images'] / 'first.jpg').write_bytes(b'first')
    clock = [100.0]
    monkeypatch.setattr(main.time, 'monotonic', lambda: clock[0])
    original_scan = main._scan_downloaded_files
    scans = []
    def scan(paths):
        scans.append(paths)
        return original_scan(paths)
    monkeypatch.setattr(main, '_scan_downloaded_files', scan)
    first = main.list_downloaded_files(dirs)
    first[0]['name'] = 'caller mutation'
    (dirs['images'] / 'second.jpg').write_bytes(b'second')
    cached = main.list_downloaded_files(dirs)
    assert [row['name'] for row in cached] == ['first.jpg']
    assert len(scans) == 1
    clock[0] += main.FILE_LIST_CACHE_SECONDS
    assert {row['name'] for row in main.list_downloaded_files(dirs)} == {'first.jpg', 'second.jpg'}
    assert len(scans) == 2
    (dirs['images'] / 'second.jpg').unlink()
    clock[0] += main.FILE_LIST_CACHE_SECONDS
    assert [row['name'] for row in main.list_downloaded_files(dirs)] == ['first.jpg']


def test_file_listing_cache_reloads_when_configured_directory_changes(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    (old / 'old.jpg').write_bytes(b'old')
    (new / 'new.jpg').write_bytes(b'new')
    assert main.list_downloaded_files({'images': old})[0]['name'] == 'old.jpg'
    assert main.list_downloaded_files({'images': new})[0]['name'] == 'new.jpg'


def test_panel_requires_auth_and_bootstrap_login_sets_security_headers(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        unauthorized = client.get("/api/state")
        status = client.get("/api/auth/status")
        login(client, main)
        authorized = client.get("/api/state")

    assert unauthorized.status_code == 401
    assert status.json()["bootstrap_required"] is True
    assert authorized.status_code == 200
    assert authorized.json()["version"] == main.__version__
    assert authorized.headers["x-content-type-options"] == "nosniff"
    assert authorized.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in authorized.headers["content-security-policy"]


def test_control_panel_and_javascript_assets_are_served(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        page = client.get("/")
        script = client.get("/static/app.js")

    assert page.status_code == 200
    assert f'<script src="/static/app.js?v={main.__version__}"></script>' in page.text
    assert f"v{main.__version__}" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "自动重试次数" in page.text
    assert "重试全部失败" in page.text
    assert page.text.index("<th>预览</th>") < page.text.index("<th>状态</th>")
    assert "function renderDownloads" in script.text
    assert "function previewMarkup" in script.text


def test_login_is_rate_limited(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        for _ in range(main.LOGIN_MAX_ATTEMPTS):
            assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        limited = client.post("/api/auth/login", json={"password": "wrong"})

    assert limited.status_code == 429


def test_passwordless_panel_allows_existing_state_and_controls_but_protects_internal_api(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_AUTH_REQUIRED", "false")
    main = load_main(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        assert client.get("/api/auth/status").json() == {
            "password_enabled": False, "bootstrap_required": False, "authenticated": True,
        }
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/logs").status_code == 200
        assert client.post("/api/controls/pause").status_code == 200
        assert client.post("/api/controls/resume").status_code == 200
        assert client.get("/internal/status").status_code == 401
        assert not client.cookies.get(main.SESSION_COOKIE)


def test_settings_validation_rejects_relative_paths(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        login(client, main)
        response = client.post("/api/settings", json={"download_dir": "relative/path"})

    assert response.status_code == 422


def test_failed_bot_validation_rolls_back_settings(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    old_api_id = main.settings_store.settings.api_id
    main.bot_manager.start = AsyncMock(side_effect=RuntimeError("invalid Telegram credentials"))
    with TestClient(main.app) as client:
        login(client, main)
        response = client.post(
            "/api/settings",
            json={"api_id": 123, "api_hash": "hash", "bot_token": "token"},
        )

    assert response.status_code == 400
    assert "已回滚" in response.json()["detail"]
    assert main.settings_store.settings.api_id == old_api_id


def test_file_route_rejects_path_traversal(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    with TestClient(main.app) as client:
        login(client, main)
        response = client.get("/files/files/%2E%2E%2Fsecret.txt")

    assert response.status_code == 404


def test_image_previews_are_generated_and_exposed_in_both_lists(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    image_dir = main.settings_store.settings.image_download_dir
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / "测试 image.png"
    Image.new("RGB", (640, 360), "#0f766e").save(image_path)
    main.history.add(
        DownloadRecord(
            "preview-job",
            10,
            123,
            image_path.name,
            str(image_path),
            status="complete",
            progress=100,
            size_bytes=image_path.stat().st_size,
        )
    )

    with TestClient(main.app) as client:
        login(client, main)
        state = client.get("/api/state").json()
        file_preview_url = state["files"][0]["preview_url"]
        history_preview_url = state["downloads"][0]["preview_url"]
        preview = client.get(file_preview_url)

    assert file_preview_url == history_preview_url
    assert "%E6%B5%8B%E8%AF%95%20image.png" in file_preview_url
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    assert preview.content.startswith(b"\xff\xd8")
    assert list((main.settings_store.settings.config_dir / "previews").glob("*.jpg"))


def test_preview_route_rejects_path_traversal(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    outside = tmp_path / "secret.png"
    Image.new("RGB", (10, 10)).save(outside)
    with TestClient(main.app) as client:
        login(client, main)
        response = client.get("/previews/images/%2E%2E%2Fsecret.png")

    assert response.status_code == 404


def test_retry_all_failed_endpoint_queues_every_available_record(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    for index, status in enumerate(("failed", "interrupted", "cancelled"), start=1):
        main.history.add(DownloadRecord(f"failed-{index}", index, 123, f"{index}.bin", str(tmp_path / f"{index}.bin"), status=status))

    class ConnectedClient:
        def is_connected(self):
            return True

        async def disconnect(self):
            return None

    main.bot_manager.client = ConnectedClient()

    async def retry_all(_target, *, all_matches=False):
        assert all_matches is True
        for record in main.history.list_statuses(main.RETRYABLE_STATUSES, limit=None):
            main.history.update(record["id"], status="queued")
        return 3

    main.bot_manager.retry = AsyncMock(side_effect=retry_all)
    with TestClient(main.app) as client:
        login(client, main)
        response = client.post("/api/downloads/retry-failed", json={})

    assert response.status_code == 200
    assert response.json() == {"total": 3, "queued": 3, "remaining": 0}
    main.bot_manager.retry.assert_awaited_once_with("failed", all_matches=True)
