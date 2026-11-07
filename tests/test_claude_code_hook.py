"""Claude Code 钩子：事件映射、只发本机、没令牌不发、报不出去也不报错。"""
import importlib.util
import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "integrations" / "claude_code_hooks" / "canaan_progress_hook.py"
spec = importlib.util.spec_from_file_location("canaan_progress_hook", PATH)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def test_event_mapping():
    w = hook.build({"hook_event_name": "Notification", "message": "Claude needs your permission to use Bash",
                    "cwd": "D:/work/my-app", "session_id": "s1"})
    assert w["kind"] == "waiting" and w["title"].startswith("my-app：") and len(w["title"]) <= 80
    assert hook.build({"hook_event_name": "Stop", "cwd": "D:/work/my-app"})["kind"] == "done"
    assert hook.build({"hook_event_name": "PreToolUse"}) is None


def test_only_local_and_needs_token(monkeypatch):
    sent = []
    monkeypatch.setattr(hook.urllib.request, "urlopen", lambda *a, **k: sent.append(a) or (_ for _ in ()).throw(OSError()))
    payload = {"kind": "done", "title": "x"}
    assert not hook.send(payload, url="http://evil.example.com:8789", token="t")
    assert not hook.send(payload, url="http://127.0.0.1:8789", token="")
    assert not hook.send(payload, url="http://127.0.0.1:1", token="t")       # 报不出去：返回 False，不抛异常
    assert len(sent) == 1


def test_main_never_fails(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main() == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"})))
    monkeypatch.delenv("CANAAN_PROGRESS_TOKEN", raising=False)
    assert hook.main() == 0
