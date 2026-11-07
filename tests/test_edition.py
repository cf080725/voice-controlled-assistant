"""两个版本：自用版没有 canaan_edition.json 就照旧；公开版的文件只能覆盖列出的几项，格式不对的忽略。"""
import json
import os

import canaan_config as cfg


def test_personal_is_default_here():
    assert cfg.EDITION in ("personal", "public")
    if not os.path.exists(cfg.EDITION_FILE):
        assert cfg.EDITION == "personal"


def test_public_overrides_only_known_keys(tmp_path):
    path = tmp_path / "canaan_edition.json"
    path.write_text(json.dumps({"edition": "public", "assistant_name": "团团", "wake_phrases": ["你好团团", "团团"],
                                "wake_asr_aliases": ["团团"], "persona_prompt_file": "personas/tuantuan.txt",
                                "kws_keywords_file": "personas/keywords_tuantuan.txt", "pet_name_gap": 99,
                                "LLM_ENABLED": False, "wake_phrases_bad": 1}, ensure_ascii=False), encoding="utf-8")
    target = {}
    assert cfg._apply_edition(str(path), target)
    assert target["EDITION"] == "public" and target["ASSISTANT_NAME"] == "团团"
    assert target["WAKE_PHRASES"] == ("你好团团", "团团") and target["PET_NAME_GAP"] == 99
    assert target["KWS_KEYWORDS_FILE"].replace("\\", "/").endswith("personas/keywords_tuantuan.txt")
    assert "LLM_ENABLED" not in target


def test_bad_values_ignored(tmp_path):
    path = tmp_path / "e.json"
    path.write_text(json.dumps({"edition": "hacker", "wake_phrases": [], "kws_keywords_file": "../../x.txt"}), encoding="utf-8")
    target = {}
    cfg._apply_edition(str(path), target)
    assert target == {}
    assert not cfg._apply_edition(str(tmp_path / "missing.json"), {})


def test_public_persona_and_keywords_exist_and_use_known_tokens():
    root = os.path.dirname(cfg.EDITION_FILE)
    persona = open(os.path.join(root, "personas", "tuantuan.txt"), encoding="utf-8").read()
    old_name = "迦" + "南"   # 拆开写：公开版导出会把整词替换掉
    assert "团团" in persona and old_name not in persona and "亲爱的" not in persona
    lines = open(os.path.join(root, "personas", "keywords_tuantuan.txt"), encoding="utf-8").read().splitlines()
    tokens_path = os.path.join(cfg.KWS_DIR, "tokens.txt")
    if os.path.exists(tokens_path):
        known = {line.split()[0] for line in open(tokens_path, encoding="utf-8") if line.strip()}
        for line in lines:
            assert all(t in known for t in line.split("@")[0].split()), line
