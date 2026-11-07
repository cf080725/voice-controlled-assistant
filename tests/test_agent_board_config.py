# -*- coding: utf-8 -*-
"""看板配置文件坏了只回退默认值，不能让 import 阶段崩掉。"""
import json

import canaan_agent_board_data as ab


def test_missing_config_returns_empty(tmp_path):
    assert ab.load_local_agent_config(tmp_path / "nope.json") == {}


def test_broken_json_returns_empty(tmp_path):
    p = tmp_path / "agent_services.json"
    p.write_text("{not json", encoding="utf-8")
    assert ab.load_local_agent_config(p) == {}


def test_non_dict_json_returns_empty(tmp_path):
    p = tmp_path / "agent_services.json"
    p.write_text(json.dumps(["x"]), encoding="utf-8")
    assert ab.load_local_agent_config(p) == {}


def test_valid_config_is_loaded(tmp_path):
    p = tmp_path / "agent_services.json"
    p.write_text(json.dumps({"muse_api": "http://127.0.0.1:9999"}), encoding="utf-8")
    assert ab.load_local_agent_config(p) == {"muse_api": "http://127.0.0.1:9999"}