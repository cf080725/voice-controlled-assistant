"""Real audio/media action methods, with all endpoint/key/session ports fake."""
import ast
import builtins
from contextlib import contextmanager
import importlib.util
import math
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def actions(monkeypatch):
    spec = importlib.util.spec_from_file_location("_r45_action_models", ROOT / "core/canaan_action_framework/models.py")
    models = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, models)
    spec.loader.exec_module(models)
    source = ROOT / "core/canaan_action_framework/actions/system_actions.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"VolumeMuteAction", "VolumeAction", "MediaControlAction"}
    body = [n for n in tree.body if
            isinstance(n, ast.ClassDef) and n.name in names or
            isinstance(n, ast.FunctionDef) and n.name.startswith(("_audio_", "_media_")) or
            isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and
              t.id.startswith(("_AUDIO_", "_MEDIA_")) for t in n.targets)]

    def no_import(name, *a, **k):
        if name.startswith(("pycaw", "comtypes")):
            raise ImportError("synthetic unavailable legacy dependency")
        raise AssertionError(f"Action escaped a fake port: {name}")

    keys = []
    base = type("BaseAction", (), {"verify": lambda self, ctx: models.VerificationResult(
        passed=True, gate_name="NO_POSTCONDITION", verified=False)})
    ns = {"__builtins__": dict(vars(builtins), __import__=no_import), "BaseAction": base,
          "ActionRegistry": SimpleNamespace(register=lambda cls: cls), "re": re, "math": math,
          "ActionCategory": models.ActionCategory, "PermissionLevel": models.PermissionLevel,
          "ActionContext": models.ActionContext, "ActionResult": models.ActionResult,
          "VerificationResult": models.VerificationResult, "send_key": lambda *a, **k: keys.append((a, k)),
          "VK_VOLUME_MUTE": 0xAD, "VK_VOLUME_UP": 0xAF, "VK_VOLUME_DOWN": 0xAE,
          "VK_MEDIA_NEXT": 0xB0, "VK_MEDIA_PREV": 0xB1, "VK_MEDIA_PLAY_PAUSE": 0xB3}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(source), "exec"), ns)
    state = SimpleNamespace(level=.72, muted=True, endpoint_id="speaker-a", snapshots=0,
                            writes=[], ignore=False, fail_read=False, fail_write=False, switch=False)

    def snapshot():
        state.snapshots += 1
        if state.fail_read: raise OSError("synthetic read failure")
        return {"level": state.level, "muted": state.muted,
                "endpoint_id": "speaker-b" if state.switch and state.snapshots > 1 else state.endpoint_id}

    def set_level(value):
        state.writes.append(("level", value))
        if state.fail_write: raise OSError("synthetic write failure")
        if not state.ignore: state.level = value

    def set_mute(value):
        state.writes.append(("muted", value))
        if state.fail_write: raise OSError("synthetic write failure")
        if not state.ignore: state.muted = value

    @contextmanager
    def endpoint_factory():
        yield SimpleNamespace(snapshot=snapshot, set_level=set_level, set_mute=set_mute)

    ns["_audio_endpoint_factory"] = endpoint_factory
    media = SimpleNamespace(status="playing", calls=[], transitions=0, verified=True, switch=False)

    def session_request(mode, timeout=6.0):
        media.calls.append(mode)
        before = media.status
        target = "paused" if mode == "pause" else "playing"
        if media.verified and before != target:
            media.status, media.transitions = target, media.transitions + 1
        return {"requested": mode, "before": before, "after": media.status, "accepted": media.verified,
                "verified": media.verified, "session_id": "session-a", "session_id_before": "session-a",
                "session_id_after": "session-b" if media.switch else "session-a"}

    ns["_media_session_request"] = session_request
    def invoke(name, param, text="", metadata=None):
        cls = ns[name]
        context = models.ActionContext(cls.name, raw_param=param, user_text=text,
                                       metadata={} if metadata is None else metadata)
        action = cls()
        result = action.execute(context)
        return action, context, result
    return SimpleNamespace(ns=ns, state=state, media=media, keys=keys, invoke=invoke)


@pytest.mark.parametrize("want, initially", [("on", False), ("off", True), ("on", True), ("off", False)])
def test_explicit_mute_sets_and_verifies_idempotently(actions, want, initially):
    f = actions
    f.state.muted = initially
    for _ in range(2):
        action, ctx, result = f.invoke("VolumeMuteAction", want)
        assert result.success and action.verify(ctx).verified
        assert f.state.muted is (want == "on")
    assert len(f.state.writes) == int(initially != (want == "on")) and not f.keys


@pytest.mark.parametrize("fault", ["fail_read", "fail_write", "ignore", "switch"])
@pytest.mark.parametrize("name,param", [("VolumeMuteAction", "off"), ("VolumeAction", "30")])
def test_audio_fault_never_falls_back_to_keys_or_success(actions, fault, name, param):
    f = actions
    setattr(f.state, fault, True)
    _, _, result = f.invoke(name, param)
    assert not result.success and result.error_code
    assert not f.keys
    if fault == "fail_read": assert not f.state.writes


def test_positive_volume_sets_level_before_unmute_and_verifies(actions):
    f = actions
    action, ctx, result = f.invoke("VolumeAction", "30")
    assert result.success and f.state.level == pytest.approx(.3) and not f.state.muted
    assert f.state.writes == [("level", .3), ("muted", False)] and not f.keys
    assert action.verify(ctx).passed and action.verify(ctx).verified


def test_zero_is_both_zero_level_and_muted(actions):
    f = actions
    f.state.muted = False
    action, ctx, result = f.invoke("VolumeAction", "0")
    assert result.success and f.state.level == 0 and f.state.muted
    assert action.verify(ctx).verified and not f.keys


def test_explicit_unmute_preserves_existing_level(actions):
    f = actions
    action, ctx, result = f.invoke("VolumeMuteAction", "off")
    assert result.success and f.state.level == .72 and not f.state.muted
    assert action.verify(ctx).verified and f.state.writes == [("muted", False)]


def test_failed_second_call_cannot_reuse_old_audio_observation(actions):
    f = actions
    action, ctx, result = f.invoke("VolumeAction", "30")
    assert result.success and action.verify(ctx).verified
    f.state.fail_read = True
    _, ctx2, failed = f.invoke("VolumeAction", "30", metadata=ctx.metadata)
    assert not failed.success and not action.verify(ctx2).verified


@pytest.mark.parametrize("mode,initial,after", [("pause", "playing", "paused"), ("play", "paused", "playing")])
def test_explicit_media_is_idempotent_and_ignores_whole_plan_text(actions, mode, initial, after):
    f = actions
    f.media.status = initial
    for _ in range(2):
        action, ctx, result = f.invoke("MediaControlAction", mode, "暂停，然后继续播放")
        assert result.success and action.verify(ctx).verified
    assert f.media.status == after and f.media.transitions == 1 and f.media.calls == [mode, mode]
    assert not f.keys


@pytest.mark.parametrize("fault", ["verified", "switch"])
def test_unverified_or_switched_media_cannot_claim_completion(actions, fault):
    f = actions
    setattr(f.media, fault, fault == "switch")
    _, _, result = f.invoke("MediaControlAction", "pause")
    assert not result.success and result.error_code and not f.keys


@pytest.mark.parametrize("mode", ["next", "prev", "play_pause"])
def test_legacy_media_keys_report_request_only(actions, mode):
    f = actions
    action, ctx, result = f.invoke("MediaControlAction", mode)
    assert result.success and len(f.keys) == 1 and not f.media.calls
    assert "请求" in result.message and "请求" in result.speech_override
    assert not action.verify(ctx).verified


def test_unknown_media_mode_does_not_toggle(actions):
    f = actions
    _, _, result = f.invoke("MediaControlAction", "invented")
    assert not result.success and result.error_code and not f.keys and not f.media.calls


@pytest.mark.parametrize("param", ["", "unknown", "-1", "101", "30.5", "30 then 60"])
def test_invalid_volume_does_not_guess_a_target(actions, param):
    f = actions
    _, _, result = f.invoke("VolumeAction", param)
    assert not result.success and result.error_code == "AUDIO_TARGET_INVALID"
    assert not f.state.writes and not f.state.snapshots and not f.keys


@pytest.mark.parametrize("name,param,changed", [("VolumeAction", "30", "40"),
    ("VolumeMuteAction", "on", "off"), ("MediaControlAction", "pause", "play")])
def test_verification_requires_evidence_for_the_current_parameter(actions, name, param, changed):
    action, ctx, result = actions.invoke(name, param)
    assert result.success and action.verify(ctx).verified
    ctx.raw_param = changed
    assert not action.verify(ctx).verified


def test_read_failure_after_setter_reports_unconfirmed_state(actions):
    f = actions
    original = f.ns["_audio_endpoint_factory"]
    @contextmanager
    def fail_after_write():
        with original() as port:
            setter = port.set_level
            def set_then_lose_read(value):
                setter(value)
                f.state.fail_read = True
            port.set_level = set_then_lose_read
            yield port
    f.ns["_audio_endpoint_factory"] = fail_after_write
    action, ctx, result = f.invoke("VolumeAction", "30")
    assert not result.success and not action.verify(ctx).verified
    assert f.state.level == .3 and not f.keys
    assert "未核实" in result.message and "未执行" not in result.message


@pytest.mark.parametrize("field,value", [("requested", "play"), ("after", "playing"),
    ("accepted", False), ("session_id", ""), ("before", "unknown"), ("error_code", "MEDIA_TIMEOUT")])
def test_media_rejects_inconsistent_bridge_evidence(actions, field, value):
    f = actions
    request = f.ns["_media_session_request"]
    def corrupt(mode, timeout=6.0):
        data = request(mode, timeout)
        data[field] = value
        return data
    f.ns["_media_session_request"] = corrupt
    action, ctx, result = f.invoke("MediaControlAction", "pause")
    assert not result.success and not action.verify(ctx).verified and not f.keys


def test_failed_second_media_call_cannot_reuse_old_observation(actions):
    f = actions
    action, ctx, result = f.invoke("MediaControlAction", "pause")
    assert result.success and action.verify(ctx).verified
    f.media.verified = False
    _, second, failed = f.invoke("MediaControlAction", "pause", metadata=ctx.metadata)
    assert not failed.success and not action.verify(second).verified


def test_empty_mute_is_only_an_unverified_toggle_request(actions):
    action, ctx, result = actions.invoke("VolumeMuteAction", "")
    assert result.success and len(actions.keys) == 1
    assert "请求" in result.message and "请求" in result.speech_override
    assert not action.verify(ctx).verified


@pytest.mark.parametrize("want,muted", [("ON", True), ("OFF", False)])
def test_mute_case_normalization_keeps_verification_bound_to_input(actions, want, muted):
    action, ctx, result = actions.invoke("VolumeMuteAction", want)
    assert result.success and action.verify(ctx).verified and actions.state.muted is muted


def test_minimum_requires_an_actual_zero_scalar(actions):
    f = actions
    original = f.ns["_audio_endpoint_factory"]
    @contextmanager
    def quantized_endpoint():
        with original() as port:
            setter = port.set_level
            port.set_level = lambda value: setter(.004)
            yield port
    f.ns["_audio_endpoint_factory"] = quantized_endpoint
    _, _, result = f.invoke("VolumeAction", "0")
    assert not result.success and result.error_code and f.state.muted is True


def test_actor_explicit_play_can_resume_a_stopped_session(actions):
    f = actions
    f.media.status = "stopped"
    action, ctx, result = f.invoke("MediaControlAction", "play")
    assert result.success and action.verify(ctx).verified
    assert f.media.status == "playing" and f.media.transitions == 1
    assert f.media.calls == ["play"] and not f.keys


def test_actor_stopped_play_without_capability_never_toggles_or_claims_completion(actions):
    f = actions
    f.media.status, f.media.verified = "stopped", False
    action, ctx, result = f.invoke("MediaControlAction", "play")
    assert not result.success and not action.verify(ctx).verified
    assert f.media.status == "stopped" and f.media.calls == ["play"] and not f.keys


def test_actor_stopped_cannot_be_claimed_as_paused(actions):
    f = actions
    def request(mode, timeout=6.0):
        f.media.calls.append(mode)
        return {"requested": mode, "before": "stopped", "after": "stopped", "accepted": True,
                "verified": True, "session_id": "session-a", "error_code": None}
    f.ns["_media_session_request"] = request
    action, ctx, result = f.invoke("MediaControlAction", "pause")
    assert not result.success and not action.verify(ctx).verified and not f.keys
