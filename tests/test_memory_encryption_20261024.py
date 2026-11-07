"""10-24：data/memory.db 加密（canaan_crypto，AES-256-GCM）。磁盘上不能留明文；检索、去重、覆盖、忘掉照常可用。"""
import hashlib
import os
import sqlite3

import canaan_crypto
import canaan_memory as cm
import pytest


class HashEmbed:
    """确定性的假向量：按字计数后归一化，意思相近（字重合多）的句子相似度高。"""

    def available(self):
        return True

    def encode(self, texts, query=False):
        out = []
        for t in texts:
            v = [0.0] * cm.EMBED_DIM
            for ch in t.replace(cm.QUERY_INSTRUCTION, ""):
                v[int(hashlib.md5(ch.encode()).hexdigest(), 16) % cm.EMBED_DIM] += 1
            n = sum(x * x for x in v) ** .5 or 1
            out.append([x / n for x in v])
        return out


@pytest.fixture
def cipher():
    return canaan_crypto.Cipher(os.urandom(32))


def store(path, cipher, embedder=None):
    return cm.MemoryStore(str(path), embedder=embedder or HashEmbed(), migrate=False, cipher=cipher)


def raw(path):
    return open(path, "rb").read()


def test_nothing_readable_on_disk(tmp_path, cipher):
    p = tmp_path / "m.db"
    s = store(p, cipher)
    s.add("我住在合肥市蜀山区", key="home")
    s.add("我女儿的生日是三月八号")
    s.add_episode("主人让团团给王小明发了消息")
    s.db.commit()
    data = raw(p)
    for secret in ("合肥", "生日", "王小明"):
        assert secret.encode("utf-8") not in data
    db = sqlite3.connect(p)
    assert all(t.startswith("enc1:") for (t,) in db.execute("SELECT text FROM facts"))
    assert all(t.startswith("enc1:") for (t,) in db.execute("SELECT summary FROM episodes"))
    assert db.execute("SELECT count(*) FROM facts_fts").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE '%_vec'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM facts_emb").fetchone()[0] == 2


def test_search_dedupe_supersede_and_forget_still_work(tmp_path, cipher):
    s = store(tmp_path / "m.db", cipher)
    a = s.add("我住在合肥", key="home")
    assert s.add("我住在合肥", key="home") is None              # 一模一样的不重复写
    b = s.add("我住在南京", key="home")
    assert [f["id"] for f in s.active_facts()] == [b] and a != b  # 同 key 覆盖
    s.add("我喜欢吃辣的火锅")
    s.add_episode("聊了周末去南京玩")
    facts, eps = s.search("我住哪里来着，南京吗")
    assert facts[0]["text"] == "我住在南京"
    assert eps and eps[0]["summary"] == "聊了周末去南京玩"
    assert any("火锅" in line for line in s.context_lines("吃火锅"))
    assert [f["text"] for f in s.matching("火锅")] == ["我喜欢吃辣的火锅"]
    assert s.overview("南京")["episodes"][0]["summary"] == "聊了周末去南京玩"
    assert s.forget("火锅", scrub_log=False) == 1
    assert not s.matching("火锅")
    assert s.db.execute("SELECT count(*) FROM facts_emb").fetchone()[0] == 2


def test_edit_and_delete_drop_encrypted_vectors(tmp_path, cipher):
    s = store(tmp_path / "m.db", cipher)
    fid = s.add("我养了一只猫")
    nid = s.edit_fact(fid, "我养了两只猫")
    assert s.active_facts()[0]["text"] == "我养了两只猫"
    assert s.delete_fact(nid)
    assert s.db.execute("SELECT count(*) FROM facts_emb").fetchone()[0] == 0
    assert s.search("猫") == ([], [])


def test_plaintext_database_is_encrypted_in_place(tmp_path, cipher):
    p = tmp_path / "m.db"
    old = cm.MemoryStore(str(p), embedder=HashEmbed(), migrate=False, cipher=None)
    old.add("我的手机号是一三八零零零零零零零零")
    old.add_episode("主人问了明天的天气")
    old.db.commit()
    old.db.close()
    assert "手机号".encode() in raw(p)
    s = store(p, cipher)
    assert s.active_facts()[0]["text"] == "我的手机号是一三八零零零零零零零零"
    assert s.search("手机号多少")[0][0]["text"].startswith("我的手机号")
    s.db.close()
    data = raw(p)
    assert "手机号".encode() not in data and "天气".encode() not in data   # VACUUM 后旧页面也没了


def test_another_key_cannot_read(tmp_path, cipher):
    p = tmp_path / "m.db"
    store(p, cipher).add("我住在合肥")
    other = store(p, canaan_crypto.Cipher(os.urandom(32)))
    with pytest.raises(Exception):
        other.active_facts()


def test_default_memory_db_is_encrypted_but_other_paths_are_not(tmp_path, monkeypatch, cipher):
    monkeypatch.setattr(canaan_crypto, "default_cipher", lambda: cipher)
    monkeypatch.setattr(cm, "DB_PATH", str(tmp_path / "memory.db"))
    assert cm.MemoryStore(cm.DB_PATH, embedder=HashEmbed(), migrate=False).cipher is cipher
    assert cm.MemoryStore(str(tmp_path / "knowledge.db"), embedder=HashEmbed(), migrate=False).cipher is None


def test_key_file_is_never_overwritten_when_it_cannot_be_opened(tmp_path):
    p = tmp_path / "secure.key"
    key = canaan_crypto.load_or_create_key(str(p), protector=lambda b: b"X" + b, unprotector=lambda b: b[1:])
    assert canaan_crypto.load_or_create_key(str(p), unprotector=lambda b: b[1:]) == key

    def broken(_):
        raise OSError("别的账号")
    before = raw(p)
    with pytest.raises(canaan_crypto.KeyUnavailable):
        canaan_crypto.load_or_create_key(str(p), unprotector=broken)
    assert raw(p) == before


def test_dpapi_round_trip():
    if os.name != "nt":
        pytest.skip("DPAPI 只在 Windows 上")
    assert canaan_crypto.unprotect(canaan_crypto.protect(b"abc")) == b"abc"
