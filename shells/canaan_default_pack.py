# -*- coding: utf-8 -*-
"""原创默认形象“团团”：用代码画出来的圆形小精灵（头顶一片小芽），生成标准 V2 形象包。

- 不用任何外部素材，版权干净：公开版默认就用它；自用版没有 72 态图片时也用它。
- 生成一张 1536×2288 的图集（8 列 × 11 行，每格 192×208）+ pet.json，放在 data/packs/tuantuan/。
- 9 个核心动作各一行（idle 6 帧，其他 8 帧），第 9–10 行是 16 个注视方向（从正上方顺时针）。
- 画法改了就把 GENERATOR_VERSION 加一，下次启动自动重画。
"""
import json
import math
import os

from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKS_DIR = os.path.join(BASE_DIR, "data", "packs")
PACK_ID = "tuantuan"
GENERATOR_VERSION = 1
CELL_W, CELL_H, COLS, ROWS = 192, 208, 8, 11
ACTIONS = ("idle", "running-right", "running-left", "waving", "jumping", "failed", "waiting", "running", "review")

BODY = (148, 214, 196, 255)
BODY_SAD = (170, 190, 186, 255)
OUTLINE = (52, 92, 84, 255)
EYE = (34, 44, 52, 255)
BLUSH = (246, 160, 160, 170)
LEAF = (96, 176, 104, 255)


def _ellipse(d, cx, cy, rx, ry, **kw):
    d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), **kw)


def draw(action="idle", frame=0, frames=8, gaze=None):
    """画一格。gaze：0–15 的注视方向或 None。"""
    img = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    phase = frame / max(1, frames) * 2 * math.pi
    cx, base = 96, 188
    rx, ry, dy, lean = 58, 52, 0, 0
    if action == "idle":
        ry += 2 * math.sin(phase)
    elif action in ("running-right", "running-left"):
        dy = -abs(math.sin(phase)) * 10
        lean = 6 if action == "running-right" else -6
        cx += lean
    elif action == "jumping":
        dy = -abs(math.sin(phase)) * 34
        ry -= 4 * (1 - abs(math.sin(phase)))
    elif action == "failed":
        ry -= 4
    cy = base - ry + dy
    body = BODY_SAD if action == "failed" else BODY
    # 影子
    shadow = max(20, 46 - int(-dy * 0.6))
    _ellipse(d, 96, base + 6, shadow, 6, fill=(0, 0, 0, 50))
    # 身体
    _ellipse(d, cx, cy, rx, ry, fill=body, outline=OUTLINE, width=3)
    # 小芽
    stem_top = cy - ry - 16
    d.line((cx, cy - ry + 2, cx + lean * 0.5, stem_top), fill=OUTLINE, width=3)
    sway = 4 * math.sin(phase) if action in ("idle", "waiting") else 0
    _ellipse(d, cx + 9 + sway, stem_top + 2, 10, 5, fill=LEAF, outline=OUTLINE, width=2)
    _ellipse(d, cx - 8 + sway, stem_top + 5, 8, 4, fill=LEAF, outline=OUTLINE, width=2)
    # 眼睛
    ex, ey = 20, cy - 4
    look = (0, 0)
    if gaze is not None:
        ang = math.radians(gaze * 22.5)
        look = (math.sin(ang) * 6, -math.cos(ang) * 6)
    elif action == "running":
        look = (0, 5)
    elif action == "review":
        look = (5, 2)
    blink = action == "idle" and frame == frames - 2
    for side in (-1, 1):
        x = cx + side * ex
        if action == "failed":
            d.line((x - 7, ey - 7, x + 7, ey + 7), fill=EYE, width=4)
            d.line((x - 7, ey + 7, x + 7, ey - 7), fill=EYE, width=4)
        elif blink:
            d.line((x - 8, ey, x + 8, ey), fill=EYE, width=4)
        else:
            _ellipse(d, x + look[0], ey + look[1], 8, 10, fill=EYE)
            _ellipse(d, x + look[0] + 3, ey + look[1] - 4, 3, 3, fill=(255, 255, 255, 255))
        _ellipse(d, x + side * 10, ey + 16, 8, 4, fill=BLUSH)
    # 嘴
    if action == "failed":
        d.arc((cx - 9, ey + 18, cx + 9, ey + 30), 200, 340, fill=EYE, width=3)
    elif action in ("jumping", "waving"):
        _ellipse(d, cx, ey + 20, 6, 5, fill=(150, 60, 70, 255))
    else:
        d.arc((cx - 7, ey + 10, cx + 7, ey + 22), 20, 160, fill=EYE, width=3)
    # 各动作的小道具
    if action == "waving":
        hy = cy - 10 - 14 * math.sin(phase)
        _ellipse(d, cx + rx + 4, hy, 11, 11, fill=body, outline=OUTLINE, width=3)
    elif action == "failed":
        _ellipse(d, cx + rx - 8, cy - ry + 16, 5, 8, fill=(140, 190, 240, 220))
    elif action == "waiting":
        shown = frame * 4 // frames
        for i in range(3):
            if i < shown:
                _ellipse(d, cx - 18 + i * 18, cy - ry - 30, 5, 5, fill=OUTLINE)
    elif action == "running":
        d.rectangle((cx - 34, base - 26, cx + 34, base - 6), fill=(80, 90, 110, 255), outline=OUTLINE, width=2)
        if frame % 2:
            d.line((cx - 20, base - 16, cx + 20, base - 16), fill=(160, 220, 255, 255), width=2)
    elif action == "review":
        mx, my = cx + 30, ey + 6
        _ellipse(d, mx, my, 15, 15, outline=OUTLINE, width=4)
        d.line((mx + 10, my + 10, mx + 24, my + 26), fill=OUTLINE, width=5)
    return img


def build_atlas():
    sheet = Image.new("RGBA", (COLS * CELL_W, ROWS * CELL_H), (0, 0, 0, 0))
    for row, action in enumerate(ACTIONS):
        frames = 6 if action == "idle" else COLS
        for col in range(frames):
            sheet.paste(draw(action, col, frames), (col * CELL_W, row * CELL_H))
    sheet.paste(draw("idle", 0, 6), (6 * CELL_W, 0))          # V2 第 0 行第 6 格：正面待机
    for i in range(16):
        r, c = 9 + i // COLS, i % COLS
        sheet.paste(draw("idle", 0, 6, gaze=i), (c * CELL_W, r * CELL_H))
    return sheet


def ensure_default_pack(packs_dir=PACKS_DIR):
    """没有就生成（或画法版本变了就重画），返回 pet.json 路径。"""
    folder = os.path.join(packs_dir, PACK_ID)
    manifest_path = os.path.join(folder, "pet.json")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            if json.load(f).get("generatorVersion") == GENERATOR_VERSION and os.path.exists(os.path.join(folder, "spritesheet.png")):
                return manifest_path
    except (OSError, ValueError):
        pass
    os.makedirs(folder, exist_ok=True)
    build_atlas().save(os.path.join(folder, "spritesheet.png"))
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"id": PACK_ID, "displayName": "团团（默认原创形象）", "spriteVersionNumber": 2,
                   "spritesheetPath": "spritesheet.png", "generatorVersion": GENERATOR_VERSION,
                   "license": "与本项目代码同一许可，可自由使用"}, f, ensure_ascii=False, indent=1)
    return manifest_path


if __name__ == "__main__":
    print(ensure_default_pack())
