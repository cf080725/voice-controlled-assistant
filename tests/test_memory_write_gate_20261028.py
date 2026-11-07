# -*- coding: utf-8 -*-
"""记忆写入侧两处缺陷的最小复现（2026-10-28 接管核实）。

1) 日期列接受 epoch 数字：episodes.started_at 存成 13 位毫秒、ended_at 存成 10 位秒，
   提示词里的日期前缀被 [5:10] 切成乱码。
2) 语音侧把整理交给 tick()，演示/游戏模式长期不叫 tick()，攒着的轮次随进程退出永久丢。
"""
import time

import canaan_memory as cm
import pytest


class NoEmbed:
    def available(self):
        return False

    def encode(self, texts, query=False):
        raise AssertionError("这个测试不该走向量编码")


@pytest.fixture
def store(tmp_path):
    s = cm.MemoryStore(str(tmp_path / "memory.db"), embedder=NoEmbed(), migrate=False)
    yield s
    s.db.close()


def _epoch_rows(store, started, ended):
    """绕过 add_episode，模拟老 Dots 桥直接 INSERT 留下的脏行。"""
    store.db.execute("INSERT INTO episodes(summary,started_at,ended_at) VALUES(?,?,?)",
                     ("团子今天晒了太阳", started, ended))
    store.db.execute("INSERT INTO episodes_fts(rowid,summary) VALUES(?,?)",
                     (store.db.execute("SELECT last_insert_rowid()").fetchone()[0], "团子今天晒了太阳"))
    store.db.commit()


def test_add_episode_converts_epoch_numbers_on_write(store):
    # 1791679566895 毫秒 / 1791679651 秒，同一时刻的两种写法（本机 2026-10-10 前后）
    eid = store.add_episode("聊了天气", "1791679566895", "1791679651")
    row = store.db.execute("SELECT started_at,ended_at FROM episodes WHERE id=?", (eid,)).fetchone()
    assert row["started_at"] == time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(1791679566.895)), row
    assert row["ended_at"] == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(1791679651)), row


def test_add_fact_converts_epoch_created_at_on_write(store):
    fid = store.add("主人住在合肥", created_at=1791679651)
    row = store.db.execute("SELECT created_at,updated_at FROM facts WHERE id=?", (fid,)).fetchone()
    assert row["created_at"] == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(1791679651))
    assert row["updated_at"] == row["created_at"]


def test_epoch_numbers_only_normalized_when_plausible(store):
    assert cm._as_time_text("20261013") == "20261013"  # 8 位是写法不是时间戳，别乱改
    assert cm._as_time_text("2026-10-13 14:45:55") == "2026-10-13 14:45:55"
    assert cm._as_time_text(None) is None


def test_legacy_epoch_row_still_renders_a_real_date(store):
    # 库里已有的脏行（生产库 6/8/9/10/11 号 episode 就是这个形状）
    _epoch_rows(store, "1791679566895", "1791679651")
    expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(1791679651))
    lines = store.context_lines("团子晒太阳")
    assert any(line.startswith(expected[5:10].replace("-", "月") + "日的聊天：") for line in lines), lines
    assert not any("79651日" in line for line in lines), lines
    board = store.overview()["episodes"]
    assert board[0]["ended_at"] == expected, board


def test_pending_turns_flush_when_nothing_calls_tick(store):
    """语音路径 on_turn(defer_consolidation=True) + 没人调 tick()：一小时后自己收尾。"""
    clock = {"t": 1000.0}
    calls = []

    def fake_llm(model, prompt, schema, **kw):
        calls.append(sorted(schema["properties"]))
        if "summary" in schema["properties"]:
            return {"summary": "虚构会话摘要"}
        if "facts" in schema["properties"]:
            return {"facts": []}
        return {"op": "NOOP", "id": 0}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cm, "_llm_json", fake_llm)
    try:
        mgr = cm.MemoryManager(store, clock=lambda: clock["t"], stale_flush_sec=3600.0)
        mgr.on_turn("第一句", "回复一", defer_consolidation=True)
        clock["t"] += 300.0  # 过了 5 分钟空闲阈值
        mgr.on_turn("第二句", "回复二", defer_consolidation=True)
        assert len(mgr.turns) == 2, "没被调 tick 时不该提前整理"
        clock["t"] += 3601.0  # 演示模式一直挡着 tick()
        mgr.on_turn("第三句", "回复三", defer_consolidation=True)
        deadline = time.time() + 5.0
        while store.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0:
            if time.time() > deadline:
                pytest.fail("超期未整理的轮次没有落进 episodes（calls=%s）" % calls)
            time.sleep(0.05)
        assert len(mgr.turns) == 1, "只该把超期的那批收走"
        assert any("summary" in c for c in calls)
    finally:
        monkey.undo()


def test_stale_flush_respects_incognito(store):
    clock = {"t": 0.0}
    mgr = cm.MemoryManager(store, clock=lambda: clock["t"], stale_flush_sec=1.0)
    store.incognito = True
    mgr.on_turn("无痕第一句", "", defer_consolidation=True)
    clock["t"] = 100.0
    mgr.on_turn("无痕第二句", "", defer_consolidation=True)
    assert mgr.turns == []
    assert store.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
