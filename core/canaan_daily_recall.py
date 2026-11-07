# -*- coding: utf-8 -*-
"""每天第一次见面时主动提起“今天要记得”的重点（最多 3 条），全部来自本机数据，不调云端。

来源（按优先级）：
  1. 今天还没到点的提醒；
  2. 还在等你确认 / 等你处理的任务（团团自己的、外部客户端提出的、Muse / Dots 等你处理的）；
  3. 上次见面以后智能体办好的任务；
  4. 置顶记忆里像待办的那几条（含“交 / 截止 / 会议 / 记得”等字样），普通喜好不提。
什么时候说：每天一次（data/recall_state.json 记日期）；由主动开口调度在“启动后或空闲很久后第一次有人”时触发。
没有可说的就不说，不凑数。
"""
import json
import os
import re
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE_DIR, "data", "recall_state.json")
MAX_ITEMS = 3
_TODO_RE = re.compile(r"交|截止|提交|会议|开会|考试|答辩|报名|记得|别忘|deadline|ddl|到期|还款|面试|汇报", re.I)


def build_recall(reminders=(), pending=(), agent_done=(), pinned=(), now=None, max_items=MAX_ITEMS):
    """reminders: [{"due": iso, "what"}]；pending: [短描述]；agent_done: [短描述]；pinned: [事实文本]。
    返回不超过 max_items 条的列表。"""
    now = now or datetime.now()
    items = []
    for r in sorted(reminders, key=lambda r: r.get("due", "")):
        try:
            due = datetime.fromisoformat(r["due"])
        except (KeyError, ValueError, TypeError):
            continue
        if due.date() == now.date() and due >= now:
            items.append(f"{due.strftime('%H:%M')} {r.get('what', '')}".strip())
    items += [f"还在等你处理：{p}" for p in pending if p]
    items += [f"已经办好：{a}" for a in agent_done if a]
    items += [f"记得：{f}" for f in pinned if f and _TODO_RE.search(f)]
    seen, out = set(), []
    for item in items:
        item = item[:60]
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:max_items]


def recall_text(items, name="我"):
    if not items:
        return ""
    return "今天要记得：" + "；".join(f"{i}. {t}" for i, t in enumerate(items, 1)) + "。"


class RecallState:
    def __init__(self, path=STATE_PATH, clock=time.time):
        self.path, self.clock = path, clock

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def due(self):
        return self._load().get("day") != datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")

    def last_seen(self):
        return float(self._load().get("at", 0) or 0)

    def mark(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"day": datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d"), "at": self.clock()}, f)
        os.replace(tmp, self.path)
