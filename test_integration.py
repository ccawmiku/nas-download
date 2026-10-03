import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "_integrated"))
from nas_auto.adapters import ServiceAdapterRegistry
from nas_auto.proxy import stream_proxy
from nas_auto.supervisor import ProcessSupervisor


class IntegrationTests(unittest.TestCase):
    def test_telegram_summary_uses_service_dns_and_internal_auth(self):
        registry = ServiceAdapterRegistry({"telegram": {"name": "Telegram", "host": "telegram-worker",
                                                       "port": 8000, "path": "/telegram/"}})
        adapter = registry["telegram"]
        self.assertEqual(adapter.host, "telegram-worker")
        with patch.dict(os.environ, {"INTERNAL_API_TOKEN": "test-internal-token"}), patch.object(adapter, "request", return_value=(200, b'{"connected":true,"running":false}')) as request:
            self.assertTrue(adapter.status()["connected"])
            self.assertEqual(request.call_args.kwargs["headers"]["X-NAS-Download-Token"], "test-internal-token")
        with patch.dict(os.environ, {"INTERNAL_API_TOKEN": ""}), patch.object(adapter, "request") as request:
            self.assertEqual(adapter.status(), {})
            request.assert_not_called()

    def test_worker_restarts_without_restarting_console(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "started"
            code = "import pathlib,time; p=pathlib.Path('started'); existed=p.exists(); p.touch(); time.sleep(30 if existed else 0)"
            supervisor = ProcessSupervisor(lambda _line: None)
            try:
                supervisor.start("worker", [sys.executable, "-u", "-c", code], temporary)
                deadline = time.monotonic() + 5
                while supervisor.children["worker"].process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                supervisor.tick()
                self.assertEqual(supervisor.state()[0]["returncode"], 0)
                self.assertEqual(supervisor.state()[0]["restart_count"], 1)
                supervisor.children["worker"].restart_at = 0
                supervisor.tick()
                self.assertIsNone(supervisor.state()[0]["returncode"])
                self.assertTrue(marker.exists())
            finally:
                supervisor.stop(timeout=3)

    def test_media_proxy_streams_range_and_preserves_cookies(self):
        class Response:
            status = 206
            def __init__(self): self.remaining = 150_000; self.read_sizes = []
            def getheader(self, key, default=None):
                return {"Content-Type": "video/mp4", "Content-Length": "150000"}.get(key, default)
            def getheaders(self): return [("Content-Type", "video/mp4"), ("Content-Range", "bytes 0-149999/200000"), ("Content-Length", "150000")]
            def read(self, size=None):
                self.read_sizes.append(size)
                if size is None: raise AssertionError("media must be streamed")
                count = min(size, self.remaining); self.remaining -= count; return b"x" * count
        class Handler:
            command = "GET"
            headers = {"Range": "bytes=0-149999", "Cookie": "session=test", "X-NAS-Download-Token": "browser-spoof"}
            rfile = io.BytesIO(); wfile = io.BytesIO()
            def __init__(self): self.sent_headers = {}
            def send_response(self, status): self.status = status
            def send_header(self, key, value): self.sent_headers[key] = value
            def end_headers(self): pass
        response = Response(); handler = Handler()
        with patch("nas_auto.proxy.http.client.HTTPConnection") as factory:
            connection = factory.return_value; connection.getresponse.return_value = response
            stream_proxy(handler, "telegram-worker", 8000, "/files/videos/test.mp4")
            headers = connection.request.call_args.kwargs["headers"]
            self.assertEqual(headers["Range"], "bytes=0-149999")
            self.assertEqual(headers["Cookie"], "session=test")
            self.assertNotIn("X-NAS-Download-Token", headers)
        self.assertEqual(handler.status, 206)
        self.assertEqual(len(handler.wfile.getvalue()), 150_000)
        self.assertTrue(all(value <= 64 * 1024 for value in response.read_sizes))
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes 0-149999/200000")

    def test_compose_preserves_every_original_media_mapping(self):
        import yaml
        services = yaml.safe_load((Path(__file__).parent / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
        expected = {"console": {"/volume1/se-v/x-v:/downloads/x/videos", "/volume1/se-p/x-p:/downloads/x/images", "/volume1/se-p/pixiv:/downloads/pixiv", "/volume1/se-p/小红书:/xhs", "/volume1/douyin:/douyin"},
                    "telegram-worker": {"/volume1/se-p/tele-p:/downloads/images", "/volume1/se-v/tele-v:/downloads/videos", "/volume1/se-v/tele-files:/downloads/files"}}
        for service, mappings in expected.items(): self.assertTrue(mappings.issubset(set(services[service]["volumes"])))


if __name__ == "__main__": unittest.main()
