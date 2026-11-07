# -*- coding: utf-8 -*-
"""灵动岛的状态通道：hub 写、岛（另一个进程）读；岛上的点击写回动作队列，hub 读。

- 状态文件 data/island_state.json：只有模式、要显示的那一行字、音量、提示队列（提示已在提示中心里按隐私规则处理过）。
  原子替换写入，每次写 seq + 1，岛按 seq 判断有没有变化。
- 动作队列 data/island_actions.jsonl：岛只能报告“点了哪里”（click / notice_action），由 hub 决定做什么。
- 音量这种高频变化做节流（每秒最多 12 次），文字和模式变化立刻写。
"""
import json
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE_DIR, "data", "island_state.json")
ACTION_PATH = os.path.join(BASE_DIR, "data", "island_actions.jsonl")
MODES = ("idle", "listening", "thinking", "speaking", "confirm", "failure", "notice", "hidden")
LEVEL_INTERVAL = 1 / 12
ACTION_KINDS = ("click", "notice_action", "confirm", "cancel", "quick")
QUICK_ACTIONS = ("control", "briefing", "muse", "dots", "kws", "hide", "phone")
_KEEP = object()


class IslandState:
    def __init__(self, path=STATE_PATH, clock=time.monotonic):
        self.path, self.clock = path, clock
        self.lock = threading.Lock()
        self.data = {"mode": "idle", "text": "", "detail": "", "level": 0.0, "notices": [], "seq": 0,
                     "present": None, "secondary": None, "settings": None}
        self._last_level_write = 0.0

    def update(self, mode=None, text=None, detail=None, notices=None, present=_KEEP, secondary=_KEEP, settings=_KEEP,
               confirm_id=_KEEP):
        """present：岛现在要呈现的那条提示（形态 variant、到达序号 arrive 等）；secondary：分离小圆的来源；
        settings：岛的设置（提示音等）；confirm_id：岛上确认卡片对应的那一次确认。不传就保持原样，传 None 表示清掉。"""
        with self.lock:
            changed = False
            for key, value in (("present", present), ("secondary", secondary), ("settings", settings)):
                if value is not _KEEP and value != self.data.get(key):
                    self.data[key], changed = (dict(value) if isinstance(value, dict) else None), True
            if confirm_id is not _KEEP and confirm_id != self.data.get("confirm_id"):
                self.data["confirm_id"], changed = confirm_id, True
            if mode is not None and mode in MODES and mode != self.data["mode"]:
                self.data["mode"], changed = mode, True
            for key, value in (("text", text), ("detail", detail)):
                if value is not None:
                    value = str(value)[:120]
                    if value != self.data[key]:
                        self.data[key], changed = value, True
            if notices is not None:
                notices = [dict(n) for n in notices][:20]
                if notices != self.data["notices"]:
                    self.data["notices"], changed = notices, True
            if changed:
                self._write_locked()
            return changed

    def level(self, value):
        """说话 / 听的时候的音量（0–1），节流写入。"""
        with self.lock:
            self.data["level"] = max(0.0, min(1.0, float(value)))
            now = self.clock()
            if now - self._last_level_write >= LEVEL_INTERVAL:
                self._last_level_write = now
                self._write_locked()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def _write_locked(self):
        self.data["seq"] += 1
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


def read_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def post_action(kind, payload=None, path=ACTION_PATH, clock=time.time):
    """岛这边：记一次点击。只接受已知的类型，内容只有编号。
    confirm / cancel 带上岛当时显示的确认项文字（hub 核对是同一个才执行）；quick 只能是固定的几个快捷操作。"""
    if kind not in ACTION_KINDS:
        return False
    payload = payload if isinstance(payload, dict) else {}
    if kind == "quick" and payload.get("action") not in QUICK_ACTIONS:
        return False
    entry = {"kind": kind, "id": str(payload.get("id", ""))[:64], "action": str(payload.get("action", ""))[:32], "ts": clock()}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


class ActionReader:
    """hub 这边：只读启动之后的新动作。"""

    def __init__(self, path=ACTION_PATH):
        self.path = path
        try:
            self.offset = os.path.getsize(path)
        except OSError:
            self.offset = 0

    def read_new(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n") + 1
        self.offset += end
        out = []
        for line in data[:end].decode("utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") in ACTION_KINDS:
                out.append(entry)
        if self.offset > 256 * 1024:
            try:
                open(self.path, "w").close()
                self.offset = 0
            except OSError:
                pass
        return out
