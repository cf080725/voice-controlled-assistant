# -*- coding: utf-8 -*-
"""
桌面伴侣的桌面感知（不依赖 Tk，可单独测试）。

后台线程每秒看一次前台窗口，判断主人在做什么，界面线程每帧读取最新结果：
  privacy  敏感窗口（密码管理器、支付/网银页面、无痕窗口、远程桌面）——沿用 canaan_privacy_shield 的名单
  code     写代码（VS Code / Cursor / PyCharm / 终端……）；typing=True 表示刚在打字
  music    听歌（音乐软件在前台，或在后台播放：窗口标题是“歌名 - 歌手”）
  None     其他

隐私：只读窗口标题和进程名，用完即丢，不存盘、不写日志、不截图、不上传。
判断“在打字”只看系统的“最后一次输入时间”加鼠标有没有动，不记录按了哪个键。
"""
import os
import time
import ctypes
import threading
from ctypes import wintypes

try:
    from canaan_privacy_shield import SENSITIVE_PROCESS_NAMES, SENSITIVE_WINDOW_KEYWORDS, PrivacyShield
except Exception:  # 没有隐私盾模块时用内置的小名单
    SENSITIVE_PROCESS_NAMES = {"1password.exe", "bitwarden.exe", "keepass.exe", "lastpass.exe", "mstsc.exe"}
    SENSITIVE_WINDOW_KEYWORDS = ["支付", "收银台", "网银", "输入密码", "incognito", "inprivate"]
    PrivacyShield = None

CODE_PROCESSES = {
    "code.exe", "cursor.exe", "trae.exe", "trae cn.exe", "windsurf.exe", "devenv.exe", "pycharm64.exe", "pycharm.exe",
    "idea64.exe", "clion64.exe", "webstorm64.exe", "goland64.exe", "rider64.exe", "sublime_text.exe", "notepad++.exe",
    "windowsterminal.exe", "wt.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "openconsole.exe",
    "mintty.exe", "wezterm-gui.exe", "alacritty.exe", "hbuilderx.exe", "android studio.exe", "studio64.exe",
    # 基于 VS Code 的编辑器
    "antigravity ide.exe", "antigravity.exe", "kiro.exe", "zed.exe", "void.exe", "codium.exe", "vscodium.exe",
    "positron.exe", "qoder.exe", "codebuddy.exe", "lingma.exe",
}
MUSIC_PROCESSES = {"cloudmusic.exe", "spotify.exe", "imappmusic.exe", "kugou.exe", "kuwomusic.exe", "foobar2000.exe",
                   "applemusic.exe", "itunes.exe", "aimp.exe", "potplayermini64.exe"}
# 窗口标题里没有“歌名 - 歌手”时（软件首页、暂停时）这些是默认标题
MUSIC_IDLE_TITLES = {"", "spotify", "spotify free", "spotify premium", "网易云音乐", "音乐软件", "酷狗音乐", "酷我音乐",
                     "foobar2000", "itunes", "aimp"}
RECORDER_PROCESSES = {"obs64.exe", "obs32.exe", "bdcam.exe", "bandicam.exe", "camtasiarecorder.exe", "camrec.exe",
                      "sharex.exe", "screenrec.exe", "ev录屏.exe", "evcapture.exe", "kk.exe", "apowerrec.exe"}
TYPING_WINDOW_SEC = 1.2   # 这么久内有键盘输入（且鼠标没动）算“在打字”


def classify(title, proc):
    """(窗口标题, 进程名) → "privacy" / "code" / "music" / None。优先级：敏感 > 代码 > 音乐。"""
    t = (title or "").lower()
    p = (proc or "").lower()
    if p in SENSITIVE_PROCESS_NAMES or any(kw in t for kw in SENSITIVE_WINDOW_KEYWORDS):
        return "privacy"
    if p in CODE_PROCESSES or "visual studio code" in t or t.endswith(" - cursor"):
        return "code"
    if p in MUSIC_PROCESSES:
        return "music"
    return None


def music_title_playing(title):
    """音乐软件窗口标题像“歌名 - 歌手”，多半正在放歌。"""
    t = (title or "").strip().lower()
    return t not in MUSIC_IDLE_TITLES and " - " in t


# ------------------------------- Windows 读取 -------------------------------

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _foreground():
    if PrivacyShield is not None:
        _, title, proc = PrivacyShield.get_foreground_window_info()
        return title, proc
    return "", ""


def _idle_sec():
    try:
        li = _LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0)
    except Exception:
        return 999.0


def _cursor():
    try:
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y
    except Exception:
        return 0, 0


def _process_names():
    """当前所有进程名（小写），用来判断有没有录屏软件、后台音乐软件。"""
    names = set()
    try:
        TH32CS_SNAPPROCESS = 0x2

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        k = ctypes.windll.kernel32
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            names.add(e.szExeFile.lower())
            ok = k.Process32NextW(snap, ctypes.byref(e))
        k.CloseHandle(snap)
    except Exception:
        pass
    return names


def _music_playing_in_background(names):
    """后台音乐软件的窗口标题是“歌名 - 歌手”则视为在放歌（只看标题，不读音频）。"""
    if not (names & MUSIC_PROCESSES):
        return False
    found = []
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _l):
            if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if music_title_playing(buf.value) and _proc_of(pid.value) in MUSIC_PROCESSES:
                found.append(1)
                return False
            return True
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        return False
    return bool(found)


def _proc_of(pid):
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(h)
    except Exception:
        pass
    return ""


# ------------------------------- 后台线程 -------------------------------

class SenseState:
    __slots__ = ("mode", "typing", "recorder", "window_key", "since")

    def __init__(self, mode=None, typing=False, recorder=False, window_key=None, since=0.0):
        self.mode, self.typing, self.recorder, self.window_key, self.since = mode, typing, recorder, window_key, since


class SenseWatcher(threading.Thread):
    """每 interval 秒读一次；latest 是最新的 SenseState（界面线程直接读，赋值是原子的）。
    测试时可注入 foreground/idle/cursor/procs 函数。"""

    def __init__(self, interval=1.0, foreground=None, idle_sec=None, cursor=None, procs=None, bg_music=None):
        super().__init__(daemon=True, name="PetSenseWatcher")
        self.interval = interval
        self._fg = foreground or _foreground
        self._idle = idle_sec or _idle_sec
        self._cursor = cursor or _cursor
        self._procs = procs or _process_names
        self._bg_music = bg_music or _music_playing_in_background
        self._halt = threading.Event()
        self.latest = SenseState()
        self._last_cursor = None
        self._procs_cache, self._procs_at = set(), float("-inf")   # 第一次一定读
        self.enabled = True

    def poll_once(self, now=None):
        now = time.time() if now is None else now
        title, proc = self._fg()
        mode = classify(title, proc)
        if now - self._procs_at > 5.0:            # 进程列表 5 秒刷新一次
            self._procs_cache, self._procs_at = self._procs(), now
        recorder = bool(self._procs_cache & RECORDER_PROCESSES)
        if mode is None and self._bg_music(self._procs_cache):
            mode = "music"
        cur = self._cursor()
        moved = self._last_cursor is not None and cur != self._last_cursor
        self._last_cursor = cur
        typing = mode == "code" and self._idle() < TYPING_WINDOW_SEC and not moved
        key = (mode, hash((title, proc))) if mode == "privacy" else (mode, None)
        since = self.latest.since if key == self.latest.window_key else now
        self.latest = SenseState(mode, typing, recorder, key, since)   # 标题只留哈希，不留原文
        return self.latest

    def run(self):
        while not self._halt.is_set():
            if self.enabled:
                try:
                    self.poll_once()
                except Exception:
                    pass
            else:
                self.latest = SenseState()
            self._halt.wait(self.interval)

    def stop(self):
        self._halt.set()
