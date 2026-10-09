import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from workers import platform_job as jobs
from workers.stopping import KnownRun, completed


class Catalog:
    def __init__(self, records=None):
        self.records = records or {}
        self.checkpoints = {}
        self.writes = []

    def request(self, path, payload=None):
        if path == "/internal/records/lookup":
            return {
                "records": {
                    i: self.records[i]
                    for i in payload["source_ids"]
                    if i in self.records
                }
            }
        if path.startswith("/internal/checkpoint/"):
            if payload:
                self.checkpoints[path] = payload["head"]
                self.writes.append((path, payload["head"]))
            return {"head": self.checkpoints.get(path, "")}
        raise AssertionError(path)


def done():
    return {"state": "complete", "files": ["/archive/already.webp"], "metadata": {}}


def store():
    return SimpleNamespace(
        get_tweet=lambda i: None,
        get_artwork=lambda i: None,
        upsert_seen=lambda i: None,
        mark_result=lambda *a, **k: None,
        should_download=lambda *a, **k: True,
    )


def test_unique_known_streak_and_non_downloads():
    guard = KnownRun(2)
    assert not guard.observe("a", True)
    assert not guard.observe("a", True)
    assert guard.consecutive == 1
    assert not guard.observe("b", False)
    assert guard.consecutive == 0
    assert not guard.observe("c", True)
    assert guard.observe("d", True)
    assert not completed({"state": "processing", "files": ["x"]})
    assert not completed({"state": "complete", "files": []})
    assert not completed({**done(), "metadata": {"legacy_status": "skipped"}})


def x_module(batches, events):
    class Page:
        mouse = SimpleNamespace(
            wheel=AsyncMock(side_effect=lambda *a: events.append("scroll"))
        )

        def on(self, *a):
            pass

        async def goto(self, *a, **k):
            pass

        async def wait_for_timeout(self, *a):
            pass

    class Collector:
        def __init__(self, *a):
            self.index = 0

        def _load_cookies(self):
            return [], "user"

        @asynccontextmanager
        async def _browser_page(self, *a):
            yield Page()

        async def _collect_visible(self, *a):
            rows = batches[min(self.index, len(batches) - 1)]
            self.index += 1
            return [
                {"tweet_id": i, "url": "https://x.com/user/status/" + i} for i in rows
            ]

    return SimpleNamespace(BrowserCollector=Collector, tweet_id_from_url=lambda u: "")


def test_x_first_run_processes_before_scrolling_and_counts_each_id_once(monkeypatch):
    catalog = Catalog({"a": done(), "b": done(), "c": done()})
    monkeypatch.setattr(
        jobs,
        "lookup",
        lambda p, ids: catalog.request("/internal/records/lookup", {"source_ids": ids})[
            "records"
        ],
    )
    events = []
    mod = x_module([["new", "a"], ["a", "b", "c", "unused"]], events)

    def process(item, known, guard):
        events.append(("process", item["tweet_id"], known))

    head, result = asyncio.run(
        jobs.x_collect(
            mod,
            {"browser": {"screen_name": "user"}, "known_stop_consecutive": 3},
            store(),
            "",
            jobs.Log(),
            process,
        )
    )
    assert head == "new"
    assert events[0] == ("process", "new", False)
    assert events[1] == ("process", "a", True)
    assert events[2] == "scroll"
    assert len([e for e in events if isinstance(e, tuple) and e[1] == "a"]) == 1
    assert result["stop_reason"] == "known_limit" and result["known_consecutive"] == 3
    assert not any(isinstance(e, tuple) and e[1] == "unused" for e in events)


def test_x_stalled_first_run_never_commits_checkpoint(monkeypatch, tmp_path):
    catalog, events = Catalog(), []
    mod = x_module([["new"]], events)
    mod.load_config = lambda p: {
        "database": str(tmp_path / "old.sqlite"),
        "download_dir": str(tmp_path),
        "browser": {"screen_name": "user"},
        "retry_failed": False,
        "request_delay_seconds": 0,
    }
    mod.Store = lambda *a: store()
    mod.Downloader = lambda *a: SimpleNamespace(
        download_item=lambda *a, **k: ("done", ["new.webp"], ""), close=lambda: None
    )
    monkeypatch.setattr(jobs, "module", lambda p: mod)
    monkeypatch.setattr(jobs, "Client", lambda: catalog)
    monkeypatch.setattr(jobs, "lookup", lambda *a: {})
    monkeypatch.setattr(jobs, "staging_config", lambda *a: (None, tmp_path))
    with pytest.raises(RuntimeError, match="停止推进"):
        jobs.x_job({"id": "task", "kind": "sync", "payload": {}})
    assert catalog.writes == []


def test_pixiv_first_run_cross_page_streak_resets_and_scopes_are_independent(
    monkeypatch, tmp_path
):
    catalog = Catalog({i: done() for i in ("a", "c", "d", "p", "r", "s")})
    downloaded, calls = [], []
    pages = {
        "public": [["a"], ["new", "c"], ["d", "unused"]],
        "private": [["p", "new2"], ["r", "s", "unused2"]],
    }

    def bookmarks(user_id=None, restrict="public", page=0):
        calls.append((restrict, page))
        items = pages[restrict][page]
        return {
            "illusts": items,
            "next_url": f"{restrict}:{page + 1}"
            if page + 1 < len(pages[restrict])
            else None,
        }

    api = SimpleNamespace(
        user_bookmarks_illust=bookmarks,
        parse_qs=lambda u: {"restrict": u.split(":")[0], "page": int(u.split(":")[1])},
    )
    cfg = {
        "download_dir": str(tmp_path),
        "restrict": ["public", "private"],
        "stop_after_consecutive_done": 2,
        "retry_failed": False,
        "request_delay_seconds": 0,
    }
    app = SimpleNamespace(config=cfg, store=store(), make_api=lambda: (api, "user"))
    mod = SimpleNamespace(
        App=lambda p: app,
        PixivCollector=lambda *a: None,
        normalize_illust=lambda i, r: SimpleNamespace(artwork_id=i),
        artwork_id_from_url=lambda u: "",
        call_with_retry=lambda label, fn, *a: fn(),
        PixivDownloader=lambda *a: SimpleNamespace(
            download_item=lambda item, **k: (
                downloaded.append(item.artwork_id) or "done",
                ["new.webp"],
                "",
            ),
            session=SimpleNamespace(close=lambda: None),
        ),
    )
    monkeypatch.setattr(jobs, "module", lambda p: mod)
    monkeypatch.setattr(jobs, "Client", lambda: catalog)
    monkeypatch.setattr(jobs, "staging_config", lambda *a: (None, tmp_path))
    result = jobs.pixiv_job({"id": "task", "kind": "sync", "payload": {}})
    assert downloaded == ["new", "new2"]
    assert [v["known_consecutive"] for v in result["sources"]] == [2, 2]
    assert all(v["stop_reason"] == "known_limit" for v in result["sources"])
    assert [head for path, head in catalog.writes] == ["a", "p"]
    mod.PixivCollector = lambda *a: SimpleNamespace(
        fetch_detail=lambda i: SimpleNamespace(artwork_id=i)
    )
    mod.artwork_id_from_url = lambda u: u
    manual = jobs.pixiv_job(
        {"id": "links", "kind": "links", "payload": {"urls": ["a", "c", "new3"]}}
    )
    assert manual["discovered"] == 3 and manual["stop_reason"] == "manual"
    retry = jobs.pixiv_job(
        {"id": "retry", "kind": "retry", "payload": {"urls": ["a", "c"]}}
    )
    assert retry["downloaded"] == 2 and retry["stop_reason"] == "manual"


def test_f2_first_run_exact_files_partial_gallery_and_multiple_sources(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "archive"
    root.mkdir()
    for identifier in ("k1", "k2", "k3", "k4"):
        for n in (1, 2):
            (root / f"{identifier}_{n}.webp").write_bytes(b"existing")
    (root / "partial_1.webp").write_bytes(b"existing")
    catalog = Catalog()
    cfg = {
        "f2_state_dir": str(tmp_path / "state"),
        "fallback_stop_consecutive_skipped": 2,
        "jobs": [
            {"url": "like", "mode": "like", "name": "likes"},
            {"url": "collection", "mode": "collection", "name": "collections"},
            {"url": "collects", "mode": "collects", "name": "folders"},
        ],
    }
    specs = {
        "like": [["new", "k1"], ["partial", "k2"], ["k3", "unused"]],
        "collection": [["k1", "k2", "unused2"]],
    }

    class Downloader:
        async def initiate_download(self, kind, payload, base, name, suffix):
            (Path(base) / (name + suffix)).write_bytes(b"new")

        initiate_static_download = initiate_download
        initiate_m3u8_download = initiate_download

        async def create_download_tasks(self, kwargs, item, base):
            # These progress phrases must never drive stopping.
            print(f"[{item['aweme_id']}] 非实况图集，跳过实况下载")
            for n in (1, 2):
                await self.initiate_download(
                    "image", "url", base, f"{item['aweme_id']}_{n}", ".webp"
                )
            print("当前任务处理完成")

        async def close(self):
            pass

    class Handler:
        def __init__(self, kwargs):
            self.kwargs = kwargs
            self.downloader = Downloader()

        async def get_or_add_user_data(self, *a):
            return root

        async def pages(self, *a):
            pages = specs[self.kwargs["mode"]]
            for index, ids in enumerate(pages):
                more = index + 1 < len(pages)
                yield SimpleNamespace(
                    _to_raw=lambda m=more: {"status_code": 0, "has_more": m},
                    _to_list=lambda ids=ids: [{"aweme_id": i} for i in ids],
                    has_more=more,
                )

        fetch_user_like_videos = pages
        fetch_user_collection_videos = pages

        async def fetch_user_collects_videos(self, folder):
            for ids in (
                [["k1", "k2", "unused3"], ["unused4"]]
                if folder == "first"
                else [["newfolder", "k1"], ["k2", "unused5"], ["unused6"]]
            ):
                yield SimpleNamespace(
                    _to_list=lambda ids=ids: [{"aweme_id": i} for i in ids]
                )

    class DB:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    async def folders(handler):
        for folder in ("first", "second"):
            async for page in handler.fetch_user_collects_videos(folder):
                await handler.downloader.create_download_tasks(
                    handler.kwargs, page._to_list(), root / folder
                )

    monkeypatch.setitem(
        sys.modules,
        "f2.apps.douyin.handler",
        SimpleNamespace(DouyinHandler=Handler, mode_function_map={"collects": folders}),
    )
    monkeypatch.setitem(
        sys.modules,
        "f2.apps.douyin.utils",
        SimpleNamespace(
            SecUserIdFetcher=SimpleNamespace(
                get_sec_user_id=AsyncMock(return_value="id")
            ),
            AwemeIdFetcher=None,
            ClientConfManager=SimpleNamespace(headers=lambda: {}),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "f2.apps.douyin.db", SimpleNamespace(AsyncUserDB=DB)
    )
    monkeypatch.setattr(
        jobs,
        "module",
        lambda p: SimpleNamespace(
            load_config=lambda p: cfg,
            load_f2_runtime_conf=lambda: {},
            build_douyin_job_payload=lambda cfg, spec: {
                "path": str(root),
                "page_counts": 2,
                "max_counts": 0,
                **spec,
            },
        ),
    )
    monkeypatch.setattr(jobs, "Client", lambda: catalog)
    monkeypatch.setattr(jobs, "prepare", lambda p, path, *a: (path, False))
    monkeypatch.setattr(
        jobs,
        "record",
        lambda p, i, state, files, meta: catalog.records.update(
            {i: {"state": state, "files": files, "metadata": meta}}
        ),
    )
    result = asyncio.run(jobs.f2_job({"id": "task", "kind": "sync", "payload": {}}))
    assert (
        result["downloaded"] == 3
    )  # new, partial gallery and a new work in the second collection folder.
    assert [v["known_consecutive"] for v in result["sources"]] == [2, 2, 2, 2]
    assert all(v["stop_reason"] == "known_limit" for v in result["sources"])
    assert (root / "partial_2.webp").exists()
    assert (
        not (root / "unused_1.webp").exists() and not (root / "unused2_1.webp").exists()
    )
    assert len(catalog.writes) == 2
    assert (root / "second" / "newfolder_1.webp").exists()
    assert not any(root.rglob("unused*.webp"))


def test_f2_missing_or_new_files_do_not_count_as_already_downloaded(tmp_path):
    image = tmp_path / "a.webp"
    image.write_bytes(b"old")
    missing = tmp_path / "missing.webp"
    assert jobs.f2_already_downloaded([(image, image, False, True)])
    assert not jobs.f2_already_downloaded(
        [(image, image, False, True), (missing, missing, False, True)]
    )
    assert not jobs.f2_already_downloaded([(image, image, False, False)])
