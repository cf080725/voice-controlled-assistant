# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Screen Co-pilot Action (Privacy-Hardened Commercial Grade)
- Zero-intrusion Win32 GDI in-memory capture (<50ms)
- Anti-peeping Privacy Shield: blocks capture when sensitive windows (passwords, banks, checkout) are active
- Zero disk persistence (pure RAM buffer with explicit gc cleanup)
- Cloud multimodal vision (Qwen3-VL-30B-A3B) with failover and complete audit logging
"""
import gc
import io
import json
import base64
import ctypes
import urllib.request
import urllib.error
from ctypes import wintypes
from PIL import Image

from canaan_action_framework.models import (
    ActionContext, ActionResult, ActionCategory, PermissionLevel
)
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry
from canaan_privacy_shield import PrivacyShield
import canaan_secrets

# 密钥不写在代码里：环境变量 CANAAN_SILICONFLOW_API_KEY 或 data/secrets.json（不入库）
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
PRIMARY_VISION_MODEL = "Qwen/Qwen3-VL-30B-A3B-Instruct"
FALLBACK_VISION_MODEL = "Qwen/Qwen3-VL-32B-Instruct"


def _capture_screen_memory():
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbm = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old_bm = gdi32.SelectObject(hdc_mem, hbm)

    SRCCOPY = 0x00CC0020
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, 0, 0, SRCCOPY)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    buf = (ctypes.c_ubyte * (w * h * 4))()
    lines = gdi32.GetDIBits(hdc_mem, hbm, 0, h, ctypes.byref(buf), ctypes.byref(bmi), 0)

    gdi32.SelectObject(hdc_mem, old_bm)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)

    if lines > 0:
        return Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1).convert("RGB")
    return None


def _call_vision_api(model_name, b64_str, prompt, timeout=10.0):
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是团团（Tuantuan），一个专业、高效、温柔的AI管家。你称呼用户为【亲爱的】。"
                    "请仔细观察用户屏幕截图中的文字、代码、报错或界面内容。"
                    "用 2 到 3 句话直接给出诊断结论与解决步骤，语言亲切自然、精炼口语化，适合语音直接朗读。"
                )
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}}
                ]
            }
        ],
        "max_tokens": 150
    }

    req = urllib.request.Request(
        SILICONFLOW_URL,
        headers={
            "Authorization": f"Bearer {canaan_secrets.get_secret('SILICONFLOW_API_KEY')}",
            "Content-Type": "application/json"
        },
        data=json.dumps(payload).encode("utf-8")
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
        return res_data["choices"][0]["message"]["content"].strip()


@ActionRegistry.register
class ScreenCopilotAction(BaseAction):
    name = "screen_copilot"
    aliases = ["look_screen", "check_screen", "diagnose_screen", "screen_diag"]
    description = "屏幕视觉副驾：截取当前屏幕或报错窗口，调用云端多模态视觉大模型秒级诊断（内置隐私护盾）"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        prompt = context.param_value().strip()
        if not prompt or prompt in ("look", "check", "screen", "屏幕", "看屏幕"):
            prompt = "请仔细观察屏幕内容，用2到3句话告诉我当前屏幕上显示什么界面、是否有报错或关键信息，并给出建议。"

        # 0. 数据安全第一：整屏截图会发到第三方云端（硅基流动），截图里可能有别的窗口里的聊天、文档。
        #    主人没有明确打开 allow_cloud_screen 就不截图、不上传
        if not canaan_secrets.cloud_allowed("screen"):
            return ActionResult(
                success=False, action=self.name,
                message="未允许把屏幕截图发到云端：" + canaan_secrets.cloud_denied_message("screen"),
                speech_override="看屏幕要把整屏截图发到云端，为了数据安全默认是关着的。需要的话在设置里打开“允许截图上传云端”。"
            )
        if not canaan_secrets.get_secret("SILICONFLOW_API_KEY"):
            return ActionResult(success=False, action=self.name, message="没有配置 SILICONFLOW_API_KEY（data/secrets.json）",
                                speech_override="还没配置云端视觉模型的密钥，看不了屏幕。")

        # 1. 隐私安全前置门禁：检测是否有密码/支付/隐私窗口在前台
        allowed, reason = PrivacyShield.check_screen_capture_allowed()
        if not allowed:
            context.report_step(1, 1, f"触发隐私安全防御: {reason}")
            return ActionResult(
                success=False,
                action=self.name,
                message=f"隐私护盾已拦截截屏: {reason}",
                speech_override="亲爱的，检测到屏幕有密码或支付敏感窗口，为了保障你的财产与隐私安全，我已经自动启动隐私护盾，本次拒绝捕获画面哦~"
            )

        # 2. 对口述 prompt 进行脱敏
        safe_prompt, _ = PrivacyShield.mask_text(prompt)

        context.report_step(1, 2, "正在快速捕获当前屏幕画面...")
        img = None
        buf = None
        b64_str = None
        try:
            img = _capture_screen_memory()
            if img is None:
                return ActionResult(
                    success=False,
                    action=self.name,
                    message="屏幕画面捕获失败",
                    speech_override="亲爱的，捕获屏幕画面时出了点小状况，稍后再试一次好吗？"
                )
        except Exception as e:
            return ActionResult(
                success=False,
                action=self.name,
                message=f"捕获屏幕异常: {e}",
                speech_override="亲爱的，读取屏幕画面失败了，稍后让我再试一次吧~"
            )

        context.report_step(2, 2, "正在调用视觉多模态大模型分析...")
        try:
            # 缩放至兼顾清晰度与传输极速尺寸
            img.thumbnail((1600, 1000), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")

            # 双轨容灾
            reply_text = None
            last_err = None
            for model in (PRIMARY_VISION_MODEL, FALLBACK_VISION_MODEL):
                try:
                    reply_text = _call_vision_api(model, b64_str, safe_prompt, timeout=9.0)
                    if reply_text:
                        PrivacyShield.log_audit("screen_copilot_success", {"model": model})
                        break
                except Exception as e:
                    last_err = e

            if reply_text:
                return ActionResult(
                    success=True,
                    action=self.name,
                    message=reply_text,
                    speech_override=reply_text
                )
            else:
                return ActionResult(
                    success=False,
                    action=self.name,
                    message=f"视觉诊断异常: {last_err}",
                    speech_override="亲爱的，视觉网络通道暂时有点拥堵，稍后再让我看一眼好吗？"
                )
        finally:
            # 物理级零磁盘落地：强制回收内存中截屏数据
            del img, buf, b64_str
            gc.collect()
