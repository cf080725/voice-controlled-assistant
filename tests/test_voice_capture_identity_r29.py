"""Automatic recordings retain the identity that owned their first audio."""
import ast
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import canaan_config
import canaan_turnmerge

from test_endpoint_recording_regressions import hub, block, FakeStream
from canaan_voice_capture import VoiceCaptureContext


def identity(hub):
    token = ["owner", 1, 0, 0]
    binding = SimpleNamespace(token=tuple(token), cancelled=threading.Event())
    hub._capture_context = lambda: binding
    hub._context_token = lambda: tuple(token)
    def switch():
        token[0], token[1] = "guest", 2
        binding.cancelled.set()
    return token, binding, switch


def record(hub, entry, stream):
    if entry == "wake":
        hub.record_command_after_wake(stream, is_bargein=True)
    elif entry == "stop":
        hub._listen_stop_continuation(stream, "别说了")
    else:
        hub.record_followup(stream, block(.1))


@pytest.mark.parametrize("entry,phase", [
    ("wake", "record"), ("stop", "record"), ("followup", "record"),
    ("wake", "endpoint"), ("stop", "endpoint"), ("followup", "endpoint"),
    ("wake", "merge_window"), ("followup", "merge_window"),
    ("wake", "merge_resumed"), ("followup", "merge_resumed"),
])
def test_switch_during_automatic_recording_discards_audio_before_dispatch(hub, monkeypatch, entry, phase):
    _, _, switch = identity(hub)
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", phase.startswith("merge"))
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: .2)
    def observe(ep):
        if phase == "endpoint" and ep.state == "end":
            switch()
        return ep.state
    prefetch = SimpleNamespace(ticket=object(), observe=observe, discard=Mock())
    hub._begin_asr_prefetch = lambda **_: prefetch
    hub.process_audio_data = Mock()
    chunks = [block(.1)] * 4 + [block()] * 2
    chunks += ([block(.2)] * 3 + [block()] * 8 if phase == "merge_resumed" else [block()] * 8)
    switch_at = {"record": 2, "endpoint": -1, "merge_window": 7, "merge_resumed": 8}[phase]
    class Stream(FakeStream):
        def read(self, n):
            result = super().read(n)
            if len(self.reads) == switch_at:
                switch()
            return result
    stream = Stream(chunks)
    record(hub, entry, stream)
    hub.process_audio_data.assert_not_called()
    assert prefetch.discard.called
    assert not hub.is_recording_command and not hub.spoken and not hub.greetings
    if switch_at > 0:
        assert len(stream.reads) == switch_at, "old capture must stop without consuming the new person's next frame"


@pytest.mark.parametrize("entry", ["wake", "stop", "followup"])
def test_switch_at_final_waveform_handoff_cannot_rebind_audio_to_new_person(hub, entry):
    token, binding, switch = identity(hub)
    delivered, calls = [], []
    factory = hub.record_command_after_wake.__func__.__globals__["create_endpointer"]
    def create(**kwargs):
        ep = factory(**kwargs)
        original = ep.get_audio
        def audio(*args, **kw):
            result = original(*args, **kw)
            switch()
            return result
        ep.get_audio = audio
        return ep
    hub.record_command_after_wake.__func__.__globals__["create_endpointer"] = create
    def dispatch(audio, tracer, **kwargs):
        calls.append(kwargs)
        expected = kwargs.get("expected_context_token")
        if expected is None or expected == tuple(token):
            delivered.append(audio)
    hub.process_audio_data = dispatch
    record(hub, entry, FakeStream([block(.1)] * 4 + [block()] * 10))
    assert not delivered, "the processing entry would accept this old audio as the new person's turn"
    assert calls and calls[0]["expected_context_token"] == binding.token


def test_live_capture_uses_cancel_event_without_per_frame_database_queries():
    binding = SimpleNamespace(token=("owner", 1, 0, 0), cancelled=threading.Event())
    provider = Mock(return_value=binding.token)
    capture = VoiceCaptureContext(SimpleNamespace(_capture_context=lambda: binding, _context_token=provider))
    for _ in range(100):
        assert capture.current()
    provider.assert_not_called()
    assert capture.current(final=True)
    provider.assert_called_once()


def test_final_check_rejects_privacy_revision_changed_without_cancel_event():
    binding = SimpleNamespace(token=("owner", 1, 0, 0), cancelled=threading.Event())
    provider = Mock(return_value=("owner", 1, 1, 1))
    capture = VoiceCaptureContext(SimpleNamespace(_capture_context=lambda: binding, _context_token=provider))
    assert capture.current()  # The final database revision check is still required.
    assert not capture.current(final=True)


def test_cancelled_binding_rejects_capture_even_if_identity_token_matches():
    binding = SimpleNamespace(token=("owner", 1, 0, 0), cancelled=threading.Event())
    capture = VoiceCaptureContext(SimpleNamespace(_capture_context=lambda: binding,
                                                 _context_token=lambda: binding.token))
    binding.cancelled.set()
    assert not capture.current() and not capture.current(final=True)


@pytest.mark.parametrize("module_name", ["canaan_voice_capture.py", "canaan_asr.py", "canaan_asr_prefetch.py",
                                        "canaan_conversation_time.py", "canaan_memory.py", "canaan_retention.py",
                                        "canaan_asr_loader.py", "canaan_public_help.py", "canaan_persona.py"])
def test_runtime_build_id_tracks_the_voice_capture_module(tmp_path, module_name):
    source = Path(__file__).resolve().parents[1] / "plugins/canaan_api_server.py"
    function = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef) and n.name == "runtime_source_digest")
    namespace = {"ROOT": tmp_path}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    core = tmp_path / "core"
    core.mkdir()
    capture = core / module_name
    capture.write_text("revision 1", encoding="utf-8")
    digest = namespace["runtime_source_digest"]
    before = digest()
    capture.write_text("revision 2", encoding="utf-8")
    assert digest() != before, "health version must change when recording ownership logic changes"
