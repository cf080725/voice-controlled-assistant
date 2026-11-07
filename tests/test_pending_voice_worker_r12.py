import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from canaan_pending_bargein import PendingSpeechListener
from canaan_pending_voice_worker import PendingVoiceWorker


def wait_result(worker):
    deadline = time.monotonic() + 2
    while worker.peek() is None and time.monotonic() < deadline:
        time.sleep(.005)
    assert worker.peek() is not None
    return worker.peek()


def test_one_running_candidate_does_not_block_or_grow_a_queue():
    entered, release = threading.Event(), threading.Event()

    def recognize(request):
        entered.set()
        assert release.wait(2)
        return {"text": "确认", "kind": "confirm"}

    worker = PendingVoiceWorker(recognize)
    audio = np.full(3200, .05, np.float32)
    item, session = object(), {}
    assert worker.submit(audio, item=item, context=("owner", 1), session=session)
    assert entered.wait(1)
    assert not release.is_set() and worker.peek() is None  # submit returned while ASR is still blocked.
    for _ in range(100):
        assert not worker.submit(audio, item=item, context=(), session=session)
    audio[:] = 0
    release.set()
    result = wait_result(worker)
    assert np.all(result["audio"] == .05)
    assert result["item"] is item and result["session"] is session
    assert result["captured_at"] <= result["asr_started_at"] <= result["recognized_at"]
    assert worker.busy
    assert worker.discard(result) and not worker.busy


def test_completed_candidate_is_retained_until_the_exact_result_is_consumed():
    worker = PendingVoiceWorker(lambda request: {"text": "确认", "kind": "confirm"})
    assert worker.submit(np.zeros(3200), item=object(), context=(), session={})
    result = wait_result(worker)
    assert not worker.discard(dict(result))
    assert worker.peek() is result
    assert worker.discard(result)


def test_asr_failure_is_a_bounded_result_and_can_be_retried():
    def fail(request):
        raise ValueError("private implementation details")

    worker = PendingVoiceWorker(fail)
    assert worker.submit(np.zeros(3200), item=object(), context=(), session={})
    result = wait_result(worker)
    assert result["error"] == "ValueError" and "text" not in result
    assert worker.discard(result)
    assert worker.submit(np.zeros(3200), item=object(), context=(), session={})
    wait_result(worker)


@pytest.mark.parametrize("audio", [np.zeros(0), np.full(1600, np.nan), np.zeros(26 * 16000 + 1)])
def test_invalid_or_unbounded_audio_never_starts_a_worker(audio):
    worker = PendingVoiceWorker(lambda request: pytest.fail("invalid audio reached recognition"))
    assert not worker.submit(audio, item=object(), context=(), session={})
    assert not worker.busy


def test_thread_start_failure_releases_the_slot_for_a_later_retry(monkeypatch):
    import canaan_pending_voice_worker as module

    class UnavailableThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    worker = PendingVoiceWorker(lambda request: {"text": "确认"})
    with monkeypatch.context() as patch:
        patch.setattr(module.threading, "Thread", UnavailableThread)
        with pytest.raises(RuntimeError):
            worker.submit(np.zeros(3200), item=object(), context=(), session={})
    assert not worker.busy
    assert worker.submit(np.zeros(3200), item=object(), context=(), session={})
    assert wait_result(worker)["text"] == "确认"


def test_two_valid_voice_frames_can_stop_and_submit_the_same_short_answer():
    now = [100.0]
    listener = PendingSpeechListener(
        canceller=SimpleNamespace(process_stream=lambda mic, ref: mic),
        reference=SimpleNamespace(active=lambda **kw: False,
                                  take=lambda end, n: np.zeros(n, np.float32)),
        clock=lambda: now[0])
    outputs, onset = [], False
    for i in range(5):
        now[0] += .1
        t = np.arange(1600) / 16000
        audio = (.04 * np.sin(2 * np.pi * 701 * t)).astype(np.float32) if i < 2 else np.zeros(1600, np.float32)
        outputs.append(listener.feed(audio, playback_silent=True))
        onset |= listener.onset
    assert onset
    assert sum(x is not None for x in outputs) == 1


def test_lost_input_block_discards_the_whole_candidate_instead_of_a_truncated_reply():
    listener = PendingSpeechListener(
        canceller=SimpleNamespace(process_stream=lambda mic, ref: mic),
        reference=SimpleNamespace(active=lambda **kw: False,
                                  take=lambda end, n: np.zeros(n, np.float32)))
    listener.feed(np.full(1600, .05, np.float32), playback_silent=True)
    listener.discontinuity()
    outputs = [listener.feed(np.zeros(1600, np.float32), playback_silent=True) for _ in range(5)]
    assert all(x is None for x in outputs) and not listener.capturing
