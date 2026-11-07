# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

import pytest

import canaan_proactive as cp


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.mark.parametrize("text,delta_min,what", [
    ("20分钟后提醒我关火", 20, "关火"), ("半小时后提醒我喝水", 30, "喝水"), ("两个小时以后提醒我开会", 120, "开会"),
    ("提醒我十分钟后看一下锅", 10, "看一下锅"),
])
def test_parse_relative(text, delta_min, what):
    now = datetime(2026, 10, 6, 14, 0)
    due, w = cp.parse_reminder(text, now)
    assert due == now + timedelta(minutes=delta_min) and w == what


def test_parse_absolute():
    now = datetime(2026, 10, 6, 14, 0)
    assert cp.parse_reminder("明天早上8点提醒我交报告", now) == (datetime(2026, 10, 7, 8, 0), "交报告")
    assert cp.parse_reminder("下午三点半提醒我开会", now) == (datetime(2026, 10, 6, 15, 30), "开会")
    assert cp.parse_reminder("10点提醒我睡觉", now)[0] == datetime(2026, 10, 7, 10, 0)  # 已过 → 明天
    assert cp.parse_reminder("提醒我一下", now) is None


def test_policy_limits(tmp_path):
    clk = Clock(datetime(2026, 10, 6, 9, 0))
    p = cp.ProactivePolicy(str(tmp_path / "s.json"), clock=clk)
    assert p.allowed("break")[0]
    p.record_spoken("break")
    assert p.allowed("break") == (False, "间隔太短")
    clk.t += timedelta(minutes=61)
    assert p.allowed("break")[0]
    p.record_dismissed("break")  # 被拒一次：间隔翻倍
    assert not p.allowed("break")[0]
    clk.t += timedelta(minutes=60)
    assert p.allowed("break")[0]
    p.record_spoken("greeting"); clk.t += timedelta(hours=3)
    p.record_spoken("after_game"); clk.t += timedelta(hours=3)
    assert p.allowed("greeting") == (False, "今天次数已满")


def test_quiet_hours_and_mute(tmp_path):
    clk = Clock(datetime(2026, 10, 6, 23, 30))
    p = cp.ProactivePolicy(str(tmp_path / "s.json"), clock=clk)
    assert p.allowed("break") == (False, "安静时段")
    clk.t = datetime(2026, 10, 7, 10, 0)
    assert cp.handle_command("今天别打扰我了", p)["reply"]
    assert p.allowed("break") == (False, "今天勿扰")


def test_scheduler_reminder_fires_even_in_quiet_hours(tmp_path):
    clk = Clock(datetime(2026, 10, 6, 23, 50))
    p = cp.ProactivePolicy(str(tmp_path / "s.json"), clock=clk)
    said = []
    s = cp.ProactiveScheduler(p, lambda text, kind: said.append((kind, text)) or True)
    cp.handle_command("5分钟后提醒我关灯", p)
    clk.t += timedelta(minutes=6)
    s.step(idle=10, notif_ok=True)
    assert said and said[0][0] == "reminder" and "关灯" in said[0][1]


def test_scheduler_skips_fullscreen_and_breaks_after_two_hours(tmp_path):
    clk = Clock(datetime(2026, 10, 6, 14, 0))
    p = cp.ProactivePolicy(str(tmp_path / "s.json"), clock=clk)
    said = []
    s = cp.ProactiveScheduler(p, lambda text, kind: said.append(kind) or True)
    s._was_idle_long = False
    for _ in range(25):  # 每 5 分钟一次，持续操作
        s.step(idle=5, notif_ok=False)
        clk.t += timedelta(minutes=5)
    assert said == []  # 全屏时不说
    s.step(idle=5, notif_ok=True)
    assert said == ["break"]


def test_scheduler_thread_starts(tmp_path, monkeypatch):
    # 10-24：属性名盖掉了 Thread._started，hub 启动时 start() 报错，提醒和主动播报全没了
    monkeypatch.setattr(cp.ProactiveScheduler, "step", lambda self, *a, **k: None)
    s = cp.ProactiveScheduler(cp.ProactivePolicy(str(tmp_path / "s.json")), lambda *a: True, poll=0.01)
    s.start()
    s.stop()
    s.join(1)
    assert not s.is_alive()
