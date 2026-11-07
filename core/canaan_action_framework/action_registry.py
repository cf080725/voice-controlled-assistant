# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Enterprise Action Registry & Middleware Pipeline
Provides dynamic action discovery, schema reflection, telemetry, and structured audit trails.
"""
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Type

from canaan_filelock import file_lock
from canaan_execution_guard import execution_allowed

from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.models import ActionContext, ActionResult

# Configure structured enterprise audit logger
AUDIT_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs")
os.makedirs(AUDIT_LOG_DIR, exist_ok=True)
AUDIT_LOG_PATH = os.environ.get("CANAAN_AUDIT_LOG") or os.path.join(AUDIT_LOG_DIR, "canaan_audit.jsonl")

def dry_run_enabled() -> bool:
    """演练模式开关（评测用）：环境变量 CANAAN_DRY_RUN=1。只在评测脚本里打开，团团正常运行时不设。"""
    return os.environ.get("CANAAN_DRY_RUN", "").strip() == "1"


def _revoked_result(context, phase, *, execution_started, effects_may_have_occurred, previous=None):
    """A lost scope stops later hooks; it does not undo an earlier OS effect."""
    detail = dict(previous.detail) if previous is not None and isinstance(previous.detail, dict) else {}
    detail.update(execution_revoked=True, phase=phase, execution_started=execution_started,
                  effects_may_have_occurred=effects_may_have_occurred,
                  partial_execution=effects_may_have_occurred)
    if previous is not None:
        detail["prior_success"] = previous.success
        if previous.error_code:
            detail["prior_error_code"] = previous.error_code
    message = ("任务授权或使用者会话已变化，后续处理已停止；已开始的操作请在电脑上核对。"
               if effects_may_have_occurred else "任务授权或使用者会话已变化，本次动作未开始执行。")
    return ActionResult(success=False, action=context.action_name, message=message,
                        detail=detail, speech_override=message, error_code="EXECUTION_REVOKED",
                        trace_id=context.trace_id, latency_ms=(time.time() - context.start_time) * 1000,
                        verification=previous.verification if previous is not None else None)


class ActionRegistry:
    """Microkernel Registry for Enterprise Desktop Actions"""
    
    _actions: Dict[str, BaseAction] = {}
    _alias_map: Dict[str, str] = {}
    _middlewares: List[Callable[[ActionContext, Callable[[], ActionResult]], ActionResult]] = []

    @classmethod
    def register(cls, action_cls: Type[BaseAction]) -> Type[BaseAction]:
        """Class decorator for registering actions"""
        instance = action_cls()
        primary_name = instance.name.lower().strip()
        cls._actions[primary_name] = instance
        cls._alias_map[primary_name] = primary_name
        
        for alias in instance.aliases:
            cls._alias_map[alias.lower().strip()] = primary_name
            
        return action_cls

    @classmethod
    def get_action(cls, name: str) -> Optional[BaseAction]:
        clean_name = name.lower().strip()
        target_name = cls._alias_map.get(clean_name, clean_name)
        return cls._actions.get(target_name)

    @classmethod
    def list_actions(cls) -> List[Dict[str, Any]]:
        """Introspection API for OpenAPI / LLM Function Calling tool generation"""
        return [
            {
                "name": act.name,
                "aliases": act.aliases,
                "description": act.description,
                "category": act.category.value,
                "permission_level": act.permission_level.value,
                "input_schema": act.input_schema
            }
            for act in cls._actions.values()
        ]

    @classmethod
    def _audit_log(cls, context: ActionContext, result: ActionResult):
        """Append immutable JSONL audit record for enterprise compliance"""
        try:
            # 自由文本的字段名常是 value/message，按字段名遮密码会漏掉内容。
            sanitized_params = {"count": len(context.params),
                                "types": sorted({type(v).__name__ for v in context.params.values()})}
                    
            record = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(context.start_time)),
                "trace_id": context.trace_id,
                "task_id": context.task_id,
                "action": context.action_name if cls.get_action(context.action_name) else "unknown",
                "params": sanitized_params,
                "source": context.source,
                "confirmed": context.confirmed,
                "success": result.success,
                "latency_ms": round(result.latency_ms, 2),
                "error_code": result.error_code,
                "message": "",
                "content_redacted": True,
            }
            with file_lock(AUDIT_LOG_PATH):
                with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @classmethod
    def dispatch(cls, action_name: str, param: Any = None, on_step: Optional[Callable] = None,
                 source: str = "llm", confirmed: bool = False, user_text: str = "") -> ActionResult:
        """调度流水线：参数归一化 → 构建上下文 → 权限闸门 → 执行 → 闭环校验 → 审计日志

        source:    "rule"（确定性规则）或 "llm"（大模型生成，默认，按最严格处理）
        confirmed: 用户是否已口头确认
        """
        start_time = time.time()
        clean_name = (action_name or "").lower().strip()
        action = cls.get_action(clean_name)
        
        # Parse params into structured dict
        params_dict = {}
        if isinstance(param, dict):
            params_dict = param
        elif isinstance(param, str):
            p_str = param.strip()
            if p_str.startswith("{") and p_str.endswith("}"):
                try:
                    params_dict = json.loads(p_str)
                except Exception:
                    params_dict = {"value": p_str}
            else:
                params_dict = {"value": p_str}
        else:
            params_dict = {"value": param}

        context = ActionContext(
            action_name=clean_name,
            params=params_dict,
            raw_param=param,
            start_time=start_time,
            on_step=on_step,
            source=source,
            confirmed=bool(confirmed),
            user_text=user_text or "",
        )

        execution_started = False
        effects_may_have_occurred = False
        res = None

        def revoked(phase, previous=None):
            if execution_allowed():
                return None
            result = _revoked_result(context, phase, execution_started=execution_started,
                                     effects_may_have_occurred=effects_may_have_occurred,
                                     previous=previous if previous is not None else res)
            cls._audit_log(context, result)
            return result

        stopped = revoked("before_authorize")
        if stopped is not None:
            return stopped

        if not action:
            res = ActionResult(
                success=False,
                action=clean_name,
                message=f"未识别的系统动作: {clean_name}",
                error_code="ACTION_NOT_FOUND",
                trace_id=context.trace_id,
                latency_ms=(time.time() - start_time) * 1000,
                speech_override="这个指令姐姐暂时还没学会呢。"
            )
            cls._audit_log(context, res)
            return res

        # 0. 权限闸门（需要确认 / 拒绝的请求不进入执行）
        try:
            gate = action.authorize(context)
        except Exception as e:
            gate = ActionResult(success=False, action=action.name, error_code="AUTHORIZE_ERROR",
                                message=f"权限检查异常: {e}", speech_override="权限检查出错了，这次先不执行。")
        stopped = revoked("after_authorize", gate)
        if stopped is not None:
            return stopped
        if gate is not None:
            gate.trace_id = context.trace_id
            gate.latency_ms = (time.time() - start_time) * 1000
            cls._audit_log(context, gate)
            return gate

        if dry_run_enabled():
            # 演练模式：权限闸门照常判断，通过后不执行、不校验、不写审计日志，只报告“将执行什么”
            return ActionResult(success=True, action=action.name, error_code="DRY_RUN",
                                message=f"[演练] 将执行 {action.name}：{context.param_value()[:60]}",
                                detail={"dry_run": True, "permission_level": action.permission_level.value},
                                trace_id=context.trace_id, latency_ms=(time.time() - start_time) * 1000)

        try:
            # 1. Pre-execution Lifecycle
            # Pre/verify/post/rollback hooks may touch the desktop too. A later
            # revocation must not authorize them merely because execute began.
            stopped = revoked("before_pre_execute")
            if stopped is not None:
                return stopped
            effects_may_have_occurred = True
            action.pre_execute(context)

            stopped = revoked("after_pre_execute")
            if stopped is not None:
                return stopped
            
            # 2. Main Execution Logic
            execution_started = True
            res = action.execute(context)
            res.trace_id = context.trace_id
            res.latency_ms = (time.time() - start_time) * 1000

            stopped = revoked("after_execute")
            if stopped is not None:
                return stopped
            
            # 3. Deterministic Verification Hook (SMD-V)
            if res.success:
                stopped = revoked("before_verify")
                if stopped is not None:
                    return stopped
                v_res = action.verify(context)
                res.verification = v_res
                stopped = revoked("after_verify")
                if stopped is not None:
                    return stopped
                if not v_res.passed:
                    res.success = False
                    res.error_code = v_res.error_code or "VERIFICATION_FAILED"
                    res.message = f"执行完成但闭环校验未通过: {v_res.reason}"
                    res.speech_override = None  # Discard the execution's earlier success announcement.
                    # Trigger Rollback（只有真正实现了补偿逻辑的动作才调用）
                    if getattr(action, "rollback_supported", False):
                        stopped = revoked("before_rollback")
                        if stopped is not None:
                            return stopped
                        action.rollback(context)
                        stopped = revoked("after_rollback")
                        if stopped is not None:
                            return stopped

            # 4. Post-execution Lifecycle
            stopped = revoked("before_post_execute")
            if stopped is not None:
                return stopped
            action.post_execute(context, res)
            stopped = revoked("after_post_execute")
            if stopped is not None:
                return stopped
            
        except Exception as e:
            stopped = revoked("execution_exception")
            if stopped is not None:
                return stopped
            res = ActionResult(
                success=False,
                action=clean_name,
                message=f"执行引擎异常: {e}",
                error_code="INTERNAL_EXECUTION_ERROR",
                trace_id=context.trace_id,
                latency_ms=(time.time() - start_time) * 1000,
                speech_override="刚才执行的时候出了点小状况呢。"
            )
            if getattr(action, "rollback_supported", False):
                stopped = revoked("before_exception_rollback")
                if stopped is not None:
                    return stopped
                try:
                    action.rollback(context)
                except Exception:
                    pass
                stopped = revoked("after_exception_rollback")
                if stopped is not None:
                    return stopped

        # 5. Emit Audit Telemetry
        cls._audit_log(context, res)
        return res
