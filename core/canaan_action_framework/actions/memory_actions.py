# -*- coding: utf-8 -*-
"""记忆管理动作：彻底删除（需用户先口头确认，由 canaan_companion.handle_memory_command 发起）。"""
from canaan_action_framework.models import ActionContext, ActionResult, ActionCategory, PermissionLevel
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry


@ActionRegistry.register
class ForgetMemoryAction(BaseAction):
    name = "forget_memory"
    aliases = ["delete_memory"]
    description = "彻底删除含关键词的长期记忆（事实、向量、会话摘要与本地对话记录）"
    category = ActionCategory.FILE_OPERATIONS
    permission_level = PermissionLevel.SENSITIVE

    def authorize(self, context: ActionContext):
        # 只能在用户口头确认后执行（来源不论）
        if not context.confirmed:
            return self.confirm_required(context, f"彻底删除关于“{context.param_value()}”的记忆")
        return None

    def execute(self, context: ActionContext) -> ActionResult:
        import canaan_memory
        kw = context.param_value()
        n = canaan_memory.get_store().forget(kw)
        return ActionResult(success=True, action=self.name, message=f"已删除 {n} 条记忆（关键词：{kw}）",
                            speech_override=f"好，删掉了{n}条，相关的聊天记录也清掉了。" if n else "我这儿没记着相关的事。")


@ActionRegistry.register
class ForgetPersonMemoryAction(BaseAction):
    name = "forget_person_memory"
    description = "彻底删除某个非主人用户含关键词的长期记忆（canaan_people，不碰主人的记忆）"
    category = ActionCategory.FILE_OPERATIONS
    permission_level = PermissionLevel.SENSITIVE

    def authorize(self, context: ActionContext):
        if not context.confirmed:
            return self.confirm_required(context, "彻底删除这个人的相关记忆")
        return None

    def execute(self, context: ActionContext) -> ActionResult:
        import json
        import canaan_people
        p = context.params if "person_id" in (context.params or {}) else {}
        if not p:
            try:
                p = json.loads(context.raw_param) if isinstance(context.raw_param, str) else dict(context.raw_param or {})
            except (ValueError, TypeError):
                p = {}
        try:
            store = canaan_people.get_people().store(int(p["person_id"]))
        except (ValueError, KeyError, TypeError):
            store = None
        n = store.forget(p.get("keyword", "")) if store is not None else 0
        return ActionResult(success=True, action=self.name, message=f"已删除 {n} 条记忆",
                            speech_override=f"好，删掉了{n}条。" if n else "我这儿没记着相关的事。")
