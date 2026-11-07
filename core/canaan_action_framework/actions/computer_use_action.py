# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Autonomous Computer Use Action
Integrates CanaanComputerUse into the Enterprise Action Framework.
Enables screen perception, mouse/keyboard primitives, window focus,
and multi-step autonomous desktop tasks with full telemetry, deterministic verification (SMD-V), and audit.
"""
import os
import sys
import json
import time
from typing import Dict, Any

from canaan_action_framework.models import (
    ActionContext, ActionResult, ActionCategory, PermissionLevel, VerificationResult
)
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry
from canaan_privacy_shield import PrivacyShield
from canaan_computer_use import CanaanComputerUse


@ActionRegistry.register
class ComputerUseAction(BaseAction):
    name = "computer_use"
    rollback_supported = True
    aliases = [
        "computer_control", "desktop_control", "auto_click",
        "screen_operate", "mouse_keyboard", "computer_task"
    ]
    description = "全自主桌面电脑控制（Computer Use）：支持屏幕感知、鼠标点击拖拽、键盘输入与多步电脑任务自动化"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.SENSITIVE
    confirm_even_from_rule = True  # 电脑自动操作：规则识别的也先确认

    def validate_params(self, context: ActionContext) -> bool:
        instruction = context.param_value() or context.params.get("instruction") or context.user_text
        return bool(instruction and str(instruction).strip())

    def execute(self, context: ActionContext) -> ActionResult:
        instruction = context.param_value() or context.params.get("instruction") or context.user_text
        instruction = str(instruction).strip()

        # 1. 敏感窗口隐私看门狗检测
        active_win = CanaanComputerUse.get_active_window()
        active_title = active_win.get("title", "")
        if active_title and PrivacyShield.is_sensitive_window(active_title):
            PrivacyShield.log_audit("computer_use_blocked_sensitive_window", {"window_title_length": len(active_title)})
            return ActionResult(
                success=False,
                action=self.name,
                message=f"检测到当前处于敏感窗口「{active_title}」，已触发防窥安全护盾拦截自主电脑操作",
                speech_override="亲爱的，当前正在处理敏感保密界面，团团已自动暂停电脑操控以保护您的隐私安全哦。"
            )

        context.report_step(1, 3, f"正在分析电脑控制指令「{instruction[:20]}」...")

        start_t = time.time()
        try:
            # 2. 执行自主任务调度（支持 on_step 回调直连桌面看板）
            res = CanaanComputerUse.execute_task(
                instruction,
                max_steps=10,
                on_step=context.report_step
            )

            latency = (time.time() - start_t) * 1000
            steps_cnt = res.get("steps_count", 0)
            if not res.get("success"):
                message = res.get("message") or "电脑操作未完成"
                return ActionResult(success=False, action=self.name, message=message, detail=res,
                                    error_code=res.get("error_code") or ("USER_CANCELLED" if res.get("stopped_by_user") else "COMPUTER_USE_FAILED"),
                                    speech_override=message, latency_ms=latency)

            # 3. 记录操作审计追踪
            PrivacyShield.log_audit("computer_use_executed", {
                "instruction_length": len(instruction),
                "plan_source": res.get("plan_source"),
                "steps_count": steps_cnt,
                "latency_ms": latency
            })

            speech = f"好的亲爱的，指令「{instruction[:18]}」已为您执行完成，共完成 {steps_cnt} 个桌面控制步骤~"

            return ActionResult(
                success=True,
                action=self.name,
                message=res.get("message", "执行成功"),
                detail=res,
                speech_override=speech,
                latency_ms=latency
            )

        except Exception as e:
            latency = (time.time() - start_t) * 1000
            return ActionResult(
                success=False,
                action=self.name,
                message=f"执行电脑控制指令出错: {str(e)}",
                error_code="COMPUTER_USE_EXECUTION_ERROR",
                latency_ms=latency,
                speech_override="抱歉亲爱的，操控电脑时遇到了系统异常，我已经为您安全中止操作了。"
            )

    def verify(self, context: ActionContext) -> VerificationResult:
        """Deterministic State Verification Hook (SMD-V) for desktop automation."""
        try:
            # 1. 确保所有键盘修饰键均已安全释放，防止键盘被卡住
            CanaanComputerUse.release_all_modifiers()

            # 2. 检查屏幕边界与鼠标光标有效性
            pos = CanaanComputerUse.get_cursor_pos()
            w, h = CanaanComputerUse.get_screen_size()
            if pos[0] < 0 or pos[0] > w + 200 or pos[1] < 0 or pos[1] > h + 200:
                return VerificationResult(
                    passed=False,
                    error_code="CURSOR_OUT_OF_BOUNDS",
                    reason=f"光标坐标越界: {pos}",
                    gate_name="COMPUTER_USE_COORDINATE_GATE"
                )
            return VerificationResult(passed=True, gate_name="COMPUTER_USE_SMDV_PASS", verified=True)
        except Exception as e:
            return VerificationResult(passed=False, reason=str(e), gate_name="COMPUTER_USE_VERIFY_EXCEPTION")

    def rollback(self, context: ActionContext) -> bool:
        """安全熔断与状态复位：强行释放所有鼠标按键与键盘修饰键"""
        try:
            CanaanComputerUse.release_all_modifiers()
            import ctypes
            user32 = ctypes.windll.user32
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
            user32.mouse_event(0x0010, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTUP
            return True
        except Exception:
            return False
