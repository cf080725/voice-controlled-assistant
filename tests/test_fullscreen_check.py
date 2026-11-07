"""“有全屏程序”的判断：系统报 QUNS_BUSY 时还要前台窗口真的铺满屏幕才算；透明全屏覆盖层不算。演示 / D3D 全屏照样算。"""
import ctypes

import canaan_proactive as p


class Shell:
    def __init__(self, state):
        self.state = state

    def SHQueryUserNotificationState(self, ref):
        ref._obj.value = self.state
        return 0


def with_state(monkeypatch, state, fg_full):
    monkeypatch.setattr(ctypes.windll, "shell32", Shell(state), raising=False)
    monkeypatch.setattr(p, "foreground_fullscreen", lambda: fg_full)
    return p.notification_ok()


def test_busy_only_when_foreground_really_fullscreen(monkeypatch):
    assert with_state(monkeypatch, 2, False) is True          # 透明覆盖层：照常提示
    assert with_state(monkeypatch, 2, True) is False          # 真的在看全屏视频
    assert with_state(monkeypatch, 4, False) is False         # 演示模式
    assert with_state(monkeypatch, 3, False) is False         # D3D 全屏游戏
    assert with_state(monkeypatch, 5, False) is True


def test_foreground_fullscreen_does_not_crash():
    assert p.foreground_fullscreen() in (True, False)
