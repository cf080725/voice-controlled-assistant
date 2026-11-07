"""A declined speculative task must not consume the current pause's chance."""
import threading
from unittest.mock import Mock

import canaan_endpoint as endpoint
import numpy as np
from canaan_asr_prefetch import ASRPrefetch, RecordingPrefetch


def block(value=0):
    return np.full(1600, value, np.float32)


def test_previous_turn_finishing_during_pause_allows_current_prefetch():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def recognize(model, audio):
        calls.append(len(audio))
        if len(audio) == 4:
            entered.set()
            assert release.wait(2)
            return "旧的指令"
        return "打开计算器"

    model, manager = object(), ASRPrefetch(transcriber=recognize)
    old = manager.start(np.ones(4, np.float32), model)
    assert entered.wait(1)
    session = RecordingPrefetch(manager, model)
    ep = endpoint.EnergyEndpointer()
    try:
        for value in [.2] * 4 + [0] * 3:
            ep.feed(block(value))
            session.observe(ep)
        assert session.ticket is None  # Previous worker is still occupied.
        release.set()
        assert old.done.wait(1)
        ep.feed(block())
        session.observe(ep)
        assert session.ticket is not None, "A declined task consumed this pause's only attempt"
        assert session.ticket.done.wait(1)
        while ep.state == "speaking":
            ep.feed(block())
            session.observe(ep)
        assert ep.endpoint_wait_ms() == 800
        assert session.resolve(ep.get_audio(.3), model) == ("打开计算器", "sensevoice")
        assert manager.resolve(old, old.audio, model) is None
        assert len(calls) == 2  # One old inference and exactly one new inference.
    finally:
        release.set()
        session.discard()


def test_busy_admission_stays_nonblocking_and_never_queues_after_endpoint():
    entered, release = threading.Event(), threading.Event()

    def recognize(model, audio):
        entered.set()
        assert release.wait(2)
        return "旧的指令"

    model, manager = object(), ASRPrefetch(transcriber=recognize)
    old = manager.start(np.ones(4, np.float32), model)
    assert entered.wait(1)
    session, ep = RecordingPrefetch(manager, model), endpoint.EnergyEndpointer()
    start = Mock(wraps=manager.start)
    manager.start = start
    try:
        for value in [.2] * 4 + [0] * 8:
            ep.feed(block(value))
            session.observe(ep)
        assert ep.state == "end" and ep.endpoint_wait_ms() == 800
        assert session.ticket is None
        assert not old.done.is_set(), "The microphone loop waited for occupied ASR"
        # At most one admission check per available 100ms silence tick.
        assert 1 <= start.call_count <= 5
        release.set()
        assert old.done.wait(1)
        count = start.call_count
        assert session.observe(ep) == "end" and start.call_count == count
    finally:
        release.set()
        session.discard()
