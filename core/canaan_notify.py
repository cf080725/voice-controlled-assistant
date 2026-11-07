# -*- coding: utf-8 -*-
"""Windows 系统通知：hub 只往一个小队列文件里追加短通知，托盘读出来用系统通知弹出。

- 只有这几类会弹：confirm 等你确认 / failure 没办成 / agent 智能体完成、失败、等你处理 /
  reminder 到点的提醒 / message 重要消息 / recall 今天要记得。普通进度不弹。
- 默认只写通用状态标题，不写原文；主人明确开启 show_preview 后才写脱敏预览。
- 同一个 key 10 分钟内只弹一次；每类每分钟最多 3 条。
- 游戏模式、主人关掉系统通知时，托盘一侧直接丢弃；Windows 的勿扰 / 专注模式由系统自己处理。
- 发通知永不抛异常、永不阻塞主流程。
"""
import json
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_PATH = os.environ.get("CANAAN_NOTIFY_QUEUE") or os.path.join(BASE_DIR, "data", "notify_queue.jsonl")
SETTINGS_PATH = os.path.join(BASE_DIR, "data", "notify_settings.json")
KINDS = ("confirm", "failure", "agent", "reminder", "message", "recall")
PRIVATE_TITLES = {"confirm": "有操作等你确认", "failure": "有任务未完成", "agent": "智能体状态有更新",
                 "reminder": "有提醒到点了", "message": "有重要消息", "recall": "有待办需要查看"}
DEDUP_SECONDS = 600.0
PER_KIND_PER_MINUTE = 3
MAX_AGE = 120.0          # 托盘读到时已经过了这么久的通知不再弹（托盘没开时积下的）
MAX_QUEUE_BYTES = 256 * 1024


def _mask(text):
    if not text:
        return ""
    try:
        from canaan_privacy_shield import PrivacyShield
        return PrivacyShield.mask_text(text)[0]
    except Exception:
        return None


def enabled(path=SETTINGS_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", True))
    except (OSError, ValueError, AttributeError):
        return True


def set_enabled(value, path=SETTINGS_PATH):
    _set_setting("enabled", bool(value), path)


def show_preview(path=SETTINGS_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("show_preview") is True
    except (OSError, ValueError, AttributeError):
        return False


def set_preview_enabled(value, path=SETTINGS_PATH):
    _set_setting("show_preview", bool(value), path)


def _set_setting(key, value, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            settings = json.load(f)
        if not isinstance(settings, dict):
            settings = {}
    except (OSError, ValueError):
        settings = {}
    settings[key] = value
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f)
    os.replace(tmp, path)


class Notifier:
    """hub 一侧：去重、限频、脱敏后追加写队列文件。"""

    def __init__(self, path=QUEUE_PATH, clock=time.time, settings_path=SETTINGS_PATH):
        self.path, self.clock, self.settings_path = path, clock, settings_path
        self.lock = threading.Lock()
        self.seen = {}
        self.sent = {k: [] for k in KINDS}

    def post(self, kind, title, body="", key=None):
        try:
            if kind not in KINDS or not title:
                return False
            now = self.clock()
            key = key or f"{kind}:{title}:{body}"
            with self.lock:
                if now - self.seen.get(key, -1e9) < DEDUP_SECONDS:
                    return False
                recent = [t for t in self.sent[kind] if now - t < 60]
                if len(recent) >= PER_KIND_PER_MINUTE:
                    self.sent[kind] = recent
                    return False
                self.seen[key] = now
                self.sent[kind] = recent + [now]
                # 默认不写标题/正文原文；正则脱敏不能消除语义上的隐私。
                preview = show_preview(self.settings_path)
                masked = _mask(body) if preview else None
                masked_title = _mask(str(title)) if preview else None
                entry = {"kind": kind, "title": (masked_title or PRIVATE_TITLES[kind])[:40],
                         "body": "" if masked is None else masked[:120], "ts": now}
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                if os.path.exists(self.path) and os.path.getsize(self.path) > MAX_QUEUE_BYTES:
                    open(self.path, "w", encoding="utf-8").close()   # 读方发现文件变小会从头读
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except Exception:
            return False


class NotifyReader:
    """托盘一侧：只读启动之后新追加的通知（不回放旧的）。"""

    def __init__(self, path=QUEUE_PATH, clock=time.time):
        self.path, self.clock = path, clock
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
        end = data.rfind(b"\n") + 1                # 只取完整的行，半行留到下次
        self.offset += end
        out, now = [], self.clock()
        for line in data[:end].decode("utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") in KINDS and now - entry.get("ts", 0) <= MAX_AGE:
                out.append(entry)
        return out


def should_show(entry, game_mode=False, settings_path=SETTINGS_PATH):
    return not game_mode and enabled(settings_path) and entry.get("kind") in KINDS


_default = None
_default_lock = threading.Lock()


def default():
    global _default
    with _default_lock:
        if _default is None:
            _default = Notifier()
        return _default


def post(kind, title, body="", key=None):
    return default().post(kind, title, body, key)


def status_event_sink(event):
    """订阅统一状态事件：等确认、失败各弹一条（只有动作名，不带原话）。"""
    import canaan_events
    if event.get("kind") == "awaiting_confirm":
        name = canaan_events.action_label(event.get("action", ""))
        post("confirm", "等你确认", name or "有一个操作等你确认", key=f"confirm:{event.get('action')}:{int(event.get('ts', 0) // 60)}")
    elif event.get("kind") == "failure":
        name = canaan_events.action_label(event.get("action", ""))
        post("failure", "被拦下了" if event.get("result") == "blocked" else "没办成", name, key=f"failure:{event.get('action')}:{int(event.get('ts', 0) // 60)}")
