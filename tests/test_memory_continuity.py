"""跨重启接着聊、每天第一次见面提起重点（最多 3 条、一天一次、正在对话时稍后再说、没有就不说）。"""
import json
import time
from datetime import datetime, timedelta

import canaan_daily_recall as dr
import canaan_memory
import canaan_proactive
from canaan_intent import ConversationMemory


def _log(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for when, user, action, spoken in entries:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when)), "user": user,
                                "action": action, "param": "", "spoken": spoken}, ensure_ascii=False) + "\n")


def test_recent_conversation_only_within_window(tmp_path):
    now = time.time()
    path = tmp_path / "c.jsonl"
    _log(path, [(now - 9000, "很早的话", "chat", "嗯"), (now - 600, "帮我查天气", "chat", "晴"),
                (now - 300, "那明天呢", "chat", "多云"), (now - 60, "", "chat", "")])
    turns = canaan_memory.recent_conversation(str(path), max_turns=4, within=7200, now=now)
    assert [u for u, _ in turns] == ["帮我查天气", "那明天呢"] and turns[-1][1]["reply"] == "多云"
    assert canaan_memory.recent_conversation(str(tmp_path / "missing.jsonl")) == []


def test_seed_survives_grace_then_expires():
    clock = [100.0]
    mem = ConversationMemory(max_turns=4, idle_expire=300.0, clock=lambda: clock[0])
    mem.seed([("帮我查天气", {"action": "chat", "param": "", "reply": "晴"})], grace=1500.0)
    clock[0] += 1700
    assert mem.messages()[0]["content"] == "帮我查天气"          # 重启后 30 分钟内开口还接得上
    clock[0] += 200
    assert mem.messages() == []


def test_build_recall_priorities_and_limit():
    now = datetime(2026, 10, 14, 9, 0)
    reminders = [{"due": "2026-10-14T15:00:00", "what": "交课题中期报告"}, {"due": "2026-10-13T15:00:00", "what": "昨天的"},
                 {"due": "2026-10-15T10:00:00", "what": "明天的"}]
    items = dr.build_recall(reminders, ["关闭应用"], ["Muse【查文献】"], ["我喜欢喝咖啡", "周五要交实验报告"], now=now)
    assert items == ["15:00 交课题中期报告", "还在等你处理：关闭应用", "已经办好：Muse【查文献】"]
    assert dr.build_recall(pinned=["我喜欢喝咖啡", "周五要交实验报告"], now=now) == ["记得：周五要交实验报告"]
    assert dr.recall_text([]) == ""


class Policy:
    def __init__(self, now):
        self.now = now
        self.state = {"reminders": []}

    def clock(self):
        return self.now

    def due_reminders(self):
        return []

    def allowed(self, kind):
        return True, ""

    def record_spoken(self, kind):
        pass

    def save(self):
        pass


def test_scheduler_recall_once_a_day_and_retries_when_busy(tmp_path):
    now = datetime(2026, 10, 14, 9, 0)
    policy = Policy(now)
    said, busy = [], [True]
    state = dr.RecallState(str(tmp_path / "r.json"), clock=lambda: policy.now.timestamp())
    sched = canaan_proactive.ProactiveScheduler(policy, lambda text, kind: (not busy[0]) and (said.append((kind, text)) or True),
                                                recall=lambda: "今天要记得：1. 15:00 交报告。", recall_state=state, recall_warmup=0)
    sched.step(idle=5, notif_ok=True)                 # 一启动人就在，但正在对话
    assert said == [] and sched._recall_pending
    busy[0] = False
    sched.step(idle=5, notif_ok=True)                 # 下一轮再试
    assert [k for k, _ in said] == ["recall"]
    sched._was_idle_long = True
    sched.step(idle=5, notif_ok=True)                 # 同一天再回来不重复
    assert len(said) == 1
    policy.now = now + timedelta(days=1)
    sched._was_idle_long = True
    sched.step(idle=5, notif_ok=True)
    assert [k for k, _ in said].count("recall") == 2


def test_scheduler_skips_when_nothing_to_say(tmp_path):
    policy = Policy(datetime(2026, 10, 14, 15, 0))
    said = []
    state = dr.RecallState(str(tmp_path / "r.json"), clock=lambda: policy.now.timestamp())
    sched = canaan_proactive.ProactiveScheduler(policy, lambda t, k: said.append(k) or True, recall=lambda: "", recall_state=state, recall_warmup=0)
    sched.step(idle=5, notif_ok=True)
    assert said == [] and not state.due() and not sched._recall_pending


def test_recall_waits_for_warmup_after_start(tmp_path):
    policy = Policy(datetime(2026, 10, 14, 9, 0))
    said = []
    state = dr.RecallState(str(tmp_path / "r.json"), clock=lambda: policy.now.timestamp())
    sched = canaan_proactive.ProactiveScheduler(policy, lambda t, k: said.append(k) or True,
                                                recall=lambda: "今天要记得：1. 交报告。", recall_state=state, recall_warmup=60)
    sched.step(idle=5, notif_ok=True)
    assert said == [] and sched._recall_pending          # 刚启动：先不说，保持待提起
    sched._created_at -= 61
    sched.step(idle=5, notif_ok=True)
    assert said == ["recall"]

def test_morning_greeting_still_happens_when_nothing_to_recall(tmp_path):
    policy = Policy(datetime(2026, 10, 14, 9, 0))
    said = []
    state = dr.RecallState(str(tmp_path / "r.json"), clock=lambda: policy.now.timestamp())
    sched = canaan_proactive.ProactiveScheduler(policy, lambda t, k: said.append(k) or True,
                                                recall=lambda: "", recall_state=state, recall_warmup=60)
    sched.step(idle=5, notif_ok=True)
    assert said == []                                     # 预热中：不问早安也不提重点
    sched._created_at -= 61
    sched.step(idle=5, notif_ok=True)
    assert said == ["greeting"]                           # 没有重点可说，补一句早安