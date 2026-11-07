# -*- coding: utf-8 -*-
"""苹果式灵动岛（独立进程）：原生 Windows 分层窗口，逐像素透明，毛玻璃，自己画（shells/canaan_island_render.py）。

    python canaan.py island

为什么不再用 WebView2：pywebview 的“透明窗口”在这台机器上是一整块不透明的底（实测截图：460×330 的灰 / 深色方块），
岛会变成屏幕顶上的一个大框。改成 UpdateLayeredWindow 逐像素透明后，边缘抗锯齿、外面完全透明。

- 毛玻璃：每 0.5 秒截一次岛下方这一小块屏幕（截的时候先让自己不出现在截图里），缩小 + 模糊 + 着色，只在内存里，不保存。
  系统关了“透明效果”、或者看板里关了“背景模糊”，就用不透明的仿玻璃。
- 深浅色跟随系统（每 5 秒看一次注册表）；Windows 关了“显示动画”时不做弹簧和抖动。
- 光标在岛上才接收点击，其余地方点得穿（切换 WS_EX_TRANSPARENT）；不抢焦点、不进任务栏。
- 状态从 data/island_state.json 读（hub 写）；点击写到 data/island_actions.jsonl（hub 决定做什么）。
- 主程序没了岛自己退出；起不来时退出码 3，hub 继续用原来的 tkinter 岛。
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import canaan  # noqa: E402,F401
from canaan_island_state import STATE_PATH, post_action, read_state  # noqa: E402

import canaan_island_render as R  # noqa: E402

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x20, 0x80000, 0x80, 0x08000000, 0x8
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0, 0x11
HOVER_DELAY, LEAVE_DELAY = 0.4, 0.35
BACKDROP_EVERY = 0.5                # 展开 / 动画时多久刷新一次背后的模糊
BACKDROP_IDLE = 2.5                 # 静止的小胶囊
ICON_DIR = os.path.join(os.path.dirname(STATE_PATH), "icon_cache")


class IslandApi:
    """读状态（按修改时间缓存，坏文件不覆盖上一次的好状态）和报告事件。"""

    def __init__(self, state_path=STATE_PATH):
        self.state_path = state_path
        self.rect = None
        self._cache, self._mtime = {"mode": "idle", "seq": 0, "notices": []}, None

    def get_state(self):
        try:
            mtime = os.path.getmtime(self.state_path)
        except OSError:
            return self._cache
        if mtime != self._mtime:
            data = read_state(self.state_path)
            if data:
                self._cache, self._mtime = data, mtime
        return self._cache

    def event(self, kind, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        if kind == "rect":
            try:
                self.rect = tuple(float(payload[k]) for k in ("x", "y", "w", "h", "dpr"))
            except (KeyError, TypeError, ValueError):
                pass
            return True
        return post_action(kind, payload)


def inside(rect, cursor, window_origin, pad=4):
    """光标（屏幕物理像素）是否落在岛的范围里。rect = (x, y, w, h, dpr)，x/y/w/h 为窗口内的逻辑像素。"""
    if not rect:
        return False
    x, y, w, h, dpr = rect
    ox, oy = window_origin
    cx, cy = cursor
    left, top = ox + (x - pad) * dpr, oy + (y - pad) * dpr
    return left <= cx <= left + (w + 2 * pad) * dpr and top <= cy <= top + (h + 2 * pad) * dpr


def _pid_alive(pid):
    """进程还在不在（Windows：OpenProcess + GetExitCodeProcess，STILL_ACTIVE = 259）。"""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------- 系统设置

def system_theme():
    """('light' | 'dark', 透明效果开着吗)。读不到就按浅色、开着。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            light = winreg.QueryValueEx(k, "AppsUseLightTheme")[0]
            try:
                transparency = winreg.QueryValueEx(k, "EnableTransparency")[0]
            except OSError:
                transparency = 1
        return ("light" if light else "dark"), bool(transparency)
    except OSError:
        return "light", True


def animations_enabled():
    """Windows 设置里的“显示动画”（SPI_GETCLIENTAREAANIMATION）。"""
    try:
        v = wintypes.BOOL(1)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(v), 0)
        return bool(v.value)
    except Exception:
        return True


def icon_paths(names):
    """来源名 → 已缓存的应用图标（hub 那边按来源存成 PNG，文件名只有来源名）。"""
    out = {}
    for n in names:
        p = os.path.join(ICON_DIR, _safe(n) + ".png")
        if os.path.exists(p):
            out[n] = p
    return out


def _safe(name):
    return "".join(ch if ch.isalnum() else "_" for ch in str(name))[:40] or "_"


_CHIME = {}


def chime(kind):
    """轻提示音（默认关）：用正弦波现做一段 0.25 秒的 wav，异步播放。"""
    try:
        import io
        import wave
        import winsound
        import numpy as np
        if kind not in _CHIME:
            sr = 22050
            t = np.arange(int(sr * 0.25)) / sr
            f = {"waiting": 880.0, "failed": 440.0, "p0": 988.0}.get(kind, 660.0)
            env = np.exp(-t * 14) * np.minimum(1, t * 200)
            y = (np.sin(2 * np.pi * f * t) * 0.6 + np.sin(2 * np.pi * f * 1.5 * t) * 0.25) * env * 0.35
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes((y * 32767).astype("<i2").tobytes())
            _CHIME[kind] = buf.getvalue()
        winsound.PlaySound(_CHIME[kind], winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


# ---------------------------------------------------------------- 交互（与窗口无关的部分，方便测试）

class Controller:
    """状态 → 画面描述；悬停预览、列表展开、点击和滑动 → 动作。时间由调用方传入。"""

    SWIPE = 60

    def __init__(self, api, scene, post=post_action, sound=chime):
        self.api, self.scene, self.post, self.sound = api, scene, post, sound
        self.state = {"mode": "idle", "notices": []}
        self.list_open = False
        self.hover = False
        self._hover_since = None
        self._leave_since = None
        self._last_arrive = None
        self._drag = None
        self.hot = []

    def refresh(self, now):
        """读一次状态；变了就更新画面描述。返回是否有变化。"""
        st = self.api.get_state()
        changed = st is not self.state
        self.state = st
        if not (st.get("notices") or []):
            self.list_open = False
        present = st.get("present") or {}
        arrive = present.get("arrive")
        is_new = bool(arrive) and arrive != self._last_arrive
        if is_new:
            self._last_arrive = arrive
            if (st.get("settings") or {}).get("sound") and present.get("variant") in ("waiting", "failed", "p0"):
                self.sound(present.get("variant"))
        self.scene.level = float(st.get("level") or 0.35)
        if changed or is_new:
            self.scene.update(R.describe(st, self.list_open, self.hover), now, arrive=is_new)
        return changed or is_new

    LIST_CLOSE_AFTER = 2.5

    def set_hover(self, over, now):
        """光标在岛上停 0.4 秒展开预览，离开 0.35 秒后收回；展开的列表在光标离开 2.5 秒后自己收起。"""
        if over:
            self._out_since = None
        elif getattr(self, "_out_since", None) is None:
            self._out_since = now
        if self.list_open and not over and now - self._out_since >= self.LIST_CLOSE_AFTER:
            self.list_open = False
            self.scene.update(R.describe(self.state, self.list_open, self.hover), now)
            return True
        if over:
            self._leave_since = None
            if self._hover_since is None:
                self._hover_since = now
            want = now - self._hover_since >= HOVER_DELAY
        else:
            self._hover_since = None
            if self.hover and self._leave_since is None:
                self._leave_since = now
            want = self.hover and (now - (self._leave_since or now) < LEAVE_DELAY)
        if want != self.hover:
            self.hover = want
            self.scene.update(R.describe(self.state, self.list_open, self.hover), now)
            return True
        return False

    def hit(self, x, y):
        for item in self.hot:
            hid, x0, y0, x1, y1 = item
            if x0 <= x <= x1 and y0 <= y <= y1:
                return hid
        return None

    def press(self, x, y):
        self._drag = (x, y, self.hit(x, y))

    def release(self, x, y, now):
        """松开鼠标：按钮 / 列表行 / 小圆 / 岛本身。返回做了什么（测试用）。"""
        if not self._drag:
            return None
        x0, y0, hid0 = self._drag
        self._drag = None
        dx = x - x0
        st = self.state
        present = st.get("present") or {}
        if hid0 and hid0.startswith("row:"):
            nid = hid0[4:]
            if dx > self.SWIPE:
                return self._send("notice_action", {"id": nid, "action": "dismiss"})
            if dx < -self.SWIPE:
                return self._send("notice_action", {"id": nid, "action": "later"})
            if abs(dx) < 8:
                return self._send("notice_action", {"id": nid, "action": "open"})
            return None
        hid = self.hit(x, y)
        if hid is None or hid != hid0:
            if hid0 is None and hid is None and abs(dx) < 8:
                return self._toggle_or_click(now)
            return None
        if hid == "secondary":
            return self._toggle_or_click(now, force_list=True)
        if st.get("mode") == "confirm" and hid in ("confirm", "cancel"):
            return self._send(hid, {"id": st.get("confirm_id", ""), "action": hid})
        if hid in R_QUICK:
            return self._send("quick", {"action": hid})
        if present.get("id"):
            return self._send("notice_action", {"id": present["id"], "action": hid})
        return None

    def _toggle_or_click(self, now, force_list=False):
        if (self.state.get("notices") or []) and (force_list or self.state.get("mode") in ("idle", "notice")):
            self.list_open = not self.list_open
            self.scene.update(R.describe(self.state, self.list_open, self.hover), now)
            return ("list", self.list_open)
        return self._send("click", {})

    def _send(self, kind, payload):
        self.post(kind, payload)
        return (kind, payload)


R_QUICK = ("control", "briefing", "muse", "dots", "kws", "hide", "phone")


# ---------------------------------------------------------------- 原生窗口

class _WNDCLASS(ctypes.Structure):
    pass


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_WNDCLASS._fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                      ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                      ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class _BLEND(ctypes.Structure):
    _fields_ = [("op", ctypes.c_byte), ("flags", ctypes.c_byte), ("alpha", ctypes.c_ubyte), ("fmt", ctypes.c_byte)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class NativeIsland:
    def __init__(self, api):
        self.u, self.g, self.k = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
        self.u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.u.DefWindowProcW.restype = ctypes.c_ssize_t
        self.u.CreateWindowExW.restype = wintypes.HWND
        # 64 位句柄：所有用到的 GDI / 窗口函数都声明类型，否则句柄值大时会随机溢出
        self.g.CreateDIBSection.restype = wintypes.HBITMAP
        self.g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                            wintypes.HANDLE, wintypes.DWORD]
        self.g.CreateCompatibleDC.restype = wintypes.HDC
        self.g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.g.SelectObject.restype = wintypes.HGDIOBJ
        self.g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self.u.GetDC.restype = wintypes.HDC
        self.u.GetDC.argtypes = [wintypes.HWND]
        self.u.LoadCursorW.restype = wintypes.HANDLE
        self.u.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        self.u.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.HDC,
                                               ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        self.u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.u.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.u.MonitorFromPoint.restype = wintypes.HMONITOR
        self.u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        self.k.GetModuleHandleW.restype = wintypes.HMODULE
        self.u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                           wintypes.HINSTANCE, ctypes.c_void_p]
        self.u.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.u.TranslateMessage.argtypes = [ctypes.c_void_p]
        self.u.DispatchMessageW.argtypes = [ctypes.c_void_p]
        self.u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.api = api
        self.theme, self.transparency = system_theme()
        self.scene = R.Scene(reduce_motion=not animations_enabled())
        self.ctrl = Controller(api, self.scene)
        self.backdrop, self.backdrop_at, self._shot = None, 0.0, None
        self.clickable = None
        self.visible = True
        self._proc = WNDPROC(self._wndproc)
        self.hwnd = self._create()
        # 截毛玻璃背景时岛不能出现在截图里。10-24：原来每次截图前后来回切 WDA_EXCLUDEFROMCAPTURE / WDA_NONE，
        # 切一次窗口就闪一下（静止时每 2.5 秒、展开时每 0.5 秒）。改成一直不进截图，不再切换；
        # 代价是截图、录屏、投屏里看不到岛。系统不支持时退回原来的来回切换。
        self.always_excluded = bool(self.u.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE))
        self.dpr = self.u.GetDpiForWindow(self.hwnd) / 96 or 1.0
        self._place()
        self._alloc()

    # ---- 创建与摆放
    def _create(self):
        hinst = self.k.GetModuleHandleW(None)
        wc = _WNDCLASS(0, self._proc, 0, 0, hinst, None, self.u.LoadCursorW(None, 32512), None, None, "CanaanIsland")
        self.u.RegisterClassW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT
        hwnd = self.u.CreateWindowExW(ex, "CanaanIsland", "Island", 0x80000000, 0, 0, 10, 10, None, None, hinst, None)
        if not hwnd:
            raise OSError("创建灵动岛窗口失败")
        self.clickable = False
        return hwnd

    def _place(self):
        """放在主显示器工作区顶部正中。"""
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        mon = self.u.MonitorFromPoint(wintypes.POINT(0, 0), 1)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        self.u.GetMonitorInfoW(mon, ctypes.byref(mi))
        wa = mi.rcWork
        self.Wp, self.Hp = int(round(R.WIN_W * self.dpr)), int(round(R.WIN_H * self.dpr))
        self.x = wa.left + (wa.right - wa.left - self.Wp) // 2
        self.y = wa.top

    def check_display(self):
        """分辨率、缩放、任务栏位置变了：重新摆放、重新分配画布。返回是否变了。"""
        self.dpr_now = (self.u.GetDpiForWindow(self.hwnd) / 96) or self.dpr
        before = (self.dpr, self.x, self.y, self.Wp, self.Hp)
        self.dpr = self.dpr_now
        self._place()
        if (self.dpr, self.x, self.y, self.Wp, self.Hp) == before:
            return False
        try:
            self.g.DeleteObject(self.hbmp)
            self.g.DeleteDC(self.mdc)
        except Exception:
            pass
        self._alloc()
        self.backdrop, self._shot, self.backdrop_at = None, None, 0.0
        return True

    def _alloc(self):
        self.g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.g.DeleteDC.argtypes = [wintypes.HDC]
        self.sdc = self.u.GetDC(None)
        self.mdc = self.g.CreateCompatibleDC(self.sdc)
        bmi = _BMIH(ctypes.sizeof(_BMIH), self.Wp, -self.Hp, 1, 32, 0, 0, 0, 0, 0, 0)
        self.bits = ctypes.c_void_p()
        self.hbmp = self.g.CreateDIBSection(self.mdc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        self.g.SelectObject(self.mdc, self.hbmp)

    def push(self, frame):
        ctypes.memmove(self.bits, frame.ctypes.data, frame.nbytes)
        pos, size, src = wintypes.POINT(self.x, self.y), wintypes.SIZE(self.Wp, self.Hp), wintypes.POINT(0, 0)
        blend = _BLEND(0, 0, 255, 1)
        self.u.UpdateLayeredWindow(self.hwnd, self.sdc, ctypes.byref(pos), ctypes.byref(size), self.mdc, ctypes.byref(src),
                                   0, ctypes.byref(blend), 2)

    # ---- 毛玻璃背景
    def capture_backdrop(self, now):
        """只截岛（含将要展开到的大小）周围那一条，模糊着色后写进整窗大小的背景里。返回背景有没有变。"""
        if not self.transparency or ((self.api.get_state().get("settings") or {}).get("blur") is False):
            changed = self.backdrop is not None
            self.backdrop = None
            self.backdrop_at = now
            return changed
        try:
            import numpy as np
            from PIL import ImageGrab
            x0, _, x1, y1 = self.scene.bounds()
            tx = R.WIN_W / 2 + max(self.scene.w.target, self.scene.w.value) / 2 + R.Scene.GAP + R.Scene.BUBBLE
            ty = R.TOP_MARGIN + max(self.scene.h.target, self.scene.h.value)
            lx0 = max(0, int((min(x0, R.WIN_W - tx) - 30) * self.dpr))
            lx1 = min(self.Wp, int((max(x1, tx) + 30) * self.dpr))
            ly1 = min(self.Hp, int((max(y1, ty) + 30) * self.dpr))
            if lx1 <= lx0 or ly1 <= 0:
                return False
            bbox = (self.x + lx0, self.y, self.x + lx1, self.y + ly1)
            if getattr(self, "always_excluded", False):
                shot = ImageGrab.grab(bbox=bbox, all_screens=True)
            else:
                self.u.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)
                try:
                    shot = ImageGrab.grab(bbox=bbox, all_screens=True)
                finally:
                    self.u.SetWindowDisplayAffinity(self.hwnd, WDA_NONE)
            sig = (lx0, lx1, ly1, self.theme, shot.resize((max(1, shot.width // 8), max(1, shot.height // 8))).tobytes())
            if sig == self._shot and self.backdrop is not None:
                return False                        # 背后没变：不重算，也不重画
            self._shot = sig
            if self.backdrop is None or self.backdrop.shape[:2] != (self.Hp, self.Wp):
                self.backdrop = np.empty((self.Hp, self.Wp, 3), np.float32)
                self.backdrop[:] = np.array(R.THEMES[self.theme]["tint"], np.float32)
            else:
                self.backdrop = self.backdrop.copy()   # 换一个新对象，画面那边的缓存才知道背景变了
            self.backdrop[0:ly1, lx0:lx1] = R.tint_backdrop(R.blur_backdrop(shot), self.theme)
            return True
        except Exception:
            changed = self.backdrop is not None
            self.backdrop = None
            return changed
        finally:
            self.backdrop_at = now

    # ---- 输入
    def _wndproc(self, hwnd, msg, wp, lp):
        if msg in (0x0201, 0x0202):                 # WM_LBUTTONDOWN / UP
            x = ctypes.c_short(lp & 0xFFFF).value / self.dpr
            y = ctypes.c_short((lp >> 16) & 0xFFFF).value / self.dpr
            if msg == 0x0201:
                self.ctrl.press(x, y)
            else:
                self.ctrl.release(x, y, time.monotonic())
            return 0
        if msg == 0x0200:                           # WM_MOUSEMOVE：按钮悬停高亮
            x = ctypes.c_short(lp & 0xFFFF).value / self.dpr
            y = ctypes.c_short((lp >> 16) & 0xFFFF).value / self.dpr
            hid = self.ctrl.hit(x, y)
            self.scene.hover_btn = hid if hid and not hid.startswith("row:") else None
            return 0
        if msg == 0x0021:                           # WM_MOUSEACTIVATE：点了也不抢焦点
            return 3                                # MA_NOACTIVATE
        return self.u.DefWindowProcW(hwnd, msg, wp, lp)

    def update_click_through(self):
        pt = wintypes.POINT()
        self.u.GetCursorPos(ctypes.byref(pt))
        x0, y0, x1, y1 = self.scene.bounds()
        lx, ly = (pt.x - self.x) / self.dpr, (pt.y - self.y) / self.dpr
        over = self.visible and self.scene.desc["layout"] != "hidden" and x0 - 4 <= lx <= x1 + 4 and y0 - 4 <= ly <= y1 + 4
        if over != self.clickable:
            style = self.u.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TRANSPARENT) if over else (style | WS_EX_TRANSPARENT)
            self.u.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)
            self.clickable = over
        return over

    # ---- 主循环
    def run(self, should_stop):
        msg = wintypes.MSG()
        self.u.ShowWindow(self.hwnd, 4)            # SW_SHOWNOACTIVATE
        last = time.monotonic()
        last_state = last_theme = last_parent = 0.0
        next_frame = 0.0
        dirty = True
        parent = int(os.environ.get("CANAAN_PARENT_PID", "0") or 0)
        while True:
            while self.u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                self.u.TranslateMessage(ctypes.byref(msg))
                self.u.DispatchMessageW(ctypes.byref(msg))
            now = time.monotonic()
            if should_stop():
                return 0
            if parent and now - last_parent > 1.0:
                last_parent = now
                if not _pid_alive(parent):
                    return 0                       # 主程序没了：岛跟着退出
            if now - last_theme > 5.0:
                last_theme = now
                theme, transp = system_theme()
                if (theme, transp) != (self.theme, self.transparency):
                    self.theme, self.transparency, self._shot = theme, transp, None
                    self.backdrop_at = 0.0
                    dirty = True
                self.scene.reduce_motion = not animations_enabled()
                try:
                    dirty |= self.check_display()
                except Exception:
                    pass
            if now - last_state > 0.1:
                last_state = now
                dirty |= self.ctrl.refresh(now)
                self.ctrl.hot = getattr(self, "_hot", [])
            hidden = self.scene.desc["layout"] == "hidden"
            if hidden != (not self.visible):
                self.visible = not hidden
                self.u.ShowWindow(self.hwnd, 0 if hidden else 4)
            over = self.update_click_through()
            dirty |= self.ctrl.set_hover(over, now)
            busy = self.scene.desc["layout"] != "pill" or not self.scene.shape_resting()
            if self.visible and now - self.backdrop_at > (BACKDROP_EVERY if busy else BACKDROP_IDLE):
                dirty |= self.capture_backdrop(now)
            dt = min(0.25, now - last)
            last = now
            moving = self.scene.step(dt, now)
            want = moving or dirty or self.scene.needs_frames()
            if self.visible and want and now >= next_frame:
                frame, hot = self.scene.frame(self.dpr, self.theme, self.backdrop, icon_paths(self._sources()))
                self._hot = self.ctrl.hot = hot
                self.push(frame)
                dirty = False
                # 动画 60 帧；只有波形 / 呼吸在动时 24 帧；只有发光在呼吸时 12 帧
                rate = 60 if moving else (24 if self.scene.animated_content() else 12)
                next_frame = now + 1.0 / rate
            time.sleep(0.004 if (moving or self.scene.needs_frames()) else 0.05)

    def _sources(self):
        st = self.ctrl.state
        names = {n.get("source", "") for n in (st.get("notices") or [])}
        for key in ("present", "secondary"):
            if st.get(key):
                names.add(st[key].get("source", ""))
        return [n for n in names if n]


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # 每个显示器各自的缩放
    except Exception:
        pass
    try:
        island = NativeIsland(IslandApi(os.environ.get("CANAAN_ISLAND_STATE") or STATE_PATH))
    except Exception as e:
        print(f"[!] 灵动岛启动失败（{type(e).__name__}），退回 tkinter 版")
        if os.environ.get("CANAAN_ISLAND_DEBUG"):
            import traceback
            traceback.print_exc()
        sys.exit(3)
    stop_after = float(os.environ.get("CANAAN_ISLAND_SECONDS", "0") or 0)   # 调试用：几秒后自己退出
    t0 = time.monotonic()
    sys.exit(island.run(lambda: bool(stop_after) and time.monotonic() - t0 > stop_after))


if __name__ == "__main__":
    main()
