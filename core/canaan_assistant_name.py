# -*- coding: utf-8 -*-
"""给助手改名字：唤醒词、界面上显示的名字、陪聊人设里的自称一起换。

只写本机 data/ 目录（不进仓库）：
    data/assistant_profile.json   名字、唤醒词、人设卡和唤醒词文件的位置（canaan_config 启动时读）
    data/keywords_custom.txt      sherpa-onnx 唤醒词文件（声母 + 带声调韵母，如 “j iā n án @团团”）
    data/persona_custom.txt       当前人设卡把旧名字换成新名字

    python canaan.py bootstrap --name 团团
"""
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
PROFILE = "assistant_profile.json"
KEYWORDS = "keywords_custom.txt"
PERSONA = "persona_custom.txt"
NAME_RE = re.compile(r"^[一-鿿]{2,4}$")


class InvalidName(ValueError):
    pass


def check_name(name):
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise InvalidName("名字要是 2 到 4 个汉字（唤醒词模型只认中文）")
    return name


def pinyin_tokens(text):
    """“团团” → ["j", "iā", "n", "án"]；没有声母的字（安、爱）只出韵母。"""
    from pypinyin import Style, pinyin
    initials = pinyin(text, style=Style.INITIALS, strict=False, heteronym=False)
    finals = pinyin(text, style=Style.FINALS_TONE, strict=False, heteronym=False)
    tokens = []
    for (ini,), (fin,) in zip(initials, finals):
        if ini:
            tokens.append(ini)
        if fin:
            tokens.append(fin)
    return tokens


def wake_phrases(name):
    return (name, f"你好{name}", f"{name}{name}")


def keyword_lines(name, vocab=None):
    """唤醒词文件的每一行；vocab 给了就检查每个音都在唤醒模型的词表里。"""
    lines = []
    for phrase in (f"你好{name}", name, f"{name}{name}"):
        tokens = pinyin_tokens(phrase)
        missing = [t for t in tokens if vocab is not None and t not in vocab]
        if missing:
            raise InvalidName(f"唤醒词模型不认识这些音：{' '.join(missing)}，换个名字试试")
        lines.append(" ".join(tokens) + f" @{phrase}")
    return lines


def load_vocab(kws_dir):
    path = os.path.join(kws_dir, "tokens.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return {line.split()[0] for line in f if line.strip()}
    except OSError:
        return None   # 模型还没下载：先不检查，下载后启动时 sherpa-onnx 会再报错


def rename(name, *, old_name, persona_text, kws_dir, data_dir=DATA_DIR):
    """写三个文件，返回写进 assistant_profile.json 的内容。"""
    name = check_name(name)
    lines = keyword_lines(name, load_vocab(kws_dir))
    os.makedirs(data_dir, exist_ok=True)
    rel = os.path.relpath(data_dir, BASE_DIR).replace("\\", "/")
    with open(os.path.join(data_dir, KEYWORDS), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    persona = (persona_text or "").replace(old_name, name) if old_name else (persona_text or "")
    profile = {"assistant_name": name, "wake_phrases": list(wake_phrases(name)),
               "wake_asr_aliases": [name, name + name], "kws_keywords_file": f"{rel}/{KEYWORDS}"}
    if persona.strip():
        with open(os.path.join(data_dir, PERSONA), "w", encoding="utf-8") as f:
            f.write(persona)
        profile["persona_prompt_file"] = f"{rel}/{PERSONA}"
    tmp = os.path.join(data_dir, PROFILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(data_dir, PROFILE))
    return profile
