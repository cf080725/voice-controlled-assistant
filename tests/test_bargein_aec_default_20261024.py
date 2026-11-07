"""10-24：播报中说“停 / 别说了”停不下来。BARGEIN_AEC 从没写进配置，打断一直用没消回声的麦克风。"""
import ast
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import canaan_aec
import canaan_config
import numpy as np


def method(name, **namespace):
    path = Path(__file__).resolve().parents[1] / "core/canaan_desk_os_hub.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CanaanDeskOSHub")
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def test_bargein_aec_is_on_by_default_with_numpy_engine():
    assert canaan_config.BARGEIN_AEC is True
    assert canaan_config.BARGEIN_AEC_ENGINE == "numpy"
    assert canaan_config.BARGEIN_KWS_THRESHOLD == 0.2


def test_bargein_input_subtracts_playback_with_numpy_canceller(monkeypatch):
    made = {}

    class Detector:
        def __init__(self, keywords_threshold):
            made["threshold"] = keywords_threshold

    fake_kws = ModuleType("canaan_wake_word_engine")
    fake_kws.CanaanWakeWordDetector = Detector
    monkeypatch.setitem(sys.modules, "canaan_wake_word_engine", fake_kws)
    monkeypatch.setattr(canaan_aec, "REF", canaan_aec.RefTap())

    bargein_input = method("_bargein_input", canaan_config=canaan_config, time=time)
    hub = SimpleNamespace(kws_detector=object(), _aec=None)
    mic = (np.random.default_rng(0).standard_normal(1600) * 0.1).astype(np.float32)
    detector, feed = bargein_input(hub, mic)

    assert isinstance(hub._aec, canaan_aec.EchoCanceller)  # 不是 WebRTC
    assert isinstance(detector, Detector) and made["threshold"] == 0.2
    # 按 256 样本一块处理，零头留到下一块，连续送入不丢样本
    _, feed2 = bargein_input(hub, mic)
    assert len(feed) + len(feed2) == 2 * len(mic) // 256 * 256
