"""Real brightness actor AST with a synthetic request port and real models."""
import ast
import builtins
import importlib.util
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def actions(monkeypatch):
    spec = importlib.util.spec_from_file_location("_r47_brightness_models", ROOT / "core/canaan_action_framework/models.py")
    models = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, models); spec.loader.exec_module(models)
    source = ROOT / "core/canaan_action_framework/actions/system_actions.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_BrightnessBase", "BrightnessAction", "BrightnessUpAction", "BrightnessDownAction"}
    body = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names or
            isinstance(n, ast.FunctionDef) and n.name.startswith("_brightness_")]
    def blocked_import(name, *a, **kw): raise AssertionError(f"escaped fake port: {name}")
    ns = {"__builtins__": dict(vars(builtins), __import__=blocked_import), "BaseAction": type("BaseAction", (), {}),
        "ActionRegistry": SimpleNamespace(register=lambda cls: cls), "re": re,
        "ActionContext": models.ActionContext, "ActionResult": models.ActionResult,
        "VerificationResult": models.VerificationResult, "ActionCategory": models.ActionCategory,
        "PermissionLevel": models.PermissionLevel}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(source), "exec"), ns)
    state = SimpleNamespace(current=60, levels=[0, 10, 30, 40, 60, 80, 90], calls=[], writes=[],
                            failure=None, corrupt=None)
    def request(action, param, timeout=6.0):
        state.calls.append((action, param, timeout))
        if state.failure: raise state.failure
        mode, value = ns["_brightness_spec"](action, param)
        allowed = [x for x in state.levels if 5 <= x <= 100]
        target = value if mode == "absolute" else min(allowed) if mode == "min" else max(allowed) if mode == "max" else max(min(allowed), min(max(allowed), state.current + value))
        old = state.current
        if target not in state.levels:
            return {"verified": False, "error_code": "BRIGHTNESS_TARGET_UNSUPPORTED"}
        if old != target: state.writes.append(target)
        state.current = target
        data = {"ok": True, "action": action, "param": param, "request_mode": mode, "request_value": value,
            "monitor_id": "panel-a", "monitor_id_before": "panel-a", "monitor_id_after": "panel-a",
            "method_id_before": "panel-a", "method_id_after": "panel-a", "active_count_before": 1,
            "active_count_after": 1, "method_count_before": 1, "method_count_after": 1,
            "current": old, "target": target, "actual": target, "supported_levels": list(state.levels),
            "write_attempted": old != target, "changed": old != target,
            "return_value": 0 if old != target else None, "verified": True, "error_code": None}
        if state.corrupt: state.corrupt(data)
        return data
    ns["_brightness_request"] = request
    def invoke(cls, param, text="", metadata=None):
        actor = ns[cls](); ctx = models.ActionContext(actor.name, raw_param=param, user_text=text,
            metadata={} if metadata is None else metadata)
        result = actor.execute(ctx)
        return actor, ctx, result
    return SimpleNamespace(ns=ns, models=models, state=state, invoke=invoke)


@pytest.mark.parametrize("param,target", [("min", 10), ("max", 90), ("30", 30)])
def test_actual_supported_target_and_exact_feedback(actions, param, target):
    actor, ctx, result = actions.invoke("BrightnessAction", param, "先最暗然后最亮最后三十")
    assert result.success and actor.verify(ctx).verified and actions.state.current == target
    assert str(target) in result.message and str(target) in result.speech_override
    assert result.detail["target"] == result.detail["actual"] == target
    assert actions.state.calls == [("set_brightness", param, 6.0)]


@pytest.mark.parametrize("cls,param,target", [("BrightnessUpAction", "", 80), ("BrightnessDownAction", "", 40),
    ("BrightnessUpAction", "delta:+20", 80), ("BrightnessDownAction", "delta:-20", 40),
    ("BrightnessUpAction", "30", 30), ("BrightnessDownAction", "80", 80)])
def test_relative_default_signed_delta_and_existing_numeric_exact(actions, cls, param, target):
    actor, ctx, result = actions.invoke(cls, param)
    assert result.success and actor.verify(ctx).verified and actions.state.current == target


@pytest.mark.parametrize("cls,param", [("BrightnessAction", ""), ("BrightnessAction", "暗"),
    ("BrightnessAction", "101"), ("BrightnessAction", "0"), ("BrightnessAction", "30.5"),
    ("BrightnessAction", "30 then60"), ("BrightnessUpAction", "delta:-20"),
    ("BrightnessDownAction", "delta:+20"), ("BrightnessUpAction", "max")])
def test_invalid_actor_input_never_calls_port(actions, cls, param):
    actor, ctx, result = actions.invoke(cls, param)
    assert not result.success and result.error_code == "BRIGHTNESS_TARGET_INVALID"
    assert not actor.verify(ctx).verified and not actions.state.calls


def test_idempotent_repeats_verify_without_second_write(actions):
    for _ in range(2):
        actor, ctx, result = actions.invoke("BrightnessAction", "30")
        assert result.success and actor.verify(ctx).verified
    assert actions.state.writes == [30] and len(actions.state.calls) == 2


@pytest.mark.parametrize("field,bad", [("ok", False), ("action", "brightness_up"), ("param", "min"),
    ("monitor_id_after", "panel-b"), ("method_id_before", "panel-b"), ("active_count_before", 2),
    ("method_count_after", 2), ("current", None), ("actual", 60), ("actual", True),
    ("target", 40), ("supported_levels", [0, 10, 60, 90]), ("return_value", False),
    ("write_attempted", False), ("verified", False), ("error_code", "BRIGHTNESS_TIMEOUT")])
def test_actor_rejects_inconsistent_or_unverified_evidence(actions, field, bad):
    actions.state.corrupt = lambda data: data.update({field: bad})
    actor, ctx, result = actions.invoke("BrightnessAction", "30")
    assert not result.success and not actor.verify(ctx).verified
    assert "未核实" in result.message and "未执行" not in result.message and "回滚" not in result.message


@pytest.mark.parametrize("mutation", ["trace", "action", "param", "evidence"])
def test_verification_bound_to_this_trace_action_parameter_and_evidence(actions, mutation):
    actor, ctx, result = actions.invoke("BrightnessAction", "30")
    assert result.success and actor.verify(ctx).verified
    if mutation == "trace": ctx.trace_id = "other"
    elif mutation == "action": ctx.action_name = "brightness_down"
    elif mutation == "param": ctx.raw_param = "max"
    else: ctx.metadata["_brightness_observation"]["evidence"]["actual"] = 90
    assert not actor.verify(ctx).verified


def test_failed_second_call_clears_success_evidence_and_does_not_fake_60(actions):
    actor, ctx, result = actions.invoke("BrightnessAction", "30")
    assert result.success and actor.verify(ctx).verified
    actions.state.failure = OSError("synthetic unavailable current state")
    _, ctx2, failed = actions.invoke("BrightnessUpAction", "", metadata=ctx.metadata)
    assert not failed.success and not actor.verify(ctx2).verified
    assert actions.state.writes == [30] and "未核实" in failed.speech_override


def test_supported_relative_clamp_reports_actual_target(actions):
    actions.state.levels = [5, 60, 100]
    actor, ctx, result = actions.invoke("BrightnessDownAction", "delta:-100")
    assert result.success and actor.verify(ctx).verified and actions.state.writes == [5]
    assert "5" in result.speech_override


def test_unsupported_relative_interior_never_selects_nearest(actions):
    actor, ctx, result = actions.invoke("BrightnessDownAction", "delta:-25")
    assert not result.success and not actor.verify(ctx).verified and not actions.state.writes


@pytest.mark.parametrize("cls,current,delta", [("BrightnessUpAction", 90, 20), ("BrightnessDownAction", 10, -20)])
def test_relative_actual_device_boundary_is_idempotent(actions, cls, current, delta):
    actions.state.current = current
    actor, ctx, result = actions.invoke(cls, "")
    assert result.success and actor.verify(ctx).verified and not actions.state.writes
    assert result.detail["requested_delta"] == delta and str(current) in result.speech_override


def test_ordered_plan_preserves_each_explicit_actor_parameter(actions):
    for param, target in [("min", 10), ("max", 90), ("30", 30)]:
        actor, ctx, result = actions.invoke("BrightnessAction", param, "调到最低，再最高，最后三十")
        assert result.success and actor.verify(ctx).verified and actions.state.current == target
    assert [call[1] for call in actions.state.calls] == ["min", "max", "30"]


@pytest.mark.parametrize("observation", [None, "invalid", [], {"evidence": []}])
def test_invalid_observation_cannot_raise_or_pass_verification(actions, observation):
    actor = actions.ns["BrightnessAction"]()
    context = actions.models.ActionContext(actor.name, raw_param="30", metadata={"_brightness_observation": observation})
    assert not actor.verify(context).verified


def test_whitespace_only_monitor_identity_is_not_evidence(actions):
    def corrupt(data):
        for key in ("monitor_id", "monitor_id_before", "monitor_id_after", "method_id_before", "method_id_after"):
            data[key] = " "
    actions.state.corrupt = corrupt
    actor, ctx, result = actions.invoke("BrightnessAction", "30")
    assert not result.success and not actor.verify(ctx).verified


