"""操作窗口时的现场保护：进来时记下前台窗口、光标、剪贴板；出去时收起目标窗口、还原前台、光标和剪贴板。
用替身 user32 / pyperclip，不真的动鼠标。"""
import pytest

sg = pytest.importorskip("canaan_action_framework.session_guard")


class FakeUser32:
    def __init__(self):
        self.calls = []
        self.fg, self.cursor, self.valid = 111, (5, 6), {222}

    def GetForegroundWindow(self):
        return self.fg

    def GetCursorPos(self, ref):
        ref._obj.x, ref._obj.y = self.cursor

    def SetCursorPos(self, x, y):
        self.calls.append(("cursor", x, y))

    def mouse_event(self, flag, *a):
        self.calls.append(("mouse", flag))

    def IsWindow(self, h):
        return h in self.valid

    def GetWindowRect(self, h, ref):
        r = ref._obj
        r.left, r.top, r.right, r.bottom = 100, 50, 900, 650

    def ShowWindow(self, h, cmd):
        self.calls.append(("show", h, cmd))

    def SetForegroundWindow(self, h):
        self.calls.append(("fg", h))


class Clip:
    def __init__(self, value="主人的剪贴板"):
        self.value = value

    def paste(self):
        return self.value

    def copy(self, v):
        self.value = v


@pytest.fixture
def env(monkeypatch):
    u, clip = FakeUser32(), Clip()
    monkeypatch.setattr(sg, "user32", u)
    monkeypatch.setattr(sg, "pyperclip", clip)
    monkeypatch.setattr(sg.time, "sleep", lambda s: None)
    return u, clip


def test_restores_foreground_cursor_clipboard_and_collapses_target(env):
    u, clip = env
    with sg.DesktopSessionGuard(target_hwnd=222) as g:
        assert g.orig_fg_hwnd == 111 and g.orig_cursor_pos == (5, 6) and g.orig_clipboard == "主人的剪贴板"
        clip.copy("操作中临时放的")
    assert ("show", 222, 6) in u.calls                       # 目标窗口最小化
    assert ("cursor", 900 - 172, 70) in u.calls               # 点的是最小化按钮的位置
    assert ("fg", 111) in u.calls and u.calls[-1] == ("cursor", 5, 6)
    assert clip.value == "主人的剪贴板"


def test_exception_inside_still_restores_and_propagates(env):
    u, clip = env
    with pytest.raises(RuntimeError):
        with sg.DesktopSessionGuard(target_hwnd=0):
            clip.copy("x")
            raise RuntimeError("中途出错")
    assert clip.value == "主人的剪贴板" and ("fg", 111) in u.calls


def test_invalid_target_not_touched_and_options_respected(env):
    u, clip = env
    with sg.DesktopSessionGuard(target_hwnd=999, preserve_clipboard=False, ghost_cursor=False):
        clip.copy("新内容")
    assert not any(c[0] == "show" for c in u.calls)
    assert clip.value == "新内容" and not any(c == ("cursor", 5, 6) for c in u.calls)


def test_same_window_not_refocused(env):
    u, _ = env
    u.fg = 222
    with sg.DesktopSessionGuard(target_hwnd=222):
        pass
    assert not any(c[0] == "fg" for c in u.calls)


def test_clipboard_read_failure_is_tolerated(env, monkeypatch):
    u, clip = env
    monkeypatch.setattr(clip, "paste", lambda: (_ for _ in ()).throw(OSError("被占用")))
    with sg.DesktopSessionGuard() as g:
        assert g.orig_clipboard is None


def test_native_click_sequence(env):
    u, _ = env
    sg.DesktopSessionGuard().native_click(10.6, 20.2, delay_after=0)
    assert u.calls == [("cursor", 10, 20), ("mouse", 0x0002), ("mouse", 0x0004)]
