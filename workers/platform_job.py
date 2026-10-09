from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import random
import sys
import time
from pathlib import Path

from core.config import CONFIG_PATHS, read_json, write_json
from .client import Client, prepare, record, submit
from .stopping import KnownRun, lookup, completed, legacy_completed, threshold, summary

BASE = Path(os.getenv("NAS_ENGINE_ROOT", "/opt/nas-auto"))


class Log:
    def write(self, message):
        print(message, flush=True)


def module(platform):
    folder, filename = {
        "x": ("x", "x_auto_worker"),
        "pixiv": ("pixiv", "pixiv_auto_worker"),
        "douyin": ("douyin", "douyin_f2_worker"),
    }[platform]
    sys.path.insert(0, str(BASE / folder))
    return importlib.import_module(filename)


def process_files(platform, source_id, files, staging, final_root, metadata=None):
    delivered, processing = [], False
    for value in files:
        path = Path(value)
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError("下载文件缺失或为空")
        if staging and path.is_relative_to(staging):
            target = Path(final_root) / path.relative_to(staging)
            outcome = submit(
                platform,
                path,
                target,
                {"source_id": str(source_id), "metadata": metadata or {}},
            )
            if isinstance(outcome, dict):
                delivered.append(outcome["path"])
            else:
                delivered.append(str(path))
                processing = True
        else:
            delivered.append(str(path))
    record(
        platform,
        source_id,
        "processing" if processing else "complete",
        delivered,
        metadata,
    )
    return delivered


def bind_catalog(platform, old_store, staging, final, config):
    native_mark = old_store.mark_result
    native_should = old_store.should_download
    delivered = {}

    def mark(identifier, status, files, error="", *args, **kwargs):
        if status == "done":
            getter = old_store.get_tweet if platform == "x" else old_store.get_artwork
            raw = getter(identifier)
            values = dict(raw) if raw else {}
            metadata = {
                "title": values.get("text") or values.get("title", ""),
                "author": values.get("author") or values.get("user_name", ""),
                "url": values.get("url")
                or f"https://www.pixiv.net/artworks/{identifier}",
            }
            files = list(files)
            if staging:
                if platform == "pixiv":
                    folder = Path(staging) / "downloads-metadata" / str(identifier)
                    files.extend(
                        str(p)
                        for p in folder.rglob("*")
                        if p.is_file()
                        and p.stat().st_size
                        and not p.name.startswith(".")
                    )
                else:
                    for folder_name in ("_metadata", "_thumbnails"):
                        folder = Path(staging) / folder_name
                        files.extend(
                            str(p)
                            for p in folder.glob(str(identifier) + "_*")
                            if p.is_file() and p.stat().st_size
                        )
                    # Only sidecars next to this download, never a walk of archived media.
                    for name in list(files):
                        file = Path(name)
                        files.extend(
                            str(p)
                            for p in file.parent.glob(file.stem + "*")
                            if p.is_file()
                            and p.suffix.lower() in {".json", ".txt"}
                            and p.stat().st_size
                        )
            files = process_files(
                platform,
                identifier,
                list(dict.fromkeys(files)),
                staging,
                final,
                metadata,
            )
            delivered[str(identifier)] = files
        elif status == "failed":
            url = (
                f"https://www.pixiv.net/artworks/{identifier}"
                if platform == "pixiv"
                else ""
            )
            if platform == "x":
                raw = old_store.get_tweet(identifier)
                url = raw["url"] if raw else ""
            metadata = {"error": error, "url": url, "download_incomplete": True}
            files = list(files)
            if platform == "pixiv" and staging:
                folder = Path(staging) / "images" / str(identifier)
                files.extend(
                    str(p)
                    for p in folder.rglob("*")
                    if p.is_file()
                    and p.stat().st_size
                    and p.suffix.lower()
                    in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4"}
                )
            if files:
                files = process_files(
                    platform,
                    identifier,
                    list(dict.fromkeys(files)),
                    staging,
                    final,
                    metadata,
                )
            record(platform, identifier, "failed", files, metadata)
        return native_mark(identifier, status, files, error, *args, **kwargs)

    def should(identifier, *args, **kwargs):
        try:
            value = Client().request(f"/internal/record/{platform}/{identifier}")
            if value["state"] == "processing":
                return False
            if value["state"] == "complete":
                return bool(
                    platform == "x"
                    and config.get("redownload_missing_files")
                    and any(not Path(p).is_file() for p in value["files"])
                )
            if (
                value["state"] == "failed"
                and config.get("retry_failed", True)
                and not value["metadata"].get("imported")
            ):
                raw = (
                    old_store.get_tweet if platform == "x" else old_store.get_artwork
                )(identifier)
                maximum = int(config.get("max_download_attempts", 0) or 0)
                if maximum and raw and raw["attempts"] >= maximum:
                    return False
                return True
        except Exception:
            pass
        return native_should(identifier, *args, **kwargs)

    old_store.mark_result = mark
    old_store.should_download = should
    return delivered


def failed_urls(platform, automatic=False):
    urls, page = [], 1
    while True:
        data = Client().request(f"/internal/failed/{platform}?page={page}")
        urls.extend(
            row["metadata"].get("url", "")
            for row in data["items"]
            if not automatic or not row["metadata"].get("imported")
        )
        if page * 100 >= data["total"]:
            break
        page += 1
    return [url for url in urls if url]


def staging_config(platform, config, identifier):
    final = config["download_dir"]
    destination, enabled = prepare(platform, Path(final) / "incoming", identifier)
    if enabled:
        staging = destination.parent
        config["download_dir"] = str(staging)
        if platform == "pixiv":
            config["image_dir"] = str(staging / "images")
            config["metadata_dir"] = str(staging / "downloads-metadata")
        return staging, final
    return None, final


async def x_collect(mod, config, old_store, checkpoint, log, process_batch):
    collector = mod.BrowserCollector(config, log, old_store)
    cookies, user_id = collector._load_cookies()
    cfg = config["browser"]
    screen_name = str(cfg.get("screen_name") or "")
    seen, ended, head = set(), False, ""
    guard = KnownRun(threshold("x", config))
    marker_cfg = config.get("stop_marker", {})
    marker = (
        mod.tweet_id_from_url(str(marker_cfg.get("url", "")))
        if marker_cfg.get("enabled", True)
        else ""
    )
    async with collector._browser_page(cookies) as page:
        if not screen_name:
            screen_name = await collector._resolve_screen_name(page, user_id or "")
        url = cfg.get("likes_url") or f"https://x.com/{screen_name}/likes"

        async def response_received(response):
            nonlocal ended
            if "/graphql/" not in response.url or "/Likes" not in response.url:
                return
            try:
                content = await response.json()

                def walk(value):
                    if isinstance(value, dict):
                        if (
                            value.get("type") == "TimelineTerminateTimeline"
                            and value.get("direction") == "Bottom"
                        ):
                            return True
                        return any(walk(v) for v in value.values())
                    return isinstance(value, list) and any(walk(v) for v in value)

                ended = ended or walk(content)
            except Exception:
                pass

        page.on("response", response_received)
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=int(cfg.get("target_timeout_ms", 45000)),
        )
        await page.wait_for_timeout(3000)
        unchanged = 0
        while True:
            batch = await collector._collect_visible(page)
            fresh = [item for item in batch if item["tweet_id"] not in seen]
            records = await asyncio.to_thread(
                lookup, "x", [i["tweet_id"] for i in fresh]
            )
            for item in fresh:
                identifier = item["tweet_id"]
                if identifier in seen:
                    continue
                seen.add(identifier)
                head = head or identifier
                if identifier == checkpoint or identifier == marker:
                    return head, summary(
                        "likes",
                        "checkpoint" if identifier == checkpoint else "marker",
                        guard,
                        identifier,
                    )
                value = records.get(identifier)
                known = (
                    completed(value)
                    if value
                    else legacy_completed("x", old_store, identifier)
                )
                await asyncio.to_thread(process_batch, item, known, guard)
                if guard.observe(identifier, known):
                    return head, summary("likes", "known_limit", guard, identifier)
            # Bottom is useful only once the final viewport has been consumed.
            # A terminated network page can still contain off-screen DOM items.
            if ended and not fresh:
                return head, summary("likes", "end", guard)
            unchanged = 0 if fresh else unchanged + 1
            if unchanged >= 5:
                raise RuntimeError("X 页面停止推进，未到达来源边界；本次未完成")
            await page.mouse.wheel(0, 850)
            await page.wait_for_timeout(random.randint(1800, 3000))


def x_job(job):
    mod = module("x")
    config = mod.load_config(CONFIG_PATHS["x"])
    staging, final = staging_config("x", config, job["id"])
    log = Log()
    old_store = mod.Store(Path(config["database"]), log)
    client = Client()
    checkpoint = client.request("/internal/checkpoint/x/likes")["head"]
    if job["kind"] == "test":
        app = mod.App(CONFIG_PATHS["x"])
        app.log = log
        app.run_cookie_test(job["payload"].get("url", ""))
        if app.last_run_message != "cookie test ok":
            raise RuntimeError(app.last_run_message or "请提供有效推文链接进行实际测试")
        return {"message": app.last_run_message}
    if job["kind"] == "retry":
        job["payload"]["urls"] = job["payload"].get("urls") or failed_urls("x")
    previous_failures = (
        failed_urls("x", True)
        if job["kind"] == "sync" and config.get("retry_failed", True)
        else []
    )
    bind_catalog("x", old_store, staging, final, config)
    downloader = mod.Downloader(config, old_store, log)
    stats = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    def process(item, known=False, guard=None):
        stats["discovered"] += 1
        old_store.upsert_seen(item)
        if known:
            status, files, error = "skipped", [], ""
        else:
            status, files, error = downloader.download_item(
                item,
                force=job["kind"] == "retry" and not job["payload"].get("automatic"),
            )
        stats[
            "downloaded"
            if status == "done"
            else "skipped"
            if status == "skipped"
            else "failed"
        ] += 1
        if status not in {"done", "skipped"}:
            record(
                "x",
                item["tweet_id"],
                "failed",
                files,
                {"error": error, "url": item["url"]},
            )
        if guard:
            stats.update(
                phase="syncing",
                known_consecutive=guard.consecutive + 1 if known else 0,
                known_threshold=guard.threshold,
            )
        print(json.dumps(stats), flush=True)
        if not known:
            time.sleep(float(config.get("request_delay_seconds", 3)))

    try:
        if job["kind"] == "sync":
            head, outcome = asyncio.run(
                x_collect(mod, config, old_store, checkpoint, log, process)
            )
            stats.update(outcome)
            if head:
                client.request("/internal/checkpoint/x/likes", {"head": head})
        else:
            collector = mod.BrowserCollector(config, log, old_store)
            for url in job["payload"].get("urls", []):
                process(asyncio.run(collector.collect_single(url)))
            stats["stop_reason"] = "manual"
    finally:
        downloader.close()
    if previous_failures:
        stats["retried"] = x_job(
            {
                **job,
                "id": job["id"] + "-retry",
                "kind": "retry",
                "payload": {"urls": previous_failures, "automatic": True},
            }
        )
    return stats


def pixiv_job(job):
    mod = module("pixiv")
    app = mod.App(CONFIG_PATHS["pixiv"])
    app.log = Log()
    if job["kind"] == "oauth-start":
        return {"url": app.start_oauth()}
    if job["kind"] == "oauth-finish":
        app.finish_oauth(job["payload"]["callback"])
        return {"message": app.oauth_message}
    if job["kind"] == "test":
        app.test_token()
        if app.last_run_message != "ok":
            raise RuntimeError(app.last_run_message)
        return {"message": app.last_run_message}
    if job["kind"] == "retry":
        job["payload"]["urls"] = job["payload"].get("urls") or failed_urls("pixiv")
    config = dict(app.config)
    previous_failures = (
        failed_urls("pixiv", True)
        if job["kind"] == "sync" and config.get("retry_failed", True)
        else []
    )
    staging, final = staging_config("pixiv", config, job["id"])
    api, user_id = app.make_api()
    collector = mod.PixivCollector(
        api,
        config,
        app.store,
        app.log,
        lambda value: print(json.dumps(value), flush=True),
    )
    bind_catalog("pixiv", app.store, staging, final, config)
    downloader = mod.PixivDownloader(api, config, app.store, app.log)
    stats = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    client = Client()
    try:
        scopes = (
            config.get("restrict", ["public", "private"])
            if job["kind"] == "sync"
            else ["manual"]
        )
        if isinstance(scopes, str):
            scopes = [scopes]
        stats["sources"] = []
        marker_cfg = config.get("stop_marker", {})
        marker = (
            mod.artwork_id_from_url(str(marker_cfg.get("url", "")))
            if marker_cfg.get("enabled", True)
            else ""
        )
        for scope in scopes:
            checkpoint = client.request(f"/internal/checkpoint/pixiv/{scope}")["head"]
            head, next_qs, reason, last_id = "", None, "end", ""
            guard = KnownRun(threshold("pixiv", config))
            cursors = set()
            while True:
                if scope == "manual":
                    items = [
                        collector.fetch_detail(mod.artwork_id_from_url(url))
                        for url in job["payload"].get("urls", [])
                    ]
                    next_url = None
                else:
                    data = mod.call_with_retry(
                        "收藏列表",
                        lambda: api.user_bookmarks_illust(**next_qs)
                        if next_qs
                        else api.user_bookmarks_illust(user_id=user_id, restrict=scope),
                        config,
                        app.log,
                    )
                    if "error" in data:
                        raise RuntimeError("Pixiv 返回错误，未更新检查点")
                    items = [
                        mod.normalize_illust(value, scope)
                        for value in data.get("illusts", [])
                    ]
                    next_url = data.get("next_url")
                    if not items and next_url:
                        raise RuntimeError("Pixiv 空页包含后续游标，本次未完成")
                records = lookup("pixiv", [item.artwork_id for item in items], client)
                for item in items:
                    identifier = item.artwork_id
                    if identifier in guard.seen:
                        continue
                    head = head or identifier
                    last_id = identifier
                    if scope != "manual" and (
                        identifier == checkpoint or identifier == marker
                    ):
                        reason = "checkpoint" if identifier == checkpoint else "marker"
                        break
                    value = records.get(identifier)
                    known = (
                        completed(value)
                        if value
                        else legacy_completed("pixiv", app.store, identifier)
                    )
                    app.store.upsert_seen(item)
                    stats["discovered"] += 1
                    if known and job["kind"] != "retry":
                        status, files, error = "skipped", [], ""
                    else:
                        status, files, error = downloader.download_item(
                            item,
                            force=job["kind"] == "retry"
                            and not job["payload"].get("automatic"),
                        )
                    stats[
                        "downloaded"
                        if status == "done"
                        else "skipped"
                        if status == "skipped"
                        else "failed"
                    ] += 1
                    if status not in {"done", "skipped"}:
                        record(
                            "pixiv",
                            identifier,
                            "failed",
                            files,
                            {
                                "error": error,
                                "url": f"https://www.pixiv.net/artworks/{identifier}",
                            },
                        )
                    reached = guard.observe(identifier, known)
                    stats.update(
                        phase="syncing",
                        source=scope,
                        known_consecutive=guard.consecutive,
                        known_threshold=guard.threshold,
                    )
                    print(json.dumps(stats), flush=True)
                    if scope != "manual" and reached:
                        reason = "known_limit"
                        break
                if (
                    reason in {"checkpoint", "marker", "known_limit"}
                    or not next_url
                    or scope == "manual"
                ):
                    break
                if next_url in cursors:
                    raise RuntimeError("Pixiv 分页游标未推进，本次未完成")
                cursors.add(next_url)
                next_qs = api.parse_qs(next_url)
                time.sleep(float(config.get("request_delay_seconds", 1)))
            outcome = summary(
                scope, "manual" if scope == "manual" else reason, guard, last_id
            )
            stats["sources"].append(outcome)
            stats.update(outcome)
            if scope != "manual":
                client.request(
                    f"/internal/checkpoint/pixiv/{scope}", {"head": head or "__empty__"}
                )
    finally:
        downloader.session.close()
    if previous_failures:
        stats["retried"] = pixiv_job(
            {
                **job,
                "id": job["id"] + "-retry",
                "kind": "retry",
                "payload": {"urls": previous_failures, "automatic": True},
            }
        )
    return stats


def f2_already_downloaded(captured):
    media = {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".heic",
        ".avif",
    }
    return bool(
        captured
        and any(source.suffix.lower() in media for source, *_ in captured)
        and all(
            existed and source.is_file() and source.stat().st_size > 0
            for source, target, enabled, existed in captured
        )
    )


async def f2_job(job):
    mod = module("douyin")
    config = mod.load_config(CONFIG_PATHS["douyin"])
    state_dir = Path(config.get("f2_state_dir", "/state/douyin/f2"))
    (state_dir / "conf").mkdir(parents=True, exist_ok=True)
    import yaml

    (state_dir / "conf" / "conf.yaml").write_text(
        yaml.safe_dump(mod.load_f2_runtime_conf(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chdir(state_dir)
    from f2.apps.douyin.handler import DouyinHandler
    from f2.apps.douyin.utils import SecUserIdFetcher, AwemeIdFetcher, ClientConfManager
    from f2.apps.douyin.db import AsyncUserDB

    stats = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    client = Client()
    if job["kind"] == "test":
        specifications = [
            v for v in config.get("jobs", []) if v.get("enabled", True) and v.get("url")
        ]
        if not specifications:
            raise RuntimeError("请先设置一个抖音来源")
        kwargs = mod.build_douyin_job_payload(config, specifications[0])
        kwargs["headers"] = ClientConfManager.headers()
        handler = DouyinHandler(kwargs)
        try:
            identifier = await SecUserIdFetcher.get_sec_user_id(kwargs["url"])
            profile = await handler.fetch_user_profile(identifier)
            if profile.status_code != 0:
                raise RuntimeError("抖音来源或 Cookie 验证未通过")
            return {"message": "抖音来源与 Cookie 请求成功"}
        finally:
            await handler.downloader.close()
    specs = config.get("jobs", [])
    if job["kind"] in {"links", "retry"}:
        urls = (
            job["payload"].get("urls", [])
            if job["kind"] == "links"
            else job["payload"].get("urls") or failed_urls("douyin")
        )
        specs = [{"url": url, "mode": "one", "enabled": True} for url in urls]
    for spec in specs:
        if not spec.get("enabled", True):
            continue
        kwargs = mod.build_douyin_job_payload(config, spec)
        kwargs["headers"] = ClientConfManager.headers()
        kwargs["max_tasks"] = 1
        if spec.get("mode", "like") not in {"one", "live"}:
            kwargs["max_counts"] = 0
        handler = DouyinHandler(kwargs)
        pages = None
        source_key = hashlib.sha256(
            (str(spec.get("url")) + str(spec.get("mode"))).encode()
        ).hexdigest()[:20]
        checkpoint = client.request("/internal/checkpoint/douyin/" + source_key)["head"]
        mode = spec.get("mode", "like")
        final_root = Path(kwargs["path"]).resolve()
        captured = []
        create_tasks = handler.downloader.create_download_tasks
        # Track exact paths submitted by upstream instead of searching the archive tree.
        for method_name in (
            "initiate_download",
            "initiate_static_download",
            "initiate_m3u8_download",
        ):
            original = getattr(handler.downloader, method_name)

            async def tracked(kind, payload, base, name, suffix, _original=original):
                final = Path(base).resolve() / (name + (suffix or ""))
                if final.is_file() and final.stat().st_size:
                    captured.append((final, final, False, True))
                    return
                staged, enabled = prepare("douyin", final, job["id"])
                if enabled:
                    staged = staged.parent / final.relative_to(final_root)
                staged.parent.mkdir(parents=True, exist_ok=True)
                captured.append((staged, final, enabled, False))
                await _original(
                    kind, payload, staged.parent, staged.stem, staged.suffix
                )

            setattr(handler.downloader, method_name, tracked)

        async def download_item(item, root, value=None):
            identifier = str(item.get("aweme_id", ""))
            if not identifier:
                raise RuntimeError("抖音返回作品缺少 ID，本次未完成")
            stats["discovered"] += 1
            if value and value["state"] in {"complete", "processing"}:
                stats["skipped"] += 1
                return completed(value)
            captured.clear()
            await create_tasks(kwargs, item, root)
            delivered, processing, failed = [], False, False
            for source, target, enabled, existed in captured:
                if not source.is_file() or not source.stat().st_size:
                    failed = True
                    continue
                if enabled:
                    outcome = submit(
                        "douyin",
                        source,
                        target,
                        {
                            "source_id": identifier,
                            "metadata": {"title": item.get("desc", "")},
                        },
                    )
                    if isinstance(outcome, dict):
                        delivered.append(outcome["path"])
                    else:
                        delivered.append(str(source))
                        processing = True
                else:
                    delivered.append(str(source))
            metadata = {
                "title": item.get("desc", ""),
                "url": f"https://www.douyin.com/video/{identifier}",
            }
            failed = failed or not delivered
            if failed:
                metadata.update(error="上游未确认文件完成", download_incomplete=True)
            record(
                "douyin",
                identifier,
                "failed" if failed else "processing" if processing else "complete",
                delivered,
                metadata,
            )
            known = f2_already_downloaded(captured) and not failed
            stats["failed" if failed else "skipped" if known else "downloaded"] += 1
            print(json.dumps(stats), flush=True)
            return known

        try:
            if mode == "one":
                identifier = await AwemeIdFetcher.get_aweme_id(kwargs["url"])
                data = await handler.fetch_one_video(identifier)
                if data._to_raw().get("status_code", 0) != 0:
                    raise RuntimeError("抖音作品接口返回错误")
                async with AsyncUserDB("douyin_users.db") as database:
                    root = await handler.get_or_add_user_data(
                        kwargs, data.sec_user_id, database
                    )
                await download_item(data._to_dict(), root)
                continue
            if mode not in {"like", "collection", "post"}:
                # Retain the fork's dispatch for mixes/collections/music/live;
                # stop batched modes at the same per-work boundary.
                from f2.apps.douyin.handler import mode_function_map

                groups, stop_page = {}, False
                # Each upstream generator stops its own source; an enclosing
                # collection-folder loop can then continue to the next folder.
                for method_name in dir(handler):
                    fetch = getattr(handler, method_name)
                    if method_name.startswith("fetch_") and inspect.isasyncgenfunction(
                        fetch
                    ):

                        async def bounded_pages(*args, _fetch=fetch, **options):
                            nonlocal stop_page
                            stream = _fetch(*args, **options)
                            try:
                                async for page in stream:
                                    stop_page = False
                                    yield page
                                    if stop_page:
                                        break
                            finally:
                                await stream.aclose()

                        setattr(handler, method_name, bounded_pages)

                async def dispatch_items(upstream_kwargs, items, root):
                    nonlocal stop_page
                    group_key = str(Path(root).resolve())
                    guard, outcome = groups.setdefault(
                        group_key, (KnownRun(threshold("douyin", config)), {})
                    )
                    items = [items] if isinstance(items, dict) else items
                    values = lookup(
                        "douyin", [str(i.get("aweme_id", "")) for i in items], client
                    )
                    for item in items:
                        identifier = str(item.get("aweme_id", ""))
                        if identifier in guard.seen:
                            continue
                        known = await download_item(item, root, values.get(identifier))
                        reached = guard.observe(identifier, known)
                        label = spec.get("name") or mode
                        if mode == "collects":
                            label += " / " + Path(root).name
                        outcome.update(
                            summary(
                                label,
                                "known_limit" if reached else "end",
                                guard,
                                identifier,
                            )
                        )
                        stats.update(
                            phase="syncing",
                            source=label,
                            known_consecutive=guard.consecutive,
                            known_threshold=guard.threshold,
                        )
                        print(json.dumps(stats), flush=True)
                        if reached:
                            stop_page = True
                            return

                handler.downloader.create_download_tasks = dispatch_items
                await mode_function_map[mode](handler)
                outcomes = [outcome for guard, outcome in groups.values() if outcome]
                stats.setdefault("sources", []).extend(outcomes)
                if outcomes:
                    stats.update(outcomes[-1])
                if mode == "live":
                    for source, target, enabled, existed in captured:
                        if source.is_file() and source.stat().st_size:
                            process_files(
                                "douyin",
                                source.stem,
                                [str(source)],
                                source.parent if enabled else None,
                                target.parent,
                                {"url": spec["url"]},
                            )
                continue
            sec_id = await SecUserIdFetcher.get_sec_user_id(kwargs["url"])
            async with AsyncUserDB("douyin_users.db") as database:
                root = await handler.get_or_add_user_data(kwargs, sec_id, database)
            if mode == "like":
                pages = handler.fetch_user_like_videos(
                    sec_id, 0, kwargs["page_counts"], kwargs["max_counts"]
                )
            elif mode == "collection":
                pages = handler.fetch_user_collection_videos(
                    0, kwargs["page_counts"], kwargs["max_counts"]
                )
            else:
                pages = handler.fetch_user_post_videos(
                    sec_id, 0, kwargs["page_counts"], kwargs["max_counts"]
                )
            head, bounded, reason, last_id = "", False, "end", ""
            guard = KnownRun(threshold("douyin", config))
            async for page in pages:
                raw = page._to_raw()
                if raw.get("status_code", 0) != 0 or "has_more" not in raw:
                    raise RuntimeError("抖音返回异常，未更新来源边界")
                items = page._to_list()
                if not items and page.has_more:
                    raise RuntimeError("抖音空页包含后续游标，本次未完成")
                values = lookup(
                    "douyin", [str(item.get("aweme_id", "")) for item in items], client
                )
                for item in items:
                    identifier = str(item.get("aweme_id", ""))
                    if not identifier:
                        raise RuntimeError("抖音返回作品缺少 ID，本次未完成")
                    if identifier in guard.seen:
                        continue
                    head = head or identifier
                    last_id = identifier
                    if identifier == checkpoint:
                        reason, bounded = "checkpoint", True
                        break
                    known = await download_item(item, root, values.get(identifier))
                    reached = guard.observe(identifier, known)
                    stats.update(
                        phase="syncing",
                        source=spec.get("name") or mode,
                        known_consecutive=guard.consecutive,
                        known_threshold=guard.threshold,
                    )
                    print(json.dumps(stats), flush=True)
                    if reached:
                        reason, bounded = "known_limit", True
                        break
                if bounded or not page.has_more:
                    bounded = True
                    break
            if not bounded:
                raise RuntimeError("上游数量限制提前结束，未到达来源边界")
            outcome = summary(spec.get("name") or mode, reason, guard, last_id)
            stats.setdefault("sources", []).append(outcome)
            stats.update(outcome)
            client.request(
                "/internal/checkpoint/douyin/" + source_key,
                {"head": head or "__empty__"},
            )
        finally:
            if pages is not None:
                await pages.aclose()
            await handler.downloader.close()
    return stats


def main():
    path = Path(sys.argv[1])
    job = read_json(path)
    if job["platform"] == "x":
        result = x_job(job)
    elif job["platform"] == "pixiv":
        result = pixiv_job(job)
    elif job["platform"] == "douyin":
        result = asyncio.run(f2_job(job))
    elif job["platform"] == "xhs":
        from .xhs_job import run

        result = asyncio.run(run(job))
    else:
        raise ValueError("unsupported executor")
    write_json(path.with_suffix(".result"), result)


if __name__ == "__main__":
    main()
