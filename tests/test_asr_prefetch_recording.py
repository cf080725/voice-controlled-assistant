"""Recording-boundary reuse must keep speaker checks and correction capture."""
import queue
import threading
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import canaan_config
import canaan_endpoint
import canaan_desk_os_hub as hub_module
import numpy as np
import pytest
from canaan_asr_prefetch import ASRPrefetch, RecordingPrefetch


def block(value=0):
    return np.full(1600, value, np.float32)


def recording(manager=None, min_speech_sec=.25):
    model = object()
    calls = []
    manager = manager or ASRPrefetch(transcriber=lambda model, audio: calls.append(audio.copy()) or "原句")
    return RecordingPrefetch(manager, model, min_speech_sec=min_speech_sec), model, calls


def feed(session, endpoint, values):
    for value in values:
        endpoint.feed(block(value))
        session.observe(endpoint)


def test_one_attempt_per_pause_reuses_exact_final_waveform():
    session, model, calls = recording()
    endpoint = canaan_endpoint.EnergyEndpointer()
    feed(session, endpoint, [.2] * 4 + [0] * 2)
    assert session.ticket is None
    feed(session, endpoint, [0] * 6)
    assert endpoint.state == "end"
    assert session.resolve(endpoint.get_audio(.3), model) == ("原句", "sensevoice")
    assert len(calls) == 1


def test_resumed_speech_invalidates_partial_transcript():
    session, model, calls = recording()
    endpoint = canaan_endpoint.EnergyEndpointer()
    feed(session, endpoint, [.2] * 4 + [0] * 3)
    old = session.ticket
    assert old is not None and old.done.wait(1)
    feed(session, endpoint, [.3])
    assert session.ticket is None
    assert session.manager.resolve(old, old.audio, model) is None
    feed(session, endpoint, [0] * 8)
    assert session.resolve(endpoint.get_audio(.3), model) == ("原句", "sensevoice")
    assert len(calls) == 2


def test_followup_threshold_is_not_lowered_by_prefetch():
    session, _, calls = recording(min_speech_sec=.4)
    endpoint = canaan_endpoint.EnergyEndpointer()
    feed(session, endpoint, [.2] * 3 + [0] * 8)
    assert session.ticket is None and calls == []


@pytest.mark.parametrize("terminal", ["no_speech", "max_len"])
def test_aborted_recording_discards_ticket(terminal):
    session, model, _ = recording()
    endpoint = canaan_endpoint.EnergyEndpointer()
    feed(session, endpoint, [.2] * 4 + [0] * 3)
    ticket = session.ticket
    assert ticket is not None and ticket.done.wait(1)
    endpoint.state = terminal
    session.observe(endpoint)
    assert session.resolve(ticket.audio, model) is None


def test_long_recordings_do_not_start_extra_inference():
    session, _, calls = recording()
    endpoint = canaan_endpoint.EnergyEndpointer()
    feed(session, endpoint, [.2] * 130 + [0] * 4)
    assert session.ticket is None and calls == []


def test_session_discard_and_different_model_miss():
    session, model, _ = recording()
    endpoint = canaan_endpoint.EnergyEndpointer()
    feed(session, endpoint, [.2] * 4 + [0] * 3)
    assert session.ticket is not None
    assert session.resolve(endpoint.get_audio(.3), object()) is None
    session.discard()
    assert session.resolve(endpoint.get_audio(.3), model) is None


class InlineWorker:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def processing_hub(monkeypatch):
    hub = object.__new__(hub_module.CanaanDeskOSHub)
    hub.ui_queue = queue.Queue()
    hub.sense_voice = object()
    hub.asr_model = object()
    hub._speaker_ok = Mock(return_value=True)
    hub.profile = SimpleNamespace(privacy_revision=0)
    hub.memory = SimpleNamespace()
    runtime = SimpleNamespace(voices=SimpleNamespace(record_command=Mock()), messages=SimpleNamespace(mark_health=Mock()))
    hub.root = SimpleNamespace(canaan_integration_runtime=runtime)
    monkeypatch.setattr(hub_module, "threading", SimpleNamespace(Thread=InlineWorker, Event=threading.Event,
                                                                local=threading.local))
    monkeypatch.setattr(hub_module, "transcribe", Mock(return_value=("。", "sensevoice")))
    return hub, runtime


def test_cached_result_keeps_speaker_verification_and_correction_record(processing_hub):
    hub, runtime = processing_hub
    audio = block(.2)
    prefetch = SimpleNamespace(resolve=Mock(return_value=("。", "sensevoice")), discard=Mock())
    hub.process_audio_data(audio, asr_ticket=prefetch)
    hub._speaker_ok.assert_called_once()
    prefetch.resolve.assert_called_once_with(audio, hub.sense_voice, should_stop=ANY)
    cancelled = prefetch.resolve.call_args.kwargs["should_stop"]
    assert callable(cancelled) and not cancelled()
    hub.turn_cancelled = True
    assert cancelled()
    runtime.voices.record_command.assert_called_once_with(audio, "。", "sensevoice")
    hub_module.transcribe.assert_not_called()
    prefetch.discard.assert_called_once()


def test_speaker_rejection_discards_without_resolving_or_capturing(processing_hub):
    hub, runtime = processing_hub
    hub._speaker_ok.return_value = False
    prefetch = SimpleNamespace(resolve=Mock(), discard=Mock())
    hub.process_audio_data(block(.2), asr_ticket=prefetch)
    prefetch.discard.assert_called_once()
    prefetch.resolve.assert_not_called()
    runtime.voices.record_command.assert_not_called()


def test_miss_uses_original_asr_and_retains_its_engine(processing_hub):
    hub, runtime = processing_hub
    audio = block(.2)
    prefetch = SimpleNamespace(resolve=Mock(return_value=None), discard=Mock())
    hub.process_audio_data(audio, asr_ticket=prefetch)
    hub_module.transcribe.assert_called_once_with(audio, sense_voice=hub.sense_voice, whisper=hub.asr_model, should_stop=ANY)
    cancelled = hub_module.transcribe.call_args.kwargs["should_stop"]
    assert callable(cancelled) and not cancelled()
    hub.turn_cancelled = True
    assert cancelled()
    runtime.voices.record_command.assert_called_once_with(audio, "。", "sensevoice")


def test_prefetch_error_does_not_break_normal_recognition(processing_hub):
    hub, runtime = processing_hub
    audio = block(.2)
    prefetch = SimpleNamespace(resolve=Mock(side_effect=RuntimeError("candidate failure")), discard=Mock())
    hub.process_audio_data(audio, asr_ticket=prefetch)
    hub_module.transcribe.assert_called_once()
    runtime.voices.record_command.assert_called_once_with(audio, "。", "sensevoice")
    prefetch.discard.assert_called_once()


def test_feature_switch_does_not_allocate_worker_when_disabled(monkeypatch):
    monkeypatch.setattr(canaan_config, "ASR_PREFETCH_ENABLED", False, raising=False)
    hub = object.__new__(hub_module.CanaanDeskOSHub)
    hub.sense_voice = object()
    assert hub._begin_asr_prefetch() is None
    assert not hasattr(hub, "_asr_prefetch_manager")


def test_speaker_lock_retains_verify_then_recognize_order(monkeypatch):
    monkeypatch.setattr(canaan_config, "ASR_PREFETCH_ENABLED", True)
    hub = object.__new__(hub_module.CanaanDeskOSHub)
    hub.sense_voice = object()
    hub.speaker_gate = SimpleNamespace(enabled=True)
    assert hub._begin_asr_prefetch() is None
    assert not hasattr(hub, "_asr_prefetch_manager")


@pytest.mark.parametrize("demo,explicit,grace,pending,expected", [
    (False, None, 0, False, 0), (True, None, 0, False, 1.5),
    (True, "0", 0, False, 0), (True, "1.2", 1.2, False, 1.2),
    (True, None, 0, True, 0), (True, "2", 2, True, 0),
])
def test_demo_listening_grace_honors_explicit_setting_and_pending(
        monkeypatch, demo, explicit, grace, pending, expected):
    import canaan_gpu_priority as gpu
    monkeypatch.setattr(canaan_config, "ASR_PREFETCH_ENABLED", True)
    monkeypatch.setattr(canaan_config, "ENDPOINT_MODE", "fixed")
    monkeypatch.setattr(canaan_config, "ENDPOINT_PARTIAL_GRACE_SEC", grace)
    monkeypatch.setattr(gpu, "demo_mode", lambda: demo)
    if explicit is None:
        monkeypatch.delenv("CANAAN_ENDPOINT_PARTIAL_GRACE", raising=False)
    else:
        monkeypatch.setenv("CANAAN_ENDPOINT_PARTIAL_GRACE", explicit)
    hub = object.__new__(hub_module.CanaanDeskOSHub)
    hub.sense_voice = object()
    hub.pending = SimpleNamespace(has_pending=lambda: pending)
    assert hub._begin_asr_prefetch().grace_sec == expected


def test_demo_switch_applies_to_the_next_recording_without_restart(monkeypatch):
    import canaan_gpu_priority as gpu
    monkeypatch.setattr(canaan_config, "ASR_PREFETCH_ENABLED", True)
    monkeypatch.setattr(canaan_config, "ENDPOINT_MODE", "fixed")
    monkeypatch.setattr(canaan_config, "ENDPOINT_PARTIAL_GRACE_SEC", 0)
    monkeypatch.delenv("CANAAN_ENDPOINT_PARTIAL_GRACE", raising=False)
    mode = {"enabled": False}
    monkeypatch.setattr(gpu, "demo_mode", lambda: mode["enabled"])
    hub = object.__new__(hub_module.CanaanDeskOSHub)
    hub.sense_voice = object()
    assert hub._begin_asr_prefetch().grace_sec == 0
    mode["enabled"] = True
    assert hub._begin_asr_prefetch().grace_sec == 1.5
    mode["enabled"] = False
    assert hub._begin_asr_prefetch().grace_sec == 0
