# -*- coding: utf-8 -*-
"""
智能体（OpenMuse / OpenDots）任务进度监听：不改任何任务，只读状态，变化时提示主人。

每次轮询用 canaan_agent_board_data 取统一格式的任务（标题、进度说明已脱敏），和上一次比较，产生：
  accepted 已接下 / step 第 k/n 步 / waiting 等你处理 / ok 办好了 / failed 没办成 / unknown 连不上、结果未知
去向：
  - 统一状态事件 agent 类型（只有提供方、步骤号、结果类别）→ 桌宠步骤卡、托盘提示文字、看板
  - Windows 系统通知：只在办好了、没办成、等你处理时弹（标题已脱敏）
  - 说出来：办好了、没办成、等你处理（正在对话时稍后再说，10 分钟内说不出就算了）
  - 记忆：办好了的任务写一条摘要（标题和结论前 60 字），以后能问“上次让你查的那个呢”
首次同步只记下现有任务，不提示。
"""
import json
import logging
import os
import threading
import time

from canaan_agent_board_data import MUSE_API as MUSE_API_URL, DOTS_API as DOTS_API_URL, STATUS_MAP  # noqa: F401  接入设置会改这两个地址

logger = logging.getLogger("canaan_agent_watcher")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "seen_agent_tasks.json")
NAMES = {"muse": "Muse", "dots": "Dots"}
ACTIVE = ("queued", "running", "waiting", "scheduled", "paused")
SPEECH_TTL = 600.0
MAX_SEEN = 500


def _norm_status(status):
    """旧版状态文件里存的是原始状态（succeeded 等），统一换成看板用的状态，免得升级后把历史任务再报一遍。"""
    return status if status in ("running", "queued", "waiting", "done", "failed", "paused", "cancelled", "scheduled") \
        else STATUS_MAP.get(status, status)


def diff_tasks(provider, prev, tasks):
    """比较前后两次快照。prev: {任务编号: {"status", "steps"}}；tasks: 统一格式任务列表。
    返回 (事件列表, 新的 prev)。只看属于这个提供方的记录。"""
    events, now_seen = [], {}
    for t in tasks:
        tid = t.get("id")
        if not tid:
            continue
        status = t["status"]
        steps = list(t["steps"]) if t.get("steps") else None
        now_seen[tid] = {"status": status, "steps": steps, "provider": provider, "title": t.get("title", "")[:40]}
        old = prev.get(tid)
        old_status = _norm_status(old.get("status")) if old else None
        base = {"provider": provider, "id": tid, "title": t.get("title", ""), "note": t.get("note", ""),
                "step": steps[0] if steps else 0, "total": steps[1] if steps else 0}
        if old is None:
            if status in ("queued", "running", "scheduled"):
                events.append({**base, "result": "accepted"})
            elif status in ("waiting", "done", "failed"):
                events.append({**base, "result": {"waiting": "waiting", "done": "ok", "failed": "failed"}[status]})
            continue
        if status == old_status:
            if status == "running" and steps and steps != old.get("steps"):
                events.append({**base, "result": ""})
            continue
        if status == "waiting":
            events.append({**base, "result": "waiting"})
        elif status == "done":
            events.append({**base, "result": "ok"})
        elif status == "failed":
            events.append({**base, "result": "failed"})
        elif status == "running":
            events.append({**base, "result": ""})
    return events, now_seen


def speech_for(event):
    who, title = NAMES.get(event["provider"], "智能体"), event["title"] or "后台任务"
    if event["result"] == "ok":
        note = f"摘要是：{event['note']}。" if event["note"] else ""
        return f"你交给 {who} 的任务【{title}】办完啦。{note}详细结果在看板里。"
    if event["result"] == "failed":
        return f"{who} 在做【{title}】时没办成，要打开看板看看吗？"
    if event["result"] == "waiting":
        return f"{who} 做【{title}】做到一半，在等你处理。"
    if event["result"] == "unknown":
        return f"{who} 现在连不上，刚才在做的任务结果还不知道，我先不重复提交。"
    return ""


NOTICE_KIND = {"accepted": "progress", "": "progress", "waiting": "waiting", "ok": "done", "failed": "failed",
               "unknown": "info"}


def post_notice(event, center=None):
    """进统一提示中心：同一个任务一条提示（key 固定），进度往前走只更新进度条；标题已由看板数据脱敏。"""
    import canaan_notice_center
    center = center or canaan_notice_center.default()
    kind = NOTICE_KIND.get(event["result"])
    if kind is None:
        return None
    who = NAMES.get(event["provider"], "智能体")
    step, total = event.get("step") or 0, event.get("total") or 0
    progress = int(step * 100 / total) if total else None
    title = event["title"] or "后台任务"
    if event["result"] == "unknown":
        title = "连不上了，结果还不知道"
    elif kind == "progress" and total:
        title = f"{title}（{step}/{total}）"
    return center.post(who, kind, title=title, progress=progress, key=f"agent:{event['provider']}:{event['id'] or 'conn'}")


class AgentTaskWatcher(threading.Thread):
    def __init__(self, deliver_func, poll_interval=4.0, initial_sync=True, remember=None,
                 fetchers=None, state_file=STATE_FILE, clock=time.time, memory_revision=None):
        super().__init__(daemon=True, name="AgentTaskWatcher")
        self.deliver = deliver_func
        self.remember = remember
        self.memory_revision = memory_revision
        self.poll_interval = poll_interval
        self.initial_sync = initial_sync
        self.state_file = state_file
        self.clock = clock
        self._fetchers = fetchers
        self._halt = threading.Event()
        self.seen_tasks = {}
        self.online = {}
        self.pending_speech = []
        self._load_state()

    # ---- 状态文件 ----
    def _load_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                self.seen_tasks = json.load(f).get("tasks", {})
        except (OSError, ValueError, AttributeError):
            self.seen_tasks = {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            items = sorted(self.seen_tasks.items(), key=lambda kv: kv[1].get("time", 0))[-MAX_SEEN:]
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"tasks": dict(items), "updated_at": self.clock()}, f, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning(f"保存智能体任务状态失败: {e}")

    @staticmethod
    def _get_muse_token(_self=None, timeout=2.5):
        from canaan_agent_coordinator import OpenMuseProvider
        return OpenMuseProvider.get_token(timeout=timeout)

    def fetchers(self):
        if self._fetchers is not None:
            return self._fetchers
        import canaan_agent_board_data as board
        return {"muse": board.fetch_muse, "dots": board.fetch_dots}

    # ---- 轮询 ----
    def poll(self, first=False):
        events, changed = [], False
        for provider, fetch in self.fetchers().items():
            # 在读远端结果前捕获版本；已有任务沿用第一次看到它时的版本。
            revision = self.memory_revision() if self.memory_revision else None
            try:
                snap = fetch()
            except Exception:
                snap = {"online": False, "tasks": []}
            was_online = self.online.get(provider)
            self.online[provider] = bool(snap.get("online"))
            if not snap.get("online"):
                # 原来在线、还有没做完的任务，现在连不上：报一次“结果未知”，不重复提交
                busy = any(v.get("provider") == provider and _norm_status(v.get("status")) in ACTIVE for v in self.seen_tasks.values())
                if was_online and busy and not first:
                    events.append({"provider": provider, "id": "", "title": "", "note": "", "step": 0, "total": 0, "result": "unknown"})
                continue
            prev = {v.get("id", k): v for k, v in self.seen_tasks.items() if v.get("provider", provider) == provider}
            found, now_seen = diff_tasks(provider, prev, snap.get("tasks") or [])
            for tid, rec in now_seen.items():
                rec["memory_revision"] = prev.get(tid, {}).get("memory_revision", revision)
                if prev.get(tid, {}).get("status") != rec["status"] or prev.get(tid, {}).get("steps") != rec["steps"] \
                        or "provider" not in prev.get(tid, {}) or "memory_revision" not in prev.get(tid, {}):
                    key = f"{provider}:{tid}"
                    self.seen_tasks[key] = {**rec, "id": tid, "time": self.clock()}
                    if tid != key and self.seen_tasks.get(tid, {}).get("provider", provider) == provider:
                        self.seen_tasks.pop(tid, None)
                    changed = True
            for event in found:
                event["memory_revision"] = now_seen[event["id"]]["memory_revision"]
            if not first:
                events += found
        if changed:
            self._save_state()
        for event in events:
            self.emit(event)
        self._flush_speech()
        return events

    def emit(self, event):
        who = NAMES.get(event["provider"], "智能体")
        try:
            import canaan_events
            canaan_events.publish("agent", event["provider"], event["step"], event["total"], event["result"])
        except Exception:
            pass
        if event["result"] in ("ok", "failed", "waiting"):
            try:
                import canaan_notify
                head = {"ok": "办好了", "failed": "没办成", "waiting": "等你处理"}[event["result"]]
                canaan_notify.post("agent", f"{who} {head}", event["title"], key=f"agent:{event['provider']}:{event['id']}:{event['result']}")
            except Exception:
                pass
        try:
            post_notice(event)
        except Exception:
            pass
        if event["result"] == "ok" and self.remember:
            try:
                note = f"：{event['note'][:60]}" if event["note"] else ""
                text = f"[{who}] 完成任务【{event['title']}】{note}"
                if self.memory_revision:
                    self.remember(text, expected_revision=event.get("memory_revision", -1),
                                  origin=f"agent:{event['provider']}:{event['id']}")
                else:  # 保留只接收一个参数的旧回调。
                    self.remember(text)
            except Exception as e:
                logger.warning(f"写入任务记忆失败: {e}")
        text = speech_for(event)
        if text:
            self.pending_speech.append((self.clock(), text))

    def _flush_speech(self):
        keep = []
        for ts, text in self.pending_speech:
            if self.clock() - ts > SPEECH_TTL:
                continue
            try:
                if not self.deliver(text, "agent_task"):
                    keep.append((ts, text))      # 正在对话，下一轮再说
            except Exception as e:
                logger.error(f"播报失败: {e}")
        self.pending_speech = keep

    def run(self):
        if self.initial_sync:
            try:
                self.poll(first=True)
            except Exception:
                pass
        while not self._halt.is_set():
            try:
                self.poll()
            except Exception as e:
                logger.debug(f"智能体监听出错: {e}")
            self._halt.wait(self.poll_interval)

    def stop(self):
        self._halt.set()
