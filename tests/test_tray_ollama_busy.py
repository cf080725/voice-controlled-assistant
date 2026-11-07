"""托盘健康检查：Ollama 端口还在听只是回得慢，标“忙”，不再重复拉起 ollama serve。"""
from types import SimpleNamespace

import canaan_tray as tray


def _sup(tmp_path, started):
    sup = object.__new__(tray.Supervisor)
    sup.status, sup.on_status = {}, None
    sup.ollama = SimpleNamespace(alive=lambda: False, backoff=SimpleNamespace(ready=lambda: True, failed=lambda: None),
                                 start=lambda: started.append("ollama"))
    sup.hub = SimpleNamespace(alive=lambda: True, proc=object())
    flag = tmp_path / "stop.flag"
    flag.write_text("x")
    return sup, str(flag)


def test_busy_ollama_not_restarted(tmp_path, monkeypatch):
    started = []
    sup, flag = _sup(tmp_path, started)
    monkeypatch.setattr(tray, "STOP_FLAG", flag)
    monkeypatch.setattr(tray.canaan_gamemode, "active", lambda: False)
    monkeypatch.setattr(tray, "ollama_up", lambda timeout=2: False)
    monkeypatch.setattr(tray, "ollama_port_open", lambda port=11434, timeout=.5: True)
    sup.check_once()
    assert sup.status["ollama"] == "忙" and started == []


def test_dead_ollama_restarted(tmp_path, monkeypatch):
    started = []
    sup, flag = _sup(tmp_path, started)
    monkeypatch.setattr(tray, "STOP_FLAG", flag)
    monkeypatch.setattr(tray.canaan_gamemode, "active", lambda: False)
    monkeypatch.setattr(tray, "ollama_up", lambda timeout=2: False)
    monkeypatch.setattr(tray, "ollama_port_open", lambda port=11434, timeout=.5: False)
    sup.check_once()
    assert sup.status["ollama"] == "已重启" and started == ["ollama"]
