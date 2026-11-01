import asyncio
import copy
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import app as server
from core.config import sanitize
from core.store import Store
from core.x_errors import MAX_ATTEMPTS, migrate_attempts, terminal
from workers import platform_job as jobs
from test_stopping import x_module

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_src/x-auto-download-nas-main'))
import x_auto_worker as x


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    store = Store(tmp_path / 'core.sqlite3')
    monkeypatch.setattr(server, 'store', store)
    class Client:
        def request(self, path, payload=None):
            if path.startswith('/internal/record/x/'):
                return server.get_record('x', path.rsplit('/', 1)[-1])
            if path.startswith('/internal/checkpoint/'):
                return {'head': ''}
            if path.startswith('/internal/failed/'):
                return store.page('records', 1, 100, 'x', 'failed')
            raise AssertionError(path)
    monkeypatch.setattr(jobs, 'Client', Client)
    monkeypatch.setattr(jobs, 'record', lambda platform, identifier, state, files, metadata: server.record(dict(platform=platform, source_id=identifier, state=state, files=files, metadata=metadata)))
    return store, Client()


def test_three_retries_then_stop_survives_restart_and_dismissal(catalog, tmp_path):
    store, client = catalog
    native = x.Store(tmp_path / 'x.sqlite3', jobs.Log())
    item = x.item_from_url('https://x.com/user/status/123')
    native.upsert_seen(item)
    jobs.bind_catalog('x', native, None, tmp_path, {'retry_failed': True, 'max_download_attempts': MAX_ATTEMPTS})
    for attempt in range(1, 5):
        assert native.should_download('123', True, MAX_ATTEMPTS, False)
        native.mark_result('123', 'failed', [], 'unavailable')
        metadata = client.request('/internal/record/x/123')['metadata']
        assert metadata['attempts'] == attempt
        assert terminal(metadata) == (attempt == 4)
    assert not native.should_download('123', True, MAX_ATTEMPTS, False)
    assert jobs.failed_urls('x', True) == []
    assert store.page('records', errors=True)['total'] == 1
    server.dismiss_x_error('123')
    server.store = Store(store.path)
    assert server.store.page('records', errors=True)['total'] == 0
    # A late receipt must not recreate an error the user already dismissed.
    server.record(dict(platform='x', source_id='123', state='failed', files=[], metadata=metadata))
    assert server.store.page('records', errors=True)['total'] == 0
    assert not native.should_download('123', True, MAX_ATTEMPTS, False)


def test_existing_failures_backfill_without_reset_or_import_downloads(catalog, tmp_path):
    store, client = catalog
    native = x.Store(tmp_path / 'x.sqlite3', jobs.Log())
    for identifier, attempts in [('123', 6), ('124', 2)]:
        item = x.item_from_url('https://x.com/user/status/' + identifier)
        native.upsert_seen(item)
        for _ in range(attempts):
            native.mark_result(identifier, 'failed', [], 'unavailable')
        server.record(dict(platform='x', source_id=identifier, state='failed', files=[], metadata={'url': item['url'], 'imported': True}))
    migrate_attempts(store, {'database': str(native.db_path)})
    assert store.page('records', errors=True)['total'] == 1
    assert client.request('/internal/record/x/123')['metadata']['attempts'] == 6
    assert jobs.failed_urls('x', True) == []
    server.dismiss_x_error('123')
    migrate_attempts(Store(store.path), {'database': str(native.db_path)})
    assert store.page('records', errors=True)['total'] == 0


def test_likes_url_does_not_require_screen_name(monkeypatch):
    mod = x_module([['123']], [])
    resolver = AsyncMock(side_effect=AssertionError('configured URL must be used'))
    monkeypatch.setattr(mod.BrowserCollector, '_resolve_screen_name', resolver, raising=False)
    monkeypatch.setattr(jobs, 'lookup', lambda *args: {})
    head, outcome = asyncio.run(jobs.x_collect(mod, {'browser': {'likes_url': 'https://x.com/user/likes'}}, None, '123', jobs.Log(), lambda *args: None))
    assert outcome['stop_reason'] == 'checkpoint'
    resolver.assert_not_called()


def test_deleted_image_staging_directory_is_recreated(tmp_path):
    config = copy.deepcopy(x.DEFAULT_CONFIG)
    config['download_dir'] = str(tmp_path)
    downloader = x.Downloader(config, None, jobs.Log())
    response = SimpleNamespace(status_code=200, headers={'content-type': 'image/jpeg'}, iter_content=lambda **kwargs: [b'image'])
    class Response:
        def __enter__(self): return response
        def __exit__(self, *args): pass
    def get(*args, **kwargs):
        # Reproduce cleanup after directory discovery, before the HTTP body is saved.
        shutil.rmtree(tmp_path / 'images')
        return Response()
    downloader.session.get = get
    files = downloader.download_images({'tweet_id': '123', 'author': 'user', 'media_ids': ['abc']})
    assert len(files) == 1 and Path(files[0]).read_bytes() == b'image'
    assert not list(tmp_path.rglob('*.part'))
    downloader.close()


def test_gallery_fallback_downloads_when_no_browser_card(tmp_path, monkeypatch):
    config = copy.deepcopy(x.DEFAULT_CONFIG)
    config.update(download_dir=str(tmp_path), gallery_fallback=True)
    config['browser']['enabled'] = False
    native = x.Store(tmp_path / 'x.sqlite3', jobs.Log())
    downloader = x.Downloader(config, native, jobs.Log())
    monkeypatch.setattr(x, 'cookie_file_for_tools', lambda config: tmp_path / 'private-cookies')
    def run(command, **kwargs):
        assert command[0] == 'gallery-dl' and '--config-ignore' in command
        output = Path(command[command.index('-D') + 1]) / 'image.jpg'
        output.write_bytes(b'image')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(x.subprocess, 'run', run)
    result = downloader.download_item({**x.item_from_url('https://x.com/user/status/123'), '_detail_checked': True})
    assert result[0] == 'done' and len(result[1]) == 1
    assert Path(result[1][0]).read_bytes() == b'image'
    downloader.close()


def test_single_page_failure_counts_and_continues_other_links(catalog, tmp_path, monkeypatch):
    config = copy.deepcopy(x.DEFAULT_CONFIG)
    config.update(database=str(tmp_path / 'x.sqlite3'), download_dir=str(tmp_path), request_delay_seconds=0)
    monkeypatch.setattr(jobs, 'module', lambda platform: x)
    monkeypatch.setattr(x, 'load_config', lambda path: config)
    monkeypatch.setattr(jobs, 'staging_config', lambda *args: (None, tmp_path))
    calls = []
    async def collect(self, url):
        calls.append(url)
        raise RuntimeError('page timeout')
    monkeypatch.setattr(x.BrowserCollector, 'collect_single', collect)
    result = jobs.x_job({'id': 'job', 'kind': 'retry', 'payload': {'urls': ['https://x.com/user/status/123', 'https://x.com/user/status/124'], 'automatic': True}})
    assert result['failed'] == 2 and len(calls) == 2
    assert catalog[1].request('/internal/record/x/123')['metadata']['attempts'] == 1


def test_error_summary_redacts_cookie_values():
    assert 'private-csrf' not in sanitize('ct0=private-csrf; auth_token=private-auth')
    assert 'private-auth' not in sanitize('ct0=private-csrf; auth_token=private-auth')
