# -*- coding: utf-8 -*-
import os

import canaan_gamemode as gm


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_backoff_grows_and_resets_after_stable_run():
    from canaan_tray import Backoff
    clk = _Clock()
    b = Backoff(delays=(5, 10, 30), stable=600, clock=clk)
    b.started()
    assert [b.failed() for _ in range(4)] == [5, 10, 30, 30]
    assert not b.ready()
    clk.t += 31
    assert b.ready()
    b.started()
    clk.t += 700  # 稳定运行超过 10 分钟后再挂，从最短间隔重新开始
    assert b.failed() == 5


def test_rotate_file(tmp_path):
    from canaan_tray import rotate_file
    p = tmp_path / "hub.log"
    p.write_bytes(b"x" * 100)
    rotate_file(str(p), max_bytes=50, keep=2)
    assert not p.exists() and (tmp_path / "hub.log.1").stat().st_size == 100
    p.write_bytes(b"y" * 10)
    rotate_file(str(p), max_bytes=50, keep=2)  # 没超过上限不轮转
    assert p.exists()


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(gm, "FLAG_PATH", str(tmp_path / "game_mode.flag"))
    monkeypatch.setattr(gm, "FORCE_PATH", str(tmp_path / "game_mode.force"))
    monkeypatch.setattr(gm, "DATA_DIR", str(tmp_path))
    gm._cache["t"] = 0.0


def test_game_mode_enter_and_exit_with_grace(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(gm, "unload_ollama_models", lambda timeout=5: calls.append("unload") or ["m"])
    w = gm.GameModeWatcher(stop_tts=lambda: calls.append("stop") or 1, start_tts=lambda: calls.append("start"),
                           log=lambda *a: None)
    game = {"name": "ExampleGame.exe"}
    monkeypatch.setattr(gm, "running_game", lambda: game["name"])
    assert w.step(now=0) and gm.active() and calls == ["unload", "stop"]
    game["name"] = None
    assert w.step(now=gm.EXIT_GRACE_SEC - 1)  # 刚退出游戏：先等一会儿，避免切窗口时来回切换
    assert w.step(now=gm.EXIT_GRACE_SEC + 1) is False and not gm.active()
    assert calls[-1] == "start"


def test_game_mode_forced(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(gm, "unload_ollama_models", lambda timeout=5: [])
    monkeypatch.setattr(gm, "running_game", lambda: "ExampleGame.exe")
    w = gm.GameModeWatcher(stop_tts=lambda: 0, start_tts=lambda: None, log=lambda *a: None)
    gm.set_forced("off")
    assert w.step(now=0) is False and not gm.active()
    gm.set_forced("on")
    assert w.step(now=1) and gm.active()
    gm.set_forced(None)
    assert not os.path.exists(gm.FORCE_PATH)


def test_hub_skips_llm_in_game_mode(monkeypatch):
    from canaan_desk_os_hub import CanaanDeskOSHub
    hub = object.__new__(CanaanDeskOSHub)

    class _NoLLM:
        def decide(self, *a, **k):
            raise AssertionError("游戏模式下不应调用大模型")

    hub.intent_client = _NoLLM()
    monkeypatch.setattr(gm, "active", lambda max_age=1.0: True)
    d = hub.query_canaan_os("今天天气怎么样")
    assert d["action"] == "chat" and d["_model"] == "game_mode"


def test_speaker_gate_rejects_other_voice(tmp_path, monkeypatch):
    import numpy as np
    import canaan_speaker as cs

    class FakeV:
        enrolled = True

        def score(self, samples, sr):
            return float(samples.mean())  # 用均值模拟相似度

    g = cs.SpeakerGate(enabled=False, threshold=0.6, flag_path=str(tmp_path / "lock.flag"))
    g._v = FakeV()
    assert g.check(np.full(16000, 0.1, np.float32)) == (True, None, 0.0)  # 关闭时一律放行
    (tmp_path / "lock.flag").write_text("")
    ok, s, _ = g.check(np.full(16000, 0.1, np.float32))
    assert not ok and abs(s - 0.1) < 1e-6
    assert g.check(np.full(16000, 0.9, np.float32))[0]


def test_vram_tiers():
    import canaan_tiers as t
    assert t.pick_tier(8151) == "8gb" and t.pick_tier(6144) == "6gb" and t.pick_tier(4096) == "4gb" and t.pick_tier(0) == "4gb"
    g = {}
    assert t.apply(g, "4gb")[0] == "4gb" and g["LLM_ENABLED"] is False and g["TTS_MODE"] == "cpu"


def test_low_vram_tier_never_calls_llm(monkeypatch):
    import canaan_config
    from canaan_desk_os_hub import CanaanDeskOSHub
    hub = object.__new__(CanaanDeskOSHub)

    class _NoLLM:
        def decide(self, *a, **k):
            raise AssertionError("4GB 档不应调用大模型")

    hub.intent_client = _NoLLM()
    monkeypatch.setattr(canaan_config, "LLM_ENABLED", False)
    monkeypatch.setattr(gm, "active", lambda max_age=1.0: False)
    assert hub.query_canaan_os("今天天气怎么样")["action"] == "chat"
