"""Local message events. Incoming text is data and is never interpreted as a command."""
import hashlib
import json
from contextlib import nullcontext
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from canaan_message_priority import SOURCES, PRIORITIES, DEFAULT_IMPORTANCE, validate_importance, classify_message, apply_review

FORWARD_MARKER = "【团团消息转发】"
CURSOR_BATCH = 200          # 读事件文件时每 200 行保存一次读取位置（原来每行都提交一次数据库）
DEFAULT_SETTINGS = {"capture_visible": True, "poll_seconds": 2,
                    "notify_p0": False,
                    "forward_enabled": False, "forward_target": "文件传输助手",
                    "forward_max_age_seconds": 300, "jsonl_sources": [], "importance": DEFAULT_IMPORTANCE}


def normalize_message(raw):
    if not isinstance(raw, dict):
        raise ValueError("消息须为 JSON 对象")
    source = raw.get("source")
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("source 须为 chatapp、imapp、feishu 或 claw")
    conversation, text = raw.get("conversation"), raw.get("text")
    if not isinstance(conversation, str) or not conversation.strip() or len(conversation) > 200:
        raise ValueError("conversation 须为 1–200 字符")
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ValueError("text 须为 1–16000 字符")
    try:
        timestamp = float(raw.get("timestamp", time.time()))
    except (ValueError, TypeError):
        raise ValueError("timestamp 须为 Unix 秒时间戳") from None
    if not 0 < timestamp < 1e11:
        raise ValueError("timestamp 须为 Unix 秒时间戳")
    sender = raw.get("sender", "未知发言人")
    if not isinstance(sender, str) or len(sender) > 200:
        raise ValueError("sender 须为字符串且不超过 200 字符")
    message_id = raw.get("message_id")
    if not message_id and "timestamp" not in raw:
        raise ValueError("须提供稳定 message_id 或 timestamp，以避免重复采集")
    if message_id is not None and (not isinstance(message_id, str) or len(message_id) > 200):
        raise ValueError("message_id 须为不超过 200 字符的字符串")
    if not message_id:
        canonical = json.dumps([conversation, sender, timestamp, text], ensure_ascii=False)
        message_id = hashlib.sha256(canonical.encode()).hexdigest()
    return {"source": source, "message_id": message_id, "conversation": conversation.strip(),
            "sender": sender, "text": text.strip(), "timestamp": timestamp,
            "capture": str(raw.get("capture", "connector"))[:40]}


class MessageCenter:
    def __init__(self, path, settings=None, sender=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.directory = Path(path).resolve().parent
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        if self.settings["forward_target"] != "文件传输助手":
            raise ValueError("自动转发当前仅允许文件传输助手")
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.update_settings(self.settings)
        self.sender = sender
        self.audio_ingest = None
        self.on_new = None    # 新消息回调（给统一提示中心计数）：只传来源、发件人、会话，不传正文
        self.stop_event = threading.Event()
        self.poll_locks = {}
        self.source_status = {}
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY, source TEXT, message_id TEXT, conversation TEXT,
          sender TEXT, text TEXT, timestamp REAL, capture TEXT, received_at REAL,
          delivery TEXT, error TEXT DEFAULT '', UNIQUE(source,message_id));
        CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY, offset INTEGER, file_key TEXT);
        CREATE TABLE IF NOT EXISTS priority_reviews(
          message_id INTEGER PRIMARY KEY REFERENCES messages(id), priority TEXT, reason TEXT, reviewed_at REAL);
        CREATE TABLE IF NOT EXISTS rejected_events(
          path TEXT, file_key TEXT, start_offset INTEGER, end_offset INTEGER,
          reason TEXT, raw_file TEXT, created_at REAL,
          PRIMARY KEY(path,file_key,start_offset,end_offset));
        CREATE TABLE IF NOT EXISTS desktop_notifications(
          message_id INTEGER PRIMARY KEY, state TEXT, created_at REAL);
        CREATE TABLE IF NOT EXISTS source_health(
          source TEXT PRIMARY KEY, last_success REAL DEFAULT 0, last_error_at REAL DEFAULT 0, last_error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS cursor_signatures(path TEXT PRIMARY KEY, offset INTEGER, signature TEXT);
        CREATE TABLE IF NOT EXISTS message_revisions(message_id INTEGER PRIMARY KEY, revision INTEGER DEFAULT 0);
        """)
        # A crash after sending is ambiguous; do not automatically risk sending twice.
        self.db.execute("UPDATE messages SET delivery='unknown', error='上次发送中断，请核对聊天软件后手动重试' WHERE delivery='sending'")
        self.db.execute("UPDATE desktop_notifications SET state='unknown' WHERE state='showing'")
        self.db.commit()

    def ingest(self, raw):
        if isinstance(raw, dict) and ("audio_path" in raw or raw.get("audio_missing")):
            if self.audio_ingest is None:
                raise ValueError("语音任务服务尚未就绪")
            return self.audio_ingest(raw)
        m = normalize_message(raw)
        if m["conversation"].lower() in ("文件传输助手", "filehelper") or FORWARD_MARKER in m["text"]:
            return {"accepted": False, "reason": "forward_loop"}
        recent = 0 <= time.time() - m["timestamp"] <= self.settings["forward_max_age_seconds"]
        delivery = "queued" if self.settings["forward_enabled"] and recent else "stored"
        with self.lock:
            # A human revision may already own the transaction.
            with (nullcontext() if self.db.in_transaction else self.db):
                cur = self.db.execute("""INSERT OR IGNORE INTO messages
                    (source,message_id,conversation,sender,text,timestamp,capture,received_at,delivery)
                    VALUES(:source,:message_id,:conversation,:sender,:text,:timestamp,:capture,:received_at,:delivery)""",
                    {**m, "received_at": time.time(), "delivery": delivery})
                if cur.rowcount and self.settings["notify_p0"] and 0 <= time.time() - m["timestamp"] <= 300 and classify_message(m, self.settings["importance"])["priority"] == "P0":
                    self.db.execute("INSERT OR IGNORE INTO desktop_notifications VALUES(?,?,?)", (cur.lastrowid, "queued", time.time()))
                self.db.execute("INSERT INTO source_health(source,last_success) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET last_success=excluded.last_success", (m["source"], time.time()))
                result = {"accepted": bool(cur.rowcount), "id": cur.lastrowid if cur.rowcount else None,
                          "reason": "new" if cur.rowcount else "duplicate"}
        # 只报 5 分钟内的新消息（补读历史时不刷屏）；回调出错不影响入库
        if result["accepted"] and self.on_new is not None and 0 <= time.time() - m["timestamp"] <= 300:
            try:
                self.on_new(m["source"], m["sender"], m["conversation"])
            except Exception:
                pass
        return result

    def list_messages(self, after=0, limit=100):
        if not isinstance(after, int) or after < 0 or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("after 必须非负；limit 须在 1–200")
        with self.lock:
            return [self._with_importance(dict(r)) for r in self.db.execute("SELECT * FROM messages WHERE id>? ORDER BY id LIMIT ?", (after, limit))]

    def _with_importance(self, message):
        review = self.db.execute("SELECT * FROM priority_reviews WHERE message_id=?", (message["id"],)).fetchone()
        message["importance"] = apply_review(classify_message(message, self.settings["importance"]), dict(review) if review else None)
        return message

    def inbox(self, source=None, priority=None, search="", limit=50):
        if source is not None and source not in SOURCES or priority is not None and priority not in PRIORITIES:
            raise ValueError("无效来源或优先级")
        if not isinstance(search, str) or len(search) > 200 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("search 最多 200 字；limit 须为 1–200")
        with self.lock:
            where, args = "timestamp BETWEEN ? AND ?", [time.time() - 86400, time.time() + 1]
            if source:
                where += " AND source=?"
                args.append(source)
            total = self.db.execute("SELECT count(*) FROM messages WHERE " + where, args).fetchone()[0]
            rows = self.db.execute("SELECT * FROM messages WHERE " + where + " ORDER BY timestamp DESC LIMIT 5000", args).fetchall()
            messages = [self._with_importance(dict(row)) for row in rows]
        counts = {p: sum(m["importance"]["priority"] == p for m in messages) for p in PRIORITIES}
        messages = [m for m in messages if (not priority or m["importance"]["priority"] == priority) and
                    (not search or search.casefold() in (m["sender"] + m["conversation"] + m["text"]).casefold())]
        messages.sort(key=lambda m: (int(m["importance"]["priority"][1]), -m["importance"]["score"], -m["timestamp"]))
        return {"messages": messages[:limit], "counts": counts, "scanned": len(rows), "total": total,
                "limited": total > len(rows), "matched": len(messages), "window_hours": 24}

    def review_priority(self, message_id, priority, reason):
        if type(message_id) is not int or not isinstance(priority, str) or priority not in PRIORITIES or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300:
            raise ValueError("须提供消息 id、P0/P1/P2 和 1–300 字标注原因")
        with self.lock:
            row = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise KeyError("消息不存在")
            self.db.execute("INSERT OR REPLACE INTO priority_reviews VALUES(?,?,?,?)", (message_id, priority, reason.strip(), time.time()))
            self.db.commit()
            return self._with_importance(dict(row))

    def poll_jsonl(self, path):
        path = Path(path).resolve()
        # 同一文件的游标串行推进，其他来源不等待它完成整次读取。
        with self.lock:
            lock = self.poll_locks.setdefault(path, threading.Lock())
        with lock:
            return self._poll_jsonl(path)

    def _poll_jsonl(self, path):
        p = Path(path).resolve()
        if not p.is_file():
            self.source_status[str(p)] = "等待消息事件文件"
            return 0
        stat = p.stat()
        key = str(stat.st_ino)
        with self.lock:
            row = self.db.execute("SELECT offset,file_key FROM cursors WHERE path=?", (str(p),)).fetchone()
        count = 0
        self.source_status[str(p)] = "已连接（增量 JSONL）"
        with p.open("rb") as f:
            with self.lock:
                saved = self.db.execute("SELECT offset,signature FROM cursor_signatures WHERE path=?", (str(p),)).fetchone()
            offset = 0
            if row and 0 < row["offset"] <= stat.st_size:
                sig_known = bool(saved) and saved["offset"] == row["offset"]
                sig_ok = sig_known and saved["signature"] == self._signature(f, row["offset"])
                if row["file_key"] == key:
                    offset = row["offset"] if (sig_ok or not sig_known) else 0
                elif sig_ok:
                    # 采集器把整个文件重写了一遍（新文件），但读到的位置之前内容没变：接着读，不再从头重读
                    # （原来每个采集周期都把 1MB 的聊天软件事件文件从头读一遍、逐行提交，主程序常年占 3 个核）
                    offset = row["offset"]
            f.seek(offset)
            pending = 0
            while not self.stop_event.is_set():
                start = f.tell()
                line = f.readline(65538)
                if not line:
                    break
                if len(line) > 65536:
                    while not line.endswith(b"\n"):
                        line = f.readline(65538)
                        if not line or self.stop_event.is_set():
                            self.source_status[str(p)] = "等待过长消息行写入完成"
                            return count
                    try:
                        self._quarantine(f, p, key, start, f.tell(), "事件行超过65536字节")
                    except (OSError, sqlite3.Error):
                        self.source_status[str(p)] = "隔离保存失败，已保留读取位置"
                        self.mark_health(str(p), error=self.source_status[str(p)])
                        return count
                    offset, pending = f.tell(), 0          # 隔离时已经存了读取位置
                    continue
                if not line.endswith(b"\n"):
                    self.source_status[str(p)] = "等待消息行写入完成"
                    break
                try:
                    count += bool(self.ingest(json.loads(line.decode("utf-8-sig")))["accepted"])
                except (ValueError, UnicodeError) as ex:
                    try:
                        self._quarantine(f, p, key, start, f.tell(), str(ex))
                    except (OSError, sqlite3.Error):
                        self.source_status[str(p)] = "隔离保存失败，已保留读取位置"
                        self.mark_health(str(p), error=self.source_status[str(p)])
                        return count
                    offset, pending = f.tell(), 0          # 隔离时已经存了读取位置
                    continue
                offset = f.tell()
                pending += 1
                if pending >= CURSOR_BATCH:          # 读取位置成批保存（重复读到的消息会被当成重复忽略，所以安全）
                    with self.lock, self.db:
                        self._save_cursor(f, p, offset, key)
                    pending = 0
            if pending:
                with self.lock, self.db:
                    self._save_cursor(f, p, offset, key)
        self.mark_health(str(p), success=True)
        return count

    @staticmethod
    def _signature(stream, offset):
        # shortcut: prefix/tail detects normal snapshot rewrites; use a full hash if producers edit old middle bytes.
        position = stream.tell()
        try:
            stream.seek(0)
            first = stream.read(min(offset, 512))
            stream.seek(max(0, offset - 512))
            return hashlib.sha256(first + stream.read(min(offset, 512))).hexdigest()
        finally:
            stream.seek(position)

    def _save_cursor(self, stream, path, offset, key):
        self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?)", (str(path), offset, key))
        self.db.execute("INSERT OR REPLACE INTO cursor_signatures VALUES(?,?,?)", (str(path), offset, self._signature(stream, offset)))

    def mark_health(self, source, success=False, error=""):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO source_health(source) VALUES(?)", (source,))
            if success:
                self.db.execute("UPDATE source_health SET last_success=? WHERE source=?", (time.time(), source))
            if error:
                self.db.execute("UPDATE source_health SET last_error_at=?,last_error=? WHERE source=?", (time.time(), error[:300], source))

    def _quarantine(self, stream, path, key, start, end, reason):
        folder = self.directory / "quarantine"
        folder.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(f"{path}:{key}:{start}:{end}".encode()).hexdigest()
        raw = folder / (name + ".bin")
        temp = raw.with_suffix(".tmp")
        stream.seek(start)
        remaining = end - start
        try:
            with temp.open("wb") as output:
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise OSError("源文件在隔离期间发生变化")
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            temp.replace(raw)
            with self.lock, self.db:
                self.db.execute("INSERT OR IGNORE INTO rejected_events VALUES(?,?,?,?,?,?,?)", (str(path), key, start, end, reason[:300], str(raw), time.time()))
                self._save_cursor(stream, path, end, key)
        finally:
            stream.seek(end)
        self.source_status[str(path)] = "无效事件已隔离，正常消息继续读取"
        self.mark_health(str(path), error=reason)

    def notify_once(self, notify):
        if not self.settings["notify_p0"] or notify is None:
            return False
        with self.lock:
            self.db.execute("UPDATE desktop_notifications SET state='expired' WHERE state='queued' AND message_id IN (SELECT id FROM messages WHERE timestamp<? OR timestamp>?)", (time.time() - 300, time.time()))
            row = self.db.execute("SELECT m.id,m.source,m.timestamp FROM desktop_notifications n JOIN messages m ON m.id=n.message_id WHERE n.state='queued' ORDER BY m.id LIMIT 1").fetchone()
            if row is None:
                self.db.commit()
                return False
            message = self._with_importance(dict(self.db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()))
            if message["importance"]["priority"] != "P0":
                self.db.execute("UPDATE desktop_notifications SET state='cancelled' WHERE message_id=?", (row["id"],))
                self.db.commit()
                return False
            self.db.execute("UPDATE desktop_notifications SET state='showing' WHERE message_id=?", (row["id"],))
            self.db.commit()
        try:
            notify(dict(row))
        except Exception:
            self.notification_result(row["id"], "failed")
        return True

    def notification_result(self, message_id, state):
        if state not in ("shown", "failed"):
            raise ValueError("提醒结果无效")
        with self.lock, self.db:
            self.db.execute("UPDATE desktop_notifications SET state=? WHERE message_id=? AND state='showing'", (state, message_id))

    def forward_once(self):
        if not self.settings["forward_enabled"] or self.sender is None:
            return False
        with self.lock:
            self.db.execute("UPDATE messages SET delivery='stored',error='已超过自动转发时间窗' WHERE delivery='queued' AND timestamp<?",
                            (time.time() - self.settings["forward_max_age_seconds"],))
            row = self.db.execute("SELECT * FROM messages WHERE delivery='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                self.db.commit()
                return False
            self.db.execute("UPDATE messages SET delivery='sending' WHERE id=?", (row["id"],))
            self.db.commit()
        body = f"{FORWARD_MARKER}\n{row['source']} · {row['conversation']}\n{row['sender']}：{row['text']}"
        try:
            result = self.sender("文件传输助手", body)
            ok = bool(result.get("success"))
            state = "sent" if ok else ("confirmation_required" if result.get("error_code") == "CONFIRM_REQUIRED" else "blocked")
            if not ok and result.get("error_code") in ("INTERNAL_EXECUTION_ERROR", "VERIFICATION_FAILED", "DELIVERY_UNCERTAIN"):
                state = "unknown"
            error = "" if ok else str(result.get("message") or "发送失败")[:300]
        except Exception:
            state, error = "unknown", "发送结果不确定，请核对聊天软件后再操作"
        with self.lock:
            self.db.execute("UPDATE messages SET delivery=?,error=? WHERE id=?", (state, error, row["id"]))
            self.db.commit()
        return state == "sent"

    def retry(self, message_id):
        with self.lock:
            changed = self.db.execute("UPDATE messages SET delivery='queued',error='' WHERE id=? AND delivery IN ('blocked','stored')", (message_id,)).rowcount
            self.db.commit()
        return bool(changed)

    def validated_settings(self, values):
        if not isinstance(values, dict) or set(values) - set(DEFAULT_SETTINGS):
            raise ValueError("未知消息设置")
        settings = {**self.settings, **values}
        if settings["forward_target"] != "文件传输助手":
            raise ValueError("自动转发仅允许文件传输助手")
        for key in ("capture_visible", "forward_enabled", "notify_p0"):
            if not isinstance(settings[key], bool):
                raise ValueError(f"{key} 须为布尔值")
        for key, low, high in (("poll_seconds", .5, 60), ("forward_max_age_seconds", 1, 3600)):
            if not isinstance(settings[key], (int, float)) or not low <= settings[key] <= high:
                raise ValueError(f"{key} 超出范围")
        if not isinstance(settings["jsonl_sources"], list) or len(settings["jsonl_sources"]) > 10 or any(not isinstance(p, str) or not p for p in settings["jsonl_sources"]):
            raise ValueError("jsonl_sources 须为最多 10 个文件路径")
        settings["importance"] = validate_importance(settings["importance"])
        return settings.copy()

    def update_settings(self, values):
        settings = self.validated_settings(values)
        with self.lock:
            self.settings = settings
        return settings.copy()

    def status(self):
        with self.lock:
            counts = {r[0]: r[1] for r in self.db.execute("SELECT delivery,count(*) FROM messages GROUP BY delivery")}
            by_source = {r[0]: {"count": r[1], "recent_count": r[2], "last_message_at": r[3], "last_received_at": r[4]}
                         for r in self.db.execute("SELECT source,count(*),sum(timestamp>=?),max(timestamp),max(received_at) FROM messages GROUP BY source", (time.time() - 86400,))}
            last_id = self.db.execute("SELECT coalesce(max(id),0) FROM messages").fetchone()[0]
            rejected = [dict(r) for r in self.db.execute("SELECT path,count(*) AS count,max(created_at) AS last_rejected_at FROM rejected_events GROUP BY path")]
            notifications = {r[0]: r[1] for r in self.db.execute("SELECT state,count(*) FROM desktop_notifications GROUP BY state")}
            health = {r["source"]: dict(r) for r in self.db.execute("SELECT * FROM source_health")}
        return {"sources": dict(self.source_status), "counts": counts,
                "quarantine": rejected, "notifications": notifications, "health": health,
                "by_source": by_source, "last_id": last_id,
                "forward_enabled": self.settings["forward_enabled"], "target": "文件传输助手"}

    def start(self, visible_source=None, can_forward=lambda: True, collector=None):
        def read_events():
            while not self.stop_event.is_set():
                paths = dict.fromkeys([str(self.directory / "feishu_events.jsonl"), *self.settings["jsonl_sources"]])
                for path in paths:
                    try:
                        self.poll_jsonl(path)
                    except Exception as ex:
                        self.source_status[str(path)] = f"本轮读取失败：{type(ex).__name__}"
                        try:
                            self.mark_health(str(Path(path).resolve()), error=self.source_status[str(path)])
                        except (OSError, ValueError, sqlite3.Error) as health_error:
                            self.source_status["worker"] = f"异常记录保存失败：{type(health_error).__name__}"
                self.stop_event.wait(max(.5, min(60, float(self.settings["poll_seconds"]))))

        def run():
            while not self.stop_event.is_set():
                try:
                    if collector is not None:
                        try:
                            collector()
                        except Exception as ex:
                            self.mark_health("collector", error=f"采集失败：{type(ex).__name__}")
                    if self.stop_event.is_set():
                        break
                    if visible_source is not None and self.settings["capture_visible"]:
                        for message in visible_source.poll():
                            try:
                                self.ingest(message)
                            except ValueError as ex:
                                self.source_status["invalid_visible_message"] = str(ex)
                        self.source_status.update(visible_source.status)
                    if can_forward():
                        self.forward_once()
                except Exception as ex:
                    self.source_status["worker"] = f"监听失败：{type(ex).__name__}"
                self.stop_event.wait(max(.5, min(60, float(self.settings["poll_seconds"]))))
        self.thread = threading.Thread(target=read_events, name="canaan-messages", daemon=True)
        self.collector_thread = threading.Thread(target=run, name="canaan-message-collectors", daemon=True)
        self.thread.start()
        self.collector_thread.start()

    def stop(self):
        self.stop_event.set()
        for name in ("thread", "collector_thread"):
            thread = getattr(self, name, None)
            if thread:
                thread.join(timeout=3)


def new_visible_items(previous, current):
    """Find new visible messages without replaying history when a chat opens."""
    if previous is None:
        return []
    if not previous:
        return current
    for overlap in range(min(len(previous), len(current)), 0, -1):
        if previous[-overlap:] == current[:overlap]:
            return current[overlap:]
    # A different page/history view is ambiguous; establish a baseline instead of forwarding it.
    return []


class VisibleMessageSource:
    """Read accessible message lists only. No focus, clicks, database keys or login bypass."""
    def __init__(self):
        self.previous = {}
        self.status = {}

    def poll(self):
        import uiautomation as auto
        import psutil
        events = []
        with auto.UIAutomationInitializerInThread():
            found = set()
            for window in auto.GetRootControl().GetChildren():
                try:
                    exe = psutil.Process(window.ProcessId).name().lower()
                except psutil.Error:
                    continue
                source = "chatapp" if exe in ("chatapp.exe", "chatapp.exe") else "imapp" if exe in ("imapp.exe", "imappnt.exe") else None
                if source is None:
                    continue
                found.add(source)
                self.status[source] = "等待登录或客户端提供可访问的消息列表"
                queue = [(window, 0)]
                visited = 0
                while queue and visited < 500:
                    control, depth = queue.pop(0)
                    visited += 1
                    if control.ControlTypeName == "ListControl" and control.Name in ("消息", "聊天消息", "消息列表", "Message List"):
                        key = (source, window.NativeWindowHandle, control.AutomationId)
                        current = []
                        for child in control.GetChildren():
                            text = str(child.Name).strip()
                            if text:
                                try:
                                    runtime_id = tuple(child.GetRuntimeId() or ())
                                except Exception:
                                    runtime_id = ()
                                current.append((runtime_id, text))
                        current = current[-80:]
                        for runtime_id, text in new_visible_items(self.previous.get(key), current):
                            stable_id = hashlib.sha256(repr((source, window.ProcessId, window.NativeWindowHandle, runtime_id, text)).encode()).hexdigest()
                            events.append({"source": source, "message_id": stable_id if runtime_id else uuid.uuid4().hex,
                                           "conversation": window.Name or f"{source}当前可见会话",
                                           "text": text, "timestamp": time.time(), "capture": "visible_window"})
                        self.previous[key] = current
                        self.status[source] = "已连接（仅当前可访问会话；时间为采集时间）"
                    elif depth < 8:
                        queue.extend((child, depth + 1) for child in control.GetChildren())
            for source in ("chatapp", "imapp"):
                if source not in found:
                    self.status[source] = "客户端未运行；可通过电脑操作打开并登录"
        return events
