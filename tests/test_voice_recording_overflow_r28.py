"""A microphone overrun must not turn a missing negation into an executable fragment."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import canaan_config
import canaan_turnmerge

from test_endpoint_recording_regressions import hub, block, FakeStream


class OverrunStream(FakeStream):
    def __init__(self, chunks, overrun_at):
        super().__init__(chunks)
        self.overrun_at = overrun_at

    def read(self, frames):
        samples, _ = super().read(frames)
        return samples, len(self.reads) == self.overrun_at


@pytest.mark.parametrize("entry,phase", [
    ("wake", "recording"), ("wake_prompt", "recording"), ("wake_tone", "recording"),
    ("stop", "recording"), ("followup", "recording"),
    ("wake", "merge_window"), ("followup", "merge_window"),
    ("wake", "merge_resumed"), ("followup", "merge_resumed"),
])
def test_audio_overrun_aborts_entire_recording_and_discards_prefetch(hub, monkeypatch, entry, phase):
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", phase != "recording")
    monkeypatch.setattr(canaan_turnmerge, "window_sec", lambda: .2)
    prefetch = SimpleNamespace(ticket=object(), observe=lambda ep: ep.state, discard=Mock())
    hub._begin_asr_prefetch = Mock(return_value=prefetch)
    hub._pending_voice_audio_gap = Mock()
    hub.process_audio_data = Mock()
    chunks = [block(.1)] * 3 + [block()] * 2
    if phase == "merge_resumed":
        chunks += [block(.2)] * 3 + [block()] * 8
    else:
        chunks += [block()] * 8
    if entry in {"wake_prompt", "wake_tone"}:
        chunks.insert(0, block(.9))  # Prompt tone is deliberately not part of the command.
    overrun_at = 1 if entry == "wake_tone" else {"recording": 2, "merge_window": 6, "merge_resumed": 7}[phase]
    stream = OverrunStream(chunks, overrun_at)
    if entry in {"wake_prompt", "wake_tone"}:
        hub.record_command_after_wake(stream, is_bargein=False)
    elif entry == "wake":
        hub.record_command_after_wake(stream, is_bargein=True)
    elif entry == "followup":
        hub.record_followup(stream, block(.1))
    else:
        hub._listen_stop_continuation(stream, "别说了")
    hub.process_audio_data.assert_not_called()
    assert prefetch.discard.call_count == int(hub._begin_asr_prefetch.called)
    hub._pending_voice_audio_gap.assert_called_once()
    assert not hub.is_recording_command
    assert not hub.greetings and not hub.spoken
