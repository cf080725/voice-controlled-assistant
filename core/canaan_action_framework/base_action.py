# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Base Action Class
Defines the core extensible contract for all desktop actions, skills, and tools.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from canaan_action_framework.models import (
    ActionCategory,
    ActionContext,
    ActionResult,
    PermissionLevel,
    VerificationResult,
)


class BaseAction(ABC):
    """Abstract Base Class for all Canaan Enterprise Actions (Open-Closed Principle)"""
    
    name: str = "base_action"
    aliases: List[str] = []
    description: str = "Base executable action"
    category: ActionCategory = ActionCategory.SYSTEM_CONTROL
    permission_level: PermissionLevel = PermissionLevel.NORMAL_MUTATION
    input_schema: Dict[str, Any] = {}
    # 为 True 时，规则识别出来的也要先确认（2026-10-13：电脑/界面自动操作、跑脚本、关程序、消息导出推送、交给云端智能体）
    confirm_even_from_rule: bool = False
    rollback_supported: bool = False

    def authorize(self, context: ActionContext) -> Optional[ActionResult]:
        """权限闸门：返回 None 表示放行，返回 ActionResult 表示拦截（需确认或拒绝）。
        默认规则：SENSITIVE（发消息、关程序、界面自动操作）与 CRITICAL_DESTRUCTIVE（执行命令/脚本）级动作
        若来自大模型且用户未确认，要求口头确认。来自确定性规则（source="rule"）的放行，
        但 confirm_even_from_rule=True 的动作不管来源都要先确认。"""
        if (self.permission_level in (PermissionLevel.SENSITIVE, PermissionLevel.CRITICAL_DESTRUCTIVE)
                and (context.source != "rule" or self.confirm_even_from_rule) and not context.confirmed):
            return self.confirm_required(context, f"执行 {self.name}：{context.param_value()[:40]}")
        return None

    def confirm_required(self, context: ActionContext, what: str, reason: str = "") -> ActionResult:
        return ActionResult(
            success=False,
            action=self.name,
            error_code="CONFIRM_REQUIRED",
            message=f"需要确认：{what}" + (f"（{reason}）" if reason else ""),
            detail={"what": what, "reason": reason},
            speech_override="这个操作需要你确认一下，屏幕上有具体内容。说“确认”就执行，说“取消”就算了。",
        )

    def pre_execute(self, context: ActionContext) -> None:
        """Lifecycle hook executed before main execution logic (e.g. state snapshot, validation)"""
        pass

    @abstractmethod
    def execute(self, context: ActionContext) -> ActionResult:
        """Core execution logic to be implemented by each action driver"""
        raise NotImplementedError

    def verify(self, context: ActionContext) -> VerificationResult:
        return VerificationResult(passed=True, gate_name="NO_POSTCONDITION", verified=False,
                                   reason="该动作没有实现后置条件校验")

    def rollback(self, context: ActionContext) -> bool:
        return False

    def post_execute(self, context: ActionContext, result: ActionResult) -> None:
        """Lifecycle hook executed after main execution (e.g. telemetry metrics, cleanup)"""
        pass
