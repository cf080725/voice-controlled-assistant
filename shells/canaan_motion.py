# -*- coding: utf-8 -*-
"""桌宠动效的物理与判定（纯函数 / 小对象，不依赖 Tk，便于单测）。

- Spring：弹簧（参数含义同灵动岛：response 秒、dampingFraction），大步长内部细分。
- VelocityTracker：拖拽时记最近 0.1 秒的位置，松手时得到真实的甩出速度（像素/秒）。
- projectile_step：甩出后的飞行：重力、空气阻力、撞墙反弹衰减、落地压扁；返回撞到哪面墙、是否落地。
- dock_target：松手时离左 / 右 / 上边缘够近就吸过去，身体藏 40% 在屏幕外（“探头 / 倒挂”）。
- breathing：很浅的呼吸缩放（±1.5%，4 秒一次，体积守恒）。
- quiet：什么时候该安静（全屏、游戏、勿扰、连续打字超过 2 分钟）。
"""
import math
from collections import deque

DOCK_THRESHOLD = 80        # 离边缘多少像素以内松手就吸附
DOCK_HIDDEN = 0.40         # 吸附后藏到屏幕外的比例
WALL_SNAP_SPEED = 900.0    # 甩向墙的速度超过这个值，撞墙直接吸附
TYPING_QUIET_AFTER = 120.0  # 连续打字多少秒算“专心干活”


class Spring:
    def __init__(self, value=0.0, response=0.45, damping=0.78):
        self.value = self.target = float(value)
        self.velocity = 0.0
        self.response, self.damping = response, damping

    def set(self, target):
        self.target = float(target)

    def snap(self, value):
        self.value = self.target = float(value)
        self.velocity = 0.0

    def step(self, dt):
        k = (2 * math.pi / self.response) ** 2
        c = 4 * math.pi * self.damping / self.response
        n = max(1, math.ceil(dt / 0.004))
        h = dt / n
        for _ in range(n):
            a = -k * (self.value - self.target) - c * self.velocity
            self.velocity += a * h
            self.value += self.velocity * h
        if abs(self.velocity) < 0.05 and abs(self.value - self.target) < 0.05:
            self.value, self.velocity = self.target, 0.0
        return self.value

    @property
    def resting(self):
        return self.velocity == 0.0 and self.value == self.target


class VelocityTracker:
    """最近 window 秒内的位置样本 → 速度（像素/秒）。样本太少或太旧时速度为 0。"""

    def __init__(self, window=0.10, max_speed=4000.0):
        self.window, self.max_speed = window, max_speed
        self.samples = deque()

    def add(self, t, x, y):
        self.samples.append((t, x, y))
        while self.samples and t - self.samples[0][0] > self.window * 2:
            self.samples.popleft()

    def velocity(self, now):
        recent = [s for s in self.samples if now - s[0] <= self.window]
        if len(recent) < 2 or recent[-1][0] - recent[0][0] < 1e-3:
            return 0.0, 0.0
        (t0, x0, y0), (t1, x1, y1) = recent[0], recent[-1]
        vx, vy = (x1 - x0) / (t1 - t0), (y1 - y0) / (t1 - t0)
        speed = math.hypot(vx, vy)
        if speed > self.max_speed:
            vx, vy = vx * self.max_speed / speed, vy * self.max_speed / speed
        return vx, vy

    def clear(self):
        self.samples.clear()


def projectile_step(x, y, vx, vy, dt, bounds, gravity=1800.0, drag=1.6, restitution=0.42, floor_friction=6.0):
    """甩出后的一步飞行。bounds=(min_x, min_y, max_x, max_y) 是窗口左上角允许的范围（max_y 就是地面）。
    返回 (x, y, vx, vy, events)；events 里可能有 ("wall", "left"/"right", 撞击速度)、("landed", 落地速度)、("rest",)。"""
    min_x, min_y, max_x, max_y = bounds
    events = []
    vy += gravity * dt
    damp = math.exp(-drag * dt)
    vx *= damp
    x += vx * dt
    y += vy * dt
    if x < min_x:
        events.append(("wall", "left", abs(vx)))
        x, vx = min_x, abs(vx) * restitution
    elif x > max_x:
        events.append(("wall", "right", abs(vx)))
        x, vx = max_x, -abs(vx) * restitution
    if y < min_y:
        y, vy = min_y, abs(vy) * restitution
    if y >= max_y:
        impact = abs(vy)
        y = max_y
        vy = -impact * restitution if impact > 160 else 0.0
        vx *= math.exp(-floor_friction * dt)
        events.append(("landed", impact))
        if vy == 0.0 and abs(vx) < 25:
            vx = 0.0
            events.append(("rest",))
    return x, y, vx, vy, events


def dock_target(win_x, win_y, win_w, win_h, avatar_w, avatar_h, area, threshold=DOCK_THRESHOLD, hidden=DOCK_HIDDEN):
    """松手时该不该吸附、吸到哪。avatar 在窗口里水平居中、贴底。
    返回 None，或 (side, x, y)：side ∈ left / right / top，(x, y) 是吸附后窗口左上角（可以在屏幕外）。"""
    left, top, right, bottom = area
    avatar_left = win_x + (win_w - avatar_w) / 2
    avatar_right = avatar_left + avatar_w
    avatar_top = win_y + win_h - avatar_h
    margin_x = (win_w - avatar_w) / 2
    if avatar_left - left <= threshold:
        return "left", int(left - hidden * avatar_w - margin_x), int(win_y)
    if right - avatar_right <= threshold:
        return "right", int(right - (1 - hidden) * avatar_w - margin_x), int(win_y)
    if avatar_top - top <= threshold:
        # 倒挂：画面上下翻转后贴窗口顶部，露出 60%
        return "top", int(win_x), int(top - hidden * avatar_h)
    return None


def dock_position(side, win_x, win_y, win_w, win_h, avatar_w, avatar_h, area, hidden=DOCK_HIDDEN):
    """指定吸附到哪一边时窗口左上角的位置（重启后恢复吸附、撞墙吸附用）。"""
    left, top, right, _ = area
    margin_x = (win_w - avatar_w) / 2
    if side == "left":
        return "left", int(left - hidden * avatar_w - margin_x), int(win_y)
    if side == "right":
        return "right", int(right - (1 - hidden) * avatar_w - margin_x), int(win_y)
    if side == "top":
        return "top", int(win_x), int(top - hidden * avatar_h)
    return None


def peek_offset(side, avatar_w, avatar_h, out=True, hidden=DOCK_HIDDEN):
    """光标靠近时从吸附位置滑出来多少（像素，正值表示朝屏幕内）。"""
    if not out:
        return 0
    return int((hidden - 0.05) * (avatar_h if side == "top" else avatar_w))


def breathing(t, amplitude=0.015, period=4.0):
    """很浅的呼吸：(scale_x, scale_y)，体积大致守恒。"""
    s = math.sin(2 * math.pi * t / period) * amplitude
    return 1.0 - s * 0.55, 1.0 + s


def stretch(vx, vy, max_stretch=0.18):
    """按速度方向拉长身体（拖拽和飞行时用）。返回 (scale_x, scale_y)。"""
    speed = math.hypot(vx, vy)
    amount = min(max_stretch, speed / 6000.0)
    if speed < 1:
        return 1.0, 1.0
    horizontal = abs(vx) / speed
    sx = 1.0 + amount * horizontal - amount * 0.5 * (1 - horizontal)
    sy = 1.0 + amount * (1 - horizontal) - amount * 0.5 * horizontal
    return sx, sy


def quiet(fullscreen=False, game=False, dnd=False, typing_streak=0.0):
    """该不该安静（不放待机动画，只保留状态提示）。"""
    return bool(fullscreen or game or dnd or typing_streak >= TYPING_QUIET_AFTER)
