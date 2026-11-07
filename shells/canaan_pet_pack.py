# -*- coding: utf-8 -*-
"""桌宠形象包加载：兼容共创平台（desktop-pet-community）的 pet.json 格式。代码自己写，不含对方素材。

支持：
- V1 图集：8 列 × 9 行，每格 192×208（1536×1872）。
- V2 图集：`"spriteVersionNumber": 2`，8 列 × 11 行（1536×2288）；第 9–10 行是 16 个注视方向。
- mediaVersion 1 动作包：`actions`（动作 ID → 相对路径的 .webp / .mp4 / .webm）、`stateBindings`、`background`、`staticActions`。
- 9 个核心动作缺了的都退回 idle；原来的 72 态 PNG 作为“高级包”继续保留（不加载形象包时就用它）。

格式文档没写清、这里按推断实现的地方（都可以在本文件开头改）：
- 图集第 0–8 行依次对应 CORE_ACTIONS 的顺序；每行从左往右取非空格子作为帧，第 0 行最多 6 帧（V2 第 0 行第 6 格是正面待机）。
- 16 个注视方向从正上方开始顺时针排列（GAZE_START_DEG = 0），每格 22.5°。
- 图集帧率：每行一圈约 1 秒。

全部在 CPU 上解码（PIL；视频用 OpenCV CPU 解码），不占显存。
"""
import io
import json
import math
import os
import re
import tempfile
import zipfile
from pathlib import PurePosixPath

from PIL import Image

CELL_W, CELL_H, COLS = 192, 208, 8
V1_SIZE, V2_SIZE = (COLS * CELL_W, 9 * CELL_H), (COLS * CELL_W, 11 * CELL_H)
CORE_ACTIONS = ("idle", "running-right", "running-left", "waving", "jumping", "failed", "waiting", "running", "review")
OPTIONAL_ACTIONS = ("sleeping", "clicked", "dragged")
GAZE_START_DEG = 0.0
IDLE_ROW_MAX_FRAMES = 6
MEDIA_FPS = 24
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_FILES, MAX_TOTAL, MAX_FILE = 1000, 1 << 30, 256 << 20
MAX_ACTIONS, MAX_MEDIA_FRAMES = 128, 600
MEDIA_EXTS = (".webp", ".mp4", ".webm")
STATIC_EXTS = (".webp", ".png")

# 团团的 72 态姿态 → 形象包动作。没列出的姿态退回 idle。
CANAAN_TO_PACK = {
    "idle": "idle", "blink": "idle", "talk": "idle", "walk": "running-right", "slide": "running-right",
    "drag": "dragged", "sleep": "sleeping", "sleepy_nod": "sleeping", "yawn": "sleeping",
    "shy": "waving", "phone": "waving", "gift": "waving", "heart_eyes": "waving", "hug_screen": "waving", "selfie": "waving",
    "cheer": "jumping", "dance": "jumping", "rainbow_star": "jumping", "worship": "jumping",
    "crying": "failed", "shock": "failed", "dizzy": "failed", "headache": "failed",
    "work": "running", "typing": "running", "overtime": "running", "blade": "running", "spear": "running",
    "audit": "review", "reading": "review", "detective": "review", "chart": "review",
    "action": "waiting", "mask": "waiting", "box_hide": "waiting",
}
FALLBACK_CHAIN = {"dragged": "jumping", "sleeping": "idle", "clicked": "waving"}


class PackError(ValueError):
    pass


class PetPack:
    def __init__(self, pack_id, name, kind):
        self.id, self.name, self.kind = pack_id, name, kind  # kind: v1 / v2 / media
        self.animations = {}   # 动作 → [(PIL RGBA 192×208, 毫秒)]
        self.bindings = {}
        self.gaze = None       # V2：16 个方向的格子
        self.neutral = None

    @property
    def has_gaze(self):
        return bool(self.gaze) and len(self.gaze) == 16

    def resolve(self, action):
        """状态 / 动作名 → 包里真正有的动作；缺了就按 FALLBACK_CHAIN，最后退回 idle。"""
        seen = set()
        while action and action not in seen:
            seen.add(action)
            bound = self.bindings.get(action)
            if bound in self.animations:
                return bound
            if action in self.animations:
                return action
            action = FALLBACK_CHAIN.get(action)
        return "idle"

    def for_canaan_state(self, key):
        key = (key or "idle").lower()
        if key.startswith("talk"):
            key = "talk"
        return self.resolve(CANAAN_TO_PACK.get(key, "idle"))

    def frame_at(self, action, t):
        """返回 (帧图, 帧序号)；按各帧时长循环播放。"""
        frames = self.animations.get(action) or self.animations["idle"]
        total = sum(ms for _, ms in frames) or 1
        pos = (t * 1000.0) % total
        for i, (img, ms) in enumerate(frames):
            if pos < ms:
                return img, i
            pos -= ms
        return frames[-1][0], len(frames) - 1

    def gaze_frame(self, direction):
        return self.gaze[direction % 16] if self.has_gaze else None


def gaze_direction(dx, dy, dead_zone=36.0, start_deg=GAZE_START_DEG):
    """光标相对锚点（宠物底部中点）的偏移 → 0–15 的方向（从正上方顺时针，每格 22.5°）；离得太近返回 None。"""
    if math.hypot(dx, dy) < dead_zone:
        return None
    deg = (math.degrees(math.atan2(dx, -dy)) - start_deg) % 360.0  # 屏幕 y 向下，正上方为 0°，顺时针增大
    return int(((deg + 11.25) % 360.0) // 22.5)


def mirror_direction(direction):
    """图像水平翻转后，为了仍然看向光标，方向要左右镜像。"""
    return (16 - direction) % 16


# ---------------- 读取包内文件（文件夹 / zip），拒绝不安全路径 ----------------

def _safe_rel(name):
    p = PurePosixPath(name.replace("\\", "/"))
    if not name or p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", name) or name.startswith("/"):
        raise PackError(f"不安全的路径：{name}")
    return str(p)


class _Source:
    def __init__(self, path):
        self.path = path
        self.zip = None
        if os.path.isfile(path) and path.lower().endswith(".zip"):
            self.zip = zipfile.ZipFile(path)
            infos = [i for i in self.zip.infolist() if not i.is_dir()]
            if len(infos) > MAX_FILES:
                raise PackError("文件太多（最多 1000 个）")
            if sum(i.file_size for i in infos) > MAX_TOTAL:
                raise PackError("包太大（最多 1 GiB）")
            for info in infos:
                _safe_rel(info.filename)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PackError(f"包里不能有链接：{info.filename}")
            roots = [i.filename for i in infos if PurePosixPath(i.filename).name == "pet.json"]
            roots = [r for r in roots if len(PurePosixPath(r).parts) <= 2]
            if not roots:
                raise PackError("没找到 pet.json（要在包的根目录，或者只套一层文件夹）")
            self.prefix = str(PurePosixPath(sorted(roots, key=len)[0]).parent)
            self.prefix = "" if self.prefix == "." else self.prefix + "/"
        else:
            base = os.path.dirname(path) if os.path.isfile(path) else path
            if not os.path.isfile(os.path.join(base, "pet.json")):
                raise PackError("文件夹里没有 pet.json")
            self.base = os.path.realpath(base)

    def read(self, rel):
        rel = _safe_rel(rel)
        if self.zip is not None:
            try:
                info = self.zip.getinfo(self.prefix + rel)
            except KeyError:
                raise PackError(f"包里缺文件：{rel}")
            if info.file_size > MAX_FILE:
                raise PackError(f"文件太大：{rel}")
            return self.zip.read(info)
        full = os.path.realpath(os.path.join(self.base, rel))
        if not full.startswith(self.base + os.sep) or os.path.islink(os.path.join(self.base, rel)):
            raise PackError(f"不安全的路径：{rel}")
        if not os.path.isfile(full):
            raise PackError(f"包里缺文件：{rel}")
        if os.path.getsize(full) > MAX_FILE:
            raise PackError(f"文件太大：{rel}")
        with open(full, "rb") as f:
            return f.read()

    def close(self):
        if self.zip is not None:
            self.zip.close()


# ---------------- 解码 ----------------

def _fit(img):
    """缩放到 192×208 画布内，底部居中（锚点在脚下）。"""
    img = img.convert("RGBA")
    if img.size != (CELL_W, CELL_H):
        scale = min(CELL_W / img.width, CELL_H / img.height)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((CELL_W - img.width) // 2, CELL_H - img.height))
    return canvas


def _chroma(img, color, tolerance):
    import numpy as np
    arr = np.array(img.convert("RGBA"))
    diff = np.abs(arr[..., :3].astype(np.int16) - np.array(color, dtype=np.int16))
    arr[(diff <= tolerance).all(axis=-1), 3] = 0
    return Image.fromarray(arr, "RGBA")


def _parse_color(value):
    m = re.match(r"^#?([0-9a-fA-F]{6})$", str(value or ""))
    if not m:
        raise PackError("background.color 须为 #RRGGBB")
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _decode_media(data, ext, background, allow_static):
    frames = []
    if ext in (".webp", ".png"):
        im = Image.open(io.BytesIO(data))
        n = getattr(im, "n_frames", 1)
        if n == 1 and not allow_static:
            raise PackError("单帧图片要在 staticActions 里声明")
        if n > MAX_MEDIA_FRAMES:
            raise PackError("动画帧太多（最多 600 帧）")
        for i in range(n):
            im.seek(i)
            frames.append((im.convert("RGBA"), int(im.info.get("duration") or 1000 / MEDIA_FPS)))
    else:
        import cv2  # CPU 解码
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            cap = cv2.VideoCapture(tmp_path)
            fps = cap.get(cv2.CAP_PROP_FPS) or MEDIA_FPS
            step = max(1, round(fps / MEDIA_FPS))
            i = 0
            while len(frames) < MAX_MEDIA_FRAMES:
                ok, bgr = cap.read()
                if not ok:
                    break
                if i % step == 0:
                    frames.append((Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).convert("RGBA"), round(1000 * step / fps)))
                i += 1
            cap.release()
        finally:
            os.unlink(tmp_path)
        if not frames:
            raise PackError("视频读不出画面")
    if background and background.get("mode") == "chroma":
        color = _parse_color(background.get("color"))
        tol = int(background.get("tolerance", 40))
        frames = [(_chroma(img, color, tol), ms) for img, ms in frames]
    return [(_fit(img), max(16, ms)) for img, ms in frames]


def _row_frames(sheet, row, limit=COLS):
    frames = []
    for col in range(COLS):
        cell = sheet.crop((col * CELL_W, row * CELL_H, (col + 1) * CELL_W, (row + 1) * CELL_H))
        if cell.getchannel("A").getbbox() is None:
            break
        frames.append(cell)
        if len(frames) >= limit:
            break
    return frames


def _load_atlas(pack, src, manifest):
    sheet = Image.open(io.BytesIO(src.read(manifest.get("spritesheetPath") or "spritesheet.webp"))).convert("RGBA")
    v2 = manifest.get("spriteVersionNumber") == 2
    if sheet.size != (V2_SIZE if v2 else V1_SIZE):
        raise PackError(f"图集尺寸 {sheet.size[0]}×{sheet.size[1]} 不对：{'V2 应为 1536×2288' if v2 else 'V1 应为 1536×1872'}")
    pack.kind = "v2" if v2 else "v1"
    for row, action in enumerate(CORE_ACTIONS):
        frames = _row_frames(sheet, row, IDLE_ROW_MAX_FRAMES if row == 0 else COLS)
        if frames:
            ms = max(80, min(200, round(1000 / len(frames))))
            pack.animations[action] = [(f, ms) for f in frames]
    if v2:
        pack.neutral = sheet.crop((6 * CELL_W, 0, 7 * CELL_W, CELL_H))
        pack.gaze = [sheet.crop((c * CELL_W, r * CELL_H, (c + 1) * CELL_W, (r + 1) * CELL_H)) for r in (9, 10) for c in range(COLS)]
    if "idle" not in pack.animations:
        if pack.neutral is not None and pack.neutral.getchannel("A").getbbox():
            pack.animations["idle"] = [(pack.neutral, 1000)]
        else:
            raise PackError("图集第 0 行（idle）是空的")


def _load_media(pack, src, manifest):
    actions = manifest.get("actions")
    if not isinstance(actions, dict) or not actions or len(actions) > MAX_ACTIONS:
        raise PackError("actions 须为 1–128 个动作")
    static = set(manifest.get("staticActions") or [])
    background = manifest.get("background") or {"mode": "alpha"}
    if background.get("mode") not in ("alpha", "chroma"):
        raise PackError("background.mode 须为 alpha 或 chroma")
    for action, rel in actions.items():
        if not isinstance(action, str) or not isinstance(rel, str):
            raise PackError("actions 的键和值都须为字符串")
        ext = os.path.splitext(rel)[1].lower()
        if ext not in MEDIA_EXTS + STATIC_EXTS:
            raise PackError(f"不支持的媒体格式：{rel}")
        pack.animations[action] = _decode_media(src.read(rel), ext, background, action in static)
    pack.kind = "media"
    if "idle" not in pack.animations:
        raise PackError("动作包至少要有 idle")


def load_pack(path):
    """读取形象包（zip、文件夹或 pet.json 路径）。出错抛 PackError，说明原因。"""
    src = _Source(path)
    try:
        try:
            manifest = json.loads(src.read("pet.json").decode("utf-8"))
        except ValueError:
            raise PackError("pet.json 不是合法的 JSON")
        if not isinstance(manifest, dict):
            raise PackError("pet.json 须为对象")
        pack_id = manifest.get("id")
        if not isinstance(pack_id, str) or not ID_RE.match(pack_id) or pack_id == "builtin":
            raise PackError("id 须为小写字母数字开头、最多 64 位的 [a-z0-9_-]，且不能是 builtin")
        pack = PetPack(pack_id, str(manifest.get("displayName") or pack_id)[:60], "")
        bindings = manifest.get("stateBindings") or {}
        if not isinstance(bindings, dict):
            raise PackError("stateBindings 须为对象")
        pack.bindings = {str(k): str(v) for k, v in bindings.items()}
        if manifest.get("mediaVersion") == 1:
            _load_media(pack, src, manifest)
        else:
            _load_atlas(pack, src, manifest)
        return pack
    finally:
        src.close()


# ---------------- 已安装的形象包（data/packs/，导入时复制进来，原文件挪走也不影响） ----------------

def packs_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "packs")


def install_pack(path, target_dir=None):
    """先完整校验，再复制到形象包目录：zip 存成 <id>.zip，文件夹整个复制成 <id>/。返回 (安装后的路径, 包)。"""
    import shutil
    pack = load_pack(path)                      # 不合格直接抛 PackError，什么都不复制
    target_dir = target_dir or packs_dir()
    os.makedirs(target_dir, exist_ok=True)
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        dest = os.path.join(target_dir, pack.id + ".zip")
        if os.path.abspath(path) != os.path.abspath(dest):
            shutil.copyfile(path, dest)
    else:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        dest = os.path.join(target_dir, pack.id)
        if os.path.abspath(folder) != os.path.abspath(dest):
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(folder, dest)
        dest = os.path.join(dest, "pet.json")
    return dest, pack


def list_installed(target_dir=None):
    """只读 pet.json 里的名字，不解码图片。返回 [(id, 显示名, 路径)]，按名字排。"""
    target_dir = target_dir or packs_dir()
    out = []
    try:
        names = os.listdir(target_dir)
    except OSError:
        return out
    for name in names:
        full = os.path.join(target_dir, name)
        try:
            if name.lower().endswith(".zip"):
                with zipfile.ZipFile(full) as z:
                    manifest = json.loads(z.read("pet.json").decode("utf-8"))
                path = full
            elif os.path.isdir(full) and os.path.exists(os.path.join(full, "pet.json")):
                with open(os.path.join(full, "pet.json"), encoding="utf-8") as f:
                    manifest = json.load(f)
                path = os.path.join(full, "pet.json")
            else:
                continue
            out.append((str(manifest.get("id", name)), str(manifest.get("displayName") or name)[:60], path))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            continue
    return sorted(out, key=lambda x: x[1])
