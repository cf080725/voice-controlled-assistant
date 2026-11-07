"""托盘的健康检查（每 5 秒一次）：主动关闭不重启并停语音服务；hub 崩了按退避重启并记退出码；
等语音服务端口起来再启动 hub；外面启动的 hub 不重复拉；Ollama 忙时不再拉一个；退避在稳定运行后清零；日志轮转。
全部用替身子进程，不启动任何东西。"""
import os

import pytest

tray = pytest.importorskip("canaan_tray")


class FakeProc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


class FakeChild:
    def __init__(self, alive=False, proc=None):
        self._alive, self.proc = alive, proc
        self.backoff = tray.Backoff(clock=lambda: 0.0)
        self.restarts, self.started, self.stopped = 0, 0, 0

    def alive(self):
        return self._alive

    def start(self):
        self.started += 1
        self._alive, self.proc = True, FakeProc()

    def stop(self):
        self.stopped += 1
        self._alive = False


def make(monkeypatch, tmp_path, *, hub=None, tts=None, ollama_ok=True, port=True, game=False, stop_flag=False, external=False):
    sup = object.__new__(tray.Supervisor)
    sup.hub, sup.tts, sup.ollama = hub or FakeChild(), tts or FakeChild(), FakeChild()
    sup.status, sup.hub_external, sup.on_status = {}, False, None
    import threading
    sup._lock = threading.Lock()
    flag = tmp_path / "stop.flag"
    if stop_flag:
        flag.write_text("1")
    monkeypatch.setattr(tray, "STOP_FLAG", str(flag))
    monkeypatch.setattr(tray, "EXIT_LOG", str(tmp_path / "exits.jsonl"))
    monkeypatch.setattr(tray, "ollama_up", lambda: ollama_ok)
    monkeypatch.setattr(tray, "ollama_port_open", lambda: False)
    monkeypatch.setattr(tray, "port_up", lambda p: port)
    monkeypatch.setattr(tray, "mutex_exists", lambda name: external)
    monkeypatch.setattr(tray.canaan_gamemode, "active", lambda: game)
    monkeypatch.setattr(tray.canaan_config, "TTS_MODE", "gpu", raising=False)
    return sup


def test_manual_stop_records_exit_and_stops_tts(monkeypatch, tmp_path):
    hub = FakeChild(alive=False, proc=FakeProc(0))
    tts = FakeChild(alive=True)
    sup = make(monkeypatch, tmp_path, hub=hub, tts=tts, stop_flag=True)
    sup.check_once()
    assert sup.status["hub"].startswith("已停止") and tts.stopped == 1 and hub.started == 0
    assert "正常关闭" in (tmp_path / "exits.jsonl").read_text(encoding="utf-8")


def test_crash_is_classified_and_backed_off(monkeypatch, tmp_path):
    hub = FakeChild(alive=False, proc=FakeProc(0xC0000005))
    sup = make(monkeypatch, tmp_path, hub=hub)
    sup.check_once()
    assert "崩溃（访问冲突）" in sup.status["hub"] and hub.proc is None and hub.started == 0
    assert "崩溃" in (tmp_path / "exits.jsonl").read_text(encoding="utf-8")


def test_starts_hub_once_tts_port_is_up(monkeypatch, tmp_path):
    sup = make(monkeypatch, tmp_path, port=False)
    sup.tts._alive = True
    sup.check_once()
    assert sup.hub.started == 0                                     # 语音服务还没起来，先等
    monkeypatch.setattr(tray, "port_up", lambda p: True)
    sup.check_once()
    assert sup.hub.started == 1 and sup.status["hub"] == "运行中"


def test_game_mode_starts_hub_without_tts(monkeypatch, tmp_path):
    sup = make(monkeypatch, tmp_path, port=False, game=True)
    sup.check_once()
    assert sup.hub.started == 1 and sup.tts.started == 0 and "游戏" in sup.status["tts"]


def test_external_hub_not_duplicated(monkeypatch, tmp_path):
    sup = make(monkeypatch, tmp_path, external=True)
    sup.check_once()
    assert sup.hub.started == 0 and sup.hub_external and "非托盘" in sup.status["hub"]


def test_ollama_states(monkeypatch, tmp_path):
    sup = make(monkeypatch, tmp_path, ollama_ok=False)
    monkeypatch.setattr(tray, "ollama_port_open", lambda: True)
    sup.check_once()
    assert sup.status["ollama"] == "忙" and sup.ollama.started == 0
    monkeypatch.setattr(tray, "ollama_port_open", lambda: False)
    sup.check_once()
    assert sup.status["ollama"] == "已重启" and sup.ollama.started == 1


def test_backoff_grows_and_resets_after_stable_run():
    t = [0.0]
    b = tray.Backoff(delays=(5, 10, 30), stable=600, clock=lambda: t[0])
    b.started()
    assert [b.failed(), b.failed(), b.failed(), b.failed()] == [5, 10, 30, 30]
    assert not b.ready()
    t[0] = 1000
    assert b.ready() and b.failed() == 5                            # 稳定运行 10 分钟后清零


def test_rotate_file(tmp_path):
    p = tmp_path / "hub.log"
    p.write_bytes(b"x" * 20)
    (tmp_path / "hub.log.1").write_bytes(b"old1")
    tray.rotate_file(str(p), max_bytes=10, keep=3)
    assert not p.exists() and (tmp_path / "hub.log.1").read_bytes() == b"x" * 20
    assert (tmp_path / "hub.log.2").read_bytes() == b"old1"
    tray.rotate_file(str(tmp_path / "missing.log"))                 # 没有文件不报错
    small = tmp_path / "s.log"
    small.write_bytes(b"x")
    tray.rotate_file(str(small), max_bytes=10)
    assert small.exists()


def test_port_up_closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert tray.port_up(port, timeout=0.2) is False
    assert os.path.basename(tray.STOP_FLAG) == "hub_stopped.flag"
