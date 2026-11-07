# -*- coding: utf-8 -*-
"""连续对话窗口：不说话则结束、说话则交给处理流程。用假麦克风流 + 真实 silero VAD。"""
import os
import glob
import queue
import time
import numpy as np
import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAD = os.path.join(BASE_DIR, "kws_models", "vad", "silero_vad.onnx")
WAVS = sorted(glob.glob(os.path.join(BASE_DIR, "dataset_tuantuan", "*.wav")))
pytestmark = pytest.mark.skipif(not (os.path.exists(VAD) and WAVS), reason="需要 silero 模型与 dataset_tuantuan 录音")


class FakeStream:
    def __init__(self, audio):
        self.audio = audio.astype(np.float32)
        self.pos = 0

    def read(self, n):
        chunk = self.audio[self.pos:self.pos + n]
        self.pos += n
        if len(chunk) < n:
            chunk = np.concatenate([chunk, np.zeros(n - len(chunk), dtype=np.float32)])
        return chunk.reshape(-1, 1), False


def _hub():
    from canaan_desk_os_hub import CanaanDeskOSHub
    hub = object.__new__(CanaanDeskOSHub)  # 不创建 Tk 窗口
    hub.ui_queue = queue.Queue()
    hub.ambient_noise_floor = 0.002
    hub.is_recording_command = False
    hub.processed = []
    hub.processed_contexts = []
    def process(audio, tracer=None, **kwargs):
        hub.processed.append((len(audio), tracer))
        hub.processed_contexts.append(kwargs.get("expected_context_token"))
    hub.process_audio_data = process
    return hub


def _speech():
    import soundfile as sf
    data, fs = sf.read(WAVS[0], dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if fs != 16000:
        n = int(len(data) * 16000 / fs)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data).astype(np.float32)
    return data / (np.abs(data).max() or 1) * 0.3


def test_silence_ends_window():
    hub = _hub()
    hub.followup_until = time.time() + 1.0
    noise = np.random.default_rng(0).standard_normal(16000 * 5).astype(np.float32) * 0.002
    s = FakeStream(noise)
    hub.record_followup(s, s.read(1600)[0].reshape(-1))
    assert hub.processed == []
    assert hub.followup_until == 0.0 and not hub.is_recording_command
    assert ("followup_ended", None) in list(hub.ui_queue.queue)
    assert s.pos <= 16000 * 2  # 窗口 1s，不会一直录下去


def test_speech_in_window_is_processed():
    hub = _hub()
    hub.followup_until = time.time() + 5.0
    rng = np.random.default_rng(1)
    audio = np.concatenate([rng.standard_normal(8000).astype(np.float32) * 0.002, _speech(),
                            rng.standard_normal(16000 * 3).astype(np.float32) * 0.002])
    s = FakeStream(audio)
    hub.record_followup(s, s.read(1600)[0].reshape(-1))
    assert len(hub.processed) == 1
    assert hub.processed[0][1].source == "followup"
    assert hub.processed_contexts == [hub._context_token()]


def test_merge_wait_joins_resumed_speech(monkeypatch):
    """拼接开启时：判定说完后窗口内又开口，接着录同一句；关闭时直接返回。"""
    import numpy as np
    import canaan_config
    from canaan_desk_os_hub import CanaanDeskOSHub
    from canaan_endpoint import EnergyEndpointer
    hub = object.__new__(CanaanDeskOSHub)
    loud, quiet = np.full(1600, 0.2, np.float32), np.zeros(1600, np.float32)
    seq = [quiet] * 3 + [loud] * 5 + [quiet] * 30

    class S:
        i = 0

        def read(self, n):
            c = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return c.reshape(-1, 1), False

    ep = EnergyEndpointer(noise_floor=0.001, hangover_short=0.3, hangover_long=0.3)
    for _ in range(5):
        ep.feed(loud)
    for _ in range(4):
        st = ep.feed(quiet)
    assert st == "end"
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", False, raising=False)
    assert hub._merge_wait(S(), ep, st) == "end" and len(ep.chunks) == 9
    monkeypatch.setattr(canaan_config, "TURN_MERGE_ENABLED", True, raising=False)
    n0 = ep.speech_samples
    assert hub._merge_wait(S(), ep, st) == "end"
    assert ep.speech_samples > n0  # 后半句被接进同一句
