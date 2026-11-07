"""原创默认形象“团团”与形象包库：生成合格的 V2 包、导入先校验再复制、列表只读名字、没有 72 态图片时自动用团团。"""
import json
import os
import zipfile

import pytest

import canaan_default_pack as dp
import canaan_pet_pack as pp


def test_default_pack_is_valid_v2_and_regenerates_only_when_needed(tmp_path):
    path = dp.ensure_default_pack(str(tmp_path))
    pack = pp.load_pack(path)
    assert pack.kind == "v2" and pack.has_gaze and set(pp.CORE_ACTIONS) <= set(pack.animations)
    assert len(pack.animations["idle"]) == 6 and len(pack.animations["waving"]) == 8
    mtime = os.path.getmtime(os.path.join(tmp_path, dp.PACK_ID, "spritesheet.png"))
    assert dp.ensure_default_pack(str(tmp_path)) == path
    assert os.path.getmtime(os.path.join(tmp_path, dp.PACK_ID, "spritesheet.png")) == mtime


def test_gaze_cells_differ_by_direction():
    up, right = dp.draw("idle", 0, 6, gaze=0), dp.draw("idle", 0, 6, gaze=4)
    assert up.tobytes() != right.tobytes()


def test_install_copies_zip_and_folder_then_lists(tmp_path):
    src = tmp_path / "src"
    folder_manifest = dp.ensure_default_pack(str(src))
    zpath = tmp_path / "mine.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("pet.json", json.dumps({"id": "mine", "displayName": "我的形象", "spriteVersionNumber": 2,
                                           "spritesheetPath": "spritesheet.png"}))
        z.write(os.path.join(src, dp.PACK_ID, "spritesheet.png"), "spritesheet.png")
    lib = tmp_path / "lib"
    dest_zip, _ = pp.install_pack(str(zpath), str(lib))
    dest_folder, _ = pp.install_pack(folder_manifest, str(lib))
    os.remove(zpath)                                                   # 原文件挪走也不影响
    assert pp.load_pack(dest_zip).name == "我的形象" and os.path.exists(dest_folder)
    assert [name for _, name, _ in pp.list_installed(str(lib))] == ["团团（默认原创形象）", "我的形象"]


def test_bad_pack_rejected_and_nothing_copied(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("pet.json", json.dumps({"id": "bad", "spritesheetPath": "spritesheet.png"}))
        z.writestr("spritesheet.png", b"not an image")
    with pytest.raises(Exception):
        pp.install_pack(str(bad), str(tmp_path / "lib"))
    assert pp.list_installed(str(tmp_path / "lib")) == []


def test_pet_uses_default_when_no_builtin_images(tmp_path, monkeypatch):
    from tests.test_pet_status_hud import _pet
    import canaan_companion_pet as ccp
    monkeypatch.setattr(ccp.CanaanCompanionPet, "has_builtin_images", staticmethod(lambda: False))
    monkeypatch.setattr(dp, "PACKS_DIR", str(tmp_path))
    monkeypatch.setattr(dp.ensure_default_pack, "__defaults__", (str(tmp_path),))
    pet = _pet()
    pet.stats.pop("pet_pack", None)
    pet._load_pack_from_settings()
    assert pet.pack is not None and pet.pack.id == dp.PACK_ID
    pet.stats["pet_pack"] = str(tmp_path / "missing.zip")              # 设置里的包坏了也退回团团
    pet._load_pack_from_settings()
    assert pet.pack.id == dp.PACK_ID
    pet.destroy()
