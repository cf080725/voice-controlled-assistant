"""桌宠动效物理：弹簧收敛、甩出速度、飞行会停下来、撞墙反弹衰减、吸附判定（左 / 右 / 上 / 不吸）、呼吸幅度、安静规则。"""
import math

import pytest

import canaan_motion as m

AREA = (0, 0, 1920, 1040)


def test_spring_converges_with_small_overshoot():
    s = m.Spring(0, response=0.45, damping=0.78)
    s.set(100)
    peak, t = 0, 0
    while t < 3:
        peak = max(peak, s.step(1 / 60))
        t += 1 / 60
    assert s.resting and peak < 106
    s2 = m.Spring(0)
    s2.set(50)
    for _ in range(12):
        s2.step(0.25)                       # 低帧率大步长也不发散
    assert abs(s2.value - 50) < 0.06


def test_velocity_tracker_px_per_second():
    vt = m.VelocityTracker()
    for i in range(6):
        vt.add(1.0 + i * 0.016, 100 + i * 16, 200)
    vx, vy = vt.velocity(1.0 + 5 * 0.016)
    assert 950 < vx < 1050 and abs(vy) < 1
    assert vt.velocity(5.0) == (0.0, 0.0)  # 停住不动一会儿再松手：不甩
    for i in range(3):
        vt.add(10 + i * 0.01, i * 500, 0)
    assert math.hypot(*vt.velocity(10.02)) <= 4000.0001


def test_projectile_comes_to_rest_and_bounces_off_walls():
    x, y, vx, vy = 900, 300, 2500, -600
    walls, landed, t = [], False, 0
    bounds = (0, 0, 1700, 800)
    while t < 8:
        x, y, vx, vy, ev = m.projectile_step(x, y, vx, vy, 1 / 60, bounds)
        walls += [e for e in ev if e[0] == "wall"]
        landed = landed or any(e[0] == "landed" for e in ev)
        if any(e[0] == "rest" for e in ev):
            break
        t += 1 / 60
    assert landed and walls and walls[0][1] == "right" and t < 8
    assert 0 <= x <= 1700 and y == 800 and vx == 0.0 and vy == 0.0


@pytest.mark.parametrize("win,expect", [
    ((40, 500), "left"), ((1920 - 200 - 30, 500), "right"), ((800, -60), "top"), ((800, 500), None),
])
def test_dock_target(win, expect):
    got = m.dock_target(win[0], win[1], 200, 260, 120, 180, AREA)
    assert (got[0] if got else None) == expect
    if expect == "left":
        avatar_left = got[1] + (200 - 120) / 2
        assert got[1] < 0 and avatar_left == pytest.approx(-120 * 0.4, abs=1)   # 40% 藏到屏幕外，露出 60%
    if expect == "right":
        avatar_left = got[1] + (200 - 120) / 2
        assert 1920 - avatar_left == pytest.approx(120 * 0.6, abs=1)


def test_dock_on_secondary_monitor_left_of_primary():
    area = (-1920, 0, 0, 1040)
    side, x, _ = m.dock_target(-1900, 400, 200, 260, 120, 180, area)
    assert side == "left" and x < -1920


def test_breathing_is_subtle_and_stretch_bounded():
    for t in range(0, 40):
        sx, sy = m.breathing(t / 10)
        assert abs(sy - 1) <= 0.0151 and abs(sx - 1) <= 0.01
    sx, sy = m.stretch(100000, 0)
    assert sx <= 1.18 + 1e-9 and sy < 1
    assert m.stretch(0, 0) == (1.0, 1.0)


def test_quiet_rules():
    assert not m.quiet()
    assert m.quiet(fullscreen=True) and m.quiet(game=True) and m.quiet(dnd=True)
    assert not m.quiet(typing_streak=119) and m.quiet(typing_streak=120)
