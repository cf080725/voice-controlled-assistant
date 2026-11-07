import json
import zipfile

import pytest
from PIL import Image, ImageDraw

import canaan_pet_pack as pp


def _atlas(rows, filled):
    """filled: {行: 帧数}；每个非空格子画一个不同颜色的方块（自己生成的测试图集）。"""
    img = Image.new("RGBA", (pp.COLS * pp.CELL_W, rows * pp.CELL_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for row, n in filled.items():
        for col in range(n):
            x, y = col * pp.CELL_W, row * pp.CELL_H
            d.rectangle((x + 40, y + 40, x + 150, y + 200), fill=(20 * row % 255, 30 * col % 255, 200, 255))
    return img


def _zip(tmp_path, manifest, files, wrapper=""):
    path = tmp_path / "pack.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(wrapper + "pet.json", json.dumps(manifest))
        for name, data in files.items():
            z.writestr(wrapper + name, data)
    return str(path)


def _png(img, fmt="WEBP"):
    import io
    buf = io.BytesIO()
    img.save(buf, fmt, lossless=True) if fmt == "WEBP" else img.save(buf, fmt)
    return buf.getvalue()


def test_v2_atlas_rows_gaze_and_fallback(tmp_path):
    filled = {0: 7, 1: 8, 2: 8, 3: 4, 4: 5, 6: 3, 9: 8, 10: 8}  # 第 5 行（failed）、7、8 行空
    path = _zip(tmp_path, {"id": "test-v2", "displayName": "测试", "spriteVersionNumber": 2,
                           "stateBindings": {"clicked": "waving"}}, {"spritesheet.webp": _png(_atlas(11, filled))}, wrapper="wrap/")
    pack = pp.load_pack(path)
    assert pack.kind == "v2" and pack.has_gaze
    assert len(pack.animations["idle"]) == 6          # 第 0 行最多 6 帧，第 6 格是正面待机
    assert len(pack.animations["running-right"]) == 8 and len(pack.animations["waving"]) == 4
    assert pack.resolve("failed") == "idle"           # 缺了退回 idle
    assert pack.resolve("clicked") == "waving"        # stateBindings
    assert pack.resolve("dragged") == "jumping"       # 可选动作退回 jumping
    assert pack.for_canaan_state("crying") == "idle" and pack.for_canaan_state("cheer") == "jumping"
    assert pack.for_canaan_state("talk_open") == "idle" and pack.for_canaan_state("rainbow_star") == "jumping"
    img, idx = pack.frame_at("waving", 0.0)
    assert img.size == (pp.CELL_W, pp.CELL_H) and idx == 0
    assert pack.gaze_frame(17).size == (pp.CELL_W, pp.CELL_H)


def test_v1_atlas_has_no_gaze(tmp_path):
    path = _zip(tmp_path, {"id": "test-v1", "displayName": "v1"}, {"spritesheet.webp": _png(_atlas(9, {0: 6, 3: 2}))})
    pack = pp.load_pack(path)
    assert pack.kind == "v1" and not pack.has_gaze and pack.resolve("waving") == "waving"


@pytest.mark.parametrize("manifest,files,msg", [
    ({"id": "Bad ID"}, {}, "id"),
    ({"id": "builtin"}, {}, "id"),
    ({"id": "ok", "spriteVersionNumber": 2}, {"spritesheet.webp": None}, "尺寸"),
    ({"id": "ok", "mediaVersion": 1, "actions": {"idle": "../../etc/x.webp"}}, {}, "不安全"),
    ({"id": "ok", "mediaVersion": 1, "actions": {"idle": "a.exe"}}, {}, "格式"),
    ({"id": "ok", "mediaVersion": 1, "actions": {"wave": "w.png"}, "staticActions": ["wave"]}, {"w.png": "img"}, "idle"),
])
def test_rejects_bad_packs(tmp_path, manifest, files, msg):
    files = {k: (_png(_atlas(9, {0: 1})) if v is None else _png(Image.new("RGBA", (50, 50), (1, 2, 3, 255)), "PNG") if v == "img" else v)
             for k, v in files.items()}
    with pytest.raises(pp.PackError, match=msg):
        pp.load_pack(_zip(tmp_path, manifest, files))


def test_zip_with_traversal_entry_rejected(tmp_path):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("pet.json", "{}")
        z.writestr("../outside.txt", "x")
    with pytest.raises(pp.PackError, match="不安全"):
        pp.load_pack(str(path))


def test_media_pack_static_and_chroma(tmp_path):
    green = Image.new("RGBA", (300, 300), (0, 255, 0, 255))
    ImageDraw.Draw(green).rectangle((100, 50, 200, 290), fill=(200, 50, 50, 255))
    manifest = {"id": "media-test", "displayName": "m", "mediaVersion": 1, "actions": {"idle": "media/idle.png"},
                "staticActions": ["idle"], "background": {"mode": "chroma", "color": "#00ff00", "tolerance": 30},
                "stateBindings": {"waiting": "idle"}}
    pack = pp.load_pack(_zip(tmp_path, manifest, {"media/idle.png": _png(green, "PNG")}))
    img = pack.animations["idle"][0][0]
    assert img.size == (pp.CELL_W, pp.CELL_H)
    assert img.getpixel((5, pp.CELL_H - 5))[3] == 0          # 绿幕被抠掉
    assert img.getpixel((pp.CELL_W // 2, pp.CELL_H - 10))[3] == 255
    assert pack.kind == "media" and pack.resolve("running") == "idle"


def test_static_image_needs_declaration(tmp_path):
    manifest = {"id": "media-test", "mediaVersion": 1, "actions": {"idle": "idle.png"}}
    with pytest.raises(pp.PackError, match="staticActions"):
        pp.load_pack(_zip(tmp_path, manifest, {"idle.png": _png(Image.new("RGBA", (10, 10)), "PNG")}))


def test_folder_pack(tmp_path):
    (tmp_path / "pet.json").write_text(json.dumps({"id": "folder-pack"}), encoding="utf-8")
    _atlas(9, {0: 2}).save(tmp_path / "spritesheet.webp", lossless=True)
    assert pp.load_pack(str(tmp_path / "pet.json")).id == "folder-pack"
    assert pp.load_pack(str(tmp_path)).id == "folder-pack"


def test_gaze_direction_math():
    assert pp.gaze_direction(0, 0) is None                 # 太近
    assert pp.gaze_direction(0, -100) == 0                 # 正上方
    assert pp.gaze_direction(100, 0) == 4                  # 右
    assert pp.gaze_direction(0, 100) == 8                  # 下
    assert pp.gaze_direction(-100, 0) == 12                # 左
    assert pp.gaze_direction(100, -100) == 2               # 右上
    assert pp.mirror_direction(4) == 12 and pp.mirror_direction(0) == 0
