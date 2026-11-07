# -*- coding: utf-8 -*-
"""
按显存分档（canaan_config.VRAM_TIER = "auto" 时启动时检测，也可手动指定 "8gb" / "6gb" / "4gb"）：

    8gb  陪聊和意图共用 canaan-chat:3b + GPT-SoVITS（10-25 起；原来是 canaan-chat:3b + 1.5B 意图，合计约 6.4GB）
    6gb  陪聊改用 1.5B（canaan-desk:1.5b）+ 意图 canaan-intent:1.5b + GPT-SoVITS
    4gb  不加载任何大模型：只走规则指令，不陪聊；语音用 Windows 自带中文语音（CPU）
小于 4GB 或检测不到独显也按 4gb 处理。
"""
import subprocess

TIERS = {
    # 10-25：陪聊和意图共用一个 3B（canaan-chat:3b，比 canaan-chat:3b + 1.5B 意图省约 1 GB 显存）
    "8gb": {"CHAT_MODEL": "canaan-chat:3b", "COMPANION_ENABLED": True, "LLM_ENABLED": True, "TTS_MODE": "gpu"},
    "6gb": {"CHAT_MODEL": "canaan-desk:1.5b", "COMPANION_ENABLED": True, "LLM_ENABLED": True, "TTS_MODE": "gpu"},
    "4gb": {"CHAT_MODEL": "", "COMPANION_ENABLED": False, "LLM_ENABLED": False, "TTS_MODE": "cpu"},
}


def detect_vram_mb():
    """独显总显存（MB），检测不到返回 0。"""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=0x08000000).stdout
        return max(int(x.strip()) for x in out.splitlines() if x.strip())
    except Exception:
        return 0


def pick_tier(vram_mb):
    if vram_mb >= 7500:
        return "8gb"
    if vram_mb >= 5500:
        return "6gb"
    return "4gb"


def apply(cfg_globals, tier="auto"):
    """把档位设置写进 canaan_config 的全局变量；返回 (档位, 显存MB)。"""
    vram = detect_vram_mb() if tier == "auto" else -1
    t = pick_tier(vram) if tier == "auto" else tier
    for k, v in TIERS[t].items():
        cfg_globals[k] = v
    return t, vram
