# -*- coding: utf-8 -*-
import canaan_pet_sense as ps


def test_classify_priority_and_categories():
    assert ps.classify("支付宝 - 收银台", "chrome.exe") == "privacy"
    assert ps.classify("Bitwarden", "bitwarden.exe") == "privacy"
    assert ps.classify("InPrivate - Microsoft Edge", "msedge.exe") == "privacy"
    assert ps.classify("main.py - AAA - Visual Studio Code", "code.exe") == "code"
    assert ps.classify("Windows PowerShell", "powershell.exe") == "code"
    assert ps.classify("app.ts - Cursor", "unknown.exe") == "code"
    assert ps.classify("AAA - Antigravity", "antigravity ide.exe") == "code"     # 本机实际在用的编辑器
    assert ps.classify("晴天 - 周杰伦", "cloudmusic.exe") == "music"
    assert ps.classify("哔哩哔哩", "chrome.exe") is None
    # 敏感优先于代码：在终端里输入密码的提示
    assert ps.classify("输入密码 - 终端", "windowsterminal.exe") == "privacy"


def test_music_title_playing():
    assert ps.music_title_playing("晴天 - 周杰伦")
    assert ps.music_title_playing("Blinding Lights - The Weeknd")
    assert not ps.music_title_playing("网易云音乐")
    assert not ps.music_title_playing("Spotify Premium")
    assert not ps.music_title_playing("")


class _Env:
    def __init__(self):
        self.title, self.proc, self.idle, self.cur, self.procs, self.bg = "", "", 99.0, (0, 0), set(), False

    def watcher(self):
        return ps.SenseWatcher(foreground=lambda: (self.title, self.proc), idle_sec=lambda: self.idle,
                               cursor=lambda: self.cur, procs=lambda: set(self.procs), bg_music=lambda n: self.bg)


def test_typing_needs_recent_input_without_mouse_move():
    env = _Env()
    w = env.watcher()
    env.title, env.proc = "main.py - Visual Studio Code", "code.exe"
    w.poll_once(now=1.0)
    env.idle = 0.3
    assert w.poll_once(now=2.0).typing is True            # 刚有输入、鼠标没动 → 在打字
    env.cur = (50, 60)
    assert w.poll_once(now=3.0).typing is False           # 鼠标动了 → 不算打字
    env.idle = 5.0
    assert w.poll_once(now=4.0).typing is False and w.latest.mode == "code"


def test_privacy_state_keeps_no_title_and_detects_recorder():
    env = _Env()
    w = env.watcher()
    env.title, env.proc = "网上银行 - 某银行", "chrome.exe"
    env.procs = {"obs64.exe", "chrome.exe"}
    st = w.poll_once(now=1.0)
    assert st.mode == "privacy" and st.recorder is True
    assert "银行" not in repr(st.window_key)              # 只存哈希，不存标题原文
    assert not any(isinstance(v, str) and "银行" in v for v in (st.mode, st.window_key[0]))
    since = st.since
    assert w.poll_once(now=2.0).since == since             # 同一个窗口，起始时间不变
    env.title = "收银台 - 另一家"
    assert w.poll_once(now=3.0).since == 3.0               # 换了窗口


def test_background_music_and_none():
    env = _Env()
    w = env.watcher()
    env.title, env.proc = "说明.docx - WPS", "wps.exe"
    assert w.poll_once(now=1.0).mode is None
    env.bg = True
    assert w.poll_once(now=2.0).mode == "music"            # 后台在放歌


def test_watcher_disabled_reports_nothing():
    env = _Env()
    w = env.watcher()
    w.interval = 0.01
    env.title, env.proc = "x - Visual Studio Code", "code.exe"
    w.enabled = False
    w.start()
    import time
    time.sleep(0.05)
    w.stop()
    assert w.latest.mode is None
