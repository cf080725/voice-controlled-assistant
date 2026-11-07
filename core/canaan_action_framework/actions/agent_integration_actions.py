# -*- coding: utf-8 -*-
"""
Canaan Desk OS - External Agent Integration Actions
Integrates OpenMuse (Autonomous Personal Computer) and OpenDots (AI Coworkers & Knowledge Hub).
"""
import json
import re
import urllib.request
import urllib.error
import webbrowser
import threading
import time
from canaan_action_framework.models import (
    ActionContext, ActionResult, ActionCategory, PermissionLevel
)
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry

from canaan_agent_board_data import MUSE_WEB as MUSE_WEB_URL, DOTS_WEB as DOTS_WEB_URL

def _open_only(action_name, text):
    # 保存记忆与协作没有“只开界面”分支，不能借界面词绕过上传确认。
    if action_name not in ("openmuse_task", "opendots_task"):
        return False
    modes = ("", "open_web", "open", "web", "ui", "界面", "控制台", "工作台")
    if action_name == "opendots_task":
        modes += ("空间", "知识库")
    return text in modes or bool(re.fullmatch(
        r"(?:打开|进入)(?:一下)?(?:Muse|Dots|OpenMuse|OpenDots)?(?:的)?(?:网页|界面|控制台|工作台|空间|知识库)[。！!]?", text, re.I))


def _confirm_before_sending(action, context):
    """交给 Muse / Dots 的内容会发到它们用的模型（可能是云端）：先把要发的内容给主人看，确认后才发。只打开网页不用确认。"""
    text = context.param_value().strip()
    if context.confirmed or _open_only(action.name, text):
        return None
    who = {"openmuse_task": "Muse", "opendots_task": "Dots", "opendots_memory": "Dots 知识库",
           "multiagent_task": "Muse 和 Dots"}.get(action.name, action.name)
    res = action.confirm_required(context, f"把这段内容交给 {who}：{text[:60]}", "内容会发给智能体使用的模型，可能是云端")
    res.speech_override = f"我准备把“{text[:20]}”交给{who}处理，内容会发出去。对吗？说确认就发，说取消就算了。"
    return res


@ActionRegistry.register
class OpenMuseTaskAction(BaseAction):
    name = "openmuse_task"
    aliases = ["muse_task", "openmuse", "muse_agent"]
    description = "指派任务给 OpenMuse 自主智能体（网页监控、深度搜索、表单处理、长后台运行、网页接管）"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    def authorize(self, context):
        return _confirm_before_sending(self, context)

    def execute(self, context: ActionContext) -> ActionResult:
        prompt = context.param_value().strip()

        # 1. 如果意图是打开界面
        if _open_only(self.name, prompt):
            context.report_step(1, 2, "正在唤起 OpenMuse 智能体工作台...")
            webbrowser.open(MUSE_WEB_URL)
            return ActionResult(
                success=True,
                action=self.name,
                message=f"已在浏览器打开 OpenMuse: {MUSE_WEB_URL}",
                speech_override="好的亲爱的，已经为你打开 OpenMuse 智能体工作台~"
            )

        # 2. 调用 Coordinator 下发任务
        context.report_step(1, 3, "正在连接 OpenMuse 智能体服务...")
        try:
            from canaan_agent_coordinator import CanaanAgentCoordinator
            context.report_step(2, 3, f"正在派发网页任务: {prompt[:15]}...")
            res = CanaanAgentCoordinator.dispatch_openmuse(prompt)
            context.report_step(3, 3, "任务派发完成")

            task_id = res.get("task_id", "")
            return ActionResult(
                success=res.get("success", False),
                action=self.name,
                message=f"OpenMuse 任务已创建 [ID: {task_id[:8]}]" if task_id else str(res.get("error")),
                detail={"provider": "muse", "task_id": task_id, "completed": False},
                speech_override=res.get("speech", "任务已经交给 OpenMuse，正在后台为你处理，有进展我会随时向你汇报~")
            )
        except Exception as e:
            webbrowser.open(MUSE_WEB_URL)
            return ActionResult(
                success=False,
                action=self.name,
                message=f"下发任务时异常 ({e})，已调出网页界面",
                speech_override="好的，我已为你调出 OpenMuse 工作台，你可以在界面中查看与跟进~"
            )


@ActionRegistry.register
class OpenDotsTaskAction(BaseAction):
    name = "opendots_task"
    aliases = ["dots_task", "opendots", "dots_agent"]
    description = "指派任务给 OpenDots 虚拟同事与知识库空间（写研报、多专家协作、知识归档）"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    def authorize(self, context):
        return _confirm_before_sending(self, context)

    def execute(self, context: ActionContext) -> ActionResult:
        prompt = context.param_value().strip()

        # 1. 如果意图是打开界面
        if _open_only(self.name, prompt):
            context.report_step(1, 2, "正在打开 OpenDots 知识库空间...")
            webbrowser.open(DOTS_WEB_URL)
            return ActionResult(
                success=True,
                action=self.name,
                message=f"已在浏览器打开 OpenDots: {DOTS_WEB_URL}",
                speech_override="好的亲爱的，已经为你打开 OpenDots 知识库空间~"
            )

        # 2. 调用 Coordinator 下发真实任务到 OpenDots 队列
        context.report_step(1, 3, "正在连接 OpenDots 虚拟同事通道...")
        try:
            from canaan_agent_coordinator import CanaanAgentCoordinator
            context.report_step(2, 3, f"正在指派知识库任务: {prompt[:15]}...")
            interval = context.params.get("interval_seconds")
            if interval is not None and (not isinstance(interval, int) or not 60 <= interval <= 31536000):
                return ActionResult(success=False, action=self.name, message="定时间隔须为 60–31536000 秒", error_code="INVALID_INTERVAL")
            res = CanaanAgentCoordinator.dispatch_opendots(prompt, interval_seconds=interval)
            context.report_step(3, 3, "任务指派完成")

            task_id = res.get("task_id", "")
            return ActionResult(
                success=res.get("success", False),
                action=self.name,
                message=f"OpenDots 任务已创建 [ID: {task_id[:8]}]" if task_id else str(res.get("error")),
                detail={"provider": "dots", "task_id": task_id, "completed": False, "interval_seconds": interval},
                speech_override=res.get("speech", "收到啦，任务已指派给 OpenDots 虚拟同事进行沉淀与研报整理，稍后向你汇报~")
            )
        except Exception as e:
            webbrowser.open(DOTS_WEB_URL)
            return ActionResult(
                success=False,
                action=self.name,
                message=f"下发任务时异常 ({e})，已调出网页界面",
                speech_override="提交给 OpenDots 时发生异常，我为你调出了知识空间界面~"
            )


@ActionRegistry.register
class OpenDotsMemoryAction(BaseAction):
    name = "opendots_memory"
    aliases = ["dots_memory", "save_dots_memory"]
    description = "将特定重要信息沉淀进 OpenDots 知识库记忆中"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    def authorize(self, context):
        return _confirm_before_sending(self, context)

    def execute(self, context: ActionContext) -> ActionResult:
        text = context.param_value().strip()
        if not text:
            return ActionResult(
                success=False,
                action=self.name,
                message="记忆内容不能为空",
                speech_override="想把什么内容记到知识库里呀？告诉我一声我这就存~"
            )

        try:
            from canaan_agent_coordinator import CanaanAgentCoordinator
            res = CanaanAgentCoordinator.save_dots_memory(text)
            if res.get("success"):
                return ActionResult(
                    success=True,
                    action=self.name,
                    message=f"已存入 OpenDots 知识库 [ID: {res.get('memory_id', '')[:8]}]",
                    speech_override="好嘞，这件事已经牢牢沉淀进 OpenDots 知识库啦~"
                )
            else:
                return ActionResult(
                    success=False,
                    action=self.name,
                    message=f"存入失败: {res.get('error')}",
                    speech_override="存入知识库时遇到了点小问题，不过我已经记在本地啦~"
                )
        except Exception as e:
            return ActionResult(
                success=False,
                action=self.name,
                message=f"知识库接口异常: {e}"
            )


@ActionRegistry.register
class MultiAgentCooperateAction(BaseAction):
    name = "multiagent_task"
    aliases = ["multiagent", "cooperate_task", "hybrid_agent"]
    description = "跨智能体多步骤协同作战（Muse 网页抓取 + Dots 知识沉淀 + 团团管家统筹）"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    def authorize(self, context):
        return _confirm_before_sending(self, context)

    def execute(self, context: ActionContext) -> ActionResult:
        prompt = context.param_value().strip()
        if not prompt:
            prompt = context.user_text or ""

        try:
            from canaan_agent_coordinator import CanaanAgentCoordinator
            context.report_step(1, 3, "启动多智能体协同流水线...")
            res = CanaanAgentCoordinator.run_hybrid_pipeline(
                prompt,
                on_progress=lambda msg: context.report_step(2, 3, msg)
            )
            context.report_step(3, 3, "各智能体任务调度就绪")
            return ActionResult(
                success=bool(res.get("success")),
                action=self.name,
                message="Muse 结果已提交给 Dots" if res.get("success") else str(res.get("error") or "协作任务未完成"),
                detail=res,
                speech_override=res.get("speech", "好的亲爱的！我已经全面启动协同作业，Muse 和 Dots 正在通力合作~")
            )
        except Exception as e:
            return ActionResult(
                success=False,
                action=self.name,
                message=f"协同调度异常: {e}",
                speech_override=f"多智能体协同调度时出了点小状况：{e}"
            )
