import sys
import tempfile
import unittest
import zipfile
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import requests
import yaml
from pixivpy3.utils import PixivError

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_integrated"))
sys.path.insert(0, str(ROOT / "_src" / "douyin-f2-auto-main"))
sys.path.insert(0, str(ROOT / "_src" / "pixiv-auto-download-nas-main"))
sys.path.insert(0, str(ROOT / "_src" / "XHS-Downloader-NAS-main"))
sys.path.insert(0, str(ROOT / "_src" / "x-auto-download-nas-main"))

import integrated_server
from douyin_f2_worker import (
    DOUYIN_REFERENCE_COOKIE_ORDER,
    DEFAULT_CONFIG as DOUYIN_DEFAULT_CONFIG,
    F2SkipStopGuard,
    build_f2_runtime_conf,
    cookie_summary,
    normalize_cookie_text,
    render_cookie_block,
    render_douyin_job_yaml,
    f2_network_hint,
)
from pixiv_auto_worker import PixivDownloader, classify_error, safe_extract_zip
from xhs_auto_worker import (
    RingLog as XhsRingLog,
    Store as XhsStore,
    cookie_summary_from_settings,
    is_transient_xhs_failure,
    post_download,
    save_settings_cookie,
    sync_downloader_settings,
    xhs_api_response_has_failure,
    xhs_api_segment_has_failure,
)
from x_auto_worker import (
    App as XApp, make_handler as x_make_handler, html_page as x_html_page,
    BrowserCollector, Downloader, DEFAULT_CONFIG as X_DEFAULT_CONFIG,
    RingLog as XRingLog, Store as XStore, browser_scroll_limit, item_from_url,
)


class IntegratedPageTests(unittest.TestCase):
    def test_frontend_dist_is_served_when_built(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            assets.mkdir()
            (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
            with patch.object(integrated_server, "FRONTEND_DIST", Path(tmp)):
                served = integrated_server.frontend_asset("/assets/app.js")
                self.assertEqual(served, (b"console.log('ok')", "text/javascript; charset=utf-8"))
                self.assertIsNone(integrated_server.frontend_asset("/assets/../secret"))

    def test_status_contract_keeps_platform_adapters_independent(self) -> None:
        status = integrated_server.service_status()
        self.assertEqual({item["key"] for item in status["services"]}, {"xhs", "x", "pixiv", "douyin", "telegram"})
        for service in status["services"]:
            self.assertIn("running", service)
            self.assertIn("current", service)
            self.assertIn("next_run_at", service)

    def test_x_browser_session_is_bounded_for_legacy_unlimited_config(self) -> None:
        self.assertEqual(browser_scroll_limit({"max_scrolls": 0}), 20)
        self.assertEqual(browser_scroll_limit({"max_scrolls": 12}), 12)
        self.assertEqual(browser_scroll_limit({"max_scrolls": 100, "safety_max_scrolls": 30}), 30)

    def test_proxy_rewrite_does_not_inject_back_bar(self) -> None:
        body = integrated_server.rewrite_html("/x/", b"<html><body><main>ok</main></body></html>", "text/html")
        text = body.decode("utf-8")
        self.assertNotIn("返回统一主页", text)
        self.assertIn("<main>ok</main>", text)

    def test_unified_cookie_import_routes_are_not_rendered(self) -> None:
        body = integrated_server.page().decode("utf-8")
        self.assertNotIn("/import-cookies", body)
        self.assertNotIn("/api/cookie-preview", body)
        self.assertNotIn("/api/cookie-import", body)

    def test_integrated_requirements_include_worker_runtime_tools(self) -> None:
        requirements = (ROOT / "_integrated" / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("gallery-dl", requirements)
        self.assertIn("yt-dlp", requirements)


class DouyinCookieTests(unittest.TestCase):
    def test_new_cookie_fields_survive_normalization_and_yaml_rendering(self):
        cookie = 'verify_fp=new; sessionid=old; future_security_token=abc==; sessionid=current; ttwid=present'
        normalized = normalize_cookie_text(cookie)
        self.assertEqual(normalized, 'sessionid=current; ttwid=present; verify_fp=new; future_security_token=abc==')
        self.assertEqual(normalize_cookie_text(render_cookie_block(normalized)), normalized)
        rendered = yaml.safe_load(render_douyin_job_yaml({'cookie': normalized, 'mode': 'like'}))
        self.assertEqual(rendered['douyin']['cookie'], normalized)
        self.assertEqual(integrated_server.extract_douyin_cookie_text(render_cookie_block(normalized)), normalized)
        integrated_yaml = yaml.safe_load(integrated_server.render_douyin_job_yaml({'cookie': normalized}))
        self.assertEqual(integrated_yaml['douyin']['cookie'], normalized)

    def test_netscape_cookies_keep_new_douyin_fields_only(self):
        cookie = ('# Netscape HTTP Cookie File\n'
                  '.douyin.com\tTRUE\t/\tTRUE\t0\tverify_fp\tnew\n'
                  '.example.com\tTRUE\t/\tTRUE\t0\tforeign_token\tignored\n')
        self.assertEqual(normalize_cookie_text(cookie), 'verify_fp=new')

    def test_network_hint_recognizes_redirects_without_login_keyword_false_positives(self):
        self.assertIn('重定向', f2_network_hint("Redirect response '301 Moved Permanently' for url"))
        self.assertIn('网络认证', f2_network_hint('https://gportal.example.net/login'))
        self.assertEqual(f2_network_hint('login_time=123; record_force_login=1'), '')
        self.assertEqual(f2_network_hint('normal request HTTP 200'), '')

    def test_default_max_job_runtime_is_300_seconds(self) -> None:
        self.assertEqual(DOUYIN_DEFAULT_CONFIG["max_job_runtime_seconds"], 300)

    def test_builds_bark_disabled_runtime_conf(self) -> None:
        runtime_conf = build_f2_runtime_conf(
            {"f2": {"enable_bark": True, "douyin": {"headers": {"Referer": "https://www.douyin.com/"}}}}
        )
        self.assertFalse(runtime_conf["f2"]["enable_bark"])
        self.assertEqual(runtime_conf["f2"]["douyin"]["headers"]["Referer"], "https://www.douyin.com/")

    def test_normalizes_cookie_text_and_summary(self) -> None:
        normalized = normalize_cookie_text(
            "cookie: sessionid=abc;\n"
            "  ttwid=def;\n"
            "  msToken=ghi;\n"
            "  random_key=keepme;\n"
            "naming: ignored\n"
        )
        self.assertEqual(normalized, "sessionid=abc; ttwid=def; msToken=ghi; random_key=keepme")
        summary = cookie_summary(normalized)
        self.assertEqual(summary["fields"], 4)
        self.assertEqual(summary["missing_required"], [])
        self.assertEqual(summary["status"], "高风险")
        self.assertEqual(summary["reference_present"], 2)
        self.assertEqual(summary["reference_total"], len(DOUYIN_REFERENCE_COOKIE_ORDER))

    def test_full_reference_cookie_is_normal(self) -> None:
        cookie_text = "; ".join(f"{name}=x" for name in DOUYIN_REFERENCE_COOKIE_ORDER)
        summary = cookie_summary(cookie_text)
        self.assertEqual(summary["status"], "正常")
        self.assertEqual(summary["risk"], "正常")
        self.assertEqual(summary["missing_reference"], [])

    def test_renders_saved_cookie_block_with_reference_line_breaks(self) -> None:
        rendered = render_cookie_block("sessionid=abc; ttwid=def")
        self.assertEqual(rendered, "cookie: sessionid=abc;\n  ttwid=def\n")
        self.assertEqual(normalize_cookie_text(rendered), "sessionid=abc; ttwid=def")

    def test_renders_saved_cookie_block_with_reference_grouping(self) -> None:
        rendered = render_cookie_block(
            "my_rd=1; volume_info=2; WallpaperGuide=3; FOLLOW_NUMBER_YELLOW_POINT_INFO=4"
        )
        self.assertEqual(
            rendered,
            "cookie: my_rd=1; volume_info=2; WallpaperGuide=3;\n  FOLLOW_NUMBER_YELLOW_POINT_INFO=4\n",
        )

    def test_renders_job_yaml_with_reference_cookie_line_breaks(self) -> None:
        rendered = render_douyin_job_yaml(
            {
                "cookie": "sessionid=abc; ttwid=def",
                "cover": False,
                "desc": False,
                "folderize": True,
                "interval": "all",
                "languages": None,
                "lyric": True,
                "max_connections": 5,
                "max_counts": 0,
                "max_retries": 5,
                "max_tasks": 10,
                "mode": "like",
                "music": None,
                "naming": "{create}-{nickname}-{aweme_id}",
                "page_counts": 20,
                "path": "/douyin",
                "timeout": 10,
                "url": "https://www.douyin.com/user/example?showTab=like",
            }
        )
        self.assertIn("  cookie: sessionid=abc;\n    ttwid=def\n", rendered)
        loaded = yaml.safe_load(rendered) or {}
        self.assertEqual(loaded["douyin"]["cookie"], "sessionid=abc; ttwid=def")

    def test_f2_skip_guard_stops_after_consecutive_skipped_content_ids(self) -> None:
        guard = F2SkipStopGuard(2)
        self.assertFalse(guard.observe("INFO     [7647184464938078835] 非实况图集，跳过实况下载"))
        self.assertFalse(guard.observe("INFO     [  跳过  ]：existing-file.webp"))
        self.assertFalse(guard.observe("INFO     [7646424149867365032] 非实况图集，跳过实况下载"))
        self.assertFalse(guard.observe("INFO     [  跳过  ]：existing-file.webp"))
        self.assertTrue(guard.observe("INFO     [7646838488175797489] 非实况图集，跳过实况下载"))
        self.assertEqual(guard.consecutive_skipped, 2)

    def test_f2_skip_guard_resets_when_content_has_completed_file(self) -> None:
        guard = F2SkipStopGuard(2)
        guard.observe("INFO     [7647184464938078835] 非实况图集，跳过实况下载")
        guard.observe("INFO     [  跳过  ]：existing-file.webp")
        guard.observe("INFO     [7646424149867365032] 非实况图集，跳过实况下载")
        guard.observe("INFO     [  完成  ]：new-file.mp4")
        self.assertFalse(guard.observe("INFO     [7646838488175797489] 非实况图集，跳过实况下载"))
        self.assertEqual(guard.consecutive_skipped, 0)


class XhsSettingsTests(unittest.TestCase):
    def test_store_migrates_existing_notes_table_before_retry_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "xhs.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    create table notes (
                        note_id text primary key,
                        url text not null,
                        source text,
                        status text not null default 'pending',
                        attempts integer not null default 0,
                        last_error text,
                        first_seen_at text not null,
                        updated_at text not null,
                        downloaded_at text
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()
            store = XhsStore(db_path, XhsRingLog())
            conn = store.connect()
            try:
                columns = [row[1] for row in conn.execute("pragma table_info(notes)").fetchall()]
                indexes = [row[1] for row in conn.execute("pragma index_list(notes)").fetchall()]
            finally:
                conn.close()
            self.assertIn("retry_after", columns)
            self.assertIn("idx_notes_retry_after", indexes)

    def test_downloader_settings_preserves_cookie_and_applies_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(
                json.dumps({"cookie": "a1=old; web_session=old", "custom": "keep"}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = {
                "settings_path": str(settings_path),
                "image_format": "AUTO",
                "sync_settings": {"path": str(settings_path), "defaults": {"work_path": "/xhs"}},
            }
            saved = sync_downloader_settings(config)
            self.assertEqual(saved["cookie"], "a1=old; web_session=old")
            self.assertEqual(saved["custom"], "keep")
            self.assertEqual(saved["work_path"], "/xhs")
            self.assertTrue(saved["folder_mode"])
            self.assertEqual(saved["image_format"], "AUTO")

    def test_xhs_record_flag_supports_both_api_versions(self) -> None:
        response = MagicMock(status_code=200, text="ok")
        response.json.return_value = {"message": "获取小红书作品数据成功", "data": {"作品ID": "abc"}}
        for skip in (True, False):
            with patch("xhs_auto_worker.requests.post", return_value=response) as post:
                self.assertTrue(post_download("http://xhs-api:5556/xhs/detail", "https://www.xiaohongshu.com/explore/abc", skip=skip, timeout=120)[0])
                self.assertEqual(post.call_args.kwargs["json"]["skip"], skip)
                self.assertEqual(post.call_args.kwargs["json"]["check_record"], skip)

    def test_saves_xhs_downloader_cookie_to_settings_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            config = {"settings_path": str(settings_path), "sync_settings": {"path": str(settings_path)}}
            save_settings_cookie(config, "a1=abc; web_session=def")
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["cookie"], "a1=abc; web_session=def")
            summary = cookie_summary_from_settings(config)
            self.assertTrue(summary["present"])
            self.assertEqual(summary["missing_required"], [])

    def test_detects_xhs_api_internal_download_failures(self) -> None:
        self.assertTrue(xhs_api_segment_has_failure("网络异常，作品 下载失败，错误信息: HTTPStatusError('400')"))
        self.assertTrue(xhs_api_segment_has_failure("6a32bc4e000000000f028e9f 获取数据失败"))
        self.assertTrue(xhs_api_segment_has_failure("获取小红书作品数据失败"))
        self.assertTrue(xhs_api_segment_has_failure("6a32bc4e000000000f028e9f 提取数据失败"))
        self.assertFalse(
            xhs_api_segment_has_failure(
                "网络异常，abc 下载失败，错误信息: ReadTimeout('')\n"
                "文件 abc.webp 下载成功\n"
                "作品处理完成：69eddca4000000001f004e2d"
            )
        )
        self.assertTrue(is_transient_xhs_failure("错误信息: ReadTimeout('') 网络异常"))
        self.assertTrue(is_transient_xhs_failure("RemoteProtocolError('peer closed connection')"))
        self.assertTrue(is_transient_xhs_failure("ConnectError('[Errno -3] Temporary failure in name resolution')"))
        self.assertTrue(is_transient_xhs_failure("RequestException('Failed to perform, curl: (6) Could not resolve host')"))
        self.assertTrue(is_transient_xhs_failure("RequestException('Failed to perform, curl: (18) transfer closed')"))
        self.assertTrue(is_transient_xhs_failure("RequestException('Failed to perform, curl: (28) Operation timed out')"))
        self.assertFalse(is_transient_xhs_failure("笔记不存在"))
        self.assertTrue(xhs_api_response_has_failure({"message": "获取小红书作品数据失败", "data": None}))
        self.assertTrue(xhs_api_response_has_failure({"message": "unknown", "data": None}))
        self.assertFalse(xhs_api_response_has_failure({"message": "获取小红书作品数据成功", "data": {"作品ID": "abc"}}))
        self.assertFalse(xhs_api_segment_has_failure("作品处理完成：69eddca4000000001f004e2d"))

    def test_xhs_retry_button_requeues_by_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = XhsStore(Path(tmp) / "xhs.sqlite3", XhsRingLog())
            url = "https://www.xiaohongshu.com/discovery/item/abc123?xsec_token=t1"
            note_id = "abc123"
            store.enqueue([url], "test")
            store.mark_failed(note_id, "old error")
            self.assertTrue(store.force_pending_url(url, "retry-button"))
            row = dict(store.pending(False, 0, 10)[0])
            self.assertEqual(row["note_id"], note_id)
            self.assertEqual(row["url"], url)
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["last_error"], "")

    def test_xhs_link_queue_accepts_and_deduplicates_browser_submissions(self) -> None:
        old_queue_file = integrated_server.XHS_QUEUE_FILE
        with tempfile.TemporaryDirectory() as tmp:
            integrated_server.XHS_QUEUE_FILE = Path(tmp) / "links.txt"
            try:
                url1 = "https://www.xiaohongshu.com/explore/abc123?xsec_token=t1"
                url2 = "https://www.xiaohongshu.com/discovery/item/def456?xsec_token=t2"
                urls, invalid = integrated_server.normalize_xhs_link_payload(
                    {
                        "urls": [url1, "not-a-url"],
                        "text": f"extra {url2} and duplicate {url1}",
                    }
                )
                self.assertEqual(urls, [url1, url2])
                self.assertEqual(invalid, ["not-a-url"])

                first = integrated_server.append_xhs_queue_links(urls)
                self.assertEqual(first["accepted"], [url1, url2])
                self.assertEqual(first["skipped"], [])
                self.assertIn(url1, integrated_server.XHS_QUEUE_FILE.read_text(encoding="utf-8"))

                second = integrated_server.append_xhs_queue_links([url1, url2])
                self.assertEqual(second["accepted"], [])
                self.assertEqual(second["skipped"], [url1, url2])
            finally:
                integrated_server.XHS_QUEUE_FILE = old_queue_file


class XDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = json.loads(json.dumps(X_DEFAULT_CONFIG))
        self.config['download_dir'] = str(self.root / 'downloads')
        self.log = XRingLog()
        self.store = XStore(self.root / 'state.sqlite3', self.log)
        self.downloader = Downloader(self.config, self.store, self.log)
        self.addCleanup(self.downloader.close)
        self.item = item_from_url('https://x.com/_Nag1chan/status/2104881580913426735')

    def image(self, name='one.jpg'):
        path = self.root / name
        path.write_bytes(b'fixture media')
        return str(path)

    def test_image_only_tweet_never_calls_yt_dlp(self):
        item = {**self.item, 'media_ids': ['HTYKoXTbMAEToXI']}
        with patch.object(self.downloader, 'download_images', return_value=[self.image()]), \
             patch.object(self.downloader, 'download_video') as video, \
             patch.object(BrowserCollector, 'collect_single') as probe:
            status, files, error = self.downloader.download_item(item)
        self.assertEqual((status, error), ('done', ''))
        self.assertEqual(len(files), 1)
        self.assertEqual(self.store.get_tweet(item['tweet_id'])['media_hint'], 'image')
        video.assert_not_called()
        probe.assert_not_called()

    def test_missing_list_media_recovered_from_detail_page(self):
        detail = {**self.item, 'media_ids': ['HTYKoXTbMAEToXI'], '_detail_checked': True}
        with patch.object(self.downloader, 'download_images', side_effect=[[], [self.image()]]), \
             patch.object(BrowserCollector, 'collect_single', new_callable=AsyncMock, return_value=detail) as probe, \
             patch.object(self.downloader, 'download_video') as video:
            self.assertEqual(self.downloader.download_item(self.item)[0], 'done')
        probe.assert_awaited_once_with(self.item['url'])
        video.assert_not_called()

    def test_no_media_is_failed_without_video_error_or_repeat_probe(self):
        with patch.object(self.downloader, 'download_images', return_value=[]), \
             patch.object(BrowserCollector, 'collect_single', new_callable=AsyncMock,
                          return_value={**self.item, '_detail_checked': True}) as probe, \
             patch.object(self.downloader, 'download_video') as video:
            result = self.downloader.download_item(self.item)
        self.assertEqual(result, ('failed', [], 'no downloadable media found in tweet'))
        probe.assert_awaited_once()
        video.assert_not_called()
        with patch.object(BrowserCollector, 'collect_single') as probe:
            self.downloader.download_item({**self.item, '_detail_checked': True}, force=True)
        probe.assert_not_called()

    def test_unparsed_single_tweet_does_not_invent_video(self):
        collector = BrowserCollector(self.config, self.log)
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def fake_page(_cookies):
            yield page
        with patch.object(collector, '_load_cookies', return_value=([], None)), \
             patch.object(collector, '_browser_page', fake_page), \
             patch.object(collector, '_collect_visible', new_callable=AsyncMock, return_value=[]):
            import asyncio
            item = asyncio.run(collector.collect_single(self.item['url']))
        self.assertFalse(item['has_video'])
        self.assertTrue(item['_detail_checked'])

    def test_video_and_mixed_tweets_still_use_video_downloader(self):
        for media_ids in ([], ['photo']):
            with self.subTest(media_ids=media_ids), \
                 patch.object(self.downloader, 'download_images', return_value=[self.image()] if media_ids else []), \
                 patch.object(self.downloader, 'download_video', return_value=[self.image('video.mp4')]) as video, \
                 patch.object(BrowserCollector, 'collect_single') as probe:
                item = {**self.item, 'media_ids': media_ids, 'has_video': True}
                self.assertEqual(self.downloader.download_item(item, force=True)[0], 'done')
                video.assert_called_once_with(item)
                probe.assert_not_called()

    def test_incomplete_multi_image_tweet_keeps_files_and_remains_retryable(self):
        file = self.image()
        item = {**self.item, 'media_ids': ['one', 'two']}
        with patch.object(self.downloader, 'download_images', return_value=[file]), \
             patch.object(self.downloader, 'download_video') as video:
            status, files, error = self.downloader.download_item(item)
        self.assertEqual(status, 'failed')
        self.assertEqual(files, [file])
        self.assertIn('1/2', error)
        self.assertEqual(json.loads(self.store.get_tweet(item['tweet_id'])['files_json']), [file])
        self.assertTrue(self.store.should_download(item['tweet_id'], True, 0, False))
        video.assert_not_called()

    def test_video_failure_retains_successful_images(self):
        file = self.image()
        with patch.object(self.downloader, 'download_images', return_value=[file]), \
             patch.object(self.downloader, 'download_video', side_effect=RuntimeError('video unavailable')):
            result = self.downloader.download_item({**self.item, 'media_ids': ['one'], 'has_video': True})
        self.assertEqual(result, ('failed', [file], 'video unavailable'))

    def test_skip_existing_done_tweet_does_not_probe_or_download(self):
        self.store.upsert_seen(self.item)
        self.store.mark_result(self.item['tweet_id'], 'done', [self.image()])
        with patch.object(self.downloader, 'download_images') as images, \
             patch.object(BrowserCollector, 'collect_single') as probe:
            self.assertEqual(self.downloader.download_item(self.item), ('skipped', [], ''))
        images.assert_not_called()
        probe.assert_not_called()

    def test_store_connections_commit_roll_back_and_close(self):
        with self.store.connection() as conn:
            conn.execute("insert into tweets(tweet_id, url, first_seen_at, updated_at) values('test', 'url', '', '')")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('select 1')
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.store.connection() as conn:
                conn.execute("delete from tweets where tweet_id='test'")
                raise RuntimeError('rollback')
        self.assertIsNotNone(self.store.get_tweet('test'))
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('select 1')

    def test_image_download_cleans_interrupted_and_empty_parts(self):
        target = self.root / 'test.jpg'
        response = MagicMock()
        response.status_code = 200
        response.headers = {'content-type': 'image/jpeg'}
        response.__enter__.return_value = response
        def interrupted(*args, **kwargs):
            yield b'partial'
            raise requests.ConnectionError('interrupted')
        response.iter_content.side_effect = interrupted
        with patch.object(self.downloader.session, 'get', return_value=response):
            with self.assertRaises(requests.ConnectionError):
                self.downloader._download_image('https://pbs.twimg.com/media/test', target)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix('.jpg.part').exists())
            response.iter_content.side_effect = None
            response.iter_content.return_value = iter([])
            self.assertFalse(self.downloader._download_image('https://pbs.twimg.com/media/test', target))
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix('.jpg.part').exists())

    def test_image_candidates_stop_after_success_and_keep_custom_config(self):
        custom = ['{media_id}.png?name=orig']
        self.config['media']['image_candidates'] = custom
        self.assertEqual(self.downloader._image_candidates('abc'), [('https://pbs.twimg.com/media/abc.png?name=orig', 'png')])
        self.config['media']['image_candidates'] = X_DEFAULT_CONFIG['media']['image_candidates']
        attempted = []
        def download(url, target):
            attempted.append(url)
            target.write_bytes(b'complete')
            return True
        with patch.object(self.downloader, '_download_image', side_effect=download):
            self.assertEqual(len(self.downloader.download_images({**self.item, 'media_ids': ['abc']})), 1)
        self.assertEqual(attempted, ['https://pbs.twimg.com/media/abc?format=jpg&name=orig'])


class XManualRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = json.loads(json.dumps(X_DEFAULT_CONFIG))
        config.update(database=str(self.root / 'state.sqlite3'), download_dir=str(self.root / 'downloads'),
                      cookie_file=str(self.root / 'cookies.txt'), request_delay_seconds=0, jitter_seconds=0,
                      retry_failed=False, max_download_attempts=1)
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        self.app = XApp(config_path)

    def failed(self, number, files=None):
        item = item_from_url(f'https://x.com/test/status/{number}')
        self.app.store.upsert_seen(item)
        self.app.store.mark_result(item['tweet_id'], 'failed', files or [], 'No video could be found in this tweet', 'manual_check')
        return item

    def test_bulk_retry_includes_records_beyond_display_limit_and_reserves_run(self):
        for number in range(1, 202):
            self.failed(number)
        self.assertEqual(len(self.app.store.manual_failed_tweets()), 200)
        with patch('x_auto_worker.threading.Thread') as thread, patch.object(self.app, '_run_manual_retries') as run:
            self.assertEqual(self.app.start_manual_retry_thread(), 201)
            self.assertTrue(self.app.run_lock.locked())
            with self.assertRaises(RuntimeError):
                self.app.start_manual_retry_thread('1')
            thread.call_args.kwargs['target']()
            self.assertEqual(len(run.call_args.args[0]), 201)
        self.app._release_run()

    def test_single_retry_rejects_missing_or_completed_records(self):
        self.failed(1)
        self.failed(2)
        self.app.store.mark_result('2', 'done', [], '', 'video')
        for tweet_id in ('2', 'missing'):
            with self.assertRaises(ValueError):
                self.app.start_manual_retry_thread(tweet_id)
            self.assertFalse(self.app.run_lock.locked())
        with patch('x_auto_worker.threading.Thread') as thread, patch.object(self.app, '_run_manual_retries') as run:
            self.assertEqual(self.app.start_manual_retry_thread('1'), 1)
            thread.call_args.kwargs['target']()
            self.assertEqual([row['tweet_id'] for row in run.call_args.args[0]], ['1'])
        self.app._release_run()

    def test_batch_continues_after_probe_failure_and_preserves_partial_files(self):
        saved = self.root / 'partial.jpg'
        saved.write_bytes(b'partial fixture')
        failed = self.failed(1, [str(saved)])
        success = self.failed(2)
        output = self.root / 'done.jpg'
        output.write_bytes(b'image fixture')
        def probe(url):
            if url == failed['url']:
                raise RuntimeError('probe unavailable')
            return {**success, 'media_ids': ['one'], '_detail_checked': True}
        downloader = Downloader(self.app.config, self.app.store, self.app.log)
        with patch('x_auto_worker.Downloader', return_value=downloader), \
             patch.object(BrowserCollector, 'collect_single', new=AsyncMock(side_effect=probe)) as collect, \
             patch.object(downloader, 'download_images', return_value=[str(output)]), \
             patch.object(downloader, 'download_video') as video:
            self.app._acquire_run('retrying_manual')
            self.app._run_manual_retries(self.app.store.manual_failed_tweets(limit=None))
        self.assertEqual(collect.await_count, 2)
        video.assert_not_called()
        self.assertFalse(self.app.run_lock.locked())
        self.assertEqual(self.app.store.get_tweet('2')['status'], 'done')
        row = self.app.store.get_tweet('1')
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(json.loads(row['files_json']), [str(saved)])
        self.assertEqual(row['error'], 'probe unavailable')
        self.assertEqual(len(self.app.store.manual_failed_tweets()), 1)
        self.assertEqual(self.app.get_progress()['download_done'], 2)

    def test_unsolved_download_stays_in_manual_list_and_deleted_rows_are_not_recreated(self):
        item = self.failed(1)
        self.failed(2)
        rows = self.app.store.manual_failed_tweets(limit=None)
        self.app.store.delete_tweet('2')
        with patch.object(BrowserCollector, 'collect_single', new=AsyncMock(return_value={**item, '_detail_checked': True})), \
             patch.object(Downloader, 'download_images', return_value=[]), \
             patch.object(Downloader, 'download_video') as video:
            self.app._acquire_run('retrying_manual')
            self.app._run_manual_retries(rows)
        self.assertIsNone(self.app.store.get_tweet('2'))
        self.assertEqual([r['tweet_id'] for r in self.app.store.manual_failed_tweets()], ['1'])
        self.assertEqual(self.app.get_progress()['skipped'], 1)
        video.assert_not_called()

    def test_http_retry_endpoints_and_delete_button(self):
        self.failed(1)
        server = ThreadingHTTPServer(('127.0.0.1', 0), x_make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            with patch.object(self.app, 'start_manual_retry_thread') as start:
                self.assertEqual(requests.post(base+'/manual-failed/retry', data={'tweet_id':'1'}, allow_redirects=False).status_code, 303)
                start.assert_called_with('1')
                self.assertEqual(requests.post(base+'/manual-failed/retry-all', allow_redirects=False).status_code, 303)
                start.assert_called_with(None)
                start.side_effect = RuntimeError('busy')
                self.assertEqual(requests.post(base+'/manual-failed/retry-all').status_code, 409)
                start.side_effect = ValueError('missing')
                self.assertEqual(requests.post(base+'/manual-failed/retry', data={'tweet_id':'999'}).status_code, 404)
            self.assertEqual(requests.post(base+'/manual-failed/retry', data={}).status_code, 400)
            self.assertEqual(requests.post(base+'/manual-failed/delete', data={'tweet_id':'1'}, allow_redirects=False).status_code, 303)
            self.assertIsNone(self.app.store.get_tweet('1'))
            page = x_html_page(self.app)
            for action in ('/manual-failed/retry-all', '/manual-failed/retry', '/manual-failed/delete'):
                self.assertIn(action, page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class XBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.collector = BrowserCollector(json.loads(json.dumps(X_DEFAULT_CONFIG)), XRingLog())
        self.page_manager = self.collector._browser_page([])
        self.page = await self.page_manager.__aenter__()

    async def asyncTearDown(self):
        await self.page_manager.__aexit__(None, None, None)

    async def load(self, contents):
        await self.page.route('https://x.com/fixture', lambda route: route.fulfill(
            content_type='text/html', body=contents))
        await self.page.goto('https://x.com/fixture')

    async def test_manual_retry_buttons_submit_and_disable_during_runs(self):
        state = {'running': False, 'manual_failed': [{'tweet_id': '123', 'url': 'https://x.com/test/status/123', 'author': 'test', 'attempts': 1, 'error': 'No video could be found'}]}
        submitted = []
        await self.page.route('https://x.com/api/status', lambda route: route.fulfill(content_type='application/json', body=json.dumps(state)))
        async def submit(route):
            submitted.append((route.request.url, route.request.post_data))
            await route.fulfill(content_type='text/html', body=x_html_page(None))
        await self.page.route('https://x.com/manual-failed/*', submit)
        await self.load(x_html_page(None))
        single = self.page.locator('form[action="/manual-failed/retry"] button')
        await single.wait_for()
        self.assertTrue(await single.is_enabled())
        self.assertTrue(await self.page.locator('#retryAllManual').is_enabled())
        self.assertTrue(await self.page.locator('form[action="/manual-failed/delete"] button').is_enabled())
        await single.click()
        await self.page.wait_for_url('https://x.com/manual-failed/retry')
        self.assertEqual(submitted[-1], ('https://x.com/manual-failed/retry', 'tweet_id=123'))
        await self.page.locator('#retryAllManual').click()
        await self.page.wait_for_url('https://x.com/manual-failed/retry-all')
        self.assertEqual(submitted[-1][0], 'https://x.com/manual-failed/retry-all')
        state['running'] = True
        await self.page.evaluate('refreshStatus()')
        self.assertTrue(await self.page.locator('#retryAllManual').is_disabled())
        self.assertTrue(await self.page.locator('form[action="/manual-failed/retry"] button').is_disabled())
        self.assertTrue(await self.page.locator('form[action="/manual-failed/delete"] button').is_disabled())

    async def test_responsive_multi_photos_survive_image_error_handlers(self):
        await self.load('''<article><a href="/test/status/123"><time>now</time></a>
          <p>Watch my Cosplay, 播放量</p>
          <div data-testid="tweetPhoto"><a href="/test/status/123/photo/1">
            <img src="https://pbs.twimg.com/media/HTYKoXTbMAEToXI.jpg?name=small"
                 onerror="this.remove()" onload="this.dataset.loaded='yes'">
          </a></div>
          <picture><source srcset="https://pbs.twimg.com/media/second_ID?format=png&amp;name=small 1x,
                        https://pbs.twimg.com/media/second_ID?format=png&amp;name=orig 2x">
            <img src="https://pbs.twimg.com/media/second_ID?format=png&amp;name=small"></picture>
          <img data-src="https://pbs.twimg.com/media/third-ID.png?name=orig">
          <img src="https://pbs.twimg.com/profile_images/avatar.jpg">
          </article>''')
        await self.page.wait_for_function("document.querySelector('img').dataset.loaded === 'yes'")
        rows = await self.collector._collect_visible(self.page)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['media_ids'], ['HTYKoXTbMAEToXI', 'second_ID', 'third-ID'])
        self.assertFalse(rows[0]['has_video'])
        self.assertTrue(await self.page.evaluate("document.querySelector('img').naturalWidth === 1"))

    async def test_sensitive_content_button_mounts_media_before_scan(self):
        await self.load('''<article><a href="/test/status/123"><time>now</time></a>
          <div data-testid="tweetPhoto">Sensitive content
          <button onclick="setTimeout(() => {this.parentNode.innerHTML =
            '<img src=&quot;https://pbs.twimg.com/media/revealed.jpg?name=orig&quot;>'}, 50)">Show</button>
          </div></article>''')
        rows = await self.collector._collect_visible(self.page)
        self.assertEqual(rows[0]['media_ids'], ['revealed'])
        self.assertFalse(rows[0]['has_video'])

    async def test_text_keywords_and_unrelated_show_buttons_are_not_media(self):
        await self.load('''<article><a href="/test/status/123"><time>now</time></a>
          <p>Play Watch Cosplay 播放</p>
          <button onclick="this.dataset.clicked='yes'">Show</button></article>''')
        row = (await self.collector._collect_visible(self.page))[0]
        self.assertEqual(row['media_ids'], [])
        self.assertFalse(row['has_video'])
        self.assertFalse(await self.page.evaluate("document.querySelector('button').dataset.clicked === 'yes'"))

    async def test_video_elements_and_responsive_video_thumbnails(self):
        await self.load('''<article><a href="/test/status/123"><time>now</time></a><video></video></article>
          <article><a href="/test/status/124"><time>now</time></a><div data-testid="videoPlayer"></div></article>
          <article><a href="/test/status/125"><time>now</time></a>
            <img srcset="https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/thumb.jpg 1x"></article>''')
        rows = await self.collector._collect_visible(self.page)
        self.assertEqual([row['tweet_id'] for row in rows], ['123', '124', '125'])
        self.assertTrue(all(row['has_video'] for row in rows))
        self.assertTrue(all(not row['media_ids'] for row in rows))


class PixivNetworkTests(unittest.TestCase):
    def test_concat_paths_escape_apostrophes_and_resolve_relative_directories(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            folder = Path(tmp).relative_to(ROOT) / "artist's frames"
            folder.mkdir()
            (folder / "frame's.png").write_bytes(b'image fixture')
            target = folder / 'out.gif'
            downloader = PixivDownloader.__new__(PixivDownloader)
            downloader.config = {}
            def ffmpeg(_command, **_kwargs):
                target.write_bytes(b'gif fixture')
                return MagicMock(returncode=0)
            with patch('pixiv_auto_worker.shutil.which', return_value='ffmpeg'), \
                 patch('pixiv_auto_worker.subprocess.run', side_effect=ffmpeg):
                downloader.convert_ugoira_to_gif(folder, [{'file': "frame's.png", 'delay': 80}], target)
            expected = (folder / "frame's.png").resolve().as_posix().replace("'", "'\\''")
            self.assertEqual((folder / 'frames.txt').read_text(encoding='utf-8'),
                             f"file '{expected}'\nduration 0.080\nfile '{expected}'\n")

    def test_ugoira_metadata_cannot_reference_frames_outside_extracted_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'frames'
            folder.mkdir()
            (root / 'outside.png').write_bytes(b'image fixture')
            downloader = PixivDownloader.__new__(PixivDownloader)
            downloader.config = {}
            with patch('pixiv_auto_worker.shutil.which', return_value='ffmpeg'), \
                 patch('pixiv_auto_worker.subprocess.run') as run:
                with self.assertRaisesRegex(RuntimeError, 'invalid frame path'):
                    downloader.convert_ugoira_to_gif(folder, [{'file': '../outside.png'}], folder / 'out.gif')
            run.assert_not_called()

    def test_classifies_transient_network_errors(self) -> None:
        self.assertEqual(classify_error(requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")), "network")
        self.assertEqual(classify_error(PixivError("requests POST https://oauth.secure.pixiv.net/auth/token error")), "network")
        response = requests.Response()
        response.status_code = 429
        self.assertEqual(classify_error(requests.exceptions.HTTPError("too many requests", response=response)), "rate_limit")

    def test_rejects_unsafe_zip_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", b"nope")
            out_dir = root / "out"
            out_dir.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(RuntimeError):
                    safe_extract_zip(archive, out_dir)


if __name__ == "__main__":
    unittest.main()
