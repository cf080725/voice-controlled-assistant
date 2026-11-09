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
import tempfile
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
MAX_ENTRY_BYTES = 4096


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
                # 默认不写标题/正文原文；正则脱敏不能消除语义上的隐私。
                preview = show_preview(self.settings_path)
                masked = _mask(body) if preview else None
                masked_title = _mask(str(title)) if preview else None
                entry = {"kind": kind, "title": (masked_title or PRIVATE_TITLES[kind])[:40],
                         "body": "" if masked is None else masked[:120], "ts": now}
                self._append((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
                self.seen[key] = now
                self.sent[kind] = recent + [now]
            return True
        except Exception:
            return False

    def _append(self, entry):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        if size + len(entry) < MAX_QUEUE_BYTES:
            with open(self.path, "a+b") as stream:
                if size:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        stream.write(b"\n")
                stream.write(entry)
            return
        # 发布一个新文件，让读方即使看到相同长度也能识别轮转。
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = stream.name
                stream.write(entry)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


class NotifyReader:
    """托盘一侧：只读启动之后新追加的通知（不回放旧的）。"""

    def __init__(self, path=QUEUE_PATH, clock=time.time):
        self.path, self.clock = path, clock
        self.offset, self.file_key, self.discarding = 0, None, False
        try:
            stat = os.stat(path)
            self.offset = stat.st_size
            self.file_key = (stat.st_dev, stat.st_ino)
        except OSError:
            pass

    def read_new(self):
        try:
            with open(self.path, "rb") as stream:
                stat = os.fstat(stream.fileno())
                file_key = (stat.st_dev, stat.st_ino)
                if file_key != self.file_key or stat.st_size < self.offset:
                    self.offset, self.file_key, self.discarding = 0, file_key, False
                stream.seek(self.offset)
                data = stream.read(MAX_QUEUE_BYTES)
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        if not end:
            if len(data) == MAX_QUEUE_BYTES:
                self.offset += len(data)
                self.discarding = True
            return []
        self.offset += end
        start = data.find(b"\n") + 1 if self.discarding else 0
        self.discarding = False
        out, now = [], self.clock()
        for line in data[start:end].split(b"\n"):
            if not line or len(line) > MAX_ENTRY_BYTES:
                continue
            try:
                entry = json.loads(line.decode("utf-8"))
                if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
                    continue
                ts, title, body = entry.get("ts"), entry.get("title"), entry.get("body", "")
                if (type(ts) not in (int, float) or not now - MAX_AGE <= ts <= now
                        or not isinstance(title, str) or not title or not isinstance(body, str)):
                    continue
                title.encode("utf-8")
                body.encode("utf-8")
            except (ValueError, RecursionError):
                continue
            out.append(entry)
        return out


def should_show(entry, game_mode=False, settings_path=SETTINGS_PATH):
    return not game_mode and enabled(settings_path) and entry.get("kind") in KINDS


def notification_content(entry, settings_path=SETTINGS_PATH):
    if show_preview(settings_path):
        return entry["title"], entry.get("body", "")
    return PRIVATE_TITLES[entry["kind"]], ""


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
