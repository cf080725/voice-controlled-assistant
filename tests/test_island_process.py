"""灵动岛进程：读状态有缓存、坏文件不覆盖；网页报告的位置只收合法数字；光标在岛上才接收点击，
否则整个窗口点得穿；状态 hidden 时隐藏窗口；主程序没了岛自己退出。用替身窗口和 Win32 调用，不开真窗口。"""
import json

import pytest

isl = pytest.importorskip("canaan_island")


def test_get_state_caches_and_ignores_broken(tmp_path):
    p = tmp_path / "s.json"
    api = isl.IslandApi(str(p))
    assert api.get_state()["mode"] == "idle"                    # 文件还没有
    p.write_text(json.dumps({"mode": "speaking", "seq": 3, "text": "你好"}), encoding="utf-8")
    assert api.get_state()["mode"] == "speaking"
    p.write_text("{坏的", encoding="utf-8")
    import os
    os.utime(p, (1, 999999999))
    assert api.get_state()["mode"] == "speaking"                # 坏文件不覆盖上一次的好状态


def test_event_rect_validation_and_actions(monkeypatch):
    posted = []
    monkeypatch.setattr(isl, "post_action", lambda kind, payload: posted.append((kind, payload)) or True)
    api = isl.IslandApi("nope.json")
    assert api.event("rect", {"x": 1, "y": 2, "w": 3, "h": 4, "dpr": 1.5}) and api.rect == (1, 2, 3, 4, 1.5)
    api.event("rect", {"x": "坏"})
    assert api.rect == (1, 2, 3, 4, 1.5)                         # 坏数据不改
    api.event("click", "不是字典")
    assert posted == [("click", {})]


class FakeUser32:
    def __init__(self, cursor):
        self.style, self.cursor, self.shown = 0, cursor, []

    def GetWindowLongW(self, hwnd, idx):
        return self.style

    def SetWindowLongW(self, hwnd, idx, style):
        self.style = style

    def GetCursorPos(self, ref):
        ref._obj.x, ref._obj.y = self.cursor

    def PeekMessageW(self, *a):
        return 0

    def ShowWindow(self, hwnd, cmd):
        self.shown.append(cmd)

    def UpdateLayeredWindow(self, *a):
        return 1


def fake_island(api, cursor=(0, 0)):
    """不建真窗口的岛：原生调用全换成替身。"""
    import canaan_island_render as R
    n = object.__new__(isl.NativeIsland)
    n.u = FakeUser32(cursor)
    n.hwnd, n.dpr, n.x, n.y = 1, 1.0, 100, 0
    n.Wp, n.Hp = R.WIN_W, R.WIN_H
    n.api, n.theme, n.transparency = api, "light", False
    n.scene = R.Scene()
    n.ctrl = isl.Controller(api, n.scene, post=lambda *a: None, sound=lambda *a: None)
    n.backdrop, n.backdrop_at, n._shot = None, 0.0, None
    n.clickable, n.visible = None, True
    n.pushed = 0
    n.push = lambda frame: setattr(n, "pushed", n.pushed + 1)
    return n


def make_api(mode="idle"):
    api = isl.IslandApi("nope.json")
    api._cache = {"mode": mode, "seq": 1, "notices": []}
    api.get_state = lambda: api._cache
    return api


def test_click_through_unless_cursor_on_island():
    n = fake_island(make_api(), cursor=(5, 5))                   # 光标不在岛上
    n.update_click_through()
    assert n.u.style & isl.WS_EX_TRANSPARENT and n.clickable is False
    n2 = fake_island(make_api(), cursor=(100 + 230, 20))          # 光标在岛上（窗口 x=100，岛在正中）
    n2.update_click_through()
    assert not n2.u.style & isl.WS_EX_TRANSPARENT and n2.clickable is True


def test_hidden_mode_hides_window_and_draws_frames(monkeypatch):
    monkeypatch.delenv("CANAAN_PARENT_PID", raising=False)
    n = fake_island(make_api("hidden"))
    ticks = [0]

    def stop():
        ticks[0] += 1
        return ticks[0] > 6
    assert n.run(stop) == 0
    assert 0 in n.u.shown and n.visible is False
    n2 = fake_island(make_api("listening"))
    ticks[0] = 0
    n2.run(stop)
    assert n2.pushed >= 1 and n2.visible


def test_exits_when_parent_gone(monkeypatch):
    monkeypatch.setenv("CANAAN_PARENT_PID", "12345")
    monkeypatch.setattr(isl, "_pid_alive", lambda pid: False)
    n = fake_island(make_api())
    assert n.run(lambda: False) == 0                              # 主程序没了：自己退出，不会卡住

def test_pid_alive_for_self_and_bogus():
    import os
    assert isl._pid_alive(os.getpid()) is True
    assert isl._pid_alive(0x7FFFFFF0) is False
