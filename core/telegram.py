"""Project Telegram download failures into the shared error catalog."""
import json
import time
from datetime import datetime, timezone

from .config import sanitize


def apply_history(store, items):
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("Telegram 历史格式无效")
    valid = []
    for item in items:
        try:
            identifier = item['id']
            stamp = datetime.fromisoformat(item['updated_at'])
            updated = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200 or not 0 < updated <= time.time() + 60:
                raise ValueError()
            if item['status'] not in {'queued','downloading','paused','retrying','verifying','processing','complete','failed','interrupted','cancelled','deleted'}:
                raise ValueError()
            valid.append((item, identifier, updated))
        except (KeyError, TypeError, ValueError):
            raise ValueError("Telegram 历史格式无效") from None
    latest = store.get('telegram_last_success', 0)
    with store.connect() as db:
        for item, identifier, updated in valid:
            status = item['status']
            if status == 'complete':
                latest = max(latest, updated)
            previous = db.execute("SELECT * FROM records WHERE platform='telegram' AND source_id=?", (identifier,)).fetchone()
            metadata = json.loads(previous['metadata']) if previous else {}
            # Replayed receipts must not overwrite a newer media reconciliation.
            if updated <= metadata.get('telegram_history_updated', 0):
                continue
            if status in {'failed', 'interrupted'}:
                chat = str(item.get('chat_id', ''))
                message = item.get('message_id')
                url = f"https://t.me/c/{chat[4:]}/{message}" if chat.startswith('-100') and chat[4:].isdigit() and type(message) is int and message > 0 else ''
                metadata.update(
                    telegram_history_error=True, telegram_history_updated=updated,
                    url=url, title=str(item.get('file_name', 'Telegram 媒体'))[:300],
                    error=sanitize(item.get('error', '下载失败'))[:2000],
                    attempts=int(item.get('retry_count', 0)) + 1,
                )
                db.execute(
                    "INSERT INTO records VALUES('telegram',?,?,?,?,?) ON CONFLICT(platform,source_id) "
                    "DO UPDATE SET state=excluded.state,metadata=excluded.metadata,updated=excluded.updated",
                    (identifier, status, previous['files'] if previous else '[]', json.dumps(metadata, ensure_ascii=False), updated),
                )
            elif previous and metadata.get('telegram_history_error'):
                metadata.pop('error', None)
                metadata.pop('error_dismissed', None)
                metadata['telegram_history_updated'] = updated
                # A retry is no longer an unresolved failure. Keep archive ownership
                # with the media pipeline while it is still processing.
                state = status if status in {'complete', 'processing', 'deleted', 'cancelled'} else 'processing'
                db.execute("UPDATE records SET state=?,metadata=?,updated=? WHERE platform='telegram' AND source_id=?", (state, json.dumps(metadata, ensure_ascii=False), updated, identifier))
    store.set('telegram_last_success', latest)
