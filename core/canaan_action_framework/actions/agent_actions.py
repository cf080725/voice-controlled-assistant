# -*- coding: utf-8 -*-
"""
对话与视觉智能体类动作：chat（纯对话，不操作电脑）、ufo_task（UFO 视觉智能体操作任意界面）。
"""
from canaan_action_framework.models import (
    ActionContext, ActionResult, ActionCategory, PermissionLevel, VerificationResult
)
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry


@ActionRegistry.register
class ChatAction(BaseAction):
    name = "chat"
    aliases = ["talk", "reply"]
    description = "普通对话，不执行任何电脑操作"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.SAFE_READONLY

    def execute(self, context: ActionContext) -> ActionResult:
        return ActionResult(success=True, action=self.name, message="闲聊对话")


@ActionRegistry.register
class UfoTaskAction(BaseAction):
    name = "ufo_task"
    aliases = ["ufo_action", "gui_agent"]
    description = "视觉智能体按自然语言描述操作软件界面（如“在记事本里写一句晚上好”）"
    category = ActionCategory.VISION_MULTIMODAL
    permission_level = PermissionLevel.SENSITIVE
    confirm_even_from_rule = True  # 界面自动操作：规则识别的也先确认

    def execute(self, context: ActionContext) -> ActionResult:
        from canaan_ufo_agent import GLOBAL_HOST_AGENT
        res = GLOBAL_HOST_AGENT.execute_ufo_task(context.param_value())
        detail = res.get("detail")
        ok = res.get("success") is True
        if ok and (not isinstance(detail, dict) or detail.get("verified") is not True):
            return ActionResult(success=False, action=self.name, detail=detail,
                                error_code="TASK_UNVERIFIED", message="界面操作缺少任务完成证据",
                                speech_override="这一步的结果还没核验，任务还不能算完成。")
        if ok:
            context.metadata["ufo_verification"] = dict(detail)
        return ActionResult(
            success=ok,
            action=self.name,
            message=res.get("message", ""),
            detail=detail,
            speech_override=res.get("reply") or ("操作完成了。" if ok else "这个界面操作没做成。"),
            error_code=None if ok else res.get("error_code") or "UFO_FAILED",
        )

    def verify(self, context: ActionContext) -> VerificationResult:
        evidence = context.metadata.get("ufo_verification") or {}
        verified = evidence.get("verified") is True
        return VerificationResult(passed=verified, verified=verified,
                                  gate_name=evidence.get("gate_name") or "UFO_TASK_POSTCONDITION",
                                  error_code=None if verified else "TASK_UNVERIFIED",
                                  reason=evidence.get("reason") or "缺少任务完成证据")
