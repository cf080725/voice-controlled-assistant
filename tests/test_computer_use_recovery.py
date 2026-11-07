from canaan_computer_use import CanaanComputerUse as CU
import pytest


@pytest.fixture(autouse=True)
def no_native_input(monkeypatch):
    monkeypatch.setattr(CU, "release_all_modifiers", lambda: None)


def test_failed_step_stops_remaining_actions(monkeypatch):
    steps = [{"action": "focus_window", "params": {}}, {"action": "type_text", "params": {"text": "must not type"}}]
    monkeypatch.setattr(CU, "_parse_deterministic_task", lambda _: steps)
    monkeypatch.setattr(CU, "_check_failsafe", lambda: None)
    calls = []
    monkeypatch.setattr(CU, "execute_action", lambda a, p: calls.append(a) or {"success": False, "error": "window missing"})
    r = CU.execute_task("test")
    assert r["success"] is False and r["error_code"] == "STEP_FAILED"
    assert calls == ["focus_window"]


def test_no_plan_never_types_instruction(monkeypatch):
    monkeypatch.setattr(CU, "_parse_deterministic_task", lambda _: None)
    monkeypatch.setattr(CU, "_plan_with_local_llm", lambda _: [])
    monkeypatch.setattr(CU, "execute_action", lambda *a: (_ for _ in ()).throw(AssertionError("unexpected input")))
    assert CU.execute_task("unknown instruction")["error_code"] == "NO_PLAN"


def test_step_limit_does_not_execute_partial_plan(monkeypatch):
    monkeypatch.setattr(CU, "_parse_deterministic_task", lambda _: [{"action": "wait"}] * 11)
    assert CU.execute_task("test")["error_code"] == "STEP_LIMIT"


def test_user_stop_prevents_next_step(monkeypatch):
    monkeypatch.setattr(CU, "_parse_deterministic_task", lambda _: [{"action": "wait"}, {"action": "wait"}])
    calls = []
    def action(a, p):
        calls.append(a)
        CU.request_stop()
        return {"success": True}
    monkeypatch.setattr(CU, "execute_action", action)
    monkeypatch.setattr(CU, "get_cursor_pos", lambda: (100, 100))
    r = CU.execute_task("test")
    CU._stop_requested.clear()
    assert not r["success"] and r["stopped_by_user"]
    assert len(calls) == 1


def test_registry_preserves_failure_and_requires_llm_confirmation(monkeypatch):
    from canaan_action_framework import ActionRegistry
    monkeypatch.setattr(CU, "get_active_window", lambda: {"title": "测试窗口"})
    monkeypatch.setattr(CU, "execute_task", lambda *a, **k: {"success": False, "error_code": "NO_PLAN", "message": "没有可靠步骤"})
    assert ActionRegistry.dispatch("computer_use", "测试", source="llm").error_code == "CONFIRM_REQUIRED"
    result = ActionRegistry.dispatch("computer_use", "测试", source="rule", confirmed=True)
    assert not result.success and result.error_code == "NO_PLAN"
    assert "完成" not in result.speech_override


def test_llm_plan_cannot_invent_coordinates_or_text(monkeypatch):
    monkeypatch.setattr(CU, "_parse_deterministic_task", lambda _: None)
    monkeypatch.setattr(CU, "execute_action", lambda *a: pytest.fail("不得输入"))
    monkeypatch.setattr(CU, "_plan_with_local_llm", lambda _: [{"action": "move_to", "params": {"x": 999, "y": 999}}])
    assert CU.execute_task("去点那个按钮")["error_code"] == "NO_VISUAL_TARGET"
    monkeypatch.setattr(CU, "_plan_with_local_llm", lambda _: [{"action": "type_text", "params": {"text": "未授权脚本", "enter": True}}])
    assert CU.execute_task("打开窗口")["error_code"] == "UNSAFE_PLAN"
    monkeypatch.setattr(CU, "_plan_with_local_llm", lambda _: [{"action": "hotkey", "params": {"keys": "win+r"}}])
    assert CU.execute_task("打开窗口")["error_code"] == "UNSAFE_PLAN"
