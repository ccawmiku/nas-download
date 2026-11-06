import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workers import telegram_delivery
from core.config import read_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/telegram'))
from app.history import DownloadHistory, DownloadRecord


def test_history_pages_same_second_by_id_and_excludes_open_seconds(tmp_path, monkeypatch):
    history = DownloadHistory(tmp_path / 'downloads.json')
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec='seconds')
    for identifier in ['a', 'b', 'c']:
        history.add(DownloadRecord(identifier, 42, -1001234, identifier + '.jpg', '/private/path', status='failed', updated_at=stamp))
    history.add(DownloadRecord('open', 42, -1001234, 'open.jpg', '/private/path'))
    through = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec='seconds')
    first = history.core_page(['', ''], through, limit=2)
    assert [row['id'] for row in first] == ['a', 'b']
    assert 'path' not in first[0] and 'notification_chat_id' not in first[0]
    second = history.core_page([stamp, 'b'], through, limit=2)
    assert [row['id'] for row in second] == ['c']
    calls = []
    class Client:
        def request(self, path, payload):
            calls.append((path, payload))
    monkeypatch.setattr(telegram_delivery, 'Client', Client)
    telegram_delivery.sync_history(history)
    telegram_delivery.sync_history(history)
    assert len(calls) == 1 and calls[0][0] == '/internal/telegram/history'
    assert read_json(tmp_path / 'core-history-cursor.json')['cursor'] == [stamp, 'c']


def test_failed_delivery_does_not_advance_history_cursor(tmp_path, monkeypatch):
    import pytest
    history = DownloadHistory(tmp_path / 'downloads.json')
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec='seconds')
    history.add(DownloadRecord('a', 42, -1001234, 'a.jpg', '/private/path', updated_at=stamp))
    class Client:
        def request(self, path, payload):
            raise RuntimeError('disconnected')
    monkeypatch.setattr(telegram_delivery, 'Client', Client)
    with pytest.raises(RuntimeError):
        telegram_delivery.sync_history(history)
    assert not (tmp_path / 'core-history-cursor.json').exists()
