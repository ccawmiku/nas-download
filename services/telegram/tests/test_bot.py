import asyncio
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import ChatWriteForbiddenError
from telethon.tl.types import MessageMediaWebPage

from app.bot import BotManager, DownloadJob, RuntimeControls, parse_pause_seconds
from app.config import Settings
from app.history import DownloadHistory, DownloadRecord


class FakeStatus:
    def __init__(self):
        self.edits = []

    async def edit(self, text):
        self.edits.append(text)


class FakeMessage:
    def __init__(self, message_id=1, failures=0, payload=b"payload"):
        self.id = message_id
        self.chat_id = 123
        self.media = object()
        self.document = None
        self.failures = failures
        self.payload = payload
        self.calls = 0

    async def download_media(self, file, progress_callback):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("temporary network error")
        Path(file).write_bytes(self.payload)
        await progress_callback(len(self.payload), len(self.payload))
        return file


class FakeEvent:
    replies = 0

    def __init__(self, message):
        self.message = message
        self.chat_id = message.chat_id

    async def reply(self, _):
        type(self).replies += 1
        await asyncio.sleep(0.01)
        return FakeStatus()


def make_settings(tmp_path, retries=3, queue_size=10):
    return Settings.from_json_dict(
        {
            "download_dir": str(tmp_path / "downloads"),
            "image_download_dir": str(tmp_path / "downloads" / "images"),
            "video_download_dir": str(tmp_path / "downloads" / "videos"),
            "file_download_dir": str(tmp_path / "downloads" / "files"),
            "session_dir": str(tmp_path / "sessions"),
            "config_dir": str(tmp_path / "config"),
            "max_auto_retries": retries,
            "queue_maxsize": queue_size,
        }
    )


def make_manager(tmp_path, retries=3):
    settings = make_settings(tmp_path, retries)
    settings.ensure_dirs()
    history = DownloadHistory(settings.config_dir / "downloads.json")
    manager = BotManager(history)
    manager.settings = settings
    manager.retry_delay = lambda _: 0
    return manager, settings, history


@pytest.mark.asyncio
async def test_reconnect_releases_retained_exception_buffers_and_backs_off(tmp_path, monkeypatch):
    manager, _, _ = make_manager(tmp_path)
    retained = OSError("disconnected")
    buffers = []
    waits = []

    class Buffer:
        pass

    class Client:
        def is_connected(self):
            return True

        async def run_until_disconnected(self):
            buffer = Buffer()
            buffer.data = bytearray(1024 * 1024)
            buffers.append(weakref.ref(buffer))
            raise retained

    async def sleep(delay):
        waits.append(delay)
        assert retained.__traceback__ is None
        assert all(ref() is None for ref in buffers)
        if len(waits) == 6:
            manager._stopping = True

    monkeypatch.setattr("app.bot.asyncio.sleep", sleep)
    await manager._run_until_disconnected(Client())
    assert waits == [3, 6, 12, 24, 30, 30]
    assert manager.last_error == "OSError: disconnected"


@pytest.mark.asyncio
async def test_reconnect_resets_backoff_after_stable_connection(tmp_path, monkeypatch):
    manager, _, _ = make_manager(tmp_path)
    clock = [100.0]
    waits = []

    class Client:
        def is_connected(self):
            return True

        async def run_until_disconnected(self):
            if len(waits) == 2:
                clock[0] += 61
            raise OSError("disconnected")

    async def sleep(delay):
        waits.append(delay)
        if len(waits) == 3:
            manager._stopping = True

    monkeypatch.setattr("app.bot.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.bot.asyncio.sleep", sleep)
    await manager._run_until_disconnected(Client())
    assert waits == [3, 6, 3]


def test_pause_duration_parser_and_runtime_controls():
    assert parse_pause_seconds("30m") == 1800
    assert parse_pause_seconds("2h") == 7200
    assert parse_pause_seconds("15s") == 15
    assert parse_pause_seconds("bad") is None

    controls = RuntimeControls()
    controls.set_limit_mb(2)
    assert controls.state()["speed_limit_bytes_per_second"] == 2 * 1024 * 1024
    controls.clear_limit()
    assert controls.state()["speed_limit_bytes_per_second"] is None
    controls.pause(None)
    assert controls.is_paused()
    controls.resume()
    assert not controls.is_paused()


@pytest.mark.asyncio
async def test_limit_off_interrupts_an_in_progress_throttle():
    controls = RuntimeControls()
    controls.set_limit_mb(1)
    throttling = asyncio.create_task(controls.throttle(5 * 1024 * 1024))
    await asyncio.sleep(0.01)

    controls.clear_limit()

    await asyncio.wait_for(throttling, timeout=0.5)


@pytest.mark.asyncio
async def test_download_retries_then_completes(tmp_path):
    manager, settings, history = make_manager(tmp_path)
    message = FakeMessage(failures=2)
    target = settings.file_download_dir / "result.bin"
    target.touch()
    record = DownloadRecord("retry-job", 1, 123, target.name, str(target), max_retries=3)
    history.add(record)
    job = DownloadJob(record.id, message, target, FakeStatus())

    await manager._process_job(job)

    saved = history.find("retry-job")
    assert message.calls == 3
    assert saved["status"] == "complete"
    assert saved["retry_count"] == 2
    assert target.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_download_fails_after_three_automatic_retries(tmp_path):
    manager, settings, history = make_manager(tmp_path)
    message = FakeMessage(failures=10)
    target = settings.file_download_dir / "failed.bin"
    target.touch()
    history.add(DownloadRecord("failed-job", 1, 123, target.name, str(target), max_retries=3))

    await manager._process_job(DownloadJob("failed-job", message, target, FakeStatus()))

    saved = history.find("failed-job")
    assert message.calls == 4
    assert saved["status"] == "failed"
    assert saved["retry_count"] == 3


@pytest.mark.asyncio
async def test_album_creation_is_serialized(tmp_path):
    manager, settings, _ = make_manager(tmp_path)
    manager.queue = asyncio.Queue(maxsize=10)
    FakeEvent.replies = 0

    await asyncio.gather(
        manager._enqueue_album_item(FakeEvent(FakeMessage(1)), settings, "album"),
        manager._enqueue_album_item(FakeEvent(FakeMessage(2)), settings, "album"),
    )

    batch = manager.albums["123:album"]
    assert FakeEvent.replies == 1
    assert batch.total == 2
    assert manager.queue.qsize() == 2


@pytest.mark.asyncio
async def test_cancelling_current_job_does_not_stop_worker(tmp_path):
    manager, _, history = make_manager(tmp_path)
    manager.queue = asyncio.Queue(maxsize=10)
    first_started = asyncio.Event()
    second_finished = asyncio.Event()

    async def process(job):
        if job.record_id == "first":
            first_started.set()
            await asyncio.Event().wait()
        second_finished.set()

    manager._process_job = process
    for record_id in ("first", "second"):
        target = tmp_path / f"{record_id}.bin"
        target.touch()
        history.add(DownloadRecord(record_id, 1, 123, target.name, str(target)))
        job = DownloadJob(record_id, FakeMessage(), target, FakeStatus())
        manager.jobs[record_id] = job
        manager.queue.put_nowait(job)

    worker = asyncio.create_task(manager._worker())
    await first_started.wait()
    assert await manager.cancel("current") == 1
    await asyncio.wait_for(second_finished.wait(), timeout=1)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert second_finished.is_set()


@pytest.mark.asyncio
async def test_safe_edit_handles_connection_error_without_raising(tmp_path):
    manager, _, _ = make_manager(tmp_path)

    class BrokenStatus:
        async def edit(self, _):
            raise ConnectionError("Network connection reset by peer")

    # Should not raise exception
    await manager.safe_edit(BrokenStatus(), "test message")


@pytest.mark.asyncio
async def test_worker_survives_unexpected_job_exception(tmp_path):
    manager, _, history = make_manager(tmp_path)
    manager.queue = asyncio.Queue(maxsize=10)
    second_finished = asyncio.Event()

    async def process(job):
        if job.record_id == "bad":
            raise RuntimeError("Unexpected failure")
        second_finished.set()

    manager._process_job = process
    for record_id in ("bad", "good"):
        target = tmp_path / f"{record_id}.bin"
        target.touch()
        history.add(DownloadRecord(record_id, 1, 123, target.name, str(target)))
        job = DownloadJob(record_id, FakeMessage(), target, FakeStatus())
        manager.jobs[record_id] = job
        manager.queue.put_nowait(job)

    worker = asyncio.create_task(manager._worker())
    await asyncio.wait_for(second_finished.wait(), timeout=1)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert second_finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [None, MessageMediaWebPage(webpage=None)])
async def test_handle_media_with_telegram_link(tmp_path, preview):
    manager, settings, history = make_manager(tmp_path)
    settings.allowed_user_ids = [123]
    manager.queue = asyncio.Queue(maxsize=10)

    class MockClient:
        def is_connected(self):
            return True

        async def get_messages(self, channel, ids=None):
            if ids == 17530:
                msg = FakeMessage(message_id=17530)
                msg.grouped_id = None
                return msg
            return None

    manager.client = MockClient()
    manager.task = asyncio.create_task(asyncio.sleep(10))

    class LinkEvent:
        def __init__(self):
            self.sender_id = 123
            self.chat_id = 123
            self.message = type("Msg", (), {
                "media": preview,
                "text": "https://t.me/CosSSDZH/17530",
                "message": "https://t.me/CosSSDZH/17530",
                "raw_text": "https://t.me/CosSSDZH/17530",
            })()
            self.replies = []

        async def reply(self, text):
            status = FakeStatus()
            status.text = text
            self.replies.append(status)
            return status

    event = LinkEvent()
    await manager._handle_media(event, settings)

    assert manager.queue.qsize() == 1
    job = manager.queue.get_nowait()
    assert job.message.id == 17530
    assert len(history.list()) == 1

    manager.task.cancel()
    await asyncio.gather(manager.task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped_id", [None, 999])
async def test_media_caption_link_preserves_received_attachment(tmp_path, grouped_id):
    manager, settings, history = make_manager(tmp_path)
    settings.allowed_user_ids = [123]
    manager.queue = asyncio.Queue(maxsize=10)
    manager.client = SimpleNamespace(get_messages=AsyncMock())
    message = FakeMessage(message_id=900)
    message.raw_text = "Source: https://t.me/example_channel/101"
    message.grouped_id = grouped_id
    event = SimpleNamespace(
        sender_id=123, chat_id=123, message=message,
        reply=AsyncMock(return_value=FakeStatus()),
    )

    await manager._handle_media(event, settings)

    manager.client.get_messages.assert_not_awaited()
    job = manager.queue.get_nowait()
    assert job.message is message
    assert bool(job.album_key) == bool(grouped_id)
    assert history.find(job.record_id)["notification_chat_id"] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
async def test_web_link_retry_after_reload_has_no_channel_notifications(tmp_path, status):
    manager, settings, history = make_manager(tmp_path)
    manager.queue = asyncio.Queue(maxsize=10)
    source = FakeMessage(message_id=101)
    source.chat_id = -1001234567890
    source.grouped_id = None
    manager.client = SimpleNamespace(
        is_connected=lambda: True,
        get_messages=AsyncMock(return_value=source),
        send_message=AsyncMock(side_effect=ChatWriteForbiddenError(request=None)),
    )
    assert await manager.enqueue_from_links_text("https://t.me/example_channel/101") == (1, [])
    original = manager.queue.get_nowait()
    manager.queue.task_done()
    manager.jobs.clear()
    history.update(original.record_id, status=status)
    manager.history = DownloadHistory(history.path)

    assert await manager.retry(original.record_id) == 1

    manager.client.get_messages.assert_awaited_with(source.chat_id, ids=101)
    manager.client.send_message.assert_not_awaited()
    retried = manager.queue.get_nowait()
    assert retried.status_message is None
    await manager._process_job(retried)
    assert manager.history.find(original.record_id)["status"] == "complete"
    assert retried.target_path.read_bytes() == source.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("album_size", [1, 2])
async def test_bot_link_retry_notifies_requester_after_reload(tmp_path, album_size):
    manager, settings, history = make_manager(tmp_path)
    settings.allowed_user_ids = [123]
    manager.queue = asyncio.Queue(maxsize=10)
    sources = [FakeMessage(message_id=101 + index) for index in range(album_size)]
    for source in sources:
        source.chat_id = -1001234567890
        source.grouped_id = 999 if album_size > 1 else None

    async def get_messages(channel, ids=None):
        if isinstance(ids, list):
            return sources
        return next(source for source in sources if source.id == ids)

    manager.client = SimpleNamespace(
        is_connected=lambda: True,
        get_messages=AsyncMock(side_effect=get_messages),
        send_message=AsyncMock(return_value=FakeStatus()),
    )
    event = SimpleNamespace(
        sender_id=123, chat_id=123,
        message=SimpleNamespace(media=None, raw_text="https://t.me/example_channel/101"),
        reply=AsyncMock(return_value=FakeStatus()),
    )
    await manager._handle_media(event, settings)
    manager.jobs.clear()
    while not manager.queue.empty():
        job = manager.queue.get_nowait()
        manager.queue.task_done()
        history.update(job.record_id, status="failed")
    manager.history = DownloadHistory(history.path)

    assert await manager.retry("failed", all_matches=True) == album_size

    assert manager.client.send_message.await_count == album_size
    assert all(call.args[0] == 123 for call in manager.client.send_message.await_args_list)
    assert all(record["chat_id"] == sources[0].chat_id for record in manager.history.list())


@pytest.mark.asyncio
async def test_retry_still_queues_when_requester_cannot_receive_notification(tmp_path):
    manager, settings, history = make_manager(tmp_path)
    manager.queue = asyncio.Queue(maxsize=10)
    source = FakeMessage()
    source.chat_id = -1001234567890
    record_id = await manager.enqueue(source, settings, notification_chat_id=123)
    manager.queue.get_nowait()
    manager.queue.task_done()
    manager.jobs.clear()
    history.update(record_id, status="failed")
    manager.client = SimpleNamespace(
        get_messages=AsyncMock(return_value=source),
        send_message=AsyncMock(side_effect=ChatWriteForbiddenError(request=None)),
    )

    assert await manager.retry(record_id) == 1
    assert manager.client.send_message.await_args.args[0] == 123
    assert manager.queue.get_nowait().status_message is None


@pytest.mark.asyncio
async def test_enqueue_from_links_text(tmp_path):
    manager, settings, history = make_manager(tmp_path)
    manager.queue = asyncio.Queue(maxsize=10)

    class MockClient:
        def is_connected(self):
            return True

        async def get_messages(self, channel, ids=None):
            if ids in (101, 102):
                msg = FakeMessage(message_id=ids)
                msg.grouped_id = None
                return msg
            return None

    manager.client = MockClient()
    manager.task = asyncio.create_task(asyncio.sleep(10))

    text = "https://t.me/CosSSDZH/101 https://t.me/CosSSDZH/102"
    count, errors = await manager.enqueue_from_links_text(text)

    assert count == 2
    assert not errors
    assert manager.queue.qsize() == 2
    assert len(history.list()) == 2

    manager.task.cancel()
    await asyncio.gather(manager.task, return_exceptions=True)
