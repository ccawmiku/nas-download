from fastapi.testclient import TestClient

from test_api import load_main, login


def test_internal_summary_is_protected_and_does_not_expose_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-internal-token")
    main = load_main(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        assert client.get("/internal/status").status_code == 401
        response = client.get("/internal/status", headers={"X-NAS-Download-Token": "test-internal-token"})
        assert response.status_code == 200
        assert set(response.json()) == {"connected", "running", "next_run_at", "current_job", "counts"}


def test_prefix_login_static_assets_and_pause_controls(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_PATH", "/telegram")
    main = load_main(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        page = client.get("/telegram/")
        assert '/telegram/static/app.js' in page.text
        assert client.get("/telegram/static/app.js").status_code == 200
        assert client.post("/telegram/api/controls/pause").status_code == 401
        login(client, main)
        assert client.post("/telegram/api/controls/pause").json()["controls"]["paused"]
        assert not client.post("/telegram/api/controls/resume").json()["controls"]["paused"]
        assert client.get("/telegram/api/status").status_code == 200


def test_media_links_include_gateway_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_PATH", "/telegram")
    main = load_main(monkeypatch, tmp_path)
    links = main._media_urls("images", "image with space.jpg", 123)
    assert links["url"] == "/telegram/files/images/image%20with%20space.jpg"
    assert links["preview_url"].startswith("/telegram/previews/images/")
