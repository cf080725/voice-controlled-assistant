# -*- coding: utf-8 -*-
"""
密钥与云端上传开关（数据安全第一）。

密钥：只从环境变量 CANAAN_<名字> 或 data/secrets.json 读取，代码里不写任何密钥；data/ 已在 .gitignore 里，不会入库。
    data/secrets.json 示例：{"GLM_API_KEY": "...", "SILICONFLOW_API_KEY": "...", "DEEPSEEK_API_KEY": "..."}

云端上传：屏幕截图、聊天记录发给第三方大模型之前，必须主人明确打开开关（默认全部关闭）：
    data/cockpit_settings.json 里 "allow_cloud_screen": true  —— 允许“看屏幕”把截图发到云端视觉模型
                                  "allow_cloud_chat": true    —— 允许早报把（已脱敏的）聊天记录发到云端大模型
"""
import os
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_FILE = os.path.join(BASE_DIR, "data", "secrets.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "data", "cockpit_settings.json")

CLOUD_KINDS = {
    "screen": ("allow_cloud_screen", "把屏幕截图发到云端视觉模型"),
    "chat": ("allow_cloud_chat", "把聊天记录（已脱敏）发到云端大模型"),
}


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def get_secret(name, path=None):
    """读取密钥；没有返回 ""。优先环境变量 CANAAN_<name>。"""
    v = os.environ.get("CANAAN_" + name, "")
    if v:
        return v
    return str(_load(path or SECRETS_FILE).get(name, "") or "")


def cloud_allowed(kind, path=None):
    """主人是否明确允许这类数据发往云端（默认 False）。"""
    key = CLOUD_KINDS[kind][0]
    return _load(path or SETTINGS_FILE).get(key) is True


def cloud_denied_message(kind):
    key, what = CLOUD_KINDS[kind]
    return (f"出于数据安全，默认不会{what}。确实需要的话，在 data\\cockpit_settings.json 里把 {key} 设为 true。")


def mask(secret):
    """显示用：只留前 4 后 4 位。"""
    s = secret or ""
    return s if len(s) <= 10 else s[:4] + "…" + s[-4:]
