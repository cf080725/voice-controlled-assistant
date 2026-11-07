"""Returning from idle must not start memory inference alongside voice work."""
from unittest.mock import Mock

import canaan_memory as memory
import pytest


class NoEmbed:
    def available(self):
        return False


@pytest.fixture
def manager(tmp_path, monkeypatch):
    store = memory.MemoryStore(str(tmp_path / "memory.db"), embedder=NoEmbed(), migrate=False)
    now = [0.0]
    result = memory.MemoryManager(store, clock=lambda: now[0])
    result.consolidate = Mock(return_value=[])
    worker = Mock()
    monkeypatch.setattr(memory.threading, "Thread", worker)
    yield result, now, worker
    store.db.close()


def test_deferred_return_keeps_turns_and_waits_for_idle_tick(manager):
    result, now, worker = manager
    result.on_turn("第一轮", "回答一")
    now[0] = 301
    result.on_turn("回来了", "回答二", defer_consolidation=True)
    worker.assert_not_called()
    assert result.turns == [("第一轮", "回答一"), ("回来了", "回答二")]
    now[0] = 600
    result.tick()
    worker.assert_not_called()
    now[0] = 602
    result.tick()
    worker.assert_called_once()
    assert worker.call_args.kwargs["args"][0] == [("第一轮", "回答一"), ("回来了", "回答二")]
    assert result.turns == []


def test_default_idle_boundary_still_closes_existing_session(manager):
    result, now, worker = manager
    result.on_turn("旧会话", "")
    now[0] = 301
    result.on_turn("新会话", "")
    worker.assert_called_once()
    assert result.turns == [("新会话", "")]


def test_privacy_revision_still_invalidates_deferred_buffer(manager):
    result, now, worker = manager
    result.on_turn("旧输入", "")
    result.store.incognito = True
    result.store.incognito = False
    now[0] = 301
    result.on_turn("新输入", "", defer_consolidation=True)
    worker.assert_not_called()
    assert result.turns == [("新输入", "")]


def test_hub_turn_logging_only_buffers_memory(monkeypatch):
    from types import SimpleNamespace

    import canaan_desk_os_hub as hub_module

    monkeypatch.setattr(hub_module.canaan_config, "CONVERSATION_LOG", False)
    monkeypatch.setattr(hub_module.canaan_gamemode, "active", lambda: False)
    hub = SimpleNamespace(profile=SimpleNamespace(privacy_revision=1, incognito=False), memory_mgr=Mock())
    hub_module.CanaanDeskOSHub.log_conversation(hub, None, "这轮问题", {"action": "chat"}, "这轮回答")
    hub.memory_mgr.on_turn.assert_called_once_with(
        "这轮问题", "这轮回答", expected_revision=1, defer_consolidation=True)
