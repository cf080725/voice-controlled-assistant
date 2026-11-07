# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Commercial Privacy and Security Defense Shield
Enterprise-grade PII de-identification, sensitive window interception,
and cryptographic audit logging to ensure absolute local data sovereignty.
"""
import ctypes
import json
import logging
import os
import re
import threading
import time
from ctypes import wintypes
from typing import Any, Dict, Tuple

from canaan_filelock import file_lock

logger = logging.getLogger("canaan_privacy_shield")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
AUDIT_LOG_FILE = os.path.join(DATA_DIR, "privacy_audit.jsonl")
_AUDIT_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 1. 敏感应用与窗口黑名单 (Sensitive Windows Blocklist)
# ---------------------------------------------------------------------------
SENSITIVE_PROCESS_NAMES = {
    "1password.exe", "bitwarden.exe", "keepass.exe", "lastpass.exe",
    "enpass.exe", "passwarden.exe", "authenticator.exe", "mstsc.exe",
    "teamviewer.exe", "anydesk.exe", "vncviewer.exe"
}

SENSITIVE_WINDOW_KEYWORDS = [
    "1password", "bitwarden", "keepass", "lastpass", "enpass",
    "支付", "收银台", "网银", "网上银行", "安全中心", "密码箱", "输入密码",
    "alipay", "chatapp pay", "payment", "checkout", "bank", "token",
    "无痕浏览", "隐私窗口", "incognito", "private browsing", "inprivate"
]

# ---------------------------------------------------------------------------
# 2. PII 脱敏正则表达式库 (PII Regex De-Identification)
# ---------------------------------------------------------------------------
# 中国大陆手机号
RE_PHONE = re.compile(r'(?<!\d)(?:(?:\+?86)?\s*)?(1[3-9]\d{9})(?!\d)')
# 中国大陆身份证号 (18位)
RE_IDCARD = re.compile(r'(?<!\d)([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)')
# 银行卡号 (16~19位)
RE_BANKCARD = re.compile(r'(?<!\d)([1-9]\d{3}\s*(?:\d{4}\s*){2,3}\d{1,4})(?!\d)')
# 各类云厂商与大模型 API Key / Secret Token
RE_SECRET_KEY = re.compile(
    r'(?<![a-zA-Z0-9_-])(?:'
    r'sk-[a-zA-Z0-9_-]{20,}|'
    r'ghp_[a-zA-Z0-9]{36,}|'
    r'AKIA[0-9A-Z]{16}|'
    r'AIza[0-9A-Za-z\-_]{35}|'
    r'AQ\.[a-zA-Z0-9_-]{20,}|'
    r'cpk-[a-zA-Z0-9_-]{20,}'
    r')(?![a-zA-Z0-9_-])'
)
# 电子邮件脱敏
RE_EMAIL = re.compile(r'([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)')


class PrivacyShield:
    @staticmethod
    def mask_text(text: str) -> Tuple[str, Dict[str, int]]:
        """
        对输入文本进行全维度脱敏，返回 (脱敏后文本, 拦截统计计数)
        """
        if not text or not isinstance(text, str):
            return text, {}

        stats = {
            "phone_masked": 0,
            "idcard_masked": 0,
            "bankcard_masked": 0,
            "secret_masked": 0,
            "email_masked": 0
        }

        # 1. 脱敏 API Keys / Secrets
        def _sub_secret(m):
            stats["secret_masked"] += 1
            return "[REDACTED_SECRET_KEY]"
        text = RE_SECRET_KEY.sub(_sub_secret, text)

        # 2. 脱敏身份证
        def _sub_idcard(m):
            stats["idcard_masked"] += 1
            val = m.group(1)
            return val[:6] + "********" + val[-4:]
        text = RE_IDCARD.sub(_sub_idcard, text)

        # 3. 脱敏银行卡
        def _sub_bank(m):
            raw = re.sub(r'\s+', '', m.group(1))
            if 15 <= len(raw) <= 19:
                stats["bankcard_masked"] += 1
                return raw[:6] + "******" + raw[-4:]
            return m.group(0)
        text = RE_BANKCARD.sub(_sub_bank, text)

        # 4. 脱敏手机号
        def _sub_phone(m):
            stats["phone_masked"] += 1
            raw = m.group(1)
            return raw[:3] + "****" + raw[-4:]
        text = RE_PHONE.sub(_sub_phone, text)

        # 5. 脱敏邮箱
        def _sub_email(m):
            stats["email_masked"] += 1
            user, domain = m.group(1), m.group(2)
            if len(user) <= 2:
                masked_user = "*借*"
            else:
                masked_user = user[0] + "***" + user[-1]
            return f"{masked_user}@{domain}"
        text = RE_EMAIL.sub(_sub_email, text)

        return text, stats

    @staticmethod
    def get_foreground_window_info() -> Tuple[int, str, str]:
        """获取当前活动窗口句柄、窗口标题与所属进程名"""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return 0, "", ""

            # 获取标题
            length = user32.GetWindowTextLengthW(hwnd)
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value or ""

            # 获取进程名
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = ""
            
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if h_proc:
                try:
                    name_buf = ctypes.create_unicode_buffer(260)
                    size = wintypes.DWORD(260)
                    if hasattr(kernel32, "QueryFullProcessImageNameW"):
                        if kernel32.QueryFullProcessImageNameW(h_proc, 0, name_buf, ctypes.byref(size)):
                            proc_name = os.path.basename(name_buf.value).lower()
                finally:
                    kernel32.CloseHandle(h_proc)

            return hwnd, title, proc_name
        except Exception:
            return 0, "", ""

    @classmethod
    def is_sensitive_window(cls, title: str, proc_name: str = "") -> bool:
        """检查指定标题或进程名是否命中敏感窗口黑名单"""
        t_low = (title or "").lower()
        p_low = (proc_name or "").lower()
        if p_low and p_low in SENSITIVE_PROCESS_NAMES:
            return True
        for kw in SENSITIVE_WINDOW_KEYWORDS:
            if kw in t_low:
                return True
        return False

    @classmethod
    def check_screen_capture_allowed(cls) -> Tuple[bool, str]:
        """
        在截屏前进行隐私安全嗅探：
        若活动窗口属于密码管理器、网银、支付等敏感界面，直接物理级阻断，返回 (False, 拦截原因)
        """
        hwnd, title, proc_name = cls.get_foreground_window_info()
        title_lower = title.lower()

        # 1. 进程名黑名单检查
        if proc_name in SENSITIVE_PROCESS_NAMES:
            reason = f"检测到密码管理器或远程桌面程序 [{proc_name}] 处于前台"
            # 审计日志只记进程名，不记窗口标题（标题里可能有账户、金额等信息）
            cls.log_audit("screen_capture_blocked", {"process": proc_name, "reason": reason})
            return False, reason

        # 2. 窗口标题关键词黑名单检查
        for kw in SENSITIVE_WINDOW_KEYWORDS:
            if kw in title_lower:
                reason = f"检测到窗口标题包含敏感隐私关键词 [{kw}]"
                cls.log_audit("screen_capture_blocked", {"process": proc_name, "keyword": kw})
                return False, reason

        return True, "允许安全捕获"

    _DETAIL_ALLOWLIST = {
        "screen_capture_blocked": {"process", "keyword", "reason"},
        "computer_use_blocked_sensitive_window": {"window_title_length", "process"},
        "computer_use_executed": {"plan_source", "steps_count", "latency_ms", "instruction_length"},
        "screen_copilot_success": {"model"},
    }

    @classmethod
    def sanitize_details(cls, event_type: str, details: Dict[str, Any]) -> Dict[str, Any]:
        allowed = cls._DETAIL_ALLOWLIST.get(str(event_type or ""), set())
        if not isinstance(details, dict):
            return {}
        # 兼容旧调用：只把原文长度留下，原文本身绝不落盘
        if str(event_type) == "computer_use_executed" and "instruction" in details and "instruction_length" not in details:
            details = {**details, "instruction_length": len(str(details.get("instruction") or ""))}
        if str(event_type) == "computer_use_blocked_sensitive_window" and "window" in details and "window_title_length" not in details:
            details = {**details, "window_title_length": len(str(details.get("window") or ""))}
        out = {}
        for key in allowed:
            if key not in details:
                continue
            value = details.get(key)
            if isinstance(value, str):
                masked, _ = cls.mask_text(value)
                out[key] = masked[:200]
            elif isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
                out[key] = value
        return out

    @classmethod
    def sanitize_record(cls, record: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(record, dict):
            return {}
        event = str(record.get("event") or "")
        return {
            "timestamp": record.get("timestamp"),
            "datetime": record.get("datetime"),
            "event": event,
            "details": cls.sanitize_details(event, record.get("details") or {}),
        }

    @classmethod
    def log_audit(cls, event_type: str, details: Dict[str, Any]):
        """记录本地结构化隐私审计日志 (JSONL)。只写允许的字段，不写原话/标题/正文。"""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            record = {
                "timestamp": time.time(),
                "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": event_type,
                "details": cls.sanitize_details(event_type, details),
            }
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with _AUDIT_LOCK, file_lock(AUDIT_LOG_FILE):
                with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as e:
            logger.debug(f"Audit log write failed: {e}")
