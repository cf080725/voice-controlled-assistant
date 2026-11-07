# -*- coding: utf-8 -*-
"""
游戏模式：检测到游戏在运行时把显卡让出来。

进入时：
    - 写 data/game_mode.flag（hub 每轮读取：只走规则指令、不调大模型、不陪聊，语音改用 CPU 的系统 TTS）
    - 让 Ollama 立即卸载所有已加载模型（keep_alive=0），释放显存
    - 停掉 GPT-SoVITS 语音服务（9880）
退出时：删除标志文件，重新拉起语音服务；大模型在下一次用到时自动加载。

检测方式（canaan_config 可改）：
    GAME_PROCESSES            进程名列表（默认含游戏的 ExampleGame.exe）
    GAME_MODE_ON_FULLSCREEN   任意 D3D 全屏程序（SHQueryUserNotificationState == QUNS_RUNNING_D3D_FULL_SCREEN）也算
手动覆盖：data/game_mode.force 内容为 on / off（托盘菜单写入），删除即恢复自动。
"""
import os
import json
import time
import threading
import urllib.request

try:
    import canaan_config as _cfg
except Exception:
    _cfg = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
FLAG_PATH = os.path.join(DATA_DIR, "game_mode.flag")
FORCE_PATH = os.path.join(DATA_DIR, "game_mode.force")
OLLAMA = "http://127.0.0.1:11434"

GAME_PROCESSES = tuple(p.lower() for p in (getattr(_cfg, "GAME_PROCESSES", None) or (
    "ExampleGame.exe",
)))
ON_FULLSCREEN = bool(getattr(_cfg, "GAME_MODE_ON_FULLSCREEN", True))
POLL_SEC = float(getattr(_cfg, "GAME_MODE_POLL_SEC", 5.0))
EXIT_GRACE_SEC = float(getattr(_cfg, "GAME_MODE_EXIT_GRACE_SEC", 20.0))  # 退出游戏后多等一会儿，避免切窗口抖动

_cache = {"t": 0.0, "v": False}


def active(max_age=1.0):
    """当前是否处于游戏模式（读标志文件，1 秒内缓存）。"""
    now = time.monotonic()
    if now - _cache["t"] > max_age:
        _cache["v"] = os.path.exists(FLAG_PATH)
        _cache["t"] = now
    return _cache["v"]


def set_flag(on, reason=""):
    os.makedirs(DATA_DIR, exist_ok=True)
    if on:
        with open(FLAG_PATH, "w", encoding="utf-8") as f:
            json.dump({"since": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}, f, ensure_ascii=False)
    else:
        try:
            os.remove(FLAG_PATH)
        except FileNotFoundError:
            pass
    _cache["t"] = 0.0


def forced():
    """手动覆盖：'on' / 'off' / None（自动）。"""
    try:
        with open(FORCE_PATH, encoding="utf-8") as f:
            v = f.read().strip().lower()
        return v if v in ("on", "off") else None
    except (OSError, ValueError):   # 文件被别的程序写成了非 UTF-8：当没设，回到自动检测
        return None


def set_forced(value):
    os.makedirs(DATA_DIR, exist_ok=True)
    if value in ("on", "off"):
        with open(FORCE_PATH, "w", encoding="utf-8") as f:
            f.write(value)
    else:
        try:
            os.remove(FORCE_PATH)
        except FileNotFoundError:
            pass


def fullscreen_d3d():
    """有 D3D 全屏程序在前台（游戏）时为 True。"""
    try:
        import ctypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value == 3  # QUNS_RUNNING_D3D_FULL_SCREEN
    except Exception:
        pass
    return False


def running_game():
    """返回检测到的游戏名（进程名或“全屏程序”），没有返回 None。"""
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if n in GAME_PROCESSES:
                return p.info["name"]
    except Exception:
        pass
    if ON_FULLSCREEN and fullscreen_d3d():
        return "全屏程序"
    return None


def unload_ollama_models(timeout=5):
    """让 Ollama 立即卸载所有已加载的模型。返回卸载的模型名列表。"""
    out = []
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=timeout) as r:
            models = [m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", [])]
    except Exception:
        return out
    for m in models:
        try:
            req = urllib.request.Request(OLLAMA + "/api/generate",
                                         data=json.dumps({"model": m, "keep_alive": 0}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=timeout).read()
            out.append(m)
        except Exception:
            pass
    return out


def tts_processes():
    """GPT-SoVITS 语音服务进程（命令行含 api_v2.py）。"""
    found = []
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if "api_v2.py" in cmd and "9880" in cmd:
                found.append(p)
    except Exception:
        pass
    return found


def stop_tts_service():
    procs = tts_processes()
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    try:
        import psutil
        psutil.wait_procs(procs, timeout=5)
    except Exception:
        pass
    return len(procs)


class GameModeWatcher(threading.Thread):
    """定时检测游戏；状态变化时调用回调。stop_tts / start_tts 可替换（托盘监管时由它管理子进程）。"""

    def __init__(self, stop_tts=stop_tts_service, start_tts=None, on_change=None, log=print):
        super().__init__(daemon=True, name="GameModeWatcher")
        self.stop_tts = stop_tts
        self.start_tts = start_tts
        self.on_change = on_change
        self.log = log
        self._halt = threading.Event()
        self._last_seen = 0.0
        self.game = None

    def decide(self, game, now):
        """返回本次检测后应处于的状态（True=游戏模式）。纯函数部分，便于测试。"""
        f = forced()
        if f == "on":
            return True
        if f == "off":
            return False
        if game:
            self._last_seen = now
            return True
        return active() and (now - self._last_seen) < EXIT_GRACE_SEC

    def step(self, now=None):
        now = time.monotonic() if now is None else now
        game = running_game() if forced() is None else None
        want = self.decide(game, now)
        if want and not active():
            self.enter(game or "手动")
        elif not want and active():
            self.exit()
        return want

    def enter(self, reason):
        set_flag(True, reason)
        unloaded = unload_ollama_models()
        n = self.stop_tts() if self.stop_tts else 0
        self.log(f"[🎮 游戏模式] 进入（{reason}）：卸载模型 {unloaded}，停止语音服务 {n} 个进程")
        if self.on_change:
            self.on_change(True, reason)

    def exit(self):
        set_flag(False)
        self.log("[🎮 游戏模式] 退出：恢复语音服务")
        if self.start_tts:
            try:
                self.start_tts()
            except Exception as e:
                self.log(f"[!] 恢复语音服务失败: {e}")
        if self.on_change:
            self.on_change(False, "")

    def run(self):
        while not self._halt.is_set():
            try:
                self.step()
            except Exception as e:
                self.log(f"[!] 游戏模式检测异常: {e}")
            self._halt.wait(POLL_SEC)

    def stop(self):
        self._halt.set()
