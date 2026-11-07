"""Brightness bridge contracts; all process results are synthetic, never WMI."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def endpoint(monkeypatch):
    path = ROOT / "core/canaan_brightness_endpoint.py"
    spec = importlib.util.spec_from_file_location("_r47_brightness_endpoint", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def evidence(mode="absolute", value=30, current=60, target=30, levels=None):
    return {"request_mode": mode, "request_value": value, "monitor_id": "panel-a",
        "monitor_id_before": "panel-a", "monitor_id_after": "panel-a",
        "method_id_before": "panel-a", "method_id_after": "panel-a",
        "active_count_before": 1, "active_count_after": 1,
        "method_count_before": 1, "method_count_after": 1,
        "current": current, "target": target, "actual": target,
        "supported_levels": [0, 10, 30, 40, 60, 80, 90] if levels is None else levels,
        "write_attempted": current != target, "changed": current != target,
        "return_value": 0 if current != target else None,
        "verified": True, "error_code": None}


def fake_response(monkeypatch, module, result, returncode=0):
    calls = []
    def run(script, timeout):
        calls.append((script, timeout))
        return SimpleNamespace(stdout=json.dumps(result), stderr="", returncode=returncode)
    monkeypatch.setattr(module, "_run_bridge", run)
    return calls


@pytest.mark.parametrize("action,param", [("set_brightness", ""), ("set_brightness", "0"),
    ("set_brightness", "4"), ("set_brightness", "101"), ("set_brightness", "-5"),
    ("set_brightness", "30.5"), ("set_brightness", "30%"), ("set_brightness", "30 then 60"),
    ("set_brightness", "暗一点"), ("set_brightness", "MIN"), ("set_brightness", None),
    ("brightness_up", "delta:-20"), ("brightness_down", "delta:+20"),
    ("brightness_up", "delta:+0"), ("brightness_down", "delta:-101"),
    ("brightness_up", "max"), ("invented", "30"), ("set_brightness", "30;Write-Host pwn")])
def test_invalid_input_rejected_before_launch(endpoint, monkeypatch, action, param):
    monkeypatch.setattr(endpoint, "_run_bridge", lambda *a: pytest.fail("invalid input launched process"))
    with pytest.raises(ValueError): endpoint.control_brightness(action, param)


@pytest.mark.parametrize("timeout", [True, None, -1, .49, 10.01, float("nan"), float("inf")])
def test_invalid_timeout_rejected_before_launch(endpoint, monkeypatch, timeout):
    monkeypatch.setattr(endpoint, "_run_bridge", lambda *a: pytest.fail("invalid timeout launched process"))
    with pytest.raises(ValueError): endpoint.control_brightness("set_brightness", "30", timeout)


@pytest.mark.parametrize("param,mode,target", [("min", "min", 10), ("max", "max", 90), ("30", "absolute", 30)])
def test_exact_and_supported_extrema(endpoint, monkeypatch, param, mode, target):
    data = evidence(mode, 30 if mode == "absolute" else 0, target=target)
    calls = fake_response(monkeypatch, endpoint, data)
    result = endpoint.control_brightness("set_brightness", param)
    assert result["target"] == target and result["actual"] == target and result["ok"] is True
    assert result["action"] == "set_brightness" and result["param"] == param
    assert len(calls) == 1 and "__" not in calls[0][0]


@pytest.mark.parametrize("action,param,delta,current,target,levels", [
    ("brightness_up", "", 20, 60, 80, [5, 30, 60, 80, 100]),
    ("brightness_down", "", -20, 60, 40, [5, 30, 40, 60, 100]),
    ("brightness_up", "delta:+100", 100, 60, 100, [5, 60, 100]),
    ("brightness_down", "delta:-100", -100, 60, 5, [5, 60, 100]),
    ("brightness_up", "30", 30, 60, 30, [5, 30, 60, 100])])
def test_relative_or_legacy_exact_target(endpoint, monkeypatch, action, param, delta, current, target, levels):
    mode = "absolute" if param == "30" else "relative"
    fake_response(monkeypatch, endpoint, evidence(mode, delta, current, target, levels))
    assert endpoint.control_brightness(action, param)["target"] == target


@pytest.mark.parametrize("field,bad", [("monitor_id_after", "panel-b"), ("method_id_before", "other"),
    ("active_count_before", 0), ("active_count_after", 2), ("method_count_before", 2),
    ("method_count_after", 0), ("current", None), ("current", True), ("current", "60"),
    ("supported_levels", []), ("supported_levels", [5, 30, True, 60]),
    ("supported_levels", [5, 60, 100]), ("actual", 60), ("actual", "30"),
    ("target", 40), ("return_value", 1), ("return_value", None), ("return_value", False),
    ("write_attempted", False), ("changed", False), ("verified", False),
    ("request_mode", "min"), ("request_value", 40), ("error_code", "BRIGHTNESS_TIMEOUT"),
    ("error_code", ["invalid-code"]), ("monitor_id", " ")])
def test_inconsistent_success_evidence_fails(endpoint, monkeypatch, field, bad):
    data = evidence(); data[field] = bad
    fake_response(monkeypatch, endpoint, data)
    with pytest.raises(endpoint.BrightnessEndpointError): endpoint.control_brightness("set_brightness", "30")


@pytest.mark.parametrize("field", ["monitor_id_before", "monitor_id_after", "method_id_after", "active_count_after", "return_value"])
def test_required_identity_and_acceptance_fields_cannot_be_missing(endpoint, monkeypatch, field):
    data = evidence(); del data[field]
    fake_response(monkeypatch, endpoint, data)
    with pytest.raises(endpoint.BrightnessEndpointError): endpoint.control_brightness("set_brightness", "30")


def test_idempotent_requires_fresh_readback_without_set(endpoint, monkeypatch):
    data = evidence(current=30)
    fake_response(monkeypatch, endpoint, data)
    result = endpoint.control_brightness("set_brightness", "30")
    assert not result["write_attempted"] and result["return_value"] is None and result["verified"]


@pytest.mark.parametrize("param,mode,value,target,levels", [("min", "min", 0, 5, [0, 10, 60, 90]),
    ("max", "max", 0, 100, [5, 60, 90]), ("30", "absolute", 30, 30, [5, 60, 100])])
def test_no_assumed_minimum_maximum_or_nearest_supported(endpoint, monkeypatch, param, mode, value, target, levels):
    fake_response(monkeypatch, endpoint, evidence(mode, value, target=target, levels=levels))
    with pytest.raises(endpoint.BrightnessEndpointError): endpoint.control_brightness("set_brightness", param)


def test_relative_clamp_is_not_permission_to_guess_supported_value(endpoint, monkeypatch):
    fake_response(monkeypatch, endpoint, evidence("relative", -100, target=5, levels=[0, 10, 60, 90]))
    with pytest.raises(endpoint.BrightnessEndpointError): endpoint.control_brightness("brightness_down", "delta:-100")


@pytest.mark.parametrize("action,param,current,target,delta", [
    ("brightness_up", "", 90, 90, 20), ("brightness_down", "", 10, 10, -20)])
def test_relative_saturates_at_actual_supported_bounds(endpoint, monkeypatch, action, param, current, target, delta):
    fake_response(monkeypatch, endpoint, evidence("relative", delta, current, target))
    result = endpoint.control_brightness(action, param)
    assert result["target"] == target and not result["write_attempted"]


def test_relative_unsupported_interior_never_rounds(endpoint, monkeypatch):
    fake_response(monkeypatch, endpoint, evidence("relative", 5, 30, 40))
    with pytest.raises(endpoint.BrightnessEndpointError): endpoint.control_brightness("brightness_up", "delta:+5")


@pytest.mark.parametrize("code", ["BRIGHTNESS_NO_MONITOR", "BRIGHTNESS_AMBIGUOUS_MONITOR",
    "BRIGHTNESS_STATE_UNAVAILABLE", "BRIGHTNESS_MONITOR_CHANGED", "BRIGHTNESS_TARGET_UNSUPPORTED",
    "BRIGHTNESS_REQUEST_REJECTED", "BRIGHTNESS_TIMEOUT"])
def test_bridge_failure_preserves_known_error_and_write_uncertainty(endpoint, monkeypatch, code):
    data = evidence(); data.update(verified=False, error_code=code, write_attempted=True)
    fake_response(monkeypatch, endpoint, data, returncode=1)
    with pytest.raises(endpoint.BrightnessEndpointError) as caught:
        endpoint.control_brightness("set_brightness", "30")
    assert caught.value.code == code and caught.value.evidence["write_attempted"] is True


def test_timeout_does_not_claim_no_write_or_rollback(endpoint, monkeypatch):
    def timeout(*a): raise subprocess.TimeoutExpired("synthetic", 6)
    monkeypatch.setattr(endpoint, "_run_bridge", timeout)
    with pytest.raises(endpoint.BrightnessEndpointError) as caught:
        endpoint.control_brightness("set_brightness", "30")
    assert caught.value.code == "BRIGHTNESS_TIMEOUT"
    assert caught.value.evidence["write_attempted"] is None
    assert "未核实" in str(caught.value) and "未执行" not in str(caught.value)


@pytest.mark.parametrize("output,returncode", [("{}", 0), ("[]", 0), ("not-json", 0),
    ("x" * 65537, 0), (json.dumps(evidence()), 1)], ids=["empty", "array", "malformed", "oversize", "exit-failure"])
def test_bad_process_output_and_exit_never_succeed(endpoint, monkeypatch, output, returncode):
    monkeypatch.setattr(endpoint, "_run_bridge", lambda *a: SimpleNamespace(stdout=output, returncode=returncode))
    with pytest.raises(endpoint.BrightnessEndpointError): endpoint.control_brightness("set_brightness", "30")


def test_fixed_hidden_bounded_windows_powershell5_process(endpoint, monkeypatch):
    calls = []
    monkeypatch.setattr(endpoint.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    endpoint._run_bridge("fixed content", 6.0)
    args, kwargs = calls[0]
    command = args[0]
    assert command[0].lower().endswith("windowspowershell\\v1.0\\powershell.exe")
    assert command[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert base64.b64decode(command[4]).decode("utf-16le") == "fixed content"
    assert kwargs["timeout"] == 6.0 and kwargs["creationflags"] == 0x08000000
    assert not kwargs.get("shell") and kwargs["encoding"] == "utf-8"


def test_script_binds_active_monitor_methods_and_checks_result_before_readback(endpoint):
    script = endpoint._SCRIPT
    for token in ("WmiMonitorBrightness", "WmiMonitorBrightnessMethods", "Active", "InstanceName",
                  "Level", "CurrentBrightness", "Invoke-CimMethod", "WmiSetBrightness", "ReturnValue",
                  "-InputObject", "-OperationTimeoutSec", "BRIGHTNESS_AMBIGUOUS_MONITOR"):
        assert token in script
    assert "Get-WmiObject" not in script and "Select-Object -First" not in script
    assert script.index("$writeAttempted = $true") < script.index("Invoke-CimMethod")


def test_import_performs_no_wmi_or_process_work(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("import started a process"))
    spec = importlib.util.spec_from_file_location("_r47_brightness_import_only", ROOT / "core/canaan_brightness_endpoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.control_brightness)
