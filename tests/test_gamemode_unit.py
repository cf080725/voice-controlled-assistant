"""游戏模式：识别游戏进程 / 全屏；进入时卸载模型、停语音服务，退出时恢复；手动开关优先；
退出游戏后多等一会儿防抖。Ollama、进程列表全是替身——测试绝不碰真实的 Ollama 和语音服务。"""
import io
import json
import types

import pytest

import canaan_gamemode as gm


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gm, "FLAG_PATH", str(tmp_path / "game_mode.flag"))
    monkeypatch.setattr(gm, "FORCE_PATH", str(tmp_path / "game_mode.force"))
    monkeypatch.setattr(gm, "_cache", {"t": 0.0, "v": False})

    def no_network(*a, **k):
        raise AssertionError("测试不能连真实的 Ollama")
    monkeypatch.setattr(gm.urllib.request, "urlopen", no_network)
    return tmp_path


def procs(*names, cmd=None):
    out = []
    for n in names:
        out.append(types.SimpleNamespace(info={"name": n, "cmdline": cmd or []}))
    return out


def test_flag_and_forced(paths):
    assert not gm.active()
    gm.set_flag(True, "测试")
    assert gm.active() and json.load(open(gm.FLAG_PATH, encoding="utf-8"))["reason"] == "测试"
    gm.set_flag(False)
    gm.set_flag(False)                                         # 重复删除不报错
    assert not gm.active()
    gm.set_forced("on")
    assert gm.forced() == "on"
    gm.set_forced(None)
    gm.set_forced(None)
    assert gm.forced() is None
    (paths / "game_mode.force").write_text("乱写")
    assert gm.forced() is None


def test_running_game_by_process_or_fullscreen(paths, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs("explorer.exe", "ExampleGame.exe"))
    assert gm.running_game() == "ExampleGame.exe"
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs("explorer.exe"))
    monkeypatch.setattr(gm, "ON_FULLSCREEN", True)
    monkeypatch.setattr(gm, "fullscreen_d3d", lambda: True)
    assert gm.running_game() == "全屏程序"
    monkeypatch.setattr(gm, "fullscreen_d3d", lambda: False)
    assert gm.running_game() is None


def test_unload_models_uses_keep_alive_zero(paths, monkeypatch):
    sent = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/api/ps"):
            return io.BytesIO(json.dumps({"models": [{"name": "a:1b"}, {"name": "b:3b"}]}).encode())
        body = json.loads(req.data)
        if body["model"] == "b:3b":
            raise OSError("卸载失败")
        sent.append(body)
        return io.BytesIO(b"{}")
    monkeypatch.setattr(gm.urllib.request, "urlopen", urlopen)
    assert gm.unload_ollama_models() == ["a:1b"] and sent == [{"model": "a:1b", "keep_alive": 0}]


def test_unload_models_when_ollama_down(paths, monkeypatch):
    monkeypatch.setattr(gm.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("连不上")))
    assert gm.unload_ollama_models() == []


def test_stop_tts_only_targets_tts_service(paths, monkeypatch):
    import psutil
    killed = []
    tts = types.SimpleNamespace(info={"cmdline": ["python", "api_v2.py", "-p", "9880"]}, terminate=lambda: killed.append("tts"))
    other = types.SimpleNamespace(info={"cmdline": ["python", "other.py"]}, terminate=lambda: killed.append("other"))
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [tts, other])
    monkeypatch.setattr(psutil, "wait_procs", lambda p, timeout=None: None)
    assert gm.stop_tts_service() == 1 and killed == ["tts"]


def test_watcher_enter_exit_and_grace(paths, monkeypatch):
    log, changes, started = [], [], []
    monkeypatch.setattr(gm, "unload_ollama_models", lambda: ["m"])
    w = gm.GameModeWatcher(stop_tts=lambda: 1, start_tts=lambda: started.append(1),
                           on_change=lambda on, r: changes.append((on, r)), log=log.append)
    monkeypatch.setattr(gm, "running_game", lambda: "ExampleGame.exe")
    assert w.step(now=100.0) is True and gm.active() and changes == [(True, "ExampleGame.exe")]
    monkeypatch.setattr(gm, "running_game", lambda: None)
    gm._cache["t"] = 0.0
    assert w.step(now=110.0) is True                           # 刚退出游戏：再等一会儿
    gm._cache["t"] = 0.0
    assert w.step(now=100.0 + gm.EXIT_GRACE_SEC + 1) is False and not gm.active()
    assert changes[-1] == (False, "") and started == [1]


def test_watcher_forced_and_start_tts_failure(paths, monkeypatch):
    log = []
    monkeypatch.setattr(gm, "unload_ollama_models", lambda: [])
    w = gm.GameModeWatcher(stop_tts=None, start_tts=lambda: 1 / 0, log=log.append)
    gm.set_forced("on")
    monkeypatch.setattr(gm, "running_game", lambda: pytest.fail("手动开关时不该检测进程"))
    assert w.step(now=1.0) is True and gm.active()
    gm.set_forced("off")
    gm._cache["t"] = 0.0
    assert w.step(now=2.0) is False and any("恢复语音服务失败" in m for m in log)


def test_watcher_run_loop_survives_errors(paths, monkeypatch):
    log = []
    w = gm.GameModeWatcher(log=log.append)
    calls = [0]

    def step():
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("偶发")
        w.stop()
    w.step = step
    monkeypatch.setattr(gm, "POLL_SEC", 0)
    w.run()
    assert calls[0] == 2 and any("检测异常" in m for m in log)
