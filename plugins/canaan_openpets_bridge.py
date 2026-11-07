# -*- coding: utf-8 -*-
"""可选插件：把团团的执行状态同步给共创平台的桌宠（desktop-pet-community / OpenPets），默认关闭。

只发状态：`openpets react <thinking|working|waiting|success|error>`；打开 say 时再发一句只含动作名和步骤号的短状态
（例如“第 2/3 步 · 打开应用”），不发原话、路径、提示词或执行结果正文。
命令不经过 shell，3 秒超时；失败只记一次日志，不影响团团。

开启：在 data/openpets_bridge.json 写 {"enabled": true}；可选 "command"（openpets 可执行文件，默认 "openpets"）、
"say": true（同时发短状态，默认 false）。
"""
import json
import os
import shutil
import subprocess
import threading
import time

REACTIONS = {"thinking": "thinking", "step": "working", "awaiting_confirm": "waiting", "success": "success", "failure": "error"}
REACT_COOLDOWN = 10.0   # 与对方文档里 Claude Code 接入的冷却时间一致
SAY_COOLDOWN = 20.0
DEFAULTS = {"enabled": False, "command": "openpets", "say": False}


def load_settings(data_dir):
    path = os.path.join(data_dir, "openpets_bridge.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    settings = dict(DEFAULTS)
    if isinstance(raw, dict):
        settings["enabled"] = raw.get("enabled") is True
        settings["say"] = raw.get("say") is True
        if isinstance(raw.get("command"), str) and raw["command"].strip():
            settings["command"] = raw["command"].strip()
    return settings


class OpenPetsBridge:
    def __init__(self, command="openpets", say=False, runner=None, clock=time.monotonic):
        self.command, self.say = command, say
        self.runner = runner or self._run
        self.clock = clock
        self._last = {}
        self._warned = False

    def _run(self, args):
        exe = shutil.which(self.command) or self.command
        subprocess.run([exe, *args], timeout=3, capture_output=True,
                       creationflags=0x08000000 if os.name == "nt" else 0, check=False)

    def _fire(self, args):
        def go():
            try:
                self.runner(args)
            except Exception as ex:
                if not self._warned:
                    self._warned = True
                    print(f"[!] 共创平台桥接调用失败（之后不再提示）：{type(ex).__name__}")
        threading.Thread(target=go, name="openpets-bridge", daemon=True).start()

    def _cool(self, key, seconds):
        now = self.clock()
        if now - self._last.get(key, -1e9) < seconds:
            return False
        self._last[key] = now
        return True

    def __call__(self, event):
        reaction = REACTIONS.get(event.get("kind"))
        if not reaction:
            return
        if self._cool(("react", reaction), REACT_COOLDOWN):
            self._fire(["react", reaction])
        if self.say and event.get("kind") in ("step", "awaiting_confirm", "success", "failure") and self._cool("say", SAY_COOLDOWN):
            import canaan_events
            text = canaan_events.label(event).replace("\n", " ")[:140]
            if text:
                self._fire(["say", text])


_started = None


def start_if_enabled(data_dir):
    """团团中枢启动时调用；配置里没开就什么都不做。"""
    global _started
    settings = load_settings(data_dir)
    if not settings["enabled"] or _started is not None:
        return None
    import canaan_events
    _started = OpenPetsBridge(settings["command"], settings["say"])
    canaan_events.subscribe(_started)
    print("[✓] 已开启共创平台状态同步（只发状态）")
    return _started
