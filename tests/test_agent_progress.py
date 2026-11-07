"""智能体进度：快照比较产生的事件、首次同步不提示、旧状态文件不重报、连不上报一次、通知 / 记忆 / 播报去向。"""
import json

import canaan_agent_watcher as aw
import canaan_events as ev


def task(tid, status, steps=None, title="查资料", note=""):
    return {"id": tid, "title": title, "status": status, "steps": steps, "current": "", "updated": 1, "note": note}


def test_diff_sequence_queued_step_waiting_done():
    prev = {}
    seq = [[task("m1", "queued")], [task("m1", "running", (1, 3))], [task("m1", "running", (2, 3))],
           [task("m1", "waiting", (2, 3))], [task("m1", "done", (3, 3), note="结论")]]
    results = []
    for tasks in seq:
        events, seen = aw.diff_tasks("muse", prev, tasks)
        prev = seen
        results += [(e["result"], e["step"], e["total"]) for e in events]
    assert results == [("accepted", 0, 0), ("", 1, 3), ("", 2, 3), ("waiting", 2, 3), ("ok", 3, 3)]


def test_unchanged_snapshot_is_silent_and_legacy_status_not_repeated():
    events, _ = aw.diff_tasks("dots", {"d1": {"status": "succeeded"}}, [task("d1", "done")])
    assert events == []                                     # 旧版文件存的是 succeeded，升级后不再报一遍
    events, _ = aw.diff_tasks("muse", {"m1": {"status": "running", "steps": [1, 3]}}, [task("m1", "running", (1, 3))])
    assert events == []


class Fake:
    def __init__(self, snaps):
        self.snaps = snaps

    def __call__(self):
        return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]


def make(tmp_path, muse_snaps, deliver=lambda text, kind: True, remember=None):
    return aw.AgentTaskWatcher(deliver, remember=remember, state_file=str(tmp_path / "seen.json"),
                               fetchers={"muse": Fake(muse_snaps)})


def snap(*tasks, online=True):
    return {"online": online, "tasks": list(tasks)}


def test_first_sync_silent_then_events_flow(tmp_path, monkeypatch):
    published, notices, memory, spoken = [], [], [], []
    monkeypatch.setattr(ev, "publish", lambda *a: published.append(a))
    import canaan_notify
    monkeypatch.setattr(canaan_notify, "post", lambda *a, **k: notices.append(a))
    w = make(tmp_path, [snap(task("old", "done")), snap(task("old", "done"), task("m2", "running", (1, 2))),
                        snap(task("old", "done"), task("m2", "done", (2, 2), title="周报", note="三条要点"))],
             deliver=lambda text, kind: spoken.append(text) or True, remember=memory.append)
    assert w.poll(first=True) == [] and not published and not notices
    w.poll()
    assert published[-1] == ("agent", "muse", 1, 2, "accepted") and not notices
    w.poll()
    assert published[-1] == ("agent", "muse", 2, 2, "ok")
    assert notices == [("agent", "Muse 办好了", "周报")]
    assert memory == ["[Muse] 完成任务【周报】：三条要点"] and "周报" in spoken[-1]
    assert json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))["tasks"]["muse:m2"]["status"] == "done"


def test_unreachable_reported_once_when_tasks_running(tmp_path, monkeypatch):
    published = []
    monkeypatch.setattr(ev, "publish", lambda *a: published.append(a))
    w = make(tmp_path, [snap(task("m1", "running", (1, 3))), snap(task("m1", "running", (1, 3))), snap(online=False), snap(online=False)])
    w.poll(first=True)
    w.poll()
    w.poll()
    w.poll()
    assert [p for p in published if p[4] == "unknown"] == [("agent", "muse", 0, 0, "unknown")]


def test_busy_speech_retried_then_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "publish", lambda *a: None)
    clock = [1000.0]
    busy = [True]
    spoken = []
    w = aw.AgentTaskWatcher(lambda text, kind: (not busy[0]) and (spoken.append(text) or True),
                            state_file=str(tmp_path / "s.json"), clock=lambda: clock[0],
                            fetchers={"muse": Fake([snap(), snap(task("m1", "failed"))])})
    w.poll(first=True)
    w.poll()
    assert spoken == [] and len(w.pending_speech) == 1          # 正在对话：先不说
    busy[0] = False
    w.poll()
    assert len(spoken) == 1 and "没办成" in spoken[0]
    busy[0] = True
    w.pending_speech.append((clock[0], "x"))
    clock[0] += aw.SPEECH_TTL + 1
    w.poll()
    assert w.pending_speech == []                               # 10 分钟说不出来就算了


def test_agent_events_kept_apart_from_executor_state():
    tracker = ev.StatusTracker()
    tracker(ev.make_event("step", "open_app", 1, 2))
    tracker(ev.make_event("agent", "muse", 2, 5, ""))
    view = tracker.view()
    assert view["state"] == "step" and view["agent"] == "Muse 第 2/5 步"
    assert ev.label(ev.make_event("agent", "dots", result="waiting")) == "Dots 等你处理"
