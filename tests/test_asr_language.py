"""SenseVoice language comes from canaan_config.ASR_LANGUAGE (default zh, switchable to auto)."""
import importlib
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import canaan_asr
import canaan_config


class FakeModel:
    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return [{"text": "<|zh|><|NEUTRAL|><|Speech|><|withitn|>确认。"}]


def test_default_language_is_zh(monkeypatch):
    monkeypatch.delenv("CANAAN_ASR_LANGUAGE", raising=False)
    try:
        assert importlib.reload(canaan_config).ASR_LANGUAGE == "zh"
    finally:
        importlib.reload(canaan_config)


def test_env_switch_back_to_auto(monkeypatch):
    monkeypatch.setenv("CANAAN_ASR_LANGUAGE", "auto")
    try:
        assert importlib.reload(canaan_config).ASR_LANGUAGE == "auto"
    finally:
        monkeypatch.delenv("CANAAN_ASR_LANGUAGE")
        importlib.reload(canaan_config)


def test_transcribe_passes_configured_language(monkeypatch):
    model = FakeModel()
    for language in ("zh", "auto"):
        monkeypatch.setattr(canaan_config, "ASR_LANGUAGE", language)
        text, engine = canaan_asr.transcribe(np.zeros(1600, np.float32), sense_voice=model)
        assert (text, engine) == ("确认。", "sensevoice")
        assert model.calls[-1]["language"] == language
        assert model.calls[-1]["use_itn"] is True
