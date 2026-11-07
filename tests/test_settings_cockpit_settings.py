# -*- coding: utf-8 -*-
"""设置大屏的配置读写：默认值、坏 JSON、部分字段、保存回读。"""
import json

import canaan_settings_cockpit as cockpit


def test_load_settings_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cockpit, "CONFIG_FILE", str(tmp_path / "missing.json"))
    cfg = cockpit.load_settings()
    assert cfg["active_text_model"]
    assert cfg["voice_speed"] == 1.0
    assert cfg["privacy_anti_peep"] is True


def test_load_settings_bad_json_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(cockpit, "CONFIG_FILE", str(path))
    cfg = cockpit.load_settings()
    assert cfg["active_text_model"]
    assert cfg["privacy_mask_phone"] is True


def test_load_settings_merges_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"voice_speed": 1.25}), encoding="utf-8")
    monkeypatch.setattr(cockpit, "CONFIG_FILE", str(path))
    cfg = cockpit.load_settings()
    assert cfg["voice_speed"] == 1.25
    assert cfg["privacy_anti_peep"] is True


def test_save_settings_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "cockpit.json"
    monkeypatch.setattr(cockpit, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit, "CONFIG_FILE", str(path))
    cockpit.save_settings({"voice_speed": 0.9})
    assert json.loads(path.read_text(encoding="utf-8")) == {"voice_speed": 0.9}