"""Background model work must not start in the middle of a recorded command."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import canaan_gpu_priority as gpu
import pytest


class EndLoop(BaseException):
    pass


def loop_method(name, *, before_sleep=False):
    source = Path(__file__).resolve().parents[1] / "core/canaan_desk_os_hub.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CanaanDeskOSHub")
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if not before_sleep or len(sleeps) == 2:
            raise EndLoop()

    globals_ = {
        "time": SimpleNamespace(time=lambda: 1000, sleep=sleep),
        "TuantuanVoiceEngine": SimpleNamespace(last_tts_call=0),
        "canaan_config": SimpleNamespace(TTS_MODE="gpu"),
        "canaan_gamemode": SimpleNamespace(active=lambda: False),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), globals_)
    return globals_[name]


@pytest.mark.parametrize("recording", [True, False])
def test_tts_warmup_defers_while_recording(recording):
    hub = SimpleNamespace(is_recording_command=recording, is_speaking=False, is_processing=False,
                          engine=SimpleNamespace(warm_up=Mock(return_value=100)))
    with pytest.raises(EndLoop):
        loop_method("_tts_keep_warm")(hub)
    assert hub.engine.warm_up.call_count == (0 if recording else 1)


@pytest.mark.parametrize("busy", ["is_recording_command", "is_speaking", "is_processing", None])
def test_memory_timer_defers_for_active_voice_and_demo(monkeypatch, busy):
    monkeypatch.setattr(gpu, "demo_mode", lambda: False)
    hub = SimpleNamespace(memory_mgr=SimpleNamespace(tick=Mock()), is_recording_command=False,
                          is_speaking=False, is_processing=False)
    if busy:
        setattr(hub, busy, True)
    with pytest.raises(EndLoop):
        loop_method("_memory_tick_loop", before_sleep=True)(hub)
    assert hub.memory_mgr.tick.call_count == (0 if busy else 1)


def test_memory_timer_does_not_run_during_demo(monkeypatch):
    monkeypatch.setattr(gpu, "demo_mode", lambda: True)
    hub = SimpleNamespace(memory_mgr=SimpleNamespace(tick=Mock()), is_recording_command=False,
                          is_speaking=False, is_processing=False)
    with pytest.raises(EndLoop):
        loop_method("_memory_tick_loop", before_sleep=True)(hub)
    hub.memory_mgr.tick.assert_not_called()
