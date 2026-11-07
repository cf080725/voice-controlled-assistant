"""Verification failures must never reuse an earlier success announcement."""
from unittest.mock import Mock

from canaan_action_framework import action_registry
from canaan_action_framework.action_registry import ActionRegistry
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.models import ActionResult, VerificationResult
from canaan_turn_pipeline import spoken_result


class FixtureAction(BaseAction):
    name = "verification_speech_fixture"

    def execute(self, context):
        return self.result


def dispatch_fixture(monkeypatch, action):
    monkeypatch.setattr(ActionRegistry, "get_action", lambda _: action)
    monkeypatch.setattr(ActionRegistry, "_audit_log", lambda *_: None)
    monkeypatch.setattr(action_registry, "dry_run_enabled", lambda: False)
    return ActionRegistry.dispatch(action.name, source="rule").to_dict()


def test_failed_verification_discards_success_speech_before_spoken_result(monkeypatch):
    action = FixtureAction()
    action.result = ActionResult(success=True, action=action.name,
                                 message="execution completed", speech_override="The action is completed.")
    action.verify = Mock(return_value=VerificationResult(
        passed=False, gate_name="FIXTURE", error_code="FIXTURE_VERIFY_FAIL", reason="fixture failed"))

    result = dispatch_fixture(monkeypatch, action)
    speech = spoken_result(action.name, {}, result, "rule")

    assert result["success"] is False and result["error_code"] == "FIXTURE_VERIFY_FAIL"
    assert result["speech_override"] is None
    assert "fixture failed" in speech and "The action is completed" not in speech


def test_explicit_execution_failure_keeps_its_specific_unsent_explanation(monkeypatch):
    action = FixtureAction()
    explanation = "Input readback failed; no message was sent."
    action.result = ActionResult(success=False, action=action.name, error_code="INPUT_READBACK_TIMEOUT",
                                 message="fixture timeout", speech_override=explanation)
    action.verify = Mock(side_effect=AssertionError("Failed execution must not be verified"))

    result = dispatch_fixture(monkeypatch, action)

    assert result["success"] is False and result["speech_override"] == explanation
    assert spoken_result(action.name, {}, result, "rule") == explanation
    action.verify.assert_not_called()
