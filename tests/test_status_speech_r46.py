"""Status answers reach the real short-speech policy; hardware is synthetic."""
import ast
import math
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_file(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def status(monkeypatch):
    models = load_file(monkeypatch, "_r46_status_models", ROOT / "core/canaan_action_framework/models.py")
    load_file(monkeypatch, "canaan_reply_policy", ROOT / "core/canaan_reply_policy.py")
    turns = load_file(monkeypatch, "_r46_status_turn", ROOT / "core/canaan_turn_pipeline.py")
    source = ROOT / "core/canaan_action_framework/actions/system_actions.py"
    node = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body
                if isinstance(n, ast.ClassDef) and n.name == "SystemStatusAction")
    calls, free = [], {"C:\\": 128.5, "D:\\": 256.8}

    def disk_usage(drive):
        calls.append(drive)
        if drive not in free:
            raise OSError("Synthetic unavailable disk")
        return SimpleNamespace(free=free[drive] * 1024 ** 3)

    import re
    ns = {"BaseAction": object, "ActionRegistry": SimpleNamespace(register=lambda cls: cls),
          "ActionCategory": models.ActionCategory, "PermissionLevel": models.PermissionLevel,
          "ActionContext": models.ActionContext, "ActionResult": models.ActionResult, "re": re, "math": math,
          "psutil": SimpleNamespace(cpu_percent=lambda **k: 12.3,
              virtual_memory=lambda: SimpleNamespace(percent=45.6),
              sensors_battery=lambda: SimpleNamespace(percent=78), disk_usage=disk_usage)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)

    def invoke(text):
        result = ns["SystemStatusAction"]().execute(models.ActionContext("system_status", user_text=text))
        spoken = turns.spoken_result("system_status", {"reply": "正在检测整机负载。"}, result.to_dict(), "rule")
        return result, spoken

    return SimpleNamespace(invoke=invoke, free=free, calls=calls)


@pytest.mark.parametrize("text", ["查一下硬盘还剩多少容亮", "硬盘还剩多少空间", "查一下磁盘还剩多少容量，然后截图", "查一下硬盘还剩多少空间吧！"])
@pytest.mark.parametrize("missing", [None, "D:\\"])
def test_disk_request_is_answered_before_short_speech_truncation(status, text, missing):
    if missing:
        del status.free[missing]
    result, spoken = status.invoke(text)
    assert result.success and result.detail["disks_free_gb"] == {d[0]: v for d, v in status.free.items()}
    for drive, free in status.free.items():
        assert drive[0] + "盘" in spoken and str(free) in spoken
    assert "CPU" not in spoken and len(spoken) <= 24
    assert status.calls == ["C:\\", "D:\\"]


def test_unreadable_disks_do_not_turn_into_a_cpu_answer_or_invent_capacity(status):
    status.free.clear()
    result, spoken = status.invoke("查一下硬盘还剩多少容亮")
    assert not result.success and result.error_code == "STATUS_UNAVAILABLE"
    assert result.detail == {"query": "legacy_disks", "complete": False}
    assert "disks_free_gb" not in result.detail
    assert status.calls == ["C:\\", "D:\\"]
    assert "未能读取" in spoken and "空间" in spoken and "CPU" not in spoken


def test_large_disk_answers_keep_both_drives_within_short_speech_budget(status):
    status.free.update({"C:\\": 100000.1, "D:\\": 200000.2})
    _, spoken = status.invoke("硬盘还剩多少空间")
    assert "C盘" in spoken and "D盘" in spoken and len(spoken) <= 24
    assert "97.7T" in spoken and "195.3T" in spoken


@pytest.mark.parametrize("text", ["看看CPU占用", "查询电脑状态", "看看CPU占用，别查硬盘剩余空间", "硬盘剩余空间，先别查，看看CPU占用"])
def test_other_status_requests_keep_cpu_and_memory_answer(status, text):
    result, spoken = status.invoke(text)
    assert result.success and "CPU" in spoken and "12.3" in spoken and "45.6" in spoken
