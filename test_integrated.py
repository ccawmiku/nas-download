import sys
import tempfile
import unittest
import zipfile
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

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
)
from pixiv_auto_worker import classify_error, safe_extract_zip
from xhs_auto_worker import (
    RingLog as XhsRingLog,
    Store as XhsStore,
    cookie_summary_from_settings,
    is_transient_xhs_failure,
    save_settings_cookie,
    sync_downloader_settings,
    xhs_api_response_has_failure,
    xhs_api_segment_has_failure,
)
from x_auto_worker import browser_scroll_limit


class IntegratedPageTests(unittest.TestCase):
    def test_home_page_includes_version_and_service_cards(self) -> None:
        index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")
        styles = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('<div id="root"></div>', index)
        self.assertIn("运行总览", app)
        self.assertIn("小红书", app)
        self.assertIn("Pixiv", app)
        self.assertIn("抖音", app)
        self.assertIn("任务中心", app)
        self.assertIn("运行日志", app)
        self.assertIn('aria-label="折叠侧边栏"', app)
        self.assertIn("grid-template-columns", styles)
        self.assertIn("@media(max-width:720px)", styles)

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
        self.assertEqual(normalized, "sessionid=abc; ttwid=def")
        summary = cookie_summary(normalized)
        self.assertEqual(summary["fields"], 2)
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


class PixivNetworkTests(unittest.TestCase):
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
