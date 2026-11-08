"""改名字：唤醒词文件的拼音格式、人设换名、canaan_config 读本机 data/assistant_profile.json。只写临时目录。"""
import importlib
import json
import os

import pytest

pytest.importorskip("pypinyin")
import canaan_assistant_name as an
import canaan_config


@pytest.mark.parametrize("text,tokens", [
    ("佳南", ["j", "iā", "n", "án"]),          # 和“你好佳南”同音的唤醒词格式
    ("团团", ["t", "uán", "t", "uán"]),        # 和公开版 keywords_tuantuan.txt 一样
    ("你好", ["n", "ǐ", "h", "ǎo"]),
    ("安安", ["ān", "ān"]),                     # 没有声母的字只出韵母
])
def test_pinyin_tokens_match_the_sherpa_keyword_format(text, tokens):
    assert an.pinyin_tokens(text) == tokens


@pytest.mark.parametrize("bad", ["", "迦", "abc", "佳南佳南迦", "迦 南", "佳南1"])
def test_names_must_be_two_to_four_chinese_characters(bad):
    with pytest.raises(an.InvalidName):
        an.check_name(bad)


def test_unknown_sound_is_rejected_before_writing_anything(tmp_path):
    with pytest.raises(an.InvalidName, match="不认识"):
        an.keyword_lines("佳南", vocab={"n", "ǐ", "h", "ǎo"})


def test_real_kws_vocab_accepts_the_default_names():
    vocab = an.load_vocab(canaan_config.KWS_DIR)
    if vocab is None:
        pytest.skip("唤醒词模型没下载")
    for name in ("佳南", "团团", "小安"):
        assert an.keyword_lines(name, vocab)


def test_rename_writes_profile_keywords_and_persona(tmp_path):
    persona = "你是团团，一个桌面助手。团团说话简短。"
    profile = an.rename("佳南", old_name="团团", persona_text=persona, kws_dir=str(tmp_path / "none"),
                        data_dir=str(tmp_path / "data"))
    assert profile["assistant_name"] == "佳南" and profile["wake_phrases"][:2] == ["佳南", "你好佳南"]
    keywords = (tmp_path / "data" / an.KEYWORDS).read_text(encoding="utf-8").splitlines()
    assert keywords == ["n ǐ h ǎo j iā n án @你好佳南", "j iā n án @佳南", "j iā n án j iā n án @佳南佳南"]
    assert (tmp_path / "data" / an.PERSONA).read_text(encoding="utf-8") == "你是佳南，一个桌面助手。佳南说话简短。"
    saved = json.loads((tmp_path / "data" / an.PROFILE).read_text(encoding="utf-8"))
    assert saved == profile and not os.path.exists(tmp_path / "data" / (an.PROFILE + ".tmp"))


def test_config_applies_the_local_profile(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / an.KEYWORDS).write_text("j iā n án @佳南\n", encoding="utf-8")
    (data / an.PROFILE).write_text(json.dumps({
        "assistant_name": "佳南", "wake_phrases": ["佳南", "你好佳南"], "wake_asr_aliases": ["佳南"],
        "kws_keywords_file": "data/" + an.KEYWORDS, "edition": "public"}, ensure_ascii=False), encoding="utf-8")
    target = {}
    monkeypatch.setattr(canaan_config, "BASE_DIR", str(tmp_path))
    assert canaan_config._apply_edition(str(data / an.PROFILE), target)
    assert target["ASSISTANT_NAME"] == "佳南" and target["WAKE_PHRASES"] == ("佳南", "你好佳南")
    assert target["KWS_KEYWORDS_FILE"] == os.path.join(str(tmp_path), "data/" + an.KEYWORDS)


def test_bootstrap_name_flag_reports_bad_names_without_writing(tmp_path, monkeypatch, capsys):
    import canaan_bootstrap as boot
    monkeypatch.setattr(an, "DATA_DIR", str(tmp_path))
    out = []
    assert boot.setup_name("x", canaan_config, out=out.append) is None
    assert "2 到 4 个汉字" in out[0] and not any(tmp_path.iterdir())
