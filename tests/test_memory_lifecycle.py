"""虚构模型与临时数据库，验证删除、无痕切换和并发后台记忆的生命周期。"""
import threading

import pytest

import canaan_memory as cm


class NoEmbed:
    def available(self):
        return False


@pytest.fixture
def store(tmp_path):
    s = cm.MemoryStore(str(tmp_path / "memory.db"), embedder=NoEmbed(), migrate=False)
    yield s
    s.db.close()


def fake_response(schema):
    if "summary" in schema["properties"]:
        return {"summary": "虚构会话摘要"}
    if "facts" in schema["properties"]:
        return {"facts": ["虚构测试事实"]}
    return {"op": "ADD", "id": 0}


@pytest.mark.parametrize("stage", ["summary", "facts", "op"])
def test_deleted_fact_not_restored_by_inflight_job(store, monkeypatch, stage):
    fid = store.add("虚构测试事实")
    entered, release = threading.Event(), threading.Event()

    def model(_model, _prompt, schema, **kw):
        if stage in schema["properties"]:
            entered.set()
            assert release.wait(3)
        return fake_response(schema)

    monkeypatch.setattr(cm, "_llm_json", model)
    manager = cm.MemoryManager(store)
    result = []
    worker = threading.Thread(target=lambda: result.append(manager.consolidate([("虚构输入", "")])) )
    worker.start()
    try:
        assert entered.wait(3)
        assert store.delete_fact(fid)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert store.texts() == []
    assert result == [[]]


@pytest.mark.parametrize("invalidate", ["delete", "incognito", "edit", "forget"])
def test_old_buffer_discarded_before_model_call(store, monkeypatch, invalidate):
    fid = store.add("虚构测试事实")
    manager = cm.MemoryManager(store)
    manager.on_turn("过去的虚构输入", "")
    if invalidate == "delete":
        store.delete_fact(fid)
    elif invalidate == "edit":
        store.edit_fact(fid, "主人更正后的虚构事实")
    elif invalidate == "forget":
        store.forget("虚构", scrub_log=False)
    else:
        store.incognito = True
        store.incognito = False
    monkeypatch.setattr(cm, "_llm_json", lambda *a, **k: pytest.fail("过期缓冲不得请求模型"))
    assert manager.close() == []


def test_new_turn_after_delete_can_be_remembered(store, monkeypatch):
    fid = store.add("虚构测试事实")
    manager = cm.MemoryManager(store)
    manager.on_turn("旧的虚构输入", "")
    store.delete_fact(fid)
    manager.on_turn("新的虚构输入", "")
    monkeypatch.setattr(cm, "_llm_json", lambda _m, prompt, schema, **kw: fake_response(schema))
    assert manager.close()
    assert store.texts() == ["虚构测试事实"]


def test_stale_write_rejected_across_connections(store):
    other = cm.MemoryStore(store.path, embedder=NoEmbed(), migrate=False)
    try:
        revision = store.privacy_revision
        fid = store.add("虚构测试事实")
        other.delete_fact(fid)
        assert store.add("虚构测试事实", expected_revision=revision) is None
        assert store.add_episode("旧任务的虚构结果", expected_revision=revision) is None
        assert store.texts() == []
    finally:
        other.db.close()


def test_deleted_task_origin_cannot_be_reimported(store):
    eid = store.add_episode("虚构任务结果", origin="dots:fake-task")
    store.delete_episode(eid)
    assert store.add_episode("同源任务换了标题", origin="dots:fake-task") is None
    assert store.add_episode("用户的新任务", origin="dots:new-fake-task")


def test_short_context_invalidated_by_delete(store):
    from canaan_intent import ConversationMemory
    memory = ConversationMemory(revision=lambda: store.privacy_revision)
    memory.add_turn("旧的虚构输入", {"action": "chat", "reply": "旧回复"})
    fid = store.add("虚构测试事实")
    store.delete_fact(fid)
    assert memory.messages() == []
    memory.add_turn("新输入", {"action": "chat", "reply": "新回复"})
    assert memory.messages()[0]["content"] == "新输入"


def test_slow_reply_cannot_join_new_memory_revision(store):
    from canaan_intent import ConversationMemory
    revision = store.privacy_revision
    memory = ConversationMemory(revision=lambda: store.privacy_revision)
    manager = cm.MemoryManager(store)
    store.forget("虚构", scrub_log=False)
    assert memory.add_turn("删除前开始的输入", {"action": "chat"}, expected_revision=revision) is False
    assert manager.on_turn("删除前开始的输入", "慢回复", expected_revision=revision) is False
    assert memory.messages() == [] and manager.turns == []


def test_hub_log_rejects_stale_turn_without_loading_app(store, tmp_path):
    import ast
    import json
    import os
    import time
    from pathlib import Path
    from types import SimpleNamespace
    module = ast.parse((Path(__file__).parents[1] / "core/canaan_desk_os_hub.py").read_text(encoding="utf-8"))
    method = next(n for c in module.body if isinstance(c, ast.ClassDef)
                  for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "log_conversation")
    from canaan_filelock import file_lock
    namespace = dict(os=os, json=json, time=time, BASE_DIR=str(tmp_path), file_lock=file_lock,
                     canaan_config=SimpleNamespace(CONVERSATION_LOG=True),
                     canaan_gamemode=SimpleNamespace(active=lambda: False))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "isolated-hub-method", "exec"), namespace)
    hub = SimpleNamespace(profile=store, memory_mgr=cm.MemoryManager(store))
    old_revision = store.privacy_revision
    store.forget("虚构", scrub_log=False)
    method = namespace["log_conversation"]
    method(hub, None, "虚构旧输入", {"action": "chat"}, "虚构旧回复", expected_revision=old_revision)
    log = tmp_path / "logs/conversations.jsonl"
    assert not log.exists() and hub.memory_mgr.turns == []
    method(hub, None, "虚构新输入", {"action": "chat"}, "虚构新回复", expected_revision=store.privacy_revision)
    import canaan_crypto   # 10-24 起原话加密落盘
    restored = canaan_crypto.open_record(json.loads(log.read_text(encoding="utf-8")))
    assert restored["user"] == "虚构新输入"
    assert isinstance(restored["timestamp"], (float, int))
    assert restored["time"] == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(restored["timestamp"]))
    assert cm.recent_conversation(str(log), now=restored["timestamp"] + .5)[0][0] == restored["user"]


def test_resume_skips_conversation_before_privacy_change(tmp_path):
    import json
    import time
    now = time.time()
    log = tmp_path / "fake.jsonl"
    log.write_text("\n".join(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
                                       "user": text, "action": "chat", "spoken": ""}, ensure_ascii=False)
                             for t, text in [(now - 60, "旧输入"), (now - 10, "新输入")]), encoding="utf-8")
    assert [u for u, _ in cm.recent_conversation(str(log), now=now, not_before=now-30)] == ["新输入"]


def test_watcher_task_keeps_original_revision_across_restart(store, tmp_path, monkeypatch):
    import canaan_agent_watcher as aw
    import canaan_notify
    monkeypatch.setattr(canaan_notify, "post", lambda *a, **kw: None)
    current = [{"id": "fake", "status": "running", "title": "虚构任务", "note": "", "steps": None}]
    kwargs = dict(remember=lambda text, **kw: store.add_episode(text, **kw),
                  memory_revision=lambda: store.privacy_revision,
                  fetchers={"dots": lambda: {"online": True, "tasks": current}},
                  state_file=str(tmp_path / "seen.json"))
    watcher = aw.AgentTaskWatcher(lambda *a: True, **kwargs)
    watcher.poll(first=True)
    store.forget("虚构", scrub_log=False)
    current[0]["status"] = "done"
    # 用同一状态文件模拟进程重启，旧任务不能得到新版本授权。
    aw.AgentTaskWatcher(lambda *a: True, **kwargs).poll()
    assert store.overview()["episodes"] == []


def test_watcher_provider_ids_do_not_collide(tmp_path):
    import canaan_agent_watcher as aw
    w = aw.AgentTaskWatcher(lambda *a: True, state_file=str(tmp_path / "seen.json"),
        fetchers={p: lambda: {"online": True, "tasks": [{"id": "same", "status": "running", "steps": None}]}
                  for p in ("dots", "muse")})
    w.poll(first=True)
    assert set(w.seen_tasks) == {"muse:same", "dots:same"}


def test_bridge_cannot_restore_deleted_episode_with_changed_title(store, tmp_path):
    import sqlite3
    import canaan_dots_memory_bridge as bridge_module
    dots = tmp_path / "dots.sqlite"
    with sqlite3.connect(dots) as db:
        db.execute("CREATE TABLE tasks(id TEXT, prompt TEXT, status TEXT, updatedAt INTEGER)")
        db.execute("INSERT INTO tasks VALUES('fake','虚构任务','done',9999999999)")
    bridge = bridge_module.CanaanDotsMemoryBridge(store.path, str(dots), enabled=True)
    assert bridge.sync_dots_to_canaan() == 1
    eid = store.overview()["episodes"][0]["id"]
    store.delete_episode(eid)
    with sqlite3.connect(dots) as db:
        db.execute("UPDATE tasks SET prompt='同源任务改名'")
    assert bridge.sync_dots_to_canaan() == 0
    assert store.overview()["episodes"] == []


def test_bridge_running_task_uses_first_observed_revision(store, tmp_path):
    import sqlite3
    from canaan_dots_memory_bridge import CanaanDotsMemoryBridge
    dots = tmp_path / "dots.sqlite"
    with sqlite3.connect(dots) as db:
        db.execute("CREATE TABLE tasks(id TEXT, prompt TEXT, status TEXT, updatedAt INTEGER)")
        db.execute("INSERT INTO tasks VALUES('fake','虚构任务','running',9999999999)")
    bridge = CanaanDotsMemoryBridge(store.path, str(dots), enabled=True)
    assert bridge.sync_dots_to_canaan() == 0
    store.forget("虚构", scrub_log=False)
    with sqlite3.connect(dots) as db:
        db.execute("UPDATE tasks SET status='done'")
    assert bridge.sync_dots_to_canaan() == 0


def test_bridge_aborts_outbound_batch_if_incognito_enabled(store, tmp_path, monkeypatch):
    from canaan_dots_memory_bridge import CanaanDotsMemoryBridge
    from canaan_privacy_shield import PrivacyShield
    store.add("虚构测试事实")
    bridge = CanaanDotsMemoryBridge(store.path, str(tmp_path / "absent.sqlite"), enabled=True)
    def mask(text):
        store.incognito = True
        return text, {}
    monkeypatch.setattr(PrivacyShield, "mask_text", mask)
    monkeypatch.setattr(bridge, "_push_to_dots_sqlite", lambda text: pytest.fail("无痕后不能继续传出"))
    assert bridge.sync_canaan_to_dots() == 0
