# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Enterprise Action Framework Models
Defines industrial-grade data contracts, telemetry schemas, and verification types.
"""
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class PermissionLevel(str, Enum):
    SAFE_READONLY = "safe_readonly"        # Safe query, e.g. system status, time, battery
    NORMAL_MUTATION = "normal_mutation"    # Volume, brightness, window minimization, open app
    SENSITIVE = "sensitive"                # Send message, write file, clipboard modification
    CRITICAL_DESTRUCTIVE = "critical_destructive" # 可能造成不可逆影响（执行任意命令/脚本）；来自大模型时须用户口头确认

class ActionCategory(str, Enum):
    MESSAGING = "messaging"
    SYSTEM_CONTROL = "system_control"
    APPLICATION_LAUNCHER = "application_launcher"
    FILE_OPERATIONS = "file_operations"
    VISION_MULTIMODAL = "vision_multimodal"
    HARDWARE_IO = "hardware_io"

@dataclass
class VerificationResult:
    """State Machine Deterministic Verification (SMD-V) Result Contract"""
    passed: bool
    gate_name: str
    error_code: Optional[str] = None
    reason: str = ""
    telemetry_data: Dict[str, Any] = field(default_factory=dict)
    verified: bool = False

@dataclass
class ActionContext:
    """Execution Context providing runtime environment, telemetry trace, and session isolation"""
    action_name: str
    params: Dict[str, Any] = field(default_factory=dict)
    raw_param: Any = None
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    trace_id: str = field(default_factory=lambda: f"trace-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}")
    caller_fg_hwnd: int = 0
    caller_cursor_pos: tuple = (0, 0)
    user_clipboard_backup: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    on_step: Optional[Callable[[int, int, str], None]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 指令来源："rule" = 代码里写死的确定性规则；"llm" = 大模型生成（需经过权限闸门）
    source: str = "llm"
    # 用户是否已经口头确认过本次操作
    confirmed: bool = False
    # 用户原话（用于核对大模型生成的参数是否忠于原意）
    user_text: str = ""

    def param_value(self) -> str:
        """单值参数（非 JSON 对象）统一取法。"""
        v = self.params.get("value")
        if v is None:
            v = self.raw_param if not isinstance(self.raw_param, dict) else ""
        return str(v if v is not None else "").strip()

    def report_step(self, current: int, total: int, description: str):
        if self.on_step:
            try:
                self.on_step(current, total, description)
            except Exception:
                pass

@dataclass
class ActionResult:
    """Standardized Action Output Contract complying with enterprise SDK standards"""
    success: bool
    action: str
    message: str = ""
    detail: Optional[Any] = None
    speech_override: Optional[str] = None
    error_code: Optional[str] = None
    trace_id: str = ""
    latency_ms: float = 0.0
    verification: Optional[VerificationResult] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "message": self.message,
            "detail": self.detail,
            "speech_override": self.speech_override,
            "error_code": self.error_code,
            "trace_id": self.trace_id,
            "latency_ms": round(self.latency_ms, 2),
            "verification": None if self.verification is None else {
                "passed": self.verification.passed,
                "verified": self.verification.verified,
                "gate_name": self.verification.gate_name,
                "error_code": self.verification.error_code,
                "reason": self.verification.reason,
            },
        }
