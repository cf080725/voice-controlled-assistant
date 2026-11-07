# -*- coding: utf-8 -*-
"""消息总结动作：用本地模型总结已经采集到本机消息库里的消息与明确待办（两个版本都有）。
从 message_suite_actions.py 拆出来：那个文件是聊天软件 / 聊天软件 工具，只在自用版里。"""
import json

from canaan_action_framework.models import ActionContext, ActionResult, ActionCategory, PermissionLevel
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry


@ActionRegistry.register
class DailyBriefingAction(BaseAction):
    name = "daily_briefing"
    aliases = ["morning_briefing", "message_briefing", "daily_digest", "today_todos"]
    description = "用本地千问总结已采集的消息与明确待办"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.SAFE_READONLY

    def execute(self, context: ActionContext) -> ActionResult:
        context.report_step(1, 2, "正在汇总并安全脱敏本地聊天记录...")
        try:
            from message_tools.canaan_daily_briefing import generate_executive_briefing
            briefing = generate_executive_briefing(use_local_fallback=True, sources=context.params.get("sources"),
                                                  message_db=context.params.get("message_db"))
            speech = briefing.get("speech_text", "未获得有效的消息总结。")
            return ActionResult(
                success=briefing.get("success", False),
                action=self.name,
                message=json.dumps(briefing, ensure_ascii=False),
                speech_override=speech, detail=briefing, error_code=briefing.get("error_code")
            )
        except Exception as e:
            return ActionResult(
                success=False,
                action=self.name,
                message=f"提炼晨报异常: {e}",
                speech_override="提炼消息总结时遇到了点小异常，稍后再试一次好吗？"
            )
