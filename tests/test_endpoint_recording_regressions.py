"""Recording regressions with fake microphone/VAD; no device or model startup."""

import ast
import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import canaan_config
import canaan_endpoint as endpoint
import canaan_turnmerge
import numpy as np
import pytest


class FakeStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.reads = []

    def read(self, frames):
        self.reads.append(frames)
        if not self.chunks:
            raise OSError("fake microphone disconnected")
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > frames:
            self.chunks.insert(0, chunk[frames:])
            chunk = chunk[:frames]
        return chunk.reshape(-1, 1), False


def block(value=0.0, samples=1600):
    return np.full(samples, value, np.float32)


def load_recording_methods(factory):
    """Compile recording methods without importing the GUI/model bootstrap."""
    path = Path(__file__).resolve().parents[1] / "core" / "canaan_desk_os_hub.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hub_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CanaanDeskOSHub")
    names = {"record_command_after_wake", "_merge_wait", "record_followup", "_listen_stop_continuation", "_begin_asr_prefetch"}
    methods = [node for node in hub_class.body if isinstance(node, ast.FunctionDef) and node.name in names]

    class Tracer:
        def __init__(self, source):
            self.source, self.values = source, {}

        def set(self, name, value):
            self.values[name] = value

    namespace = {"np": np, "time": SimpleNamespace(time=lambda: 100.0), "canaan_config": canaan_config,
                 "SAMPLE_RATE": endpoint.SAMPLE_RATE, "create_endpointer": factory,
                 "TurnTracer": Tracer, "winsound": SimpleNamespace(Beep=lambda *args: None)}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), namespace)
    hub_type = type("RecordingHub", (), {name: namespace[name] for name in names})
    hub = hub_type()
    hub.ui_queue = queue.Queue()
    hub.ambient_noise_floor = .001
    hub.is_recording_command = False
    hub.followup_until = 101.0
    hub.processed, hub.greetings, hub.spoken = [], [], []
    hub.process_audio_data = lambda audio, tracer: hub.processed.append((audio, tracer))
    hub.play_canaan_greeting = lambda: hub.greetings.append(True)
    hub.speak = lambda text, emotion: hub.spoken.append(text)
    return hub


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", False)
    monkeypatch.setitem(sys.modules, "canaan_events", SimpleNamespace(publish=lambda *_args: None))
    return load_recording_methods(lambda **kwargs: endpoint.EnergyEndpointer(
        hangover_short=.2, hangover_long=.2, no_speech_timeout=kwargs.get("no_speech_timeout", .3),
        max_record_sec=3.0, noise_floor=.001))


def ended_endpoint(**kwargs):
    ep = endpoint.EnergyEndpointer(noise_floor=.001, hangover_short=.2, hangover_long=.2, **kwargs)
    for _ in range(3):
        ep.feed(block(.2))
    for _ in range(2):
        state = ep.feed(block())
    assert state == "end"
    return ep


def test_no_speech_timeout_ends_at_exact_sample_boundary():
    ep = endpoint.EnergyEndpointer(no_speech_timeout=.3)
    for _ in range(2):
        assert ep.feed(block()) == "waiting"
    assert ep.feed(block()) == "no_speech"
    assert ep.record_ms() == 300


def test_delayed_vad_retains_onset_without_counting_it_as_confirmed_speech():
    class Delayed(endpoint._BaseEndpointer):
        def _is_speech(self, chunk):
            return bool(chunk[0] >= .3)

    ep = Delayed()
    for value in (.1, .2, .3):
        ep.feed(block(value))
    np.testing.assert_allclose(ep.get_audio(), np.concatenate([block(.1), block(.2), block(.3)]))
    assert ep.speech_samples == 1600
    assert not ep.is_valid_utterance()


def test_preroll_is_bounded_after_long_wait():
    class Delayed(endpoint._BaseEndpointer):
        def _is_speech(self, chunk):
            return bool(chunk[0] > .1)

    ep = Delayed()
    for _ in range(20):
        ep.feed(block(.01))
    ep.feed(block(.2))
    assert len(ep.get_audio()) == 4000 + 1600  # 250ms maximum look-back
    assert ep.total_samples == 21 * 1600
    assert ep.speech_samples == 1600


def test_max_record_limit_caps_payload_and_vad_work():
    class Counted(endpoint.EnergyEndpointer):
        def _is_speech(self, chunk):
            self.calls = getattr(self, "calls", 0) + 1
            return super()._is_speech(chunk)

    ep = Counted(max_record_sec=.25)
    for _ in range(4):
        state = ep.feed(block(.2))
    assert state == "max_len"
    assert ep.total_samples == ep.speech_samples == 4000
    assert len(ep.get_audio()) == 4000 and ep.calls == 3


def test_normal_endpoint_at_exact_max_boundary_preserves_complete_terminal_state():
    ep = endpoint.EnergyEndpointer(noise_floor=.001, hangover_short=.2, max_record_sec=.5)
    for _ in range(3):
        ep.feed(block(.2))
    ep.feed(block())
    assert ep.feed(block()) == "end"
    before_audio = ep.get_audio().copy()
    assert ep.feed(block(.2)) == "max_len"
    np.testing.assert_array_equal(ep.get_audio(), before_audio)
    assert ep.total_samples == 8000


def test_no_speech_at_exact_max_boundary_keeps_normal_timeout():
    ep = endpoint.EnergyEndpointer(no_speech_timeout=.3, max_record_sec=.3)
    ep.feed(block())
    ep.feed(block())
    assert ep.feed(block()) == "no_speech"
    assert not ep.is_valid_utterance() and not len(ep.get_audio())


def test_empty_chunk_does_not_advance_vad_or_sample_clock():
    class Counted(endpoint.EnergyEndpointer):
        def _is_speech(self, chunk):
            raise AssertionError("Empty input must not reach VAD")

    ep = Counted()
    assert ep.feed(np.zeros(0, np.float32)) == "waiting"
    assert ep.total_samples == ep.speech_samples == ep.silence_samples == 0


def test_bargein_does_not_discard_first_command_block(hub):
    stream = FakeStream([block(.11), block(.12), block(.13)] + [block()] * 8)
    hub.record_command_after_wake(stream, is_bargein=True)
    assert len(hub.processed) == 1
    audio, _ = hub.processed[0]
    np.testing.assert_allclose(audio[:1600], .11)
    assert not hub.is_recording_command


def test_normal_prompt_tone_still_discards_its_first_buffer(hub):
    stream = FakeStream([block(.99), block(.11), block(.12), block(.13)] + [block()] * 8)
    hub.record_command_after_wake(stream)
    assert len(hub.processed) == 1
    np.testing.assert_allclose(hub.processed[0][0][:1600], .11)


@pytest.mark.parametrize("method", ["record_command_after_wake", "record_followup"])
def test_microphone_failure_restores_flag_and_does_not_process_truncated_audio(hub, method):
    stream = FakeStream([OSError("fake microphone disconnected")] * 3)
    if method == "record_followup":
        hub.record_followup(stream, block(.2))
    else:
        hub.record_command_after_wake(stream, is_bargein=True)
    assert not hub.is_recording_command
    assert not hub.processed


@pytest.mark.parametrize("method", ["record_command_after_wake", "record_followup"])
def test_disconnect_after_enough_speech_still_does_not_submit_partial_command(hub, method):
    stream = FakeStream([block(.2)] * 5 + [OSError("fake microphone disconnected")])
    if method == "record_followup":
        hub.record_followup(stream, block(.2))
    else:
        hub.record_command_after_wake(stream, is_bargein=True)
    assert not hub.is_recording_command and not hub.processed


@pytest.mark.parametrize("method", ["record_command_after_wake", "record_followup"])
def test_merge_read_failure_aborts_recording_without_executing_partial_audio(hub, monkeypatch, method):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True)
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: .2)
    stream = FakeStream([block(.2)] * 4 + [block()] * 2 + [OSError("fake microphone disconnected")])
    if method == "record_followup":
        hub.record_followup(stream, block(.2))
    else:
        hub.record_command_after_wake(stream, is_bargein=True)
    assert not hub.is_recording_command and not hub.processed


@pytest.mark.parametrize("method", ["record_command_after_wake", "record_followup"])
def test_endpointer_creation_failure_restores_recording_flag(hub, method):
    globals_ = getattr(hub, method).__func__.__globals__
    globals_["create_endpointer"] = lambda **_kw: (_ for _ in ()).throw(RuntimeError("bad VAD"))
    stream = FakeStream([block()] * 5)
    if method == "record_followup":
        hub.record_followup(stream, block())
    else:
        hub.record_command_after_wake(stream, is_bargein=True)
    assert not hub.is_recording_command and not hub.processed


def test_merge_one_second_is_ten_reads_and_all_samples_are_counted(hub, monkeypatch):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True)
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: 1.0)
    ep = ended_endpoint()
    before_total, before_silence = ep.total_samples, ep.silence_samples
    stream = FakeStream([block()] * 20)
    assert hub._merge_wait(stream, ep, "end") == "end"
    assert stream.reads == [1600] * 10
    assert ep.total_samples == before_total + 16000
    assert ep.silence_samples == before_silence + 16000
    assert len(ep.get_audio()) == ep.speech_samples + 4800


def test_fractional_merge_window_reads_exact_integer_budget(hub, monkeypatch):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True)
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: .25)
    ep = ended_endpoint()
    stream = FakeStream([block()] * 10)
    assert hub._merge_wait(stream, ep, "end") == "end"
    assert stream.reads == [1600, 1600, 800]


def test_zero_merge_window_does_not_read_or_change_endpoint(hub, monkeypatch):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True)
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: 0.0)
    ep = ended_endpoint()
    before = ep.total_samples
    stream = FakeStream([])
    assert hub._merge_wait(stream, ep, "end") == "end"
    assert not stream.reads and ep.total_samples == before


def test_normal_endpoint_exactly_at_limit_needs_no_additional_merge_audio(hub, monkeypatch):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True)
    ep = ended_endpoint(max_record_sec=.5)
    stream = FakeStream([])
    assert hub._merge_wait(stream, ep, "end") == "end"
    assert not stream.reads and ep.total_samples == 8000


def test_merge_resumed_first_block_updates_speech_and_vad_once(hub, monkeypatch):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True)
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: .2)
    ep = ended_endpoint()
    before_total, before_speech = ep.total_samples, ep.speech_samples
    vad_calls = []
    original = ep._is_speech

    def counted(chunk):
        vad_calls.append(len(chunk))
        return original(chunk)

    ep._is_speech = counted
    stream = FakeStream([block(), block(.3), block(), block(), block(), block()])
    assert hub._merge_wait(stream, ep, "end") == "end"
    assert ep.speech_samples == before_speech + 1600
    assert ep.total_samples == before_total + sum(stream.reads)
    assert len(vad_calls) == len(stream.reads)


def test_merge_cannot_run_past_max_record_limit(hub, monkeypatch):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True)
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: 1.0)
    ep = ended_endpoint(max_record_sec=1.1)
    stream = FakeStream([block()] * 20)
    assert hub._merge_wait(stream, ep, "end") == "max_len"
    assert ep.total_samples == 17600 and len(stream.reads) == 6


@pytest.mark.parametrize("method", ["record_command_after_wake", "record_followup"])
def test_recording_max_length_prompts_for_shorter_sentence_without_submitting_audio(hub, method):
    globals_ = getattr(hub, method).__func__.__globals__
    globals_["create_endpointer"] = lambda **_kw: endpoint.EnergyEndpointer(
        noise_floor=.001, max_record_sec=.45, hangover_short=.2)
    stream = FakeStream([block(.2)] * 10)
    if method == "record_followup":
        hub.record_followup(stream, block(.2))
    else:
        hub.record_command_after_wake(stream, is_bargein=True)
    assert not hub.processed and not hub.greetings and not hub.is_recording_command
    assert len(hub.spoken) == 1 and "短句" in hub.spoken[0]


@pytest.mark.parametrize("method", ["record_command_after_wake", "record_followup"])
def test_max_length_during_merge_propagates_to_recording_and_does_not_submit(hub, monkeypatch, method):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True)
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: 1.0)
    globals_ = hub.record_command_after_wake.__func__.__globals__
    globals_["create_endpointer"] = lambda **_kw: endpoint.EnergyEndpointer(
        noise_floor=.001, max_record_sec=.9, hangover_short=.2)
    stream = FakeStream([block(.2)] * 3 + [block()] * 20)
    if method == "record_followup":
        hub.record_followup(stream, block(.2))
    else:
        hub.record_command_after_wake(stream, is_bargein=True)
    assert not hub.processed and not hub.greetings and not hub.is_recording_command
    assert len(hub.spoken) == 1 and "短句" in hub.spoken[0]


@pytest.mark.parametrize("is_bargein", [False, True])
def test_wake_without_following_speech_does_not_submit_audio(hub, is_bargein):
    # Models/KWS are absent: this represents no post-wake command, not proof that
    # a spoken wake word is semantically a valid command.
    stream = FakeStream([block()] * 12)
    hub.record_command_after_wake(stream, is_bargein=is_bargein)
    assert not hub.processed and not hub.is_recording_command
    assert bool(hub.greetings) is (not is_bargein)


def test_short_wake_tail_below_existing_speech_gate_is_not_a_command(hub):
    stream = FakeStream([block(.2)] + [block()] * 12)
    hub.record_command_after_wake(stream, is_bargein=True)
    assert not hub.processed and not hub.greetings


def test_wake_tail_then_command_keeps_audio_for_existing_asr_guard(hub):
    stream = FakeStream([block(.15), block(.2), block(.25), block(.3)] + [block()] * 12)
    hub.record_command_after_wake(stream, is_bargein=True)
    assert len(hub.processed) == 1
    np.testing.assert_allclose(hub.processed[0][0][:1600], .15)
    assert hub.processed[0][1].source == "kws"


def test_stop_continuation_disconnect_does_not_submit_partial_command(hub):
    stream = FakeStream([block(.2)] * 5 + [OSError("fake microphone disconnected")])
    hub._listen_stop_continuation(stream, "别说了")
    assert not hub.processed and not hub.is_recording_command


def test_stop_continuation_creation_failure_restores_recording_flag(hub):
    globals_ = hub._listen_stop_continuation.__func__.__globals__
    globals_["create_endpointer"] = lambda **_kw: (_ for _ in ()).throw(RuntimeError("bad VAD"))
    hub._listen_stop_continuation(FakeStream([]), "别说了")
    assert not hub.processed and not hub.is_recording_command


def test_stop_continuation_max_length_prompts_without_submitting_truncated_audio(hub):
    globals_ = hub._listen_stop_continuation.__func__.__globals__
    globals_["create_endpointer"] = lambda **_kw: endpoint.EnergyEndpointer(noise_floor=.001, max_record_sec=.45)
    hub._listen_stop_continuation(FakeStream([block(.2)] * 10), "别说了")
    assert not hub.processed and not hub.greetings and not hub.is_recording_command
    assert len(hub.spoken) == 1 and "短句" in hub.spoken[0]


def test_stop_continuation_complete_utterance_keeps_existing_processing(hub):
    hub._listen_stop_continuation(FakeStream([block(.2)] * 4 + [block()] * 8), "别说了")
    assert len(hub.processed) == 1 and hub.processed[0][1].source == "bargein_continuation"
    assert not hub.is_recording_command


def test_stop_without_following_command_stays_quiet(hub):
    hub._listen_stop_continuation(FakeStream([block()] * 10), "别说了")
    assert not hub.processed and not hub.spoken and not hub.greetings
    assert not hub.is_recording_command
