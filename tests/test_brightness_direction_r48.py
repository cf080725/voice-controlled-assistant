"""Relative brightness is monotonic; every bridge response here is fabricated.

Load the real endpoint with its process port blocked and only the actor's two
pure validation helpers. No actor module, real bridge, device, or native API is
imported or executed. The separate R48 fake-CIM harness tests the fixed script.
"""
import ast
import builtins
import importlib.util
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAYERS = ["endpoint", "actor"]


@pytest.fixture
def validators(monkeypatch):
    def blocked_process(*args, **kwargs):
        raise AssertionError("R48 test escaped its fake bridge")

    monkeypatch.setattr(subprocess, "run", blocked_process)
    source = ROOT / "core/canaan_brightness_endpoint.py"
    spec = importlib.util.spec_from_file_location("_r48_brightness_endpoint", source)
    endpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(endpoint)

    actor_source = ROOT / "core/canaan_action_framework/actions/system_actions.py"
    tree = ast.parse(actor_source.read_text(encoding="utf-8"))
    names = {"_brightness_spec", "_brightness_matches"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in body} == names

    def blocked_import(name, *args, **kwargs):
        raise AssertionError(f"R48 actor helper imported {name}")

    namespace = {"__builtins__": dict(vars(builtins), __import__=blocked_import), "re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(actor_source), "exec"), namespace)
    return SimpleNamespace(endpoint=endpoint, actor_matches=namespace["_brightness_matches"])


def proof(action, param, mode, value, current, target, levels=None):
    changed = current != target
    return {
        "ok": True, "action": action, "param": param, "request_mode": mode, "request_value": value,
        "monitor_id": "synthetic-panel", "monitor_id_before": "synthetic-panel",
        "monitor_id_after": "synthetic-panel", "method_id_before": "synthetic-panel",
        "method_id_after": "synthetic-panel", "active_count_before": 1, "active_count_after": 1,
        "method_count_before": 1, "method_count_after": 1, "current": current, "target": target,
        "actual": target, "supported_levels": list(range(101)) if levels is None else list(levels),
        "write_attempted": changed, "changed": changed, "return_value": 0 if changed else None,
        "verified": True, "error_code": None,
    }


def check_proof(layer, validators, monkeypatch, evidence, accepted):
    action, param = evidence["action"], evidence["param"]
    if layer == "actor":
        assert validators.actor_matches(action, param, evidence) is accepted
        return

    calls = []

    def fake_run_bridge(script, timeout):
        calls.append((script, timeout))
        return SimpleNamespace(stdout=json.dumps(evidence), stderr="", returncode=0)

    endpoint = validators.endpoint
    monkeypatch.setattr(endpoint, "_run_bridge", fake_run_bridge)
    if accepted:
        result = endpoint.control_brightness(action, param)
        assert result["ok"] is True and result["target"] == evidence["target"]
        assert result["write_attempted"] is evidence["write_attempted"]
    else:
        with pytest.raises(endpoint.BrightnessEndpointError) as caught:
            endpoint.control_brightness(action, param)
        assert caught.value.code == "BRIGHTNESS_STATE_UNAVAILABLE"
        assert caught.value.evidence["current"] == evidence["current"]
        assert caught.value.evidence["target"] == evidence["target"]
    assert len(calls) == 1 and calls[0][1] == 6.0


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("current", range(5))
@pytest.mark.parametrize("param,delta", [("", -20), ("delta:-1", -1), ("delta:-100", -100)])
def test_forged_dimming_below_software_floor_cannot_claim_success(layer, validators, monkeypatch, current, param, delta):
    # All identities, exact readback, and acceptance fields are internally
    # consistent: only the relative request's direction makes this proof false.
    evidence = proof("brightness_down", param, "relative", delta, current, 5)
    check_proof(layer, validators, monkeypatch, evidence, False)


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("current,levels,target", [
    (0, [0, 10, 30, 100], 10),
    (4, [0, 4, 35, 100], 35),
])
def test_actual_supported_floor_also_cannot_reverse_dimming(layer, validators, monkeypatch, current, levels, target):
    check_proof(layer, validators, monkeypatch,
                proof("brightness_down", "", "relative", -20, current, target, levels), False)


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("action,param,delta,current,target,levels", [
    ("brightness_down", "", -20, 5, 5, None),
    ("brightness_up", "", 20, 100, 100, None),
    ("brightness_down", "", -20, 10, 10, [0, 10, 30, 90]),
    ("brightness_up", "", 20, 90, 90, [0, 10, 30, 90]),
    ("brightness_down", "", -20, 60, 40, None),
    ("brightness_down", "delta:-1", -1, 60, 59, None),
    ("brightness_down", "delta:-100", -100, 60, 5, [0, 5, 60, 100]),
    ("brightness_up", "", 20, 0, 20, None),
    ("brightness_up", "", 20, 4, 24, None),
    ("brightness_up", "delta:+1", 1, 0, 5, None),
    ("brightness_up", "delta:+1", 1, 4, 5, None),
    ("brightness_up", "delta:+1", 1, 0, 10, [0, 10, 30, 100]),
])
def test_relative_direction_and_supported_boundary_noop_remain_valid(
        layer, validators, monkeypatch, action, param, delta, current, target, levels):
    check_proof(layer, validators, monkeypatch,
                proof(action, param, "relative", delta, current, target, levels), True)


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("action,param,mode,value,current,target,levels", [
    ("set_brightness", "5", "absolute", 5, 0, 5, None),
    ("set_brightness", "min", "min", 0, 0, 5, None),
    ("set_brightness", "max", "max", 0, 0, 100, None),
    ("brightness_down", "5", "absolute", 5, 0, 5, None),
    ("brightness_up", "5", "absolute", 5, 100, 5, None),
    ("set_brightness", "min", "min", 0, 0, 10, [0, 10, 30, 90]),
    ("set_brightness", "max", "max", 0, 0, 90, [0, 10, 30, 90]),
])
def test_absolute_and_extremum_requests_keep_existing_semantics(
        layer, validators, monkeypatch, action, param, mode, value, current, target, levels):
    check_proof(layer, validators, monkeypatch,
                proof(action, param, mode, value, current, target, levels), True)


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("action,param,mode,value,current,target,levels", [
    ("brightness_up", "", "relative", 20, 60, 40, None),
    ("brightness_up", "delta:+5", "relative", 5, 30, 40, [5, 30, 40, 60, 100]),
    ("brightness_down", "delta:-5", "relative", -5, 30, 20, [5, 20, 30, 60, 100]),
    ("set_brightness", "30", "absolute", 30, 60, 40, [5, 40, 60, 100]),
])
def test_direction_guard_does_not_allow_opposite_or_nearest_numeric_substitution(
        layer, validators, monkeypatch, action, param, mode, value, current, target, levels):
    check_proof(layer, validators, monkeypatch,
                proof(action, param, mode, value, current, target, levels), False)


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("field,bad", [
    ("current", None), ("current", True), ("monitor_id_after", "other-panel"),
    ("method_count_after", 2), ("return_value", False), ("write_attempted", False),
    ("actual", 59), ("verified", False),
])
def test_valid_relative_direction_still_requires_complete_success_proof(layer, validators, monkeypatch, field, bad):
    evidence = proof("brightness_down", "", "relative", -20, 60, 40)
    evidence[field] = bad
    check_proof(layer, validators, monkeypatch, evidence, False)
