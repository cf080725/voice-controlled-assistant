import pytest

from canaan_message_center import MessageCenter


def enqueue(center):
    center.ingest({"source": "feishu", "conversation": "Synthetic contact", "text": "Test event",
                   "message_id": "test-1"})


@pytest.mark.parametrize("result, expected", [
    ({"success": True}, "sent"),
    ({"success": False, "error_code": "CONFIRM_REQUIRED"}, "confirmation_required"),
    ({"success": False, "error_code": "DELIVERY_UNCERTAIN"}, "unknown"),
    ({"success": False}, "blocked"),
    ({"success": "false"}, "unknown"),
    ({"success": 1}, "unknown"),
    ({}, "unknown"),
    (None, "unknown"),
])
def test_delivery_receipt_requires_explicit_boolean(tmp_path, result, expected):
    center = MessageCenter(tmp_path / "messages.db", {"forward_enabled": True}, lambda *_: result)
    enqueue(center)
    assert center.forward_once() is (expected == "sent")
    assert center.status()["counts"] == {expected: 1}
    if expected == "unknown":
        assert not center.retry(1)


def test_late_receipt_does_not_replace_recovered_delivery(tmp_path):
    path = tmp_path / "messages.db"

    def send(*_):
        recovery = MessageCenter(path)
        assert recovery.status()["counts"] == {"unknown": 1}
        recovery.db.close()
        return {"success": True}

    center = MessageCenter(path, {"forward_enabled": True}, send)
    enqueue(center)
    assert not center.forward_once()
    assert center.status()["counts"] == {"unknown": 1}


def test_delivery_with_failed_receipt_write_requires_manual_check(tmp_path):
    calls = []

    def send(*args):
        calls.append(args)
        return {"success": True}

    center = MessageCenter(tmp_path / "messages.db", {"forward_enabled": True}, send)
    enqueue(center)
    with center.db:
        center.db.execute("""CREATE TRIGGER reject_receipt BEFORE UPDATE ON messages
            WHEN NEW.delivery='sent' BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END""")
    assert not center.forward_once()
    assert center.status()["counts"] == {"unknown": 1}
    assert not center.retry(1)
    assert not center.forward_once()
    assert len(calls) == 1


def test_stopped_worker_cannot_claim_another_message(tmp_path):
    calls = []
    center = MessageCenter(tmp_path / "messages.db", {"forward_enabled": True}, lambda *args: calls.append(args))
    enqueue(center)
    center.stop()
    assert not center.forward_once()
    assert not calls
    assert center.status()["counts"] == {"queued": 1}
