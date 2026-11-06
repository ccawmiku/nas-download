import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from core import app as server
from core import notifications
from core.config import DEFAULTS, sanitize, write_json
from core.store import PLATFORMS, Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'store', Store(tmp_path / 'state.sqlite3'))
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'TOKEN', 'test-key')
    monkeypatch.setenv('NAS_SCHEDULER', 'false')
    monkeypatch.setenv('NAS_INITIALIZE_CONFIGS', 'false')
    monkeypatch.setenv('NAS_MEDIA_ROOTS', str(tmp_path))
    server.store.set('preferences', {**DEFAULTS, 'workspace_root': str(tmp_path)})
    with TestClient(server.app) as client:
        yield client


def test_all_platform_errors_filter_hide_and_keep_media(client, tmp_path):
    media = tmp_path / 'existing.jpg'
    media.write_bytes(b'keep')
    for key in PLATFORMS:
        server.record(dict(platform=key, source_id='same', state='failed', files=[str(media)],
                           metadata={'retry_exhausted': True, 'error': 'cookie=private-secret'}))
    server.record(dict(platform='x', source_id='still-retrying', state='failed', metadata={}))
    assert client.get('/api/errors').json()['total'] == 5
    assert 'private-secret' not in client.get('/api/errors').text
    for key in PLATFORMS:
        response = client.get('/api/errors', params={'platform': key}).json()
        assert response['total'] == 1 and response['items'][0]['platform'] == key
        assert client.delete(f'/api/errors/{key}/same').status_code == 200
        assert client.delete(f'/api/errors/{key}/same').status_code == 404
    assert client.get('/api/errors').json()['total'] == 0
    assert media.read_bytes() == b'keep'
    # A duplicate failure receipt must not resurrect a hidden row. A later,
    # genuinely new failure after recovery must be visible for non-X platforms.
    for state, expected in [('failed', 0), ('complete', 0), ('failed', 1)]:
        server.record(dict(platform='pixiv', source_id='same', state=state, metadata={}))
        total = client.get('/api/errors?platform=pixiv').json()['total']
        assert total == expected
    assert client.get('/api/errors?platform=unknown').status_code == 422


def test_logo_is_served_as_an_image_instead_of_spa_html(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'STATIC', tmp_path)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" />'
    (tmp_path / 'logo.svg').write_text(svg)
    response = client.get('/logo.svg')
    assert response.status_code == 200 and response.text == svg
    assert response.headers['content-type'].startswith('image/svg+xml')
    assert client.get('/favicon.png').status_code == 404


def test_last_success_uses_sync_not_test_or_partial_failure(client):
    for kind, state, stamp in [('sync', 'complete', 100), ('test', 'complete', 200), ('sync', 'failed', 300)]:
        identifier, _ = server.store.enqueue('x', kind)
        with server.store.connect() as db:
            db.execute('UPDATE tasks SET state=?,updated=? WHERE id=?', (state, stamp, identifier))
    server.store.set('telegram_last_success', 500)
    data = client.get('/api/overview').json()
    platforms = {row['key']: row for row in data['platforms']}
    assert platforms['x']['last_success'] == 100
    assert platforms['telegram']['last_success'] == 500
    assert 'instagram' not in platforms
    assert platforms['douyin']['last_success'] is None


def test_instagram_retirement_stops_old_tasks_without_removing_media(client, tmp_path):
    media = tmp_path / 'saved-instagram.jpg'
    media.write_bytes(b'preserve')
    with server.store.connect() as db:
        db.execute("INSERT INTO tasks(id,platform,kind,payload,state,created,updated) VALUES('retired','instagram','sync','{}','queued',1,1)")
        db.execute("INSERT INTO records VALUES('instagram','archived','complete',?,'{}',1)", (json.dumps([str(media)]),))
    assert server.store.claim('tasks', 'old-engine', ['instagram']) is None
    assert client.post('/api/tasks/retired/retry', json={}).status_code == 409
    server.retire_instagram()
    with server.store.connect() as db:
        assert db.execute("SELECT state FROM tasks WHERE id='retired'").fetchone()[0] == 'cancelled'
        assert db.execute("SELECT COUNT(*) FROM records WHERE platform='instagram'").fetchone()[0] == 1
    assert client.get('/api/records').json()['total'] == 0
    assert media.read_bytes() == b'preserve'


def test_bark_settings_are_private_validated_and_preserve_saved_url(client, tmp_path):
    url = 'https://api.day.app/test-private-device/'
    response = client.patch('/api/notifications', json={'url': url, 'enabled': True})
    assert response.status_code == 200 and response.json()['configured']
    assert 'test-private-device' not in response.text
    assert 'test-private-device' not in client.get('/api/settings').text
    assert client.patch('/api/notifications', json={'enabled': False}).status_code == 200
    assert json.loads((tmp_path / 'bark.json').read_text())['url'] == url
    for bad in ['http://api.day.app/test-private-device/', 'https://localhost/private-key/', 'https://api.day.app/short', 'https://api.day.app/test-private-device/?key=other']:
        assert client.patch('/api/notifications', json={'url': bad, 'enabled': True}).status_code == 422
    assert client.patch('/api/notifications', json={'enabled': 'yes'}).status_code == 422
    assert 'test-private-device' not in sanitize('HTTP error https://api.day.app/test-private-device/failed')


def test_notification_grace_daily_digest_reminder_and_restart(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.sqlite3')
    path = tmp_path / 'bark.json'
    write_json(path, {'enabled': True, 'url': 'https://api.day.app/test-private-device/'})
    issues = {'errors:x': 'X 有待处理错误'}
    monkeypatch.setattr(notifications, 'incidents', lambda *args: dict(issues))
    sent = []
    sender = lambda url, body: sent.append(body)
    def tick(now):
        notifications.tick(Store(store.path), path, {}, now=now, sender=sender)
    tick(1000)
    tick(1599)
    assert not sent
    tick(1600)
    assert sent == ['X 有待处理错误']
    # New incidents wait for the next daily digest; already notified issues
    # remain suppressed for 72 hours even across process restarts.
    issues['disk:1'] = '磁盘空间不足'
    tick(2000)
    tick(2600)
    assert len(sent) == 1
    tick(1600 + notifications.DAY)
    assert sent[-1] == '磁盘空间不足' and len(sent) == 2
    tick(1600 + 2 * notifications.DAY)
    assert len(sent) == 2
    tick(1600 + 3 * notifications.DAY)
    assert sent[-1] == 'X 有待处理错误' and len(sent) == 3
    issues.clear()
    tick(1600 + 4 * notifications.DAY)
    assert len(sent) == 3  # Recovery stays silent.


def test_failed_notification_retries_slowly_without_leaking_url(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.sqlite3')
    path = tmp_path / 'bark.json'
    url = 'https://api.day.app/test-private-device/'
    write_json(path, {'enabled': True, 'url': url})
    monkeypatch.setattr(notifications, 'incidents', lambda *args: {'error:x': 'X 错误'})
    attempts = []
    def fail(url, body):
        attempts.append(1)
        raise RuntimeError(url)
    for now in [1000, 1600, 1900, 2200, 5199, 5200]:
        notifications.tick(store, path, {}, now=now, sender=fail)
    assert len(attempts) == 2
    assert url not in json.dumps(store.get(notifications.STATE_KEY))


def test_notifications_skip_legacy_errors_and_require_three_executions(client, tmp_path, monkeypatch):
    monkeypatch.setattr(notifications.shutil, 'disk_usage', lambda path: type('Usage', (), dict(total=100 * 1024**3, free=50 * 1024**3)))
    server.record(dict(platform='pixiv', source_id='legacy', state='failed', metadata={}))
    with server.store.connect() as db:
        db.execute('UPDATE records SET updated=500')
    for stamp in [100, 200, 1100]:
        identifier, _ = server.store.enqueue('pixiv')
        with server.store.connect() as db:
            db.execute("UPDATE tasks SET state='failed',updated=? WHERE id=?", (stamp, identifier))
    issues = notifications.incidents(server.store, {'enabled_at': 1000}, server.settings(), 1200)
    assert 'errors:pixiv' not in issues
    assert 'execution:pixiv' in issues
    server.record(dict(platform='pixiv', source_id='new', state='failed', metadata={}))
    assert 'errors:pixiv' in notifications.incidents(server.store, {'enabled_at': 1000}, server.settings(), 1200)


def test_bark_uses_json_key_and_passive_grouped_notification(monkeypatch):
    real_client = httpx.Client
    def accept(request):
        assert str(request.url) == 'https://api.day.app/push'
        payload = json.loads(request.content)
        assert payload['device_key'] == 'test-private-device'
        assert payload['level'] == 'passive' and payload['group'] == 'NAS自动下载'
        return httpx.Response(200, json={'code': 200})
    monkeypatch.setattr(notifications.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(accept), **kwargs))
    notifications.send('https://api.day.app/test-private-device/', '汇总')


def test_service_monitor_ignores_replaced_containers_and_checks_space(client, monkeypatch):
    monkeypatch.setattr(notifications.shutil, 'disk_usage', lambda path: type('Usage', (), dict(total=100 * 1024**3, free=4 * 1024**3)))
    with server.store.connect() as db:
        db.execute('INSERT INTO workers VALUES(?,?,?,?)', ('old-engine', '["x"]', 100, '{}'))
        db.execute('INSERT INTO workers VALUES(?,?,?,?)', ('new-engine', '["x"]', 1990, '{}'))
        db.execute('INSERT INTO workers VALUES(?,?,?,?)', ('telegram', '["telegram"]', 1990, '{"connection_status":"credential_error"}'))
    issues = notifications.incidents(server.store, {'enabled_at': 1000}, server.settings(), 2000)
    assert 'service:x' not in issues
    assert 'telegram:credentials' in issues
    assert any(key.startswith('disk:') for key in issues)
    issues = notifications.incidents(server.store, {'enabled_at': 1000}, server.settings(), 2100)
    assert 'service:x' in issues


def test_telegram_failure_projection_recovery_and_old_receipt(client):
    headers = {'X-NAS-Download-Token': 'test-key'}
    now = int(time.time()) - 10
    def post(status, stamp):
        item = dict(id='telegram-test', status=status, updated_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(), chat_id=-1001234567, message_id=42, retry_count=3, error='api_hash=private-hash')
        assert client.post('/internal/telegram/history', headers=headers, json={'items': [item]}).status_code == 200
    post('failed', now)
    row = client.get('/api/errors?platform=telegram').json()['items'][0]
    assert row['metadata']['url'] == 'https://t.me/c/1234567/42'
    assert 'private-hash' not in json.dumps(row)
    assert row['updated'] == now
    assert client.delete('/api/errors/telegram/telegram-test').status_code == 200
    post('failed', now)
    assert client.get('/api/errors?platform=telegram').json()['total'] == 0
    post('retrying', now + 1)
    post('failed', now + 2)
    assert client.get('/api/errors?platform=telegram').json()['total'] == 1
    post('complete', now + 3)
    post('failed', now + 2)
    assert client.get('/api/errors?platform=telegram').json()['total'] == 0
    assert server.store.get('telegram_last_success') == now + 3
    assert client.post('/internal/telegram/history', json={'items': []}).status_code == 401
