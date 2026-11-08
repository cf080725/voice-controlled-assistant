# -*- coding: utf-8 -*-
"""
团团托盘监管程序：用 pythonw 运行，没有控制台窗口。

    - 单实例（命名互斥量），重复启动直接退出
    - 子进程都放进一个 Job Object（KILL_ON_JOB_CLOSE）：托盘退出或崩溃时，语音服务、hub 一起结束，不留孤儿进程
    - 健康检查（每 5 秒）：hub 进程、GPT-SoVITS 端口 9880、Ollama /api/version；挂了按退避间隔重启
    - 游戏模式：检测到游戏时卸载 Ollama 模型、停掉语音服务；hub 改为只听规则指令、用 CPU 系统语音（见 canaan_gamemode.py）
    - 日志：logs/supervisor.log（轮转 1MB×3），子进程输出 logs/hub.log、logs/tts.log（超过 5MB 启动时轮转）
    - 开机自启：托盘菜单勾选，或 `pythonw canaan_tray.py --install-autostart`（任务计划程序，登录时启动）；
      `--uninstall-autostart` 取消

旧的 “启动团团整机智能管家.bat” 仍可单独使用（hub 会自己检测游戏模式、自己拉起语音服务）。
"""
import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import os
import sys
import json
import time
import shutil
import ctypes
import socket
import logging
import argparse
import threading
import subprocess
import urllib.request
from ctypes import wintypes
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import canaan_config  # noqa: E402
import canaan_gamemode  # noqa: E402
import canaan_notify  # noqa: E402

LOG_DIR = os.path.join(BASE_DIR, "logs")
TASK_NAME = "CanaanDeskTray"
TRAY_MUTEX = "Local\\Canaan_Desk_Tray_SingleInstance"
HUB_MUTEX = "Global\\Canaan_Desk_OS_Hub_SingleInstance_Mutex_2026"
TTS_PORT = 9880
STOP_FLAG = os.path.join(BASE_DIR, "data", "hub_stopped.flag")    # hub 主动退出时写入：不自动重启
PAUSE_FLAG = os.path.join(BASE_DIR, "data", "listen_paused.flag")  # 暂停聆听（麦克风关闭）
SPEAKER_FLAG = os.path.join(BASE_DIR, "data", "speaker_lock.flag")  # 声纹锁定开关
TTS_UNHEALTHY_FLAG = os.path.join(BASE_DIR, "data", "tts_unhealthy.flag")  # 主程序写：语音服务连续合成失败
OLLAMA = "http://127.0.0.1:11434"
BACKOFF = (5, 10, 30, 60, 120)
STABLE_RESET_SEC = 600  # 连续正常运行这么久后退避计数清零

kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None
log = logging.getLogger("canaan_tray")


# ----------------------------------------------------------------------------
# Windows 原语
# ----------------------------------------------------------------------------

def acquire_mutex(name):
    """创建命名互斥量；已存在返回 None。"""
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    h = kernel32.CreateMutexW(None, False, name)
    if kernel32.GetLastError() == 183:
        return None
    return h


def mutex_exists(name):
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    h = kernel32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class Job:
    """Job Object：句柄关闭（本进程退出/崩溃）时结束所有子进程。"""

    def __init__(self):
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self.handle = kernel32.CreateJobObjectW(None, None)
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            log.warning("SetInformationJobObject 失败: %s", kernel32.GetLastError())

    def add(self, proc):
        kernel32.OpenProcess.restype = wintypes.HANDLE
        h = kernel32.OpenProcess(0x1F0FFF, False, proc.pid)
        ok = kernel32.AssignProcessToJobObject(self.handle, h)
        kernel32.CloseHandle(h)
        if not ok:
            log.warning("进程 %s 加入 Job 失败: %s", proc.pid, kernel32.GetLastError())


# ----------------------------------------------------------------------------
# 健康检查
# ----------------------------------------------------------------------------

def port_up(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def ollama_up(timeout=2):
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ollama_port_open(port=11434, timeout=0.5):
    """端口还有人在听：Ollama 只是忙（加载模型、跑评测时 /api/version 可能超过 2 秒），不是挂了。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def rotate_file(path, max_bytes=5 * 1024 * 1024, keep=3):
    try:
        if os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return
    for i in range(keep - 1, 0, -1):
        src, dst = f"{path}.{i}", f"{path}.{i + 1}"
        if os.path.exists(src):
            os.replace(src, dst)
    os.replace(path, path + ".1")


class Backoff:
    def __init__(self, delays=BACKOFF, stable=STABLE_RESET_SEC, clock=time.monotonic):
        self.delays, self.stable, self.clock = delays, stable, clock
        self.failures, self.next_at, self.started_at = 0, 0.0, None

    def started(self):
        self.started_at = self.clock()

    def failed(self):
        if self.started_at is not None and self.clock() - self.started_at >= self.stable:
            self.failures = 0
        delay = self.delays[min(self.failures, len(self.delays) - 1)]
        self.failures += 1
        self.next_at = self.clock() + delay
        return delay

    def ready(self):
        return self.clock() >= self.next_at


# ----------------------------------------------------------------------------
# 子进程
# ----------------------------------------------------------------------------

class Child:
    def __init__(self, name, cmd, cwd, env=None, log_name=None, job=None):
        self.name, self.cmd, self.cwd, self.env = name, cmd, cwd, env
        self.log_path = os.path.join(LOG_DIR, log_name or f"{name}.log")
        self.job = job
        self.proc = None
        self.backoff = Backoff()
        self.restarts = 0

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        rotate_file(self.log_path)
        out = open(self.log_path, "a", encoding="utf-8", errors="replace")
        out.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {self.name} =====\n")
        out.flush()
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdout=out, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=0x08000000)  # CREATE_NO_WINDOW
        out.close()
        if self.job:
            self.job.add(self.proc)
        self.backoff.started()
        log.info("启动 %s pid=%s", self.name, self.proc.pid)

    def stop(self, timeout=8):
        if not self.alive():
            self.proc = None
            return
        log.info("停止 %s pid=%s", self.name, self.proc.pid)
        try:
            import psutil
            parent = psutil.Process(self.proc.pid)
            procs = parent.children(recursive=True) + [parent]
            for p in procs:
                p.terminate()
            psutil.wait_procs(procs, timeout=timeout)
            for p in procs:
                if p.is_running():
                    p.kill()
        except Exception as e:
            log.warning("停止 %s 出错: %s", self.name, e)
        self.proc = None


EXIT_LOG = os.path.join(LOG_DIR, "hub_exits.jsonl")
_NT_CODES = {0xC0000005: "崩溃（访问冲突）", 0xC0000409: "崩溃（栈缓冲区溢出）", 0xC00000FD: "崩溃（栈溢出）",
             0xC0000374: "崩溃（堆损坏）", 0xC000013A: "被 Ctrl+C / 关机结束", 0x40010004: "被系统结束（注销 / 关机）"}


def exit_kind(code, manual):
    """主程序的退出码 → 一句话：区分正常关闭、出错退出和真的崩溃（只有崩溃才是要查的）。
    托盘停它用的是 terminate，返回码 1 且有主动关闭标志也算正常关闭。"""
    if code is None:
        return "还在运行"
    u = code & 0xFFFFFFFF
    if u in _NT_CODES:
        return _NT_CODES[u]
    if manual:
        return "正常关闭" if code in (0, 1) else f"关闭时出错（返回码 {code}）"
    if code == 0:
        return "自己退出了（返回码 0）"
    return f"出错退出（返回码 {code}）"


def record_exit(code, manual, path=None):
    """追加一行到 logs/hub_exits.jsonl（只有时间、返回码、分类），最多留 200 行。"""
    path = path or EXIT_LOG
    row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "code": code, "manual": bool(manual), "kind": exit_kind(code, manual)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()[-199:]
        lines.append(json.dumps(row, ensure_ascii=False))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("记录退出码失败: %s", e)
    return row


def pythonw_exe():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def find_ollama():
    exe = shutil.which("ollama")
    if exe:
        return exe
    cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    return cand if os.path.exists(cand) else None


class Supervisor:
    def __init__(self):
        self.job = Job()
        env = os.environ.copy()
        env["CANAAN_SUPERVISED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # numpy 的 OpenBLAS 默认按 CPU 线程数预留缓冲（本机 18 线程，每个进程约 595 MB 提交内存），限到 4 个
        env.setdefault("OPENBLAS_NUM_THREADS", "4")
        self.hub = Child("hub", [pythonw_exe(), os.path.join(BASE_DIR, "core", "canaan_desk_os_hub.py")], BASE_DIR,
                         env=env, job=self.job)
        g = canaan_config.GPT_SOVITS_DIR
        tts_env = os.environ.copy()
        tts_env["PATH"] = g + ";" + os.path.join(g, "runtime") + ";" + tts_env.get("PATH", "")
        tts_env.setdefault("OPENBLAS_NUM_THREADS", "4")   # 合成走显卡，numpy 的矩阵运算用不上多线程
        from canaan_tts_launch import tts_command
        self.tts = Child("tts", tts_command(g, os.path.join(BASE_DIR, "tuantuan_tts_infer.yaml"), port=TTS_PORT),
                         g, env=tts_env, job=self.job)
        ol = find_ollama()
        # Ollama 是公共服务（别的程序也可能在用）：不放进 Job，关掉团团时不跟着结束
        self.ollama = Child("ollama", [ol, "serve"], BASE_DIR, job=None) if ol else None
        self.status = {"hub": "启动中", "tts": "启动中", "ollama": "检查中"}
        self.hub_external = False
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self.on_status = None
        self.game = canaan_gamemode.GameModeWatcher(stop_tts=self.stop_tts, start_tts=self.start_tts,
                                                    on_change=self._game_changed, log=log.info)

    # --- 语音服务 ---
    def stop_tts(self):
        with self._lock:
            self.tts.stop()
        # 不是我们启动的语音服务（比如 .bat 拉起的）也一并停掉，把显存让出来
        return canaan_gamemode.stop_tts_service()

    def start_tts(self):
        with self._lock:
            if not port_up(TTS_PORT) and not self.tts.alive():
                self.tts.start()

    def _game_changed(self, on, reason):
        self._set("game", f"开（{reason}）" if on else "关")

    def _set(self, key, value):
        if self.status.get(key) != value:
            self.status[key] = value
            if self.on_status:
                try:
                    self.on_status()
                except Exception:
                    pass

    # --- 主循环 ---
    def check_once(self):
        game = canaan_gamemode.active()
        # Ollama：没在运行就拉起（多数情况下 Ollama 桌面版已在后台运行，只做检查）
        if ollama_up():
            self._set("ollama", "正常")
        elif ollama_port_open():
            self._set("ollama", "忙")   # 端口在听只是回得慢：别再拉一个 ollama serve（会因端口被占立刻退出，日志里白白记一笔）
        elif self.ollama and not self.ollama.alive() and self.ollama.backoff.ready():
            self.ollama.backoff.failed()
            self.ollama.start()
            self._set("ollama", "已重启")
        else:
            self._set("ollama", "不可用")

        # 用户主动关闭了团团（关闭按钮 / 任务栏关闭 / 语音“退出团团”）：不重启，语音服务也停掉省资源
        if os.path.exists(STOP_FLAG):
            if self.hub.alive() and self.hub.proc is not None:
                return  # 刚写标志、进程还没退完
            if self.hub.proc is not None:
                record_exit(self.hub.proc.returncode, manual=True)
            self.hub.proc = None
            with self._lock:
                if self.tts.alive():
                    self.tts.stop()
            self._set("hub", "已停止（手动关闭）")
            self._set("tts", "已停止")
            return

        # 语音服务：游戏模式下、或 4GB 档（CPU 语音）保持关闭
        with self._lock:
            if getattr(canaan_config, "TTS_MODE", "gpu") == "cpu":
                self._set("tts", "低显存档：用系统语音")
            elif game:
                self._set("tts", "游戏模式下暂停")
            elif port_up(TTS_PORT) and os.path.exists(TTS_UNHEALTHY_FLAG):
                # 端口在听、但主程序报告连续合成失败（显卡出错后服务不会自己好）：重启语音服务
                try:
                    os.remove(TTS_UNHEALTHY_FLAG)
                except OSError:
                    pass
                log.warning("语音服务连续合成失败，重启语音服务")
                delay = self.tts.backoff.failed()
                if self.tts.alive():
                    self.tts.stop()
                else:
                    canaan_gamemode.stop_tts_service()  # 不是托盘拉起的语音服务也停掉，下一轮重新拉起
                self._set("tts", f"合成连续失败，{delay}s 内重启")
            elif port_up(TTS_PORT):
                self._set("tts", "正常")
            elif self.tts.alive():
                self._set("tts", "启动中")
                if time.monotonic() - (self.tts.backoff.started_at or 0) > 90:
                    log.warning("语音服务 90 秒仍未监听端口，重启")
                    self.tts.stop()
            elif self.tts.backoff.ready():
                if self.tts.proc is not None or self.tts.restarts:
                    self.tts.backoff.failed()
                self.tts.restarts += 1
                self.tts.start()
                self._set("tts", "启动中")

        # hub：等语音服务端口起来（或游戏模式）再启动，避免 hub 以为服务没开
        if mutex_exists(HUB_MUTEX) and not self.hub.alive():
            self.hub_external = True
            self._set("hub", "运行中（非托盘启动）")
        elif self.hub.alive():
            self.hub_external = False
            self._set("hub", "运行中")
        else:
            if self.hub.proc is not None and os.path.exists(STOP_FLAG):
                return  # 主动退出，由上面的分支处理
            if self.hub.proc is not None:
                code = self.hub.proc.returncode
                kind = record_exit(code, manual=False)["kind"]
                delay = self.hub.backoff.failed()
                log.warning("hub 退出（返回码 %s，%s），%s 秒后重启", code, kind, delay)
                self.hub.proc = None
                self._set("hub", f"{kind}，{delay}s 后重启")
            elif self.hub.backoff.ready() and (game or port_up(TTS_PORT) or self.tts.restarts > 2
                                               or getattr(canaan_config, "TTS_MODE", "gpu") == "cpu"):
                self.hub.restarts += 1
                self.hub.start()
                self._set("hub", "运行中")

    def run(self):
        self.game.step()
        self._set("game", "开" if canaan_gamemode.active() else "关")
        self.game.start()
        while not self._halt.is_set():
            try:
                self.check_once()
            except Exception as e:
                log.exception("健康检查异常: %s", e)
            self._halt.wait(5)

    def start_hub(self):
        """托盘菜单“启动团团”：清除主动关闭标志，下一次健康检查（≤5 秒）拉起语音服务与 hub。"""
        try:
            os.remove(STOP_FLAG)
        except FileNotFoundError:
            pass
        self.hub.backoff = Backoff()
        self.tts.backoff = Backoff()
        self._set("hub", "启动中")

    @staticmethod
    def toggle_listen():
        if os.path.exists(PAUSE_FLAG):
            os.remove(PAUSE_FLAG)
        else:
            os.makedirs(os.path.dirname(PAUSE_FLAG), exist_ok=True)
            open(PAUSE_FLAG, "w").close()

    def restart_hub(self):
        self.hub.stop()
        self.hub.backoff = Backoff()

    def shutdown(self):
        self._halt.set()
        self.game.stop()
        self.hub.stop()
        self.tts.stop()
        if self.ollama:
            self.ollama.stop()


# ----------------------------------------------------------------------------
# 开机自启（任务计划程序）
# ----------------------------------------------------------------------------

def autostart_installed():
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, creationflags=0x08000000)
    return r.returncode == 0


def install_autostart():
    tr = f'"{pythonw_exe()}" "{os.path.join(BASE_DIR, "canaan_tray.py")}"'
    r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", tr,
                        "/RL", "LIMITED", "/DELAY", "0000:30"], capture_output=True, creationflags=0x08000000)
    return r.returncode == 0, (r.stdout + r.stderr).decode("gbk", errors="replace").strip()


def uninstall_autostart():
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, creationflags=0x08000000)
    return r.returncode == 0, (r.stdout + r.stderr).decode("gbk", errors="replace").strip()


# ----------------------------------------------------------------------------
# 托盘
# ----------------------------------------------------------------------------

def make_icon(game=False):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(110, 110, 120, 255) if game else (168, 85, 247, 255))
    try:
        font = ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc"), 36)
        d.text((32, 31), (canaan_config.ASSISTANT_NAME or "迦")[0], font=font, fill="white", anchor="mm")
    except Exception:
        pass
    return img


def run_tray(sup):
    import pystray
    from pystray import Menu, MenuItem as Item

    def status_text(key, label):
        return lambda item: f"{label}：{sup.status.get(key, '-')}"

    def set_game(value):
        def _f(icon, item):
            canaan_gamemode.set_forced(value)
            sup.game.step()
        return _f

    def is_game(value):
        return lambda item: canaan_gamemode.forced() == value

    def toggle_autostart(icon, item):
        ok, msg = uninstall_autostart() if autostart_installed() else install_autostart()
        log.info("开机自启切换: %s %s", ok, msg)

    def toggle_speaker_lock(icon, item):
        if os.path.exists(SPEAKER_FLAG):
            os.remove(SPEAKER_FLAG)
        else:
            if not os.path.exists(os.path.join(BASE_DIR, "data", "speaker_profile.npy")):
                log.info("尚未录入声纹，先打开录入工具")
                enroll_voice(icon, item)
                return
            open(SPEAKER_FLAG, "w").close()

    def enroll_voice(icon, item):
        py = os.path.join(os.path.dirname(pythonw_exe()), "python.exe")
        subprocess.Popen(["cmd", "/c", "start", "录入声纹", py, os.path.join(BASE_DIR, "tools", "enroll_voice.py")],
                         cwd=BASE_DIR)

    def open_cockpit(icon, item):
        subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "shells", "canaan_settings_cockpit.py")], cwd=BASE_DIR)  # 当前解释器（虚拟环境里装的依赖才找得到）

    def open_logs(icon, item):
        os.startfile(LOG_DIR)

    def toggle_notify(icon, item):
        canaan_notify.set_enabled(not canaan_notify.enabled())

    def quit_(icon, item):
        icon.visible = False
        sup.shutdown()
        icon.stop()

    name = canaan_config.ASSISTANT_NAME
    menu = Menu(
        Item(status_text("hub", name), None, enabled=False),
        Item(status_text("tts", "语音服务"), None, enabled=False),
        Item(status_text("ollama", "大模型"), None, enabled=False),
        Item(status_text("game", "游戏模式"), None, enabled=False),
        Menu.SEPARATOR,
        Item("游戏模式", Menu(
            Item("自动检测", set_game(None), checked=is_game(None), radio=True),
            Item("强制开启", set_game("on"), checked=is_game("on"), radio=True),
            Item("强制关闭", set_game("off"), checked=is_game("off"), radio=True))),
        Item(f"启动{name}", lambda icon, item: sup.start_hub(), visible=lambda item: os.path.exists(STOP_FLAG)),
        Item(f"重启{name}", lambda icon, item: sup.restart_hub(), visible=lambda item: not os.path.exists(STOP_FLAG)),
        Item(lambda item: "继续聆听" if os.path.exists(PAUSE_FLAG) else "暂停聆听（关麦克风）",
             lambda icon, item: sup.toggle_listen()),
        Item("声纹锁定（只听我的声音）", toggle_speaker_lock, checked=lambda item: os.path.exists(SPEAKER_FLAG)),
        Item("录入我的声音", enroll_voice),
        Item("🎛️ 控制面板与设置大屏", open_cockpit),
        Item("打开日志文件夹", open_logs),
        Item("开机自启", toggle_autostart, checked=lambda item: autostart_installed()),
        Item("系统通知（等确认 / 失败 / 提醒 / 智能体结果）", toggle_notify, checked=lambda item: canaan_notify.enabled()),
        Menu.SEPARATOR,
        Item("退出", quit_),
    )
    icon = pystray.Icon("canaan", make_icon(canaan_gamemode.active()), f"{name} · Desk", menu)

    status_file = os.path.join(BASE_DIR, "data", "canaan_status.json")
    live = {"label": "", "agent": ""}

    def title():
        # 提示框里加上执行器的当前状态（订阅自统一状态事件，hub 写成小文件；只有状态、动作名、步骤号）
        extra = f" · {live['label']}" if live["label"] and live["label"] != "空闲" else ""
        extra += f" · {live['agent']}" if live["agent"] else ""
        return f"{name} · {sup.status.get('hub')} · 游戏模式{sup.status.get('game')}{extra}"[:120]

    def refresh():
        icon.icon = make_icon(canaan_gamemode.active())
        icon.title = title()
        icon.update_menu()

    def watch_status():
        import canaan_events
        notices = canaan_notify.NotifyReader()
        while True:
            try:
                view = canaan_events.read_status_file(status_file) or {"label": ""}
                if view.get("label") != live["label"] or view.get("agent", "") != live["agent"]:
                    live["label"], live["agent"] = view.get("label", ""), view.get("agent", "")
                    icon.title = title()
            except Exception:
                pass
            try:  # hub 写进队列的通知用 Windows 系统通知弹出；游戏模式或主人关掉时丢弃
                for entry in notices.read_new():
                    if canaan_notify.should_show(entry, game_mode=canaan_gamemode.active()):
                        icon.notify(entry.get("body") or entry["title"], f"{name} · {entry['title']}")
            except Exception:
                pass
            time.sleep(3)

    sup.on_status = refresh
    threading.Thread(target=sup.run, daemon=True).start()
    threading.Thread(target=watch_status, name="tray-status", daemon=True).start()
    icon.run()


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    h = RotatingFileHandler(os.path.join(LOG_DIR, "supervisor.log"), maxBytes=1024 * 1024, backupCount=3,
                            encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-autostart", action="store_true")
    ap.add_argument("--uninstall-autostart", action="store_true")
    args = ap.parse_args()
    setup_logging()
    if args.install_autostart:
        print(install_autostart())
        return
    if args.uninstall_autostart:
        print(uninstall_autostart())
        return
    if acquire_mutex(TRAY_MUTEX) is None:
        log.info("托盘已在运行，退出")
        return
    log.info("托盘监管程序启动 pid=%s", os.getpid())
    # 新启动托盘（开机自启或手动双击）= 用户想用团团：清掉上次的“手动关闭”“暂停聆听”标志
    for flag in (STOP_FLAG, PAUSE_FLAG):
        try:
            os.remove(flag)
        except FileNotFoundError:
            pass
    sup = Supervisor()
    try:
        run_tray(sup)
    finally:
        sup.shutdown()
        log.info("托盘监管程序退出")


if __name__ == "__main__":
    main()
