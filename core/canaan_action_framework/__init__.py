# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Enterprise Action Framework
Exports core models, registry, and auto-discovers all action drivers.
"""
from canaan_action_framework.models import (
    ActionContext, ActionResult, VerificationResult,
    ActionCategory, PermissionLevel
)
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry
from canaan_action_framework.session_guard import DesktopSessionGuard

# 导入即注册全部动作
import canaan_action_framework.actions.system_actions
import canaan_action_framework.actions.app_actions
import canaan_action_framework.actions.engineering_actions
import canaan_action_framework.actions.agent_actions
import canaan_action_framework.actions.memory_actions
import canaan_action_framework.actions.agent_integration_actions
import canaan_action_framework.actions.briefing_actions
import canaan_action_framework.actions.reminder_actions
import canaan_action_framework.actions.screen_copilot_action
import canaan_action_framework.actions.computer_use_action
import canaan_action_framework.actions.knowledge_actions

# 聊天软件 / 聊天软件 动作只在自用版里：公开版不带这两个文件，文件不在就不注册（文件在但出错照常报错）
import importlib as _importlib
import importlib.util as _importlib_util
for _name in ("chatapp_action", "message_suite_actions"):
    if _importlib_util.find_spec("canaan_action_framework.actions." + _name) is not None:
        _importlib.import_module("canaan_action_framework.actions." + _name)

__all__ = [
    "ActionContext",
    "ActionResult",
    "VerificationResult",
    "ActionCategory",
    "PermissionLevel",
    "BaseAction",
    "ActionRegistry",
    "DesktopSessionGuard"
]
