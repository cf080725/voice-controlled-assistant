# -*- coding: utf-8 -*-
from canaan_action_framework import ActionRegistry, ActionResult, BaseAction, PermissionLevel, VerificationResult
from canaan_action_framework.models import ActionContext


def _cleanup(name):
    ActionRegistry._actions.pop(name, None)
    ActionRegistry._alias_map.pop(name, None)


def test_default_verify_is_not_verified():
    class _A(BaseAction):
        name = "_test_no_verify"
        permission_level = PermissionLevel.SAFE_READONLY

        def execute(self, context):
            return ActionResult(success=True, action=self.name)

    a = _A()
    v = a.verify(ActionContext(action_name=a.name))
    assert v.passed is True and v.verified is False
    assert v.gate_name == "NO_POSTCONDITION"
    assert a.rollback_supported is False
    assert a.rollback(ActionContext(action_name=a.name)) is False


def test_dispatch_skips_rollback_when_unsupported():
    calls = []

    class _A(BaseAction):
        name = "_test_fail_verify_no_rb"
        permission_level = PermissionLevel.SAFE_READONLY

        def execute(self, context):
            return ActionResult(success=True, action=self.name)

        def verify(self, context):
            return VerificationResult(passed=False, gate_name="TEST", error_code="TEST_FAIL")

        def rollback(self, context):
            calls.append(context.action_name)
            return True

    ActionRegistry.register(_A)
    try:
        r = ActionRegistry.dispatch("_test_fail_verify_no_rb", "", source="rule")
        assert r.success is False and r.error_code == "TEST_FAIL"
        assert calls == []
    finally:
        _cleanup("_test_fail_verify_no_rb")


def test_dispatch_calls_rollback_when_supported():
    calls = []

    class _A(BaseAction):
        name = "_test_fail_verify_rb"
        permission_level = PermissionLevel.SAFE_READONLY
        rollback_supported = True

        def execute(self, context):
            return ActionResult(success=True, action=self.name)

        def verify(self, context):
            return VerificationResult(passed=False, gate_name="TEST", error_code="TEST_FAIL")

        def rollback(self, context):
            calls.append(context.action_name)
            return True

    ActionRegistry.register(_A)
    try:
        r = ActionRegistry.dispatch("_test_fail_verify_rb", "", source="rule")
        assert r.success is False and r.error_code == "TEST_FAIL"
        assert calls == ["_test_fail_verify_rb"]
    finally:
        _cleanup("_test_fail_verify_rb")


def test_to_dict_exposes_verification_flag():
    verified = ActionResult(
        success=True,
        action="x",
        verification=VerificationResult(passed=True, gate_name="TEST", verified=True),
    ).to_dict()
    assert verified["verification"]["verified"] is True
    assert verified["verification"]["gate_name"] == "TEST"

    unverified = ActionResult(success=True, action="x").to_dict()
    assert unverified["verification"] is None
