"""Forgetting keywords are literal substrings in real temporary stores."""
from types import SimpleNamespace

import pytest

import canaan_crypto
import canaan_memory as cm
import canaan_people as cp


class FixedEmbedder:
    """Exercise encrypted index cleanup without a model or external runtime."""

    def __init__(self, available):
        self.enabled = available

    def available(self):
        return self.enabled

    def encode(self, texts, query=False):
        return [[1.0] + [0.0] * (cm.EMBED_DIM - 1) for _ in texts]


@pytest.fixture(params=[False, True], ids=["plain", "encrypted"])
def stores(tmp_path, monkeypatch, request):
    # Every database/log path is temporary; no default key or legacy migration.
    monkeypatch.setattr(cm, "CONV_LOG", str(tmp_path / "conversations.jsonl"))
    monkeypatch.setattr(cm, "LEGACY_PROFILE", str(tmp_path / "legacy.json"))
    (tmp_path / "conversations.jsonl").write_bytes(b"temporary log must remain unchanged\n")
    cipher = canaan_crypto.Cipher(b"L" * 32) if request.param else None
    owner = cm.MemoryStore(str(tmp_path / "memory.db"), migrate=False,
                           cipher=cipher, embedder=FixedEmbedder(request.param))
    people = cp.PeopleDB(str(tmp_path / "people.db"), owner_store=owner,
                         cipher=cipher, clock=lambda: 1_800_000_000.0)
    alice = people.store(people.switch_to("小明")["id"])
    other = people.store(people.switch_to("小红")["id"])
    try:
        yield SimpleNamespace(owner=owner, people=people, alice=alice, other=other,
                              encrypted=request.param, path=tmp_path)
    finally:
        people.close()
        owner.db.close()


@pytest.mark.parametrize("scope", ["facts", "episodes", "person"])
@pytest.mark.parametrize("keyword,target,unrelated", [
    ("%", "完成100%", "完成100天"),
    ("_", "账号user_name", "账号userXname"),
    ("100%", "主人完成100%审核", "主人完成100天审核"),
    ("user_name", "账号user_name", "账号userXname"),
    ("\\", r"路径a\b", "路径ab"),
    (r"a\b", r"路径a\b", "路径a/b"),
    (r"\%", r"路径a\%b", "路径a%b"),
    (r"\_", r"路径a\_b", "路径a_b"),
    (r"a\\b", r"路径a\\b", r"路径a\b"),
    ("BLUE", "主人喜欢blue sky", "主人喜欢blué sky"),
    ("É", "主人喜欢Été", "主人喜欢été"),
    ("[a-z].*", "主人有[a-z].*标记", "主人有abc标记"),
    ("  蓝色  ", "主人喜欢蓝色", "主人喜欢蓝 色"),
    ("%' OR 1=1 --", "主人记住%' OR 1=1 --这个文本", "主人记住100%这个文本"),
])
def test_literal_keyword_targets_only_matching_rows(stores, scope, keyword, target, unrelated):
    if scope == "episodes":
        deleted = stores.owner.add_episode(target)
        keep = stores.owner.add_episode(unrelated)
        stores.owner.forget(keyword, scrub_log=False)
        remaining = stores.owner.overview()["episodes"]
        assert [(r["id"], r["summary"]) for r in remaining] == [(keep, unrelated)]
        assert deleted != keep
    else:
        store = stores.owner if scope == "facts" else stores.alice
        deleted, keep = store.add(target), store.add(unrelated)
        other = stores.other.add(target)
        assert [(r["id"], r["text"]) for r in store.matching(keyword)] == [(deleted, target)]
        removed = store.forget(keyword, scrub_log=False) if scope == "facts" else store.forget(keyword)
        assert removed == 1
        assert [(r["id"], r["text"]) for r in store.active_facts()] == [(keep, unrelated)]
        assert [(r["id"], r["text"]) for r in stores.other.active_facts()] == [(other, target)]


@pytest.mark.parametrize("keyword", [None, "", "   ", "所有", "全部", "一切", "所有事", "全部的事", " 所有 "])
def test_explicit_all_and_empty_semantics_still_clear_only_the_selected_store(stores, keyword):
    stores.owner.add("主人完成100%")
    stores.owner.add("主人账号user_name")
    stores.owner.add_episode(r"主人路径a\b")
    stores.alice.add("主人完成100%")
    stores.alice.add("主人账号user_name")
    stores.other.add("别人的内容100%")
    other_before = stores.other.active_facts()
    owner_revision = stores.owner.privacy_revision
    assert len(stores.owner.matching(keyword)) == 2
    assert stores.owner.forget(keyword, scrub_log=False) == 2
    assert stores.owner.active_facts() == []
    assert stores.owner.overview()["episodes"] == []
    assert stores.owner.privacy_revision == owner_revision + 1
    # Owner forgetting still leaves both separate people intact.
    assert len(stores.alice.active_facts()) == 2
    assert stores.other.active_facts() == other_before
    person_revision = stores.people.privacy_revision
    assert len(stores.alice.matching(keyword)) == 2
    assert stores.alice.forget(keyword) == 2
    assert stores.alice.active_facts() == []
    assert stores.people.privacy_revision == person_revision + 1
    assert stores.other.active_facts() == other_before
    assert (stores.path / "conversations.jsonl").read_bytes() == b"temporary log must remain unchanged\n"


def test_literal_forget_removes_fact_and_episode_indexes_and_preserves_other_records(stores):
    owner = stores.owner
    deleted_fact = owner.add("主人完成100%")
    keep_fact = owner.add("主人完成100天")
    deleted_episode = owner.add_episode("这次完成100%", origin="literal-percent")
    keep_episode = owner.add_episode("这次完成100天", origin="keep-other")
    stores.other.add("别人的100%应保留")
    before_other = stores.other.active_facts()
    if stores.encrypted:
        # Actual Cipher and actual encrypted embedding records, no fake decoder.
        assert owner.db.execute("SELECT count(*) FROM facts_emb").fetchone()[0] == 2
        assert owner.db.execute("SELECT count(*) FROM episodes_emb").fetchone()[0] == 2
        factory = owner.db.row_factory
        owner.db.row_factory = None
        try:
            assert all(r[0].startswith("enc1:") for r in owner.db.execute("SELECT text FROM facts"))
            assert all(r[0].startswith("enc1:") for r in owner.db.execute("SELECT summary FROM episodes"))
        finally:
            owner.db.row_factory = factory
    assert owner.forget("%", scrub_log=False) == 1
    assert [r["id"] for r in owner.matching("全部")] == [keep_fact]
    assert [r["id"] for r in owner.overview()["episodes"]] == [keep_episode]
    assert stores.other.active_facts() == before_other
    assert owner.add_episode("不能重新写回", origin="literal-percent") is None
    assert owner.add_episode("重复也不能写入", origin="keep-other") is None
    for table, deleted, keep in (("facts", deleted_fact, keep_fact),
                                 ("episodes", deleted_episode, keep_episode)):
        if stores.encrypted:
            assert [r[0] for r in owner.db.execute(f"SELECT id FROM {table}_emb")] == [keep]
            assert owner.db.execute(f"SELECT count(*) FROM {table}_fts").fetchone()[0] == 0
        else:
            assert [r[0] for r in owner.db.execute(f"SELECT rowid FROM {table}_fts")] == [keep]
            assert owner.db.execute(f"SELECT 1 FROM {table}_fts WHERE rowid=?", (deleted,)).fetchone() is None


def test_literal_absence_does_not_delete_records_or_change_person_revision(stores):
    stores.owner.add("主人完成100天")
    stores.owner.add_episode("主人账号userXname")
    stores.alice.add("主人完成100天")
    stores.other.add("别人的内容")
    before_owner, before_people = stores.owner.active_facts(), stores.people.overview()
    before_episodes = stores.owner.overview()["episodes"]
    before_revision = stores.owner.privacy_revision
    before_person_revision = stores.people.privacy_revision
    assert stores.owner.matching("%") == []
    assert stores.owner.forget("%", scrub_log=False) == 0
    assert stores.owner.active_facts() == before_owner
    assert stores.owner.overview()["episodes"] == before_episodes
    # Preserve the existing revision behavior, including no-match owner forget.
    assert stores.owner.privacy_revision == before_revision + 1
    assert stores.alice.matching("_") == []
    assert stores.alice.forget("_") == 0
    assert stores.people.privacy_revision == before_person_revision
    assert stores.people.overview() == before_people


def test_matching_and_forgetting_still_include_superseded_facts(stores):
    old = stores.owner.add("主人旧账号user_name", key="account")
    current = stores.owner.add("主人新账号userXname", key="account")
    assert [r["id"] for r in stores.owner.active_facts()] == [current]
    assert [r["id"] for r in stores.owner.matching("user_name")] == [old]
    assert stores.owner.forget("user_name", scrub_log=False) == 1
    assert [r["id"] for r in stores.owner.matching("全部")] == [current]
