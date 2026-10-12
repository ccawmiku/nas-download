import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import DEFAULTS, read_json
from core.instagram import validate_cookies, validate_settings
from workers import instagram_job as worker


def post(identifier, children=None):
    value = {"pk": str(identifier), "code": "ABC", "caption": None,
             "user": {"pk": "99", "username": "tester"}, "taken_at": 1700000000,
             "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/photo.jpg", "width": 10, "height": 10}]}}
    if children:
        value["carousel_media"] = [post(i) for i in children]
        value["carousel_media_count"] = len(children)
    return value


class Catalog:
    def __init__(self):
        self.records = {}
        self.checkpoints = {}

    def request(self, path, payload=None):
        if path == "/internal/records/lookup":
            return {"records": {i: copy.deepcopy(self.records[i]) for i in payload["source_ids"] if i in self.records}}
        if path.startswith("/internal/checkpoint/"):
            if payload is not None:
                self.checkpoints[path] = payload["head"]
            return {"head": self.checkpoints.get(path, "")}
        if path.startswith("/internal/failed/"):
            return {"items": [copy.deepcopy(v) for v in self.records.values() if v["state"] == "failed"]}
        raise AssertionError(path)

    def record(self, platform, identifier, state, files, metadata):
        self.records[identifier] = copy.deepcopy({"source_id": identifier, "state": state, "files": files, "metadata": metadata})


class Source:
    account = "42"
    media = worker.GallerySource.media

    def __init__(self, pages):
        from gallery_dl import extractor
        self.extractor = extractor.find("https://www.instagram.com/id:42/saved/all-posts/")
        self.extractor.initialize()
        self.extractor.videos_dash = False
        self.values = pages
        self.fetched = 0
        self.downloads = []
        self.fail = set()

    def pages(self):
        for value in self.values:
            self.fetched += 1
            if isinstance(value, Exception):
                raise value
            yield value

    def refresh(self, identifier):
        return post(identifier)

    def download(self, item, target, journal):
        self.downloads.append(item["id"])
        if item["id"] in self.fail:
            raise OSError("mock failure")
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = item["id"].encode()
        entry = {"source": str(target), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        journal(entry)
        target.write_bytes(data)
        return entry


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = {"image_dir": str(tmp_path / "images"), "video_dir": str(tmp_path / "videos"),
                "state_dir": str(tmp_path / "state"), "known_stop_consecutive": 2,
                "sleep_download_min": 1, "sleep_download_max": 1, "max_download_attempts": 3}
    catalog = Catalog()
    monkeypatch.setattr(worker, "record", catalog.record)
    monkeypatch.setattr(worker, "prepare", lambda platform, root, identifier: (root, False))
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    return settings, catalog


def runner(setup, source):
    settings, catalog = setup
    return worker.SavedJob({"id": "task", "kind": "sync"}, settings, source, catalog)


def seed(setup, identifier):
    source = Source([])
    runner(setup, source).process(post(identifier))


def test_defaults_and_credentials():
    assert DEFAULTS["schedule"]["instagram"] == {"enabled": False, "hours": 72}
    assert validate_cookies("# Netscape HTTP Cookie File\n#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n.instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t42") == "42"
    for bad in ("sessionid=secret", "wrong\tformat", ""):
        with pytest.raises(ValueError) as error:
            validate_cookies(bad)
        assert "secret" not in str(error.value)
    for bad in ({"sleep_request_min": 0}, {"sleep_download_max": float("inf")}, {"max_download_attempts": True}, {"retry_failed": "yes"}):
        with pytest.raises(ValueError):
            validate_settings(bad)


def test_cross_page_stopping_ignores_duplicates_and_never_fetches_next(setup):
    seed(setup, "3")
    seed(setup, "2")
    source = Source([([post("4"), post("3")], True), ([post("3"), post("2")], True), RuntimeError("must not fetch")])
    result = runner(setup, source).sync()
    assert result["stop_reason"] == "known_limit"
    assert result["known_consecutive"] == 2
    assert source.fetched == 2 and source.downloads == ["4"]
    assert list(setup[1].checkpoints.values()) == ["4"]


def test_checkpoint_only_stops_when_post_is_complete(setup):
    seed(setup, "3")
    catalog = setup[1]
    catalog.checkpoints["/internal/checkpoint/instagram/saved-42"] = "3"
    Path(catalog.records["3"]["files"][0]).unlink()
    source = Source([([post("3"), post("2")], False)])
    result = runner(setup, source).sync()
    assert result["stop_reason"] == "end" and source.downloads == ["3", "2"]


def test_partial_carousel_resumes_only_missing_and_blocks_checkpoint(setup):
    source = Source([([post("5", ["51", "52"])], False)])
    source.fail.add("52")
    result = runner(setup, source).sync()
    assert result["failed"] == 1 and not result["checkpoint_updated"]
    value = setup[1].records["5"]
    assert len(value["files"]) == 1 and value["metadata"]["download_incomplete"]
    source.fail.clear()
    source.downloads.clear()
    result = runner(setup, source).sync()
    assert source.downloads == ["52"] and result["checkpoint_updated"]
    assert setup[1].records["5"]["state"] == "complete"


def test_incomplete_carousel_and_corrupt_output_are_not_known(setup):
    seed(setup, "3")
    value = setup[1].records["3"]
    Path(value["files"][0]).write_bytes(b"x")  # Same size, different digest.
    assert not worker.complete(value, ["3"])
    bad = post("5", ["51", "52"])
    bad["carousel_media"][1]["image_versions2"] = {"candidates": []}
    result = runner(setup, Source([([bad], False)])).sync()
    assert result["failed"] == 1 and not result["checkpoint_updated"]
    assert setup[1].records["5"]["metadata"]["download_incomplete"]


def test_mixed_carousel_uses_separate_roots(setup):
    item = post("5", ["51", "52"])
    item["carousel_media"][1]["video_versions"] = [{"url": "https://video.fbcdn.net/video.mp4"}]
    result = runner(setup, Source([([item], False)])).sync()
    files = setup[1].records["5"]["files"]
    assert [Path(f).parent for f in files] == [Path(setup[0]["image_dir"]), Path(setup[0]["video_dir"])]
    assert result["downloaded"] == 1


def test_pagination_failure_preserves_old_checkpoint(setup):
    path = "/internal/checkpoint/instagram/saved-42"
    setup[1].checkpoints[path] = "old"
    with pytest.raises(worker.SourceError):
        runner(setup, Source([([post("4")], True), worker.SourceError("pagination")])).sync()
    assert setup[1].checkpoints[path] == "old"
    assert setup[1].records["4"]["state"] == "complete"


@pytest.mark.parametrize("response", [
    {"items": []}, {"items": [], "more_available": 1},
    {"items": [], "more_available": True, "next_max_id": "next"},
    {"items": [{"media": post("3")}], "more_available": True},
    {"items": [{}], "more_available": False},
])
def test_strict_upstream_pagination(response):
    source = object.__new__(worker.GallerySource)
    source.call = lambda *args, **kwargs: response
    with pytest.raises(worker.SourceError):
        list(source.pages())


def test_repeated_cursor_fails_before_advancing():
    source = object.__new__(worker.GallerySource)
    source.call = lambda *args, **kwargs: {"items": [{"media": post("3")}], "more_available": True, "next_max_id": "next"}
    iterator = source.pages()
    assert next(iterator)[1]
    with pytest.raises(worker.SourceError):
        next(iterator)


def test_explicit_empty_last_page():
    source = object.__new__(worker.GallerySource)
    source.call = lambda *args, **kwargs: {"items": [], "more_available": False}
    assert list(source.pages()) == [([], False)]


def test_auth_risk_is_fatal_and_not_retried(setup):
    source = Source([([post("5", ["51", "52"])], False)])
    download = source.download
    def reject(item, target, journal):
        if item["id"] == "52":
            raise worker.SourceError("limited")
        return download(item, target, journal)
    source.download = reject
    with pytest.raises(worker.SourceError):
        runner(setup, source).sync()
    assert not setup[1].checkpoints and len(setup[1].records["5"]["files"]) == 1


def test_attempt_limit_and_account_isolation(setup):
    catalog = setup[1]
    catalog.record("instagram", "3", "failed", [], {"account_id": "42", "attempts": 3})
    catalog.record("instagram", "2", "failed", [], {"account_id": "other", "attempts": 1})
    source = Source([])
    job = runner(setup, source)
    job.retry()
    assert not source.downloads
    job.retry(manual=True)
    assert source.downloads == ["3"]
    assert catalog.records["2"]["state"] == "failed"


def test_retried_download_is_not_a_known_stopping_boundary(setup):
    setup[0]["known_stop_consecutive"] = 1
    setup[1].record("instagram", "3", "failed", [], {"account_id": "42", "attempts": 1})
    source = Source([([post("3"), post("2")], False)])
    job = runner(setup, source)
    job.retry()
    result = job.sync()
    assert source.downloads == ["3", "2"]
    assert result["stop_reason"] == "end" and result["checkpoint_updated"]


def test_single_record_retry_selects_only_saved_failure(setup):
    for identifier in ("3", "2"):
        setup[1].record("instagram", identifier, "failed", [], {"account_id": "42", "url": "url-" + identifier})
    source = Source([])
    runner(setup, source).retry(manual=True, urls=["url-3", "https://arbitrary.invalid"])
    assert source.downloads == ["3"]


def test_disabled_retry_does_not_retry_failed_feed_items(setup):
    setup[0]["retry_failed"] = False
    setup[1].record("instagram", "3", "failed", [], {"account_id": "42", "attempts": 1})
    source = Source([([post("3")], False)])
    result = runner(setup, source).sync()
    assert not source.downloads and result["failed"] == 1 and not result["checkpoint_updated"]


def test_processing_is_not_known_and_does_not_redownload(setup):
    seed(setup, "3")
    setup[1].records["3"]["state"] = "processing"
    source = Source([([post("3")], False)])
    result = runner(setup, source).sync()
    assert not source.downloads and not result["checkpoint_updated"]
    assert result["known_consecutive"] == 0


def test_staged_partial_resume_redelivers_original_without_redownload(setup, tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    monkeypatch.setattr(worker, "prepare", lambda *args: (stage, True))
    delivered = []
    monkeypatch.setattr(worker, "submit", lambda platform, source, target, details: delivered.append((str(source), str(target))))
    source = Source([([post("5", ["51", "52"])], False)])
    source.fail.add("52")
    runner(setup, source).sync()
    assert len(delivered) == 1 and setup[1].records["5"]["state"] == "failed"
    source.fail.clear()
    source.downloads.clear()
    result = runner(setup, source).sync()
    assert source.downloads == ["52"] and result["processing"] == 1
    assert not result["checkpoint_updated"]
    assert setup[1].records["5"]["state"] == "processing"
    assert not setup[1].records["5"]["metadata"]["download_incomplete"]
    assert delivered[0] == delivered[1]  # Same source yields idempotent intake.


@pytest.mark.parametrize("data", [{"status": "fail", "message": "secret"}, {"status": "ok", "challenge": {"url": "secret"}}])
def test_api_risk_body_is_redacted_and_stops(data):
    source = object.__new__(worker.GallerySource)
    source.extractor = SimpleNamespace(api=SimpleNamespace(_call=lambda *args, **kwargs: data))
    with pytest.raises(worker.SourceError) as error:
        source.call("/v1/feed/saved/posts/")
    assert "secret" not in str(error.value)


def test_crash_after_publishing_uses_journal_on_resume(setup):
    source = Source([])
    download = source.download
    def crash(item, target, journal):
        download(item, target, journal)
        raise KeyboardInterrupt
    source.download = crash
    with pytest.raises(KeyboardInterrupt):
        runner(setup, source).process(post("3"))
    assert read_json(Path(setup[0]["state_dir"]) / "posts/3.json")["media"]["3"]
    source.download = download
    source.downloads.clear()
    runner(setup, source).process(post("3"), setup[1].records["3"])
    assert not source.downloads and setup[1].records["3"]["state"] == "complete"


def test_real_download_rejects_collision_and_html(tmp_path):
    import io
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="JPEG")
    data = output.getvalue()
    class Response:
        status_code = 200
        headers = {"Content-Type": "image/jpeg", "Content-Length": str(len(data))}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): return iter([data])
    response = Response()
    source = object.__new__(worker.GallerySource)
    source.session = SimpleNamespace(get=lambda *args, **kwargs: response)
    target = tmp_path / "photo.jpg"
    journal = []
    item = {"url": "https://scontent.cdninstagram.com/photo.jpg", "kind": "images"}
    source.download(item, target, journal.append)
    assert worker.owned(journal[0])
    with pytest.raises(FileExistsError):
        source.download(item, target, journal.append)
    assert target.read_bytes() == data and len(journal) == 1
    target.unlink()
    response.headers = {"Content-Type": "text/html"}
    with pytest.raises(ValueError):
        source.download(item, target, journal.append)
    assert not target.exists() and not list(tmp_path.glob("*.part"))


def test_cookie_session_initializes_pinned_extractor_offline(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tfake\n.instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t42\n")
    source = worker.GallerySource({"cookie_file": str(path)})
    assert source.account == "42"
    assert source.extractor._retries == 0
    assert not list(source.session.cookies)
