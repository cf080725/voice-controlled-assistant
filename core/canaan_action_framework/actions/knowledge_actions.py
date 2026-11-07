from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.models import ActionCategory, ActionResult, PermissionLevel
from canaan_action_framework.action_registry import ActionRegistry


@ActionRegistry.register
class KnowledgeQueryAction(BaseAction):
    name = "knowledge_query"
    aliases = ["rag_query"]
    description = "检索本地 RAG 知识库，回答时附文档来源"
    category = ActionCategory.FILE_OPERATIONS
    permission_level = PermissionLevel.SAFE_READONLY
    input_schema = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}

    def execute(self, context):
        from canaan_knowledge import get_knowledge
        question = context.params.get("question") or context.param_value()
        result = get_knowledge().answer(question)
        return ActionResult(success=True, action=self.name, message=result["answer"], detail=result, speech_override=result["answer"])


@ActionRegistry.register
class MessagingLoginAction(BaseAction):
    name = "messaging_login"
    aliases = ["open_messaging_client"]
    description = "打开聊天软件或 聊天软件 客户端，引导用户完成扫码/验证码登录"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION
    input_schema = {"type": "object", "properties": {"app": {"type": "string", "enum": ["聊天软件", "chatapp", "chatapp", "IMApp", "imapp"]}}, "required": ["app"]}

    def execute(self, context):
        app = context.params.get("app") or context.param_value()
        if str(app).lower() not in ("聊天软件", "chatapp", "chatapp", "imapp"):
            return ActionResult(success=False, action=self.name, error_code="INVALID_TARGET", message="只支持聊天软件或 IMApp")
        result = ActionRegistry.dispatch("open_app", str(app), source=context.source, confirmed=context.confirmed)
        if result.success:
            result.message = "已打开客户端；未登录时请在客户端扫码或输入验证码，完成后消息监听自动重试。"
            result.speech_override = result.message
            result.detail = {"requires_user_login": True, "login_verified": False}
        return result
