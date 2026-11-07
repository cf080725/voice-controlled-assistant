# -*- coding: utf-8 -*-
"""
研发副驾类动作：工程文档检索、运行工程脚本、剪贴板报错诊断、一键打开开发环境。
具体实现见 canaan_engineering_agent.CanaanEngineeringAgent。
"""
from canaan_action_framework.models import (
    ActionContext, ActionResult, ActionCategory, PermissionLevel
)
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry


def _agent():
    from canaan_engineering_agent import CanaanEngineeringAgent
    return CanaanEngineeringAgent


def _from_agent(name, res, speech_key="summary"):
    ok = bool(res.get("success", False))
    return ActionResult(
        success=ok,
        action=name,
        message=res.get("message", ""),
        detail=res.get("detail") or res.get("summary"),
        speech_override=res.get(speech_key) or None,
        error_code=None if ok else res.get("error_code") or "AGENT_FAILED",
    )


@ActionRegistry.register
class DocSearchAction(BaseAction):
    name = "doc_search"
    aliases = ["search_doc"]
    description = "检索竞赛指南与开发文档并用本地大模型总结要点"
    category = ActionCategory.FILE_OPERATIONS
    permission_level = PermissionLevel.SAFE_READONLY

    def execute(self, context: ActionContext) -> ActionResult:
        return _from_agent(self.name, _agent().search_document("指南", context.param_value()))


@ActionRegistry.register
class RunScriptAction(BaseAction):
    """运行工程目录下的 Python 脚本。会执行代码，大模型发起时需要用户口头确认。"""
    name = "run_script"
    aliases = ["execute_script"]
    description = "运行工程目录内的 .py 脚本并总结输出"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.CRITICAL_DESTRUCTIVE
    confirm_even_from_rule = True  # 跑脚本：规则识别的也先确认

    def execute(self, context: ActionContext) -> ActionResult:
        return _from_agent(self.name, _agent().execute_and_analyze_script(context.param_value()))


@ActionRegistry.register
class DiagnoseErrorAction(BaseAction):
    name = "diagnose_error"
    aliases = ["diagnose"]
    description = "根据用户描述或指定的报错来源给出排查建议"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.SAFE_READONLY

    def execute(self, context: ActionContext) -> ActionResult:
        from canaan_intent import diagnostic_request_text, diagnostic_source
        # 原话优先，不能用模型补出的故障细节替换用户提供的证据。
        problem = diagnostic_request_text(context.user_text, context.param_value()) if context.user_text.strip() else context.param_value()
        if problem is None:
            return ActionResult(success=False, action=self.name, error_code="AMBIGUOUS_EVIDENCE",
                                message="无法确定当前诊断步骤的来源", speech_override="这一步要分析哪个来源？请说清楚后再继续。")
        source = diagnostic_source(problem)
        if source == "screen":
            result = ActionRegistry.dispatch("screen_copilot", problem, source=context.source,
                                             confirmed=context.confirmed, user_text=context.user_text,
                                             on_step=context.on_step)
            result.action = self.name
            return result
        if source == "clipboard":
            return _from_agent(self.name, _agent().diagnose_clipboard_or_screen_error())
        return _from_agent(self.name, _agent().diagnose_problem(problem))


@ActionRegistry.register
class SetupWorkspaceAction(BaseAction):
    name = "setup_workspace"
    aliases = ["open_workspace"]
    description = "打开机器鱼开发环境（VS Code、工程目录、开发指南）"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        return _from_agent(self.name, _agent().setup_workspace(context.param_value() or "fishsim"))
