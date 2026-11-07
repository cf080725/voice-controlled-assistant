# -*- coding: utf-8 -*-
"""设提醒：只写本机提醒列表（到点弹系统通知并说出来），不碰别的东西。
单独一句的提醒仍由 canaan_proactive.handle_command 处理；这个动作给多步任务里的“……再提醒我明早八点开会”用。"""
from canaan_action_framework.models import ActionContext, ActionResult, ActionCategory, PermissionLevel
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry


@ActionRegistry.register
class SetReminderAction(BaseAction):
    name = "set_reminder"
    aliases = ["add_reminder", "reminder"]
    description = "设一个本机提醒（参数是原话里的提醒分句，如“明早八点提醒我开会”）"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        import canaan_proactive
        text = str(context.params.get("value") or context.params.get("text") or "").strip()
        policy = canaan_proactive.default_policy()
        parsed = canaan_proactive.parse_reminder(text, policy.clock())
        if not parsed:
            return ActionResult(success=False, action=self.name, error_code="INVALID_PARAM",
                                message=f"没认出提醒时间：{text[:30]}", speech_override="提醒的时间我没听清，再说一遍几点？")
        due, what = parsed
        try:
            policy.add_reminder(due, what)
        except OSError:
            return ActionResult(success=False, action=self.name, error_code="REMINDER_SAVE_FAILED",
                                message="提醒没有保存成功", speech_override="提醒暂时没有保存成功，请稍后再试。")
        when = due.strftime("%H:%M") if due.date() == policy.clock().date() else due.strftime("%m月%d日 %H:%M")
        return ActionResult(success=True, action=self.name, message=f"已设提醒：{when} {what}",
                            speech_override=f"好，{when}提醒你{what}。")
