# -*- coding: utf-8 -*-
import json
import pytest

import canaan_memory as cm


class _NoEmbed:
    def available(self):
        return False


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "LEGACY_PROFILE", str(tmp_path / "user_profile.json"))
    monkeypatch.setattr(cm, "CONV_LOG", str(tmp_path / "conversations.jsonl"))
    return cm.MemoryStore(str(tmp_path / "m.db"), embedder=_NoEmbed())


def test_add_dedupe_and_key_supersede(store):
    assert store.add("主人住在合肥", key="home")
    assert store.add("主人住在合肥", key="home") is None
    assert store.add("主人住在杭州", key="home")
    assert store.texts() == ["主人住在杭州"]


def test_fulltext_search_chinese(store):
    store.add("主人养了一只叫团子的猫")
    store.add("主人在准备机器鱼比赛")
    facts, _ = store.search("我的猫叫什么来着")
    assert facts and "团子" in facts[0]["text"]


def test_update_and_retire(store):
    fid = store.add("主人喜欢喝咖啡")
    nid = store.update(fid, "主人现在改喝茶了")
    assert nid and store.texts() == ["主人现在改喝茶了"]
    store.retire(nid)
    assert store.texts() == []


def test_forget_hard_deletes_and_scrubs_log(store, tmp_path):
    store.add("主人的生日是五月三号")
    store.add_episode("主人说生日是五月三号，想吃蛋糕")
    log = tmp_path / "conversations.jsonl"
    log.write_text(json.dumps({"user": "我的生日是五月三号"}, ensure_ascii=False) + "\n"
                   + json.dumps({"user": "打开记事本"}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert store.forget("生日") == 1
    assert store.texts() == [] and store.search("生日")[1] == []
    assert "生日" not in log.read_text(encoding="utf-8") and "记事本" in log.read_text(encoding="utf-8")
    assert store.db.execute("SELECT count(*) FROM facts_fts").fetchone()[0] == 0


def test_incognito_writes_nothing(store):
    store.incognito = True
    assert store.add("主人住在合肥") is None
    assert store.add_episode("聊了天气") is None
    assert store.texts() == []


def test_incognito_persists_and_is_shared_between_connections(tmp_path):
    path = str(tmp_path / "memory.db")
    a = cm.MemoryStore(path, embedder=_NoEmbed(), migrate=False)
    b = cm.MemoryStore(path, embedder=_NoEmbed(), migrate=False)
    try:
        a.incognito = True
        assert b.incognito and b.add("虚构内容") is None
        a.db.close()
        a = cm.MemoryStore(path, embedder=_NoEmbed(), migrate=False)
        assert a.incognito and a.add_episode("虚构摘要") is None
        b.incognito = False
        assert not a.incognito and a.add("虚构内容")
    finally:
        a.db.close()
        b.db.close()


def test_migrate_legacy_profile(tmp_path, monkeypatch):
    legacy = tmp_path / "user_profile.json"
    legacy.write_text(json.dumps({"facts": [{"text": "主人叫小福", "key": "name", "time": "2026-10-01 10:00"}]},
                                 ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cm, "LEGACY_PROFILE", str(legacy))
    s = cm.MemoryStore(str(tmp_path / "m.db"), embedder=_NoEmbed())
    assert s.texts() == ["主人叫小福"] and not legacy.exists()


def test_manager_consolidates_after_idle(store, monkeypatch):
    calls = []

    def fake_llm(model, prompt, schema, num_predict=300, timeout=60):
        calls.append(prompt[:6])
        if "summary" in json.dumps(schema):
            return {"summary": "主人说搬到了杭州"}
        if "facts" in json.dumps(schema):
            return {"facts": ["主人住在杭州"]}
        return {"op": "UPDATE", "id": 1}

    monkeypatch.setattr(cm, "_llm_json", fake_llm)
    store.add("主人住在合肥")
    clock = {"t": 0.0}
    m = cm.MemoryManager(store, idle_sec=300, clock=lambda: clock["t"])
    m.on_turn("我搬到杭州了", "")
    ops = m.close()
    assert ops == [{"fact": "主人住在杭州", "op": "UPDATE", "id": 1}]
    assert store.texts() == ["主人住在杭州"]
    assert store.search("搬家")[1] or store.db.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1
