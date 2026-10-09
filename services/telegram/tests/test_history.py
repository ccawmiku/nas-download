import json

import pytest

from app.history import DownloadHistory, DownloadRecord


@pytest.mark.parametrize("chat_id", [123, -1001234567890])
def test_legacy_history_keeps_requesting_chat_for_retry_notifications(tmp_path, chat_id):
    path = tmp_path / "downloads.json"
    path.write_text(json.dumps([{
        "id": "legacy", "message_id": 1, "chat_id": chat_id,
        "file_name": "one.bin", "path": str(tmp_path / "one.bin"), "status": "failed",
    }]), encoding="utf-8")

    history = DownloadHistory(path)

    assert history.find("legacy")["notification_chat_id"] == chat_id


def test_recover_incomplete_downloads(tmp_path):
    complete_path = tmp_path / "complete.bin"
    complete_path.write_bytes(b"abc")
    partial_path = tmp_path / "partial.bin"
    partial_path.write_bytes(b"a")
    reserved_path = tmp_path / "reserved.bin"
    reserved_path.touch()
    hidden_partial = tmp_path / ".reserved.bin.reserve1.part"
    hidden_partial.write_bytes(b"partial")
    history = DownloadHistory(tmp_path / "downloads.json")
    history.add(DownloadRecord("complete", 1, 1, complete_path.name, str(complete_path), status="downloading", total_bytes=3))
    history.add(DownloadRecord("partial", 2, 1, partial_path.name, str(partial_path), status="downloading", total_bytes=3))
    history.add(DownloadRecord("missing", 3, 1, "missing.bin", str(tmp_path / "missing.bin"), status="queued"))
    history.add(DownloadRecord("reserve1", 4, 1, reserved_path.name, str(reserved_path), status="downloading", total_bytes=10))

    result = history.recover_incomplete()

    assert result == {"recovered": 1, "interrupted": 3}
    assert history.find("complete")["status"] == "complete"
    assert history.find("partial")["status"] == "interrupted"
    assert history.find("missing")["status"] == "interrupted"
    assert not reserved_path.exists()
    assert not hidden_partial.exists()


def test_history_corruption_is_backed_up_and_service_can_continue(tmp_path):
    path = tmp_path / "downloads.json"
    path.write_text("{broken", encoding="utf-8")

    history = DownloadHistory(path)

    assert (tmp_path / "downloads.json.corrupt").exists()
    assert history.list()[0]["status"] == "failed"
    assert DownloadHistory(path).list()[0]['status']=='failed'
    assert not list(tmp_path.glob("*.tmp"))


def test_progress_updates_are_throttled_but_flush_persists_latest_state(tmp_path):
    path = tmp_path / "downloads.json"
    history = DownloadHistory(path, flush_interval=60)
    history.add(DownloadRecord("one", 1, 1, "one.bin", str(tmp_path / "one.bin")))

    history.update("one", persist=False, status="downloading", progress=55)
    before_flush = DownloadHistory(path).find('one')
    history.flush()
    after_flush = DownloadHistory(path).find('one')

    assert before_flush["progress"] == 0
    assert after_flush["progress"] == 55


def test_history_is_not_pruned_by_display_limit_and_imports_only_once(tmp_path):
    path=tmp_path/'downloads.json'
    path.write_text(json.dumps([{'id':'legacy','message_id':1,'chat_id':1,'file_name':'old','path':'old','status':'complete'}]),encoding='utf-8')
    history=DownloadHistory(path,limit=2)
    for index in range(205):
        history.add(DownloadRecord(str(index),index,1,f'{index}.bin',str(tmp_path/f'{index}.bin')))
    assert history.page()['total']==206
    assert history.find('legacy')['status']=='complete'
    path.write_text('[]',encoding='utf-8')
    assert DownloadHistory(path).page()['total']==206


def test_list_statuses_can_return_all_matches(tmp_path):
    history = DownloadHistory(tmp_path / "downloads.json")
    for index in range(12):
        history.add(DownloadRecord(str(index), index, 1, f"{index}.bin", str(tmp_path / f"{index}.bin"), status="failed"))

    assert len(history.list_statuses({"failed"})) == 10
    assert len(history.list_statuses({"failed"}, limit=None)) == 12
