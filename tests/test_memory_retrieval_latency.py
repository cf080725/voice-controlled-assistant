"""Optimize repeated embedding work while preserving actual hybrid retrieval results."""
import pytest

import canaan_memory as cm
from canaan_knowledge import KnowledgeBase


class CountingEmbedder:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def available(self):
        return True

    def encode(self, texts, query=False):
        self.calls.append((tuple(texts), query))
        return [[1.] + [0.] * (cm.EMBED_DIM - 1) for _ in texts]


@pytest.fixture
def vector_store(tmp_path):
    pytest.importorskip("sqlite_vec")
    embedder = CountingEmbedder()
    store = cm.MemoryStore(str(tmp_path / "memory.db"), embedder=embedder, migrate=False)
    assert store.vec
    yield store, embedder
    store.db.close()


def test_memory_search_embeds_question_once_for_both_indexes(vector_store):
    store, embedder = vector_store
    fid = store.add("主人养了一只叫团子的猫")
    eid = store.add_episode("今天聊到猫喜欢晒太阳")
    embedder.calls.clear()
    facts, episodes = store.search("猫叫什么名字")
    assert facts[0]["id"] == fid and episodes[0]["id"] == eid
    assert embedder.calls == [(("猫叫什么名字",), True)], "Same question was embedded twice"


def test_empty_vector_indexes_do_not_load_embedding_model(vector_store):
    store, embedder = vector_store
    assert store.search("猫叫什么名字") == ([], [])
    assert not embedder.calls, "Empty indexes caused needless model inference/loading"


def test_default_model_is_shared_but_injected_embedders_remain_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "Embedder", CountingEmbedder)
    monkeypatch.setattr(cm, "_default_embedder", None, raising=False)
    monkeypatch.setattr(cm.MemoryStore, "_load_vec", lambda self: False)
    memory = cm.MemoryStore(str(tmp_path / "m.db"), migrate=False)
    knowledge = KnowledgeBase(tmp_path / "k.db")
    custom = CountingEmbedder()
    independent = cm.MemoryStore(str(tmp_path / "custom.db"), embedder=custom, migrate=False)
    try:
        assert memory.embedder is knowledge.store.embedder, "Default retrieval model was duplicated"
        assert independent.embedder is custom and custom is not memory.embedder
    finally:
        memory.db.close()
        knowledge.store.db.close()
        independent.db.close()


def test_optimized_search_keeps_ranking_and_fresh_facts(vector_store):
    store, embedder = vector_store
    old = store.add("主人住在合肥", key="home")
    current = store.add("主人住在杭州", key="home")
    episode = store.add_episode("主人刚搬家到了杭州")
    facts, episodes = store.search("搬家杭州")
    assert [f["id"] for f in facts] == [current] and all(f["id"] != old for f in facts)
    assert [e["id"] for e in episodes] == [episode]
    # The ordinary per-index retrieval must rank the same records.
    assert [i for i, _ in store._hybrid("facts", "text", "搬家杭州", 5, "superseded_by IS NULL")] == [current]
    assert [i for i, _ in store._hybrid("episodes", "summary", "搬家杭州", 2)] == [episode]
