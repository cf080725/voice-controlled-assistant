# -*- coding: utf-8 -*-
"""灵动岛的画法（纯计算，不碰 Windows 窗口，方便单独测试）。

为什么不用网页：WebView2 窗口在这台机器上做不到逐像素透明（透明的地方是一整块深色底），
岛会变成屏幕顶上的一个大方块。这里改成自己画：
- 形状用距离场（SDF）算，边缘天然抗锯齿；主胶囊和旁边的小圆用“平滑并集”连起来，分开时有液体拉丝的效果；
- 玻璃：岛下方那一小块屏幕的模糊图（由窗口那边截取，只在内存里）+ 白 / 深色着色 + 轻微提饱和 + 顶部高光 + 细描边 + 柔和投影；
- 内容（图标、文字、圆环进度、对勾、角标、按钮、波形）用 Pillow 画，小图形 4 倍超采样再缩小，保证抗锯齿；
- 弹簧和网页版同一套参数（SwiftUI 的 response / dampingFraction）。
所有尺寸在“逻辑像素”里定，画的时候乘缩放比（dpr）。
"""
import math
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
TOP_MARGIN = 8                      # 岛离屏幕顶边
WIN_W, WIN_H = 460, 300             # 窗口（逻辑像素），够放最大的卡片 + 小圆 + 投影
SHADOW = 18                         # 投影向外扩的范围

THEMES = {
    "light": {"tint": (255, 255, 255), "tint_amt": 0.62, "solid_alpha": 0.94, "ink": (29, 29, 31), "muted": (110, 110, 115),
              "border": (0, 0, 0, 0.10), "highlight": 0.55, "shadow": 0.16, "chip": (0, 0, 0, 0.07), "chip_hi": (0, 0, 0, 0.12)},
    "dark": {"tint": (28, 28, 30), "tint_amt": 0.58, "solid_alpha": 0.92, "ink": (245, 245, 247), "muted": (152, 152, 157),
             "border": (255, 255, 255, 0.14), "highlight": 0.18, "shadow": 0.32, "chip": (255, 255, 255, 0.12), "chip_hi": (255, 255, 255, 0.2)},
}
# 苹果系统色（浅色 / 深色各一套，浅底上看得清）
ACCENTS = {
    "light": {"green": (52, 199, 89), "amber": (255, 149, 0), "red": (255, 59, 48), "blue": (0, 122, 255), "purple": (175, 82, 222), "teal": (48, 176, 199)},
    "dark": {"green": (48, 209, 88), "amber": (255, 159, 10), "red": (255, 69, 58), "blue": (10, 132, 255), "purple": (191, 90, 242), "teal": (100, 210, 255)},
}
BRAND = {"聊天软件": (7, 193, 96), "ChatApp": (7, 193, 96), "IMApp": (18, 183, 245), "TIM": (18, 183, 245), "飞书": (51, 112, 255),
         "Lark": (51, 112, 255), "钉钉": (0, 137, 255), "企业聊天软件": (0, 120, 220), "VS Code": (0, 122, 204),
         "Visual Studio Code": (0, 122, 204), "Claude": (217, 119, 87), "Muse": (175, 82, 222), "Dots": (48, 176, 199),
         "Outlook": (0, 120, 212), "邮件": (0, 122, 255), "Mail": (0, 122, 255), "提示": (142, 142, 147)}


# ---------------------------------------------------------------- 弹簧

class Spring:
    """SwiftUI 式弹簧：刚度 (2π/response)²，阻尼 4π·damping/response；细分步长积分，不会发散。"""

    def __init__(self, value, response=0.55, damping=0.825):
        self.value = self.target = float(value)
        self.velocity = 0.0
        self.response, self.damping = response, damping

    def set(self, target, response=None, damping=None):
        self.target = float(target)
        if response is not None:
            self.response = response
        if damping is not None:
            self.damping = damping

    def snap(self, v):
        self.value = self.target = float(v)
        self.velocity = 0.0

    def kick(self, velocity):
        self.velocity += velocity

    def step(self, dt):
        k = (2 * math.pi / self.response) ** 2
        c = 4 * math.pi * self.damping / self.response
        n = max(1, math.ceil(dt / 0.004))
        h = dt / n
        for _ in range(n):
            a = -k * (self.value - self.target) - c * self.velocity
            self.velocity += a * h
            self.value += self.velocity * h
        if abs(self.velocity) < 0.01 and abs(self.value - self.target) < 0.01:
            self.value, self.velocity = self.target, 0.0
        return self.value

    def resting(self):
        return self.velocity == 0.0 and self.value == self.target


# ---------------------------------------------------------------- 状态 → 画面描述

# 列表底部的快捷操作（原来旧胶囊上的按钮）
QUICK = [("control", "看板"), ("briefing", "早报"), ("muse", "Muse"), ("dots", "Dots"), ("kws", "唤醒词"), ("hide", "收起"), ("phone", "手机")]

VARIANT_ACCENT = {"chat": "green", "p0": "blue", "progress": "purple", "done": "green", "failed": "red",
                  "waiting": "amber", "summary": "blue", "info": "blue"}


def describe(state, list_open=False, hover=False):
    """把岛的状态翻译成一份“画面描述”：尺寸、圆角、用哪种布局、画什么内容、有哪些按钮。纯函数。"""
    mode = state.get("mode", "idle")
    notices = state.get("notices") or []
    present = state.get("present") or None
    n = len(notices)
    text = str(state.get("text") or "")
    d = {"mode": mode, "w": 126, "h": 37, "layout": "pill", "lead": None, "title": "", "detail": "", "trail": None,
         "buttons": [], "accent": None, "glow": None, "shake": False, "secondary": None, "key": mode}
    if mode == "hidden":
        d.update(w=0, h=0, layout="hidden")
        return d
    if list_open:
        rows = notices[:5]
        d.update(w=384, h=len(rows) * 44 + 24 + 40, layout="list", rows=rows, quick=QUICK,
                 key="list:" + ",".join(r.get("id", "") for r in rows))
        return d
    if mode in ("listening", "thinking"):
        d.update(w=210, lead=("wave" if mode == "listening" else "breath"), title=text or ("在听…" if mode == "listening" else "在想…"),
                 accent="green" if mode == "listening" else "teal", key=mode)
    elif mode == "speaking":
        long = len(text) > 18
        d.update(w=372 if long else 340, h=76 if long else 37, layout="tall" if long else "pill", lead="wave", title=text,
                 accent="green", key="speak:" + text[:40])
    elif mode == "confirm":
        d.update(w=372, h=104, layout="card", lead="glyph:!", accent="amber", title="等你确认", detail=text or "有一个操作",
                 buttons=[{"id": "confirm", "label": "确认", "primary": True}, {"id": "cancel", "label": "取消"}],
                 glow="amber", key="confirm:" + text)
    elif mode == "failure":
        d.update(w=340, lead="glyph:!", accent="red", title=text or "没办成", shake=True, key="fail:" + text)
    elif mode == "notice" and present:
        d.update(_notice_desc(present, hover))
    else:   # 待机
        if present and present.get("variant") in ("progress", "waiting"):
            d.update(_notice_desc(present, hover, resting=True))
        elif n:
            d.update(w=150, trail=("dot", n), key="idle:%d" % n)
        else:
            d.update(key="idle")
    sec = state.get("secondary")
    if sec and d["layout"] in ("pill",) and mode in ("idle", "notice"):
        d["secondary"] = sec
    return d


def _notice_desc(p, hover, resting=False):
    v = p.get("variant", "info")
    src, title, count = p.get("source", ""), p.get("title", ""), int(p.get("count") or 1)
    lead = ("icon", src)
    acts = [{"id": a.get("id"), "label": a.get("label"), "primary": i == 0} for i, a in enumerate(p.get("actions") or [])][:3]
    key = "n:%s:%s" % (p.get("id", ""), v)
    if v == "chat":
        if hover:
            return dict(w=384, h=104, layout="card", lead=lead, title=src, detail=title or "%d 条新消息" % count,
                        buttons=acts, accent="green", key=key + ":card")
        return dict(w=240, layout="pill", lead=lead, title=src, trail=("count", count), accent="green", key=key)
    if v == "p0":
        if resting and not hover:
            return dict(w=240, layout="pill", lead=lead, title=src, trail=("count", count), accent="blue", key=key)
        return dict(w=384, h=104, layout="card", lead=lead, title="有重要消息需要核对", detail=src, buttons=acts,
                    accent="blue", glow="blue", key=key + ":card")
    if v == "progress":
        pr = p.get("progress")
        if hover:
            return dict(w=384, h=80, layout="card", lead=lead, title=title, detail=src + (" · %d%%" % pr if pr is not None else ""),
                        trail=("ring", pr), accent="purple", key=key + ":card")
        return dict(w=250, layout="pill", lead=lead, title=title or src, trail=("ring", pr), accent="purple", key=key)
    if v == "done":
        return dict(w=340, layout="pill", lead=lead, title="%s · %s" % (src, title) if title else src, trail=("check", 1),
                    accent="green", key=key)
    if v == "failed":
        return dict(w=372, h=104 if acts else 80, layout="card", lead=lead, title=title or "没办成", detail=src,
                    trail=("glyph", "!"), buttons=acts[:2], accent="red", shake=True, key=key)
    if v == "waiting":
        if resting and not hover:
            return dict(w=250, layout="pill", lead=lead, title=title or src, trail=("dot_amber", 1), accent="amber",
                        glow="amber_soft", key=key + ":rest")
        return dict(w=384, h=104, layout="card", lead=lead, title=title or "等你处理", detail=src, buttons=acts,
                    accent="amber", glow="amber", key=key + ":card")
    # summary / info（有按钮就用高一点的卡片，按钮不压字）
    return dict(w=384, h=104 if acts else 80, layout="card", lead=lead, title=title, detail=p.get("detail") or src,
                buttons=acts[:2], accent="blue", key=key)


def radius_for(desc):
    return 18.5 if desc["layout"] == "pill" else (24 if desc["layout"] == "list" else 30)


# ---------------------------------------------------------------- 字体与小图形

_FONTS = {}


def font(size, bold=False):
    key = (round(size), bold)
    if key not in _FONTS:
        names = ["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc", "msyhl.ttc"]
        f = None
        for name in names + ["segoeui.ttf", "arial.ttf"]:
            try:
                f = ImageFont.truetype(os.path.join(FONT_DIR, name), key[0])
                break
            except OSError:
                continue
        _FONTS[key] = f or ImageFont.load_default()
    return _FONTS[key]


def fit(text, fnt, max_w):
    """一行放不下就截断加省略号。"""
    text = str(text or "")
    if fnt.getlength(text) <= max_w:
        return text
    while text and fnt.getlength(text + "…") > max_w:
        text = text[:-1]
    return text + "…"


def _ss(size, draw_fn, scale=4):
    """4 倍超采样画一个小图形再缩小（抗锯齿）。draw_fn(draw, s) 用放大后的坐标画。"""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(img), scale)
    return img.resize((w, h), Image.LANCZOS)


_ICON_CACHE = {}


def icon_image(source, px, icons=None):
    """来源图标：有缓存的应用图标用图标，没有就“首字 + 品牌色”的圆形头像。"""
    key = (source, px)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    path = (icons or {}).get(source)
    img = None
    if path and os.path.exists(path):
        try:
            src = Image.open(path).convert("RGBA").resize((px * 4, px * 4), Image.LANCZOS)
            mask = _ss((px, px), lambda d, s: d.rounded_rectangle([0, 0, px * s - 1, px * s - 1], radius=px * s * 0.24,
                                                                   fill=(255, 255, 255, 255)))
            img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
            img.paste(src.resize((px, px), Image.LANCZOS), (0, 0), mask.getchannel("A"))
        except Exception:
            img = None
    if img is None:
        color = BRAND.get(source) or _hash_color(source)
        ch = (source or "?")[:1].upper()

        def draw(d, s):
            d.ellipse([0, 0, px * s - 1, px * s - 1], fill=color + (255,))
            f = font(px * s * 0.5, bold=True)
            d.text((px * s / 2, px * s / 2), ch, font=f, fill=(255, 255, 255, 255), anchor="mm")
        img = _ss((px, px), draw)
    _ICON_CACHE[key] = img
    return img


def _hash_color(name):
    palette = [(0, 122, 255), (52, 199, 89), (255, 149, 0), (175, 82, 222), (255, 45, 85), (48, 176, 199), (162, 132, 94)]
    h = 0
    for ch in name or "":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return palette[h % len(palette)]


def ring(px, frac, color, track):
    """圆环进度（frac 为 0~1，None 时画转圈的短弧）。"""
    def draw(d, s):
        w = px * s
        lw = max(2, int(w * 0.13))
        box = [lw / 2, lw / 2, w - lw / 2 - 1, w - lw / 2 - 1]
        d.ellipse(box, outline=track, width=lw)
        if frac is None:
            start = (time.time() * 360) % 360
            d.arc(box, start, start + 90, fill=color + (255,), width=lw)
        elif frac > 0:
            d.arc(box, -90, -90 + 360 * min(1.0, frac), fill=color + (255,), width=lw)
    return _ss((px, px), draw)


def check(px, t, color):
    """对勾，t 为 0~1 的描线进度。"""
    def draw(d, s):
        w = px * s
        d.ellipse([0, 0, w - 1, w - 1], fill=color + (255,))
        pts = [(w * 0.28, w * 0.53), (w * 0.44, w * 0.68), (w * 0.73, w * 0.36)]
        seg1 = math.dist(pts[0], pts[1])
        seg2 = math.dist(pts[1], pts[2])
        length = (seg1 + seg2) * max(0.0, min(1.0, t))
        lw = max(2, int(w * 0.12))
        if length > 0:
            if length <= seg1:
                f = length / seg1
                d.line([pts[0], (pts[0][0] + (pts[1][0] - pts[0][0]) * f, pts[0][1] + (pts[1][1] - pts[0][1]) * f)],
                       fill=(255, 255, 255, 255), width=lw, joint="curve")
            else:
                f = (length - seg1) / seg2
                d.line([pts[0], pts[1], (pts[1][0] + (pts[2][0] - pts[1][0]) * f, pts[1][1] + (pts[2][1] - pts[1][1]) * f)],
                       fill=(255, 255, 255, 255), width=lw, joint="curve")
    return _ss((px, px), draw)


def glyph(px, ch, color):
    def draw(d, s):
        w = px * s
        d.ellipse([0, 0, w - 1, w - 1], fill=color + (255,))
        d.text((w / 2, w / 2 + w * 0.02), ch, font=font(w * 0.62, bold=True), fill=(255, 255, 255, 255), anchor="mm")
    return _ss((px, px), draw)


def wave_bars(w, h, phase, level, color):
    def draw(d, s):
        n, gap = 5, w * s / 5
        lvl = max(0.18, min(1.0, level or 0.35))
        for i in range(n):
            amp = (0.35 + 0.65 * abs(math.sin(phase + i * 0.9))) * lvl
            bh = max(3 * s, amp * h * s)
            x = i * gap + gap * 0.22
            d.rounded_rectangle([x, (h * s - bh) / 2, x + gap * 0.56, (h * s + bh) / 2], radius=gap * 0.28, fill=color + (255,))
    return _ss((w, h), draw)


def pill_button(w, h, label, fg, bg, fnt):
    def draw(d, s):
        d.rounded_rectangle([0, 0, w * s - 1, h * s - 1], radius=h * s / 2, fill=bg)
    img = _ss((w, h), draw)
    ImageDraw.Draw(img).text((w / 2, h / 2), label, font=fnt, fill=fg + (255,), anchor="mm")
    return img


# ---------------------------------------------------------------- 内容层

def draw_content(desc, w, h, dpr, theme, anim, icons=None):
    """画岛里面的内容（不含玻璃底）。返回 (RGBA 图, 按钮热区列表[(id, x0, y0, x1, y1) 逻辑像素，相对岛左上角])。"""
    th, ac = THEMES[theme], ACCENTS[theme]
    W, H = max(1, int(round(w * dpr))), max(1, int(round(h * dpr)))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hot = []
    accent = ac.get(desc.get("accent") or "blue", ac["blue"])
    ink, muted = th["ink"], th["muted"]
    lay = desc["layout"]
    if lay == "hidden":
        return img, hot
    if lay == "list":
        y = 12
        for row in desc.get("rows", []):
            ico = icon_image(row.get("source", ""), int(28 * dpr), icons)
            img.alpha_composite(ico, (int(14 * dpr), int((y + 6) * dpr)))
            f1, f2 = font(13 * dpr, True), font(11.5 * dpr)
            d = ImageDraw.Draw(img)
            title = row.get("title") or ""
            d.text((52 * dpr, (y + 7) * dpr), fit(title, f1, (w - 70) * dpr), font=f1, fill=ink + (255,))
            sub = (row.get("source") or "") + (" · " + row["detail"] if row.get("detail") else "")
            d.text((52 * dpr, (y + 24) * dpr), fit(sub, f2, (w - 70) * dpr), font=f2, fill=muted + (255,))
            hot.append(("row:" + row.get("id", ""), 0, y, w, y + 44))
            y += 44
        if not desc.get("rows"):
            ImageDraw.Draw(img).text((w / 2 * dpr, (y + 4) * dpr), "没有新提示", font=font(12 * dpr), fill=muted + (255,), anchor="mt")
        # 快捷操作一排小按钮
        fq = font(11.5 * dpr, True)
        qx, qy, qh = 12, h - 12 - 26, 26
        qw = (w - 24 - 6 * (len(desc.get("quick", [])) - 1)) / max(1, len(desc.get("quick", [])))
        for qid, label in desc.get("quick", []):
            bg = tuple(th["chip"][:3]) + (int(255 * th["chip"][3] * (1.7 if anim.get("hover_btn") == qid else 1)),)
            img.alpha_composite(pill_button(int(qw * dpr), int(qh * dpr), label, ink, bg, fq), (int(qx * dpr), int(qy * dpr)))
            hot.append((qid, qx, qy, qx + qw, qy + qh))
            qx += qw + 6
        return img, hot
    pad = 10 if lay == "pill" else 16
    lead = desc.get("lead")
    icon_px = 22 if lay == "pill" else 34
    x = pad
    cy = h / 2 if lay == "pill" else (16 + icon_px / 2)
    if lead:
        if lead == "wave":
            bars = wave_bars(int(30 * dpr), int(16 * dpr), anim.get("phase", 0), anim.get("level", 0.4), ac["green"])
            img.alpha_composite(bars, (int(x * dpr), int((cy - 8) * dpr)))
            x += 36
        elif lead == "breath":
            s = 0.8 + 0.35 * (0.5 + 0.5 * math.sin(anim.get("phase", 0) * 0.7))
            r = 5 * s
            dot = _ss((int(16 * dpr), int(16 * dpr)), lambda d, k: d.ellipse(
                [(8 - r) * dpr * k, (8 - r) * dpr * k, (8 + r) * dpr * k, (8 + r) * dpr * k], fill=ac["teal"] + (255,)))
            img.alpha_composite(dot, (int((x + 2) * dpr), int((cy - 8) * dpr)))
            x += 26
        elif isinstance(lead, str) and lead.startswith("glyph:"):
            g = glyph(int(icon_px * dpr), lead[6:], accent)
            img.alpha_composite(g, (int(x * dpr), int((cy - icon_px / 2) * dpr)))
            x += icon_px + 10
        elif isinstance(lead, tuple) and lead[0] == "icon":
            pop = anim.get("icon_pop", 1.0)
            px = max(1, int(icon_px * dpr * pop))
            ico = icon_image(lead[1], px, icons)
            img.alpha_composite(ico, (int((x + icon_px / 2) * dpr - px / 2), int(cy * dpr - px / 2)))
            x += icon_px + 10
    # 右侧部件
    trail = desc.get("trail")
    right = w - pad
    if trail:
        kind, val = trail
        if kind == "count":
            f = font(12 * dpr, True)
            txt = str(val)
            tw = max(20, f.getlength(txt) / dpr + 12)
            badge = pill_button(int(tw * dpr), int(20 * dpr), txt, (255, 255, 255), accent + (255,), f)
            roll = anim.get("count_roll", 0.0)          # 数字滚动：0 到位，>0 从下面上来
            img.alpha_composite(badge, (int((right - tw) * dpr), int((cy - 10 + roll * 8) * dpr)))
            right -= tw + 8
        elif kind == "dot":
            dot = _ss((int(10 * dpr), int(10 * dpr)), lambda d, s: d.ellipse([0, 0, 10 * dpr * s - 1, 10 * dpr * s - 1],
                                                                             fill=ac["red"] + (255,)))
            img.alpha_composite(dot, (int((right - 14) * dpr), int((cy - 5) * dpr)))
            right -= 22
        elif kind == "dot_amber":
            a = int(255 * (0.55 + 0.45 * math.sin(anim.get("phase", 0) * 0.6)))
            dot = _ss((int(10 * dpr), int(10 * dpr)), lambda d, s: d.ellipse([0, 0, 10 * dpr * s - 1, 10 * dpr * s - 1],
                                                                             fill=ac["amber"] + (a,)))
            img.alpha_composite(dot, (int((right - 14) * dpr), int((cy - 5) * dpr)))
            right -= 22
        elif kind == "ring":
            frac = None if val is None else max(0.0, min(1.0, val / 100.0))
            rg = ring(int(22 * dpr), frac, accent, th["chip_hi"][:3] + (int(255 * th["chip_hi"][3] * 2),))
            img.alpha_composite(rg, (int((right - 22) * dpr), int((cy - 11) * dpr)))
            right -= 30
        elif kind == "check":
            ck = check(int(22 * dpr), anim.get("check_t", 1.0), ac["green"])
            img.alpha_composite(ck, (int((right - 22) * dpr), int((cy - 11) * dpr)))
            right -= 30
        elif kind == "glyph":
            g = glyph(int(22 * dpr), val, accent)
            img.alpha_composite(g, (int((right - 22) * dpr), int((cy - 11) * dpr)))
            right -= 30
    d = ImageDraw.Draw(img)
    if lay == "pill":
        f = font(13.5 * dpr, True)
        d.text((x * dpr, cy * dpr), fit(desc.get("title"), f, max(0, right - x - 4) * dpr), font=f, fill=ink + (255,), anchor="lm")
    else:
        f1, f2 = font(14 * dpr, True), font(12 * dpr)
        tx = x
        title = desc.get("title") or ""
        if lay == "tall":         # 两行字幕
            lines = _wrap(title, f1, (right - tx) * dpr, 2)
            for i, line in enumerate(lines):
                d.text((tx * dpr, (14 + i * 22) * dpr), line, font=f1, fill=ink + (255,))
        else:
            d.text((tx * dpr, 14 * dpr), fit(title, f1, (right - tx) * dpr), font=f1, fill=ink + (255,))
            if desc.get("detail"):
                d.text((tx * dpr, 35 * dpr), fit(desc["detail"], f2, (right - tx) * dpr), font=f2, fill=muted + (255,))
        # 按钮：卡片底部从左边图标下方对齐排开
        bx, by, bh = tx, h - 16 - 28, 28
        for b in desc.get("buttons", []):
            fb = font(12.5 * dpr, True)
            bw = max(64, fb.getlength(b["label"]) / dpr + 28)
            primary = b.get("primary")
            bg = accent + (255,) if primary else tuple(th["chip"][:3]) + (int(255 * th["chip"][3] * (1.6 if anim.get("hover_btn") == b["id"] else 1)),)
            fg = (255, 255, 255) if primary else ink
            img.alpha_composite(pill_button(int(bw * dpr), int(bh * dpr), b["label"], fg, bg, fb), (int(bx * dpr), int(by * dpr)))
            hot.append((b["id"], bx, by, bx + bw, by + bh))
            bx += bw + 8
    return img, hot


def _wrap(text, fnt, max_w, max_lines):
    lines, cur = [], ""
    for ch in text:
        if fnt.getlength(cur + ch) > max_w:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                break
        else:
            cur += ch
    if len(lines) < max_lines and cur:
        lines.append(cur)
    if len(lines) == max_lines and "".join(lines) != text:
        lines[-1] = fit(lines[-1] + "……", fnt, max_w)
    return lines


# ---------------------------------------------------------------- 形状、玻璃、合成

_GRIDS = {}


def _grid(W, H):
    """像素中心坐标网格（按尺寸缓存；动画时尺寸常变，只留最近几个）。"""
    key = (W, H)
    g = _GRIDS.get(key)
    if g is None:
        if len(_GRIDS) > 8:
            _GRIDS.clear()
        yy = (np.arange(H, dtype=np.float32) + 0.5)[:, None] * np.ones((1, W), np.float32)
        xx = np.ones((H, 1), np.float32) * (np.arange(W, dtype=np.float32) + 0.5)[None, :]
        g = _GRIDS[key] = (yy, xx)
    return g


def shape_sdf(W, H, cx, top, w, h, r, sec=None):
    """主胶囊（中心 cx、上沿 top、宽 w 高 h 圆角 r）和可选小圆（sec=(圆心x, 圆心y, 半径, 平滑量)）的距离场，单位：像素。"""
    yy, xx = _grid(W, H)
    cy = top + h / 2
    qx = np.abs(xx - cx) - (w / 2 - r)
    qy = np.abs(yy - cy) - (h / 2 - r)
    d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r
    if sec:
        sx, sy, sr, k = sec
        d2 = np.hypot(xx - sx, yy - sy) - sr
        if k > 0:
            hh = np.clip(0.5 + 0.5 * (d2 - d) / k, 0, 1)
            d = d2 * (1 - hh) + d * hh - k * hh * (1 - hh)     # 平滑并集：两者靠近时像液体一样连起来
        else:
            d = np.minimum(d, d2)
    return d, yy


def tint_backdrop(blurred, theme):
    """模糊背景 → 着色 + 轻微提饱和（苹果毛玻璃的“通透感”）。每次截屏只算一次，之后每帧直接裁用。"""
    th = THEMES[theme]
    t = th["tint_amt"]
    base = blurred * (1 - t) + np.array(th["tint"], np.float32) * t
    lum = base.mean(axis=2, keepdims=True)
    base -= lum
    base *= 1.3
    base += lum
    np.clip(base, 0, 255, out=base)
    return base


def glass(sdf, yy, top, h, base, theme):
    """玻璃层：返回 (预乘 RGB float32, alpha float32, 覆盖率)。base 为着色后的背景（同尺寸），None 时用不透明着色。
    高光和描边只是逐像素的系数，所以三通道运算只有两次。"""
    th = THEMES[theme]
    Hh, Ww = sdf.shape
    cov = np.clip(0.5 - sdf, 0, 1)
    if base is None:
        base = np.empty((Hh, Ww, 3), np.float32)
        base[:] = np.array(th["tint"], np.float32)
        glass_a = cov * th["solid_alpha"]
    else:
        glass_a = cov
    rel = np.clip((yy - top) / max(1.0, h), 0, 1)
    hl = np.clip(1 - np.abs(sdf + 1.3) / 1.1, 0, 1) * np.clip(1 - rel * 2.2, 0, 1) * th["highlight"]
    bc = th["border"]
    edge = np.clip(1 - np.abs(sdf + 0.5) / 0.8, 0, 1) * bc[3]
    k = (1 - hl) * (1 - edge) * glass_a                     # 背景保留的比例（已乘 alpha）
    add = (255 * hl * (1 - edge) + bc[0] * edge) * glass_a   # 高光 / 描边加上的颜色（描边是灰阶，三通道相同）
    rgb = base * k[..., None]
    rgb += add[..., None]
    alpha = glass_a.copy()
    # 柔和投影：形状往下挪 4 像素后的外侧衰减
    sd = np.empty_like(sdf)
    sd[4:] = sdf[:-4]
    sd[:4] = sdf[:1]
    sh = np.clip(1 - np.maximum(sd, 0) / SHADOW, 0, 1)
    sh *= sh
    sh *= th["shadow"]
    alpha += sh * (1 - alpha)
    return rgb, alpha, cov


def compose(sdf, yy, top, h, base, theme, content=None, content_pos=(0, 0), content_alpha=1.0, glow=None,
            glow_amt=0.0, opacity=1.0, glass_layer=None, reuse=False):
    """玻璃底 + 投影 + 高光 + 描边 + 发光 + 内容 → 预乘 alpha 的 BGRA（uint8），可以直接交给 UpdateLayeredWindow。
    glass_layer：算好的 glass()；reuse=True 表示它还要留给下一帧用，这里先复制一份再改。"""
    rgb, alpha, cov = glass_layer if glass_layer is not None else glass(sdf, yy, top, h, base, theme)
    if reuse:
        rgb, alpha = rgb.copy(), alpha.copy()
    Hh, Ww = sdf.shape
    if glow is not None and glow_amt > 0:          # 发光描边（等你处理 / 重要消息），在形状外侧
        g = np.clip(1 - np.maximum(sdf, 0) / 7.0, 0, 1)
        g *= g
        g *= (sdf > -0.5) * glow_amt * (1 - alpha)
        rgb += np.array(glow, np.float32) * g[..., None]
        alpha += g
    if content is not None:                         # 内容：裁进形状里（只处理有内容的那一块）
        x0, y0 = content_pos
        bbox = content.getchannel("A").getbbox() if hasattr(content, "getchannel") else None
        if bbox is not None:
            content = content.crop(bbox)
            x0, y0 = x0 + bbox[0], y0 + bbox[1]
        c = np.asarray(content, np.float32)
        x1, y1 = min(Ww, x0 + c.shape[1]), min(Hh, y0 + c.shape[0])
        sx0, sy0 = max(0, x0), max(0, y0)
        if x1 > sx0 and y1 > sy0:
            cc = c[sy0 - y0:y1 - y0, sx0 - x0:x1 - x0]
            a = cc[..., 3] * (content_alpha / 255.0) * cov[sy0:y1, sx0:x1]
            sub = rgb[sy0:y1, sx0:x1]
            sub *= (1 - a)[..., None]
            sub += cc[..., :3] * a[..., None]
            alpha[sy0:y1, sx0:x1] = a + alpha[sy0:y1, sx0:x1] * (1 - a)
    if opacity < 1:
        rgb *= opacity
        alpha *= opacity
    out = np.empty((Hh, Ww, 4), np.uint8)
    np.clip(rgb, 0, 255, out=rgb)
    out[..., :3] = rgb[..., ::-1]
    out[..., 3] = np.clip(alpha * 255, 0, 255)
    return out


class Scene:
    """岛的动画状态：尺寸弹簧、到达动画（果冻、图标弹出、对勾描线、数字滚动、抖动）、内容交叉淡入淡出、分离小圆。
    update(描述) 换目标；step(dt) 推进；frame(...) 出一帧。时间都由调用方传入，方便测试。"""
    GAP, BUBBLE = 8.0, 37.0

    def __init__(self, reduce_motion=False):
        self.w, self.h, self.r = Spring(126), Spring(37), Spring(18.5)
        self.press = Spring(1, 0.35, 0.7)
        self.sec = Spring(0, 0.5, 0.62)
        self.icon_pop = Spring(1, 0.42, 0.52)
        self.reduce_motion = reduce_motion
        self.desc = describe({"mode": "idle"})
        self.key = self.desc["key"]
        self.prev = None               # (描述, 开始淡出的时间)
        self.fade_start = -10.0
        self.effects = {}              # 名称 → 开始时间
        self.count = None
        self.phase = 0.0
        self.level = 0.35
        self.hover_btn = None
        self.now = 0.0
        self._content_cache = {}

    # ---- 目标
    def update(self, desc, now, arrive=False):
        self.now = now
        new_key = desc["key"]
        if desc["layout"] == "hidden":
            self.desc, self.key = desc, new_key
            return
        resp, damp = 0.55, 0.825
        if arrive and not self.reduce_motion:
            resp, damp = 0.5, 0.58                     # 到达：弹一下（果冻）
            self.icon_pop.snap(0.6)
            self.icon_pop.set(1.0)
            if desc.get("trail") and desc["trail"][0] == "check":
                self.effects["check"] = now
        if desc.get("shake") and new_key != self.key and not self.reduce_motion:
            self.effects["shake"] = now
        trail = desc.get("trail")
        if trail and trail[0] == "count":
            if self.count is not None and trail[1] != self.count and new_key == self.key:
                self.effects["roll"] = now
            self.count = trail[1]
        if new_key != self.key:
            self.prev, self.fade_start = self.desc, now
        self.desc, self.key = desc, new_key
        for s, v in ((self.w, desc["w"]), (self.h, desc["h"]), (self.r, radius_for(desc))):
            s.set(v, resp, damp)
        self.sec.set(1.0 if desc.get("secondary") else 0.0)
        if self.reduce_motion:
            for s in (self.w, self.h, self.r, self.sec, self.icon_pop):
                s.snap(s.target)

    # ---- 推进
    def step(self, dt, now):
        self.now = now
        moving = False
        for s in (self.w, self.h, self.r, self.press, self.sec, self.icon_pop):
            s.step(dt)
            moving = moving or not s.resting()
        self.phase += dt * 9
        for name, dur in (("check", 0.45), ("roll", 0.3), ("shake", 0.42)):
            if name in self.effects and now - self.effects[name] > dur:
                del self.effects[name]
        moving = moving or bool(self.effects) or (now - self.fade_start) < 0.25
        return moving

    def shape_resting(self):
        return all(s.resting() for s in (self.w, self.h, self.r, self.press, self.sec)) and "shake" not in self.effects

    def animated_content(self):
        d = self.desc
        lead, trail = d.get("lead"), d.get("trail")
        return lead in ("wave", "breath") or bool(trail and (trail[0] == "dot_amber" or (trail[0] == "ring" and trail[1] is None)))

    def needs_frames(self):
        """静止时是否还要定时出帧（内容在动、或者有呼吸发光）。"""
        return self.animated_content() or bool(self.desc.get("glow"))

    # ---- 几何
    def geometry(self):
        """返回岛（和小圆）在窗口里的逻辑坐标：主胶囊 (x0, y0, w, h, r)、小圆 (cx, cy, 半径, 平滑量) 或 None。"""
        w, h = max(0.0, self.w.value), max(0.0, self.h.value)
        r = min(self.r.value, h / 2)
        s = max(0.0, min(1.2, self.sec.value))
        shift = (self.GAP + self.BUBBLE) / 2 * s
        cx = WIN_W / 2 - shift
        if "shake" in self.effects:
            t = (self.now - self.effects["shake"]) / 0.42
            cx += 4 * math.sin(t * math.pi * 6) * (1 - t)
        x0 = cx - w / 2
        sec = None
        if s > 0.01:
            br = self.BUBBLE / 2 * (0.4 + 0.6 * min(1.0, s))
            bcx = x0 + w - self.BUBBLE / 2 + (self.GAP + self.BUBBLE) * s
            sec = (bcx, TOP_MARGIN + h / 2 if h < 60 else TOP_MARGIN + 18.5, br, 16 * (1 - min(1.0, s)) + 3)
        return (x0, TOP_MARGIN, w, h, r), sec

    def bounds(self):
        (x0, y0, w, h, r), sec = self.geometry()
        x1 = x0 + w
        if sec:
            x1 = max(x1, sec[0] + sec[2])
        return x0, y0, x1, y0 + h

    # ---- 出一帧
    def frame(self, dpr, theme, backdrop=None, icons=None, glow_pulse=None):
        """返回 (整窗 BGRA, 热区[(id, x0, y0, x1, y1) 窗口逻辑坐标])。
        backdrop 为整窗大小、已经 tint_backdrop() 着色过的模糊背景（物理像素）；None 时用不透明的仿玻璃。"""
        Wp, Hp = int(round(WIN_W * dpr)), int(round(WIN_H * dpr))
        out = np.zeros((Hp, Wp, 4), np.uint8)
        if self.desc["layout"] == "hidden" or self.w.value < 2:
            return out, []
        (x0, y0, w, h, r), sec = self.geometry()
        press = self.press.value
        # 只算岛周围一圈（加投影 / 发光）
        m = SHADOW + 10
        bx0 = max(0, int((x0 - m) * dpr))
        by0 = 0
        bx1 = min(Wp, int((self.bounds()[2] + m) * dpr) + 1)
        by1 = min(Hp, int((y0 + h + m) * dpr) + 1)
        if bx1 <= bx0 or by1 <= by0:
            return out, []
        cw, chh = bx1 - bx0, by1 - by0
        cx = (x0 + w / 2) * dpr - bx0
        top = y0 * dpr - by0
        pw, ph = w * dpr * press, h * dpr * press
        secp = None
        if sec:
            secp = ((sec[0]) * dpr - bx0, sec[1] * dpr - by0, sec[2] * dpr, sec[3] * dpr)
        gkey = (bx0, by0, bx1, by1, round(cx, 2), round(top, 2), round(pw, 2), round(ph, 2), round(r * dpr * press, 2),
                None if secp is None else tuple(round(v, 2) for v in secp), theme, id(backdrop))
        reuse = getattr(self, "_glass_key", None) == gkey
        if reuse:
            sdf, yy, glayer = self._glass_val
        else:
            sdf, yy = shape_sdf(cw, chh, cx, top + (h * dpr - ph) / 2, pw, ph, r * dpr * press, secp)
            bd = None if backdrop is None else backdrop[by0:by1, bx0:bx1]
            glayer = glass(sdf, yy, top, h * dpr, bd, theme)
            if self.shape_resting():           # 形状停下来了：留给后面的帧复用（这一帧也要先复制）
                self._glass_key, self._glass_val = gkey, (sdf, yy, glayer)
                reuse = True
            else:
                self._glass_key = None
        # 内容（交叉淡入淡出：新内容 0.22 秒内淡入、轻微放大到位，旧内容淡出）
        t = min(1.0, (self.now - self.fade_start) / 0.22)
        anim = {"phase": self.phase, "level": self.level, "icon_pop": self.icon_pop.value, "hover_btn": self.hover_btn,
                "check_t": min(1.0, (self.now - self.effects["check"]) / 0.45) if "check" in self.effects else 1.0,
                "count_roll": 1 - (self.now - self.effects["roll"]) / 0.3 if "roll" in self.effects else 0.0}
        img, hot = self._content(self.desc, dpr, theme, anim, icons)
        content = img
        calpha = t
        if self.prev is not None and t < 1:
            pimg, _ = self._content(self.prev, dpr, theme, anim, icons)
            blend = Image.new("RGBA", (max(img.width, pimg.width), max(img.height, pimg.height)), (0, 0, 0, 0))
            po = Image.eval(pimg, lambda v: v)
            pa = po.getchannel("A").point(lambda v: int(v * (1 - t)))
            po.putalpha(pa)
            if t < 0.6:
                po = po.filter(ImageFilter.GaussianBlur(t * 4 * dpr))
            blend.alpha_composite(po, (int((blend.width - pimg.width) / 2), 0))
            na = img.getchannel("A").point(lambda v: int(v * t))
            ni = img.copy()
            ni.putalpha(na)
            if t < 0.6:
                ni = ni.filter(ImageFilter.GaussianBlur((1 - t) * 3 * dpr))
            blend.alpha_composite(ni, (int((blend.width - img.width) / 2), 0))
            content, calpha = blend, 1.0
        elif t >= 1:
            self.prev = None
        # 内容按岛的中心水平对齐、上沿对齐
        cpos = (int(cx - content.width / 2), int(top))
        glow_c, glow_amt = None, 0.0
        g = self.desc.get("glow")
        if g:
            name = "amber" if g.startswith("amber") else g
            glow_c = ACCENTS[theme].get(name, ACCENTS[theme]["blue"])
            pulse = 0.5 + 0.5 * math.sin(self.phase * (0.35 if g == "amber_soft" else 0.5)) if glow_pulse is None else glow_pulse
            glow_amt = (0.25 if g == "amber_soft" else 0.55) * (0.45 + 0.55 * pulse)
        canvas = Image.new("RGBA", (cw, chh), (0, 0, 0, 0))
        if calpha < 1:
            content = content.copy()
            content.putalpha(content.getchannel("A").point(lambda v: int(v * calpha)))
        canvas.alpha_composite(content, (max(0, cpos[0]), max(0, cpos[1])),
                               (max(0, -cpos[0]), max(0, -cpos[1])))
        if secp and self.desc.get("secondary") and self.sec.value > 0.6:
            px = max(1, int(secp[2] * 1.15))
            ico = icon_image(self.desc["secondary"].get("source", ""), px, icons)
            canvas.alpha_composite(ico, (int(secp[0] - px / 2), int(secp[1] - px / 2)))
        block = compose(sdf, yy, top, h * dpr, None, theme, canvas, (0, 0), 1.0, glow_c, glow_amt, glass_layer=glayer,
                        reuse=reuse)
        out[by0:by1, bx0:bx1] = block
        ox = cpos[0] + bx0
        hot_win = [(i, ox / dpr + a, (cpos[1] + by0) / dpr + b, ox / dpr + c, (cpos[1] + by0) / dpr + d) for i, a, b, c, d in hot]
        if sec:
            hot_win.append(("secondary", sec[0] - sec[2], sec[1] - sec[2], sec[0] + sec[2], sec[1] + sec[2]))
        return out, hot_win

    def _content(self, desc, dpr, theme, anim, icons):
        animated = desc is self.desc and (self.animated_content() or anim["check_t"] < 1 or anim["count_roll"] > 0
                                          or anim["icon_pop"] != 1.0 or anim.get("hover_btn"))
        key = (desc["key"], desc["w"], desc["h"], dpr, theme, desc.get("title"), desc.get("detail"), str(desc.get("trail")))
        if not animated and key in self._content_cache:
            return self._content_cache[key]
        res = draw_content(desc, desc["w"], desc["h"], dpr, theme, anim, icons)
        if not animated:
            if len(self._content_cache) > 24:
                self._content_cache.clear()
            self._content_cache[key] = res
        return res


def blur_backdrop(img, scale=6, radius=4):
    """屏幕截图 → 缩小、模糊、放大（快，且看不清任何细节）。返回 float32 RGB。"""
    w, h = img.size
    small = img.convert("RGB").resize((max(1, w // scale), max(1, h // scale)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(small.resize((w, h), Image.BICUBIC), np.float32)
