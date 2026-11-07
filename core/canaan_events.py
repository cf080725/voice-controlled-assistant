# -*- coding: utf-8 -*-
"""统一状态事件：执行器和中枢只往外发状态，桌宠、看板、托盘、共创平台桥接都来订阅。

- 7 种事件：listening 在听 / thinking 在想 / step 第 n/总 步 / awaiting_confirm 等确认 /
  success 成功 / failure 失败 / idle 空闲。
- 另有 agent：Muse / Dots 等智能体的进度（action 是提供方名，result 是 accepted / waiting / ok / failed / unknown），
  单独记在快照的 agent 字段里，不顶掉执行器自己的状态。
- 事件里只有动作名、步骤号和结果类别，不带原话、路径、提示词或执行结果正文。
- 发出就不管：放进有界队列由后台线程分发，队列满就丢；订阅方出错只影响它自己，不影响主流程。
- 状态快照（StatusTracker）：进行中的状态 90 秒没有新事件显示“待确认”，10 分钟没有就清回空闲。
"""
import json
import os
import queue
import re
import threading
import time

KINDS = ("listening", "thinking", "step", "awaiting_confirm", "success", "failure", "idle", "agent")
ACTIVE = ("listening", "thinking", "step", "awaiting_confirm")
RESULTS = ("", "ok", "failed", "blocked", "confirm", "accepted", "waiting", "unknown")
AGENT_NAMES = {"muse": "Muse", "dots": "Dots"}
STALE_AFTER = 90.0
CLEAR_AFTER = 600.0
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

_subscribers = []
_lock = threading.Lock()
_queue = queue.Queue(maxsize=256)
_worker = None


def make_event(kind, action="", step=0, total=0, result="", ts=None):
    """校验并只保留允许的字段；不合规的值丢掉而不是报错（发事件不能影响主流程）。"""
    if kind not in KINDS:
        return None
    action = action if isinstance(action, str) and _ACTION_RE.match(action) else ""
    step = step if type(step) is int and 0 <= step <= 999 else 0
    total = total if type(total) is int and 0 <= total <= 999 else 0
    result = result if result in RESULTS else ""
    return {"kind": kind, "action": action, "step": step, "total": total, "result": result,
            "ts": time.time() if ts is None else ts}


def subscribe(callback):
    """订阅事件，返回取消订阅的函数。回调在后台分发线程里调用，界面要自己切回主线程。"""
    with _lock:
        _subscribers.append(callback)

    def unsubscribe():
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)
    return unsubscribe


def _dispatch(event):
    with _lock:
        targets = list(_subscribers)
    for callback in targets:
        try:
            callback(event)
        except Exception:
            pass


def _run():
    while True:
        event = _queue.get()
        _dispatch(event)


def publish(kind, action="", step=0, total=0, result=""):
    """发出一个状态事件。永不抛异常、永不阻塞。"""
    global _worker
    try:
        event = make_event(kind, action, step, total, result)
        if event is None:
            return
        if _worker is None or not _worker.is_alive():
            with _lock:
                if _worker is None or not _worker.is_alive():
                    _worker = threading.Thread(target=_run, name="canaan-events", daemon=True)
                    _worker.start()
        _queue.put_nowait(event)
    except Exception:
        pass


def flush(timeout=2.0):
    """测试用：等队列里的事件分发完。"""
    end = time.time() + timeout
    while not _queue.empty() and time.time() < end:
        time.sleep(0.01)
    time.sleep(0.02)


class StatusTracker:
    """把事件流变成“现在是什么状态”，给看板、托盘读。"""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.last = None
        self.last_agent = None
        self.lock = threading.Lock()

    def __call__(self, event):
        with self.lock:
            if event.get("kind") == "agent":
                self.last_agent = dict(event)
            else:
                self.last = dict(event)

    def agent_label(self):
        with self.lock:
            last = dict(self.last_agent) if self.last_agent else None
        if last is None or self.clock() - last["ts"] >= CLEAR_AFTER:
            return ""
        return label(last)

    def view(self):
        now = self.clock()
        with self.lock:
            last = dict(self.last) if self.last else None
        view = self._view(now, last)
        view["agent"] = self.agent_label()
        return view

    def _view(self, now, last):
        if last is None:
            return {"state": "idle", "label": "空闲", "action": "", "step": 0, "total": 0, "result": "", "age_s": None, "stale": False}
        age = max(0.0, now - last["ts"])
        if age >= CLEAR_AFTER or last["kind"] == "idle":
            return {"state": "idle", "label": "空闲", "action": "", "step": 0, "total": 0, "result": "", "age_s": round(age, 1), "stale": False}
        stale = last["kind"] in ACTIVE and age >= STALE_AFTER
        return {"state": "pending" if stale else last["kind"], "label": "待确认" if stale else label(last),
                "action": last["action"], "step": last["step"], "total": last["total"], "result": last["result"],
                "age_s": round(age, 1), "stale": stale}


def label(event):
    """给界面用的短标签（中文），只由动作名和步骤号组成。"""
    kind, step, total = event["kind"], event["step"], event["total"]
    name = action_label(event["action"])
    if kind == "listening":
        return "在听"
    if kind == "thinking":
        return "在想"
    if kind == "step":
        return f"第 {step}/{total} 步" + (f" · {name}" if name else "") if total else (name or "执行中")
    if kind == "awaiting_confirm":
        return "等你确认" + (f" · {name}" if name else "")
    if kind == "success":
        return "办好了" + (f" · {name}" if name else "")
    if kind == "failure":
        return ("被拦下" if event["result"] == "blocked" else "没办成") + (f" · {name}" if name else "")
    if kind == "agent":
        who = AGENT_NAMES.get(event["action"], event["action"] or "智能体")
        result = event["result"]
        if result == "waiting":
            return f"{who} 等你处理"
        if result == "ok":
            return f"{who} 办好了"
        if result == "failed":
            return f"{who} 没办成"
        if result == "unknown":
            return f"{who} 连不上，结果未知"
        if result == "accepted":
            return f"{who} 已接下任务"
        return f"{who} 第 {step}/{total} 步" if total else f"{who} 执行中"
    return "空闲"


def action_label(action):
    if not action:
        return ""
    try:
        from canaan_intent import describe_action
        text = describe_action({"action": action, "param": ""})
        text = re.sub(r"[：:].*$", "", str(text)).strip("。？?！! ")
        return text[:12] if text else action
    except Exception:
        return action


_default_tracker = None


def default_tracker():
    global _default_tracker
    with _lock:
        if _default_tracker is None:
            _default_tracker = StatusTracker()
            _subscribers.append(_default_tracker)
        return _default_tracker


class StatusFileSink:
    """把最新状态写成小文件，给另一个进程（托盘）读。只写 view() 里的字段，原子替换。"""

    def __init__(self, path, tracker=None):
        self.path = path
        self.tracker = tracker or default_tracker()

    def __call__(self, _event):
        view = self.tracker.view()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**view, "written_at": time.time()}, f, ensure_ascii=False)
        os.replace(tmp, self.path)


def read_status_file(path, clock=time.time):
    """托盘用：读状态文件并按写入时间重新判断过期（文件可能是很久以前写的）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    written = data.get("written_at", 0)
    agent = data.get("agent", "") if clock() - written < CLEAR_AFTER else ""
    age = clock() - written + (data.get("age_s") or 0)
    if age >= CLEAR_AFTER or data.get("state") == "idle":
        return {"state": "idle", "label": "空闲", "agent": agent}
    if data.get("state") in ACTIVE and age >= STALE_AFTER:
        return {"state": "pending", "label": "待确认", "agent": agent}
    return {"state": data.get("state", "idle"), "label": data.get("label", ""), "agent": agent}
