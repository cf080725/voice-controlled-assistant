"""Local, profile-scoped conversation and unfinished task storage.

Facts remain in their existing stores. This database holds exact turns and task
slots; it never invokes a model or executes an action. A claimed action cannot be
claimed again, including when its eventual delivery result is unknown.
"""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "context.db")
OPEN_STATES = ("awaiting_slot", "awaiting_confirmation")
TERMINAL_STATES = ("succeeded", "failed", "unknown", "cancelled", "superseded", "expired")
STATES = (*OPEN_STATES, "executing", *TERMINAL_STATES)
_RECALL_RE = re.compile(
    r"^(?:(?:你(?:还)?记得|还记得|回忆一下|回顾一下)[，,\s]*)?"
    r"(?:我(?:今天|刚才|之前)|(?:今天|刚才|之前)我)"
    r"(?:(?:跟你|对你)?(?:说过|说了|说|提过|提到|让你|叫你|要|想)(?:我)?(?:要|想)?)?"
    r"(?:做|干|办)?(?:的是|的|是)?(?:什么|啥|干嘛|干啥|做啥|说啥)(?:事|事情)?(?:来着|了吗|吗|呢|了)?[？?。！!\s]*$"
)


# 10-24 起 data/context.db 默认加密这些列（canaan_crypto）；别的路径（测试）默认不加密。
ENCRYPTED_COLUMNS = {c: f"context.{c}" for c in ("user_text", "decision", "spoken", "result_note", "slots")}
_AUTO = object()


def _profile(value):
    if value == "owner" or re.fullmatch(r"person:[1-9][0-9]*", str(value or "")):
        return str(value)
    raise ValueError("无效的使用者编号")


def _namespace(value):
    if (type(value) is not str or len(value) > 256 or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("无效的任务命名空间")
    return value


_CONTENT_ALL = ("", "所有", "全部", "一切", "所有事", "全部的事")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _content_keyword(value):
    if type(value) is not str:
        raise ValueError("遗忘关键词必须是字符串；清空内容须显式提供空字符串")
    value = value.strip()
    return "" if value in _CONTENT_ALL else value.translate(_ASCII_LOWER)


def _content_value_matches(value, keyword):
    """Literal content, including user JSON; never treat a keyword as a pattern."""
    if isinstance(value, str):
        if keyword in value.translate(_ASCII_LOWER):
            return True
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return False
        return (isinstance(decoded, (dict, list)) or isinstance(decoded, str) and decoded != value) and _content_value_matches(decoded, keyword)
    if isinstance(value, dict):
        return any(_content_value_matches(key, keyword) or _content_value_matches(item, keyword)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_content_value_matches(item, keyword) for item in value)
    if isinstance(value, (int, float, bool)) or value is None:
        return keyword in json.dumps(value, ensure_ascii=False, allow_nan=False).translate(_ASCII_LOWER)
    return False


def _context_content_matches(value, keyword):
    """Ignore protocol metadata, retaining original text and executable payloads."""
    if not isinstance(value, dict):
        return _content_value_matches(value, keyword)
    for name, item in value.items():
        if name in ("action", "emotion", "guard", "source"):
            continue
        if name in ("_pending", "_draft"):
            if _context_content_matches(item, keyword):
                return True
        elif not name.startswith("_") or name == "_user_text":
            if _content_value_matches(item, keyword):
                return True
    return False


class ContextStore:
    def __init__(self, path=DB_PATH, clock=time.time, auto_resume=600.0,
                 explicit_resume=86400.0, turn_retention_days=30, cipher=_AUTO):
        self.path, self.clock = str(path), clock
        self.auto_resume, self.explicit_resume = float(auto_resume), float(explicit_resume)
        self.turn_retention_days = int(turn_retention_days)
        self.incognito = False
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        import canaan_crypto
        if cipher is _AUTO:
            same = os.path.abspath(self.path) == os.path.abspath(DB_PATH)
            cipher = canaan_crypto.default_cipher() if same else None
        self.cipher = cipher
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        canaan_crypto.attach(self.db, cipher, ENCRYPTED_COLUMNS)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS context_profiles(
            profile_key TEXT PRIMARY KEY, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS context_sessions(
            id TEXT PRIMARY KEY, profile_key TEXT NOT NULL REFERENCES context_profiles(profile_key) ON DELETE CASCADE,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, ended_at REAL);
        CREATE INDEX IF NOT EXISTS context_sessions_profile ON context_sessions(profile_key,updated_at);
        CREATE TABLE IF NOT EXISTS context_turns(
            id INTEGER PRIMARY KEY, profile_key TEXT NOT NULL REFERENCES context_profiles(profile_key) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES context_sessions(id) ON DELETE CASCADE,
            user_text TEXT NOT NULL, decision TEXT NOT NULL, spoken TEXT NOT NULL,
            result_note TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS context_turns_profile ON context_turns(profile_key,created_at,id);
        CREATE TABLE IF NOT EXISTS context_tasks(
            id TEXT PRIMARY KEY, profile_key TEXT NOT NULL REFERENCES context_profiles(profile_key) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES context_sessions(id) ON DELETE CASCADE,
            namespace TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL, slots TEXT NOT NULL, missing_slot TEXT NOT NULL,
            status TEXT NOT NULL, version INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            confirm_expires REAL, result_note TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS context_tasks_profile ON context_tasks(profile_key,status,updated_at);
        """)
        with self._tx():
            if "namespace" not in {row[1] for row in self.db.execute("PRAGMA table_info(context_tasks)")}:
                self.db.execute("ALTER TABLE context_tasks ADD COLUMN namespace TEXT NOT NULL DEFAULT ''")
            self.db.execute("CREATE INDEX IF NOT EXISTS context_tasks_namespace "
                            "ON context_tasks(profile_key,namespace,status,updated_at)")
        self.db.commit()
        if cipher is not None:
            self._encrypt_existing()

    def _enc(self, value, column):
        return self.cipher.encrypt(value, ENCRYPTED_COLUMNS[column]) if self.cipher is not None else value

    def _encrypt_existing(self):
        """旧的明文库就地加密，再 VACUUM 覆盖旧页面（WAL 也一并合回主文件）。"""
        cols = {"context_turns": ("user_text", "decision", "spoken", "result_note"),
                "context_tasks": ("slots", "result_note")}
        n = 0
        with self._tx():
            for table, columns in cols.items():
                for col in columns:
                    for rid, value in self.db.execute(
                            f"SELECT rowid, {col} FROM {table} WHERE {col} NOT LIKE 'enc1:%'").fetchall():
                        self.db.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (self._enc(value, col), rid))
                        n += 1
        if n:
            with self._lock:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.db.execute("VACUUM")
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            print(f"[🔒] 会话上下文库已加密：{n} 项")

    @contextmanager
    def _tx(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _session_locked(self, key, session_id=None):
        now = self.clock()
        self.db.execute("INSERT OR IGNORE INTO context_profiles VALUES(?,?)", (key, now))
        if session_id:
            row = self.db.execute("SELECT profile_key FROM context_sessions WHERE id=?", (session_id,)).fetchone()
            if not row or row[0] != key:
                raise ValueError("会话不属于当前使用者")
            self.db.execute("UPDATE context_sessions SET updated_at=? WHERE id=?", (now, session_id))
            return session_id
        row = self.db.execute("SELECT id FROM context_sessions WHERE profile_key=? AND ended_at IS NULL "
                              "ORDER BY updated_at DESC LIMIT 1", (key,)).fetchone()
        if row:
            return row[0]
        sid = uuid.uuid4().hex
        self.db.execute("INSERT INTO context_sessions VALUES(?,?,?,?,NULL)", (sid, key, now, now))
        return sid

    def open_session(self, profile_key):
        key = _profile(profile_key)
        if self.incognito:
            return None
        with self._tx():
            return self._session_locked(key)

    ensure_session = open_session

    def close_session(self, profile_key, session_id=None):
        key = _profile(profile_key)
        with self._tx():
            sql, args = "UPDATE context_sessions SET ended_at=? WHERE profile_key=? AND ended_at IS NULL", [self.clock(), key]
            if session_id:
                sql += " AND id=?"
                args.append(session_id)
            return self.db.execute(sql, args).rowcount

    def append_turn(self, profile_key, session_id, user_text, decision, spoken="", result_note="", **_):
        key = _profile(profile_key)
        if self.incognito:
            return None
        if not isinstance(user_text, str) or not user_text.strip():
            return None
        with self._tx():
            sid = self._session_locked(key, session_id)
            decision = dict(decision or {})
            if decision.get("action", "chat") == "chat":
                decision["reply"] = str(spoken or "")
            return self.db.execute("INSERT INTO context_turns(profile_key,session_id,user_text,decision,spoken,result_note,created_at) "
                                   "VALUES(?,?,?,?,?,?,?)",
                                   (key, sid, self._enc(user_text[:20000], "user_text"),
                                    self._enc(json.dumps(decision, ensure_ascii=False), "decision"),
                                    self._enc(str(spoken or "")[:20000], "spoken"),
                                    self._enc(str(result_note or "")[:2000], "result_note"), self.clock())).lastrowid

    def add_turn(self, profile_key, user_text, decision, result_note="", spoken="", session_id=None, **kwargs):
        return self.append_turn(profile_key, session_id, user_text, decision, spoken, result_note, **kwargs)

    @staticmethod
    def _turn(row):
        out = dict(row)
        out["decision"] = json.loads(out["decision"])
        out["user"] = out["user_text"]
        return out

    def recent_turns(self, profile_key, max_turns=4, within=7200.0):
        key = _profile(profile_key)
        if self.incognito:
            return []
        with self._lock:
            rows = self.db.execute("SELECT * FROM context_turns WHERE profile_key=? AND created_at>=? "
                                   "ORDER BY created_at DESC,id DESC LIMIT ?", (key, self.clock()-float(within), max(0, min(int(max_turns), 200)))).fetchall()
        return [self._turn(row) for row in reversed(rows)]

    def recent_conversation(self, profile_key, max_turns=4, within=7200.0):
        return [(r["user"], dict(r["decision"], reply=r["spoken"] or r["decision"].get("reply", "")))
                for r in self.recent_turns(profile_key, max_turns, within)]

    @staticmethod
    def _task(row):
        if row is None:
            return None
        out = dict(row)
        out["slots"] = json.loads(out["slots"])
        out["task_id"], out["kind"] = out["id"], out["action"]
        return out

    def get_task(self, profile_key, task_id, *, namespace=""):
        scope = _namespace(namespace)
        with self._lock:
            return self._task(self.db.execute("SELECT * FROM context_tasks WHERE id=? AND profile_key=? AND namespace=?",
                                              (task_id, _profile(profile_key), scope)).fetchone())

    def save_task(self, profile_key, session_id=None, action=None, slots=None, status="awaiting_slot",
                  task_id=None, expected_version=None, missing_slot="", kind=None, *, namespace="", **_):
        key = _profile(profile_key)
        scope = _namespace(namespace)
        action = action or kind
        if self.incognito:
            return None
        if not isinstance(action, str) or not action or status not in STATES or not isinstance(slots, dict):
            raise ValueError("无效的任务状态")
        now = self.clock()
        confirm_expires = now + 30 if status == "awaiting_confirmation" else None
        encoded = self._enc(json.dumps(slots, ensure_ascii=False), "slots")
        with self._tx():
            sid = self._session_locked(key, session_id)
            if task_id:
                if expected_version is None:
                    raise ValueError("修改任务须提供版本")
                old = self.db.execute("SELECT * FROM context_tasks WHERE id=? AND profile_key=? AND namespace=? AND version=?",
                                      (task_id, key, scope, expected_version)).fetchone()
                if not old or old["action"] != action or old["status"] not in OPEN_STATES or status not in OPEN_STATES:
                    return None
                self.db.execute("UPDATE context_tasks SET slots=?,missing_slot=?,status=?,version=version+1,updated_at=?,"
                                "confirm_expires=? WHERE id=? AND profile_key=? AND namespace=? AND version=?",
                                (encoded, missing_slot, status, now, confirm_expires, task_id, key, scope, expected_version))
            else:
                if status not in OPEN_STATES:
                    raise ValueError("新任务必须先补充信息或等待确认")
                task_id = uuid.uuid4().hex
                self.db.execute("UPDATE context_tasks SET status='superseded',version=version+1,updated_at=? "
                                "WHERE profile_key=? AND namespace=? AND action=? AND status IN ('awaiting_slot','awaiting_confirmation')",
                                (now, key, scope, action))
                self.db.execute("INSERT INTO context_tasks(id,profile_key,session_id,namespace,action,slots,missing_slot,status,version,created_at,updated_at,confirm_expires) "
                                "VALUES(?,?,?,?,?,?,?,?,1,?,?,?)", (task_id,key,sid,scope,action,encoded,missing_slot,status,now,now,confirm_expires))
            return self._task(self.db.execute("SELECT * FROM context_tasks WHERE id=? AND profile_key=? AND namespace=?",
                                              (task_id, key, scope)).fetchone())

    def active_task(self, profile_key, explicit=False, action=None, *, namespace=""):
        key = _profile(profile_key)
        scope = _namespace(namespace)
        if self.incognito:
            return None
        cutoff = self.clock() - (self.explicit_resume if explicit else self.auto_resume)
        with self._lock:
            sql = "SELECT * FROM context_tasks WHERE profile_key=? AND namespace=? AND status IN ('awaiting_slot','awaiting_confirmation') AND updated_at>=?"
            args = [key, scope, cutoff]
            if action:
                sql += " AND action=?"
                args.append(action)
            row = self.db.execute(sql + " ORDER BY updated_at DESC,rowid DESC LIMIT 1", args).fetchone()
            return self._task(row)

    def latest_task(self, profile_key, kind=None, explicit=False, *, namespace=""):
        return self.active_task(profile_key, explicit=explicit, action=kind, namespace=namespace)

    def claim_task(self, task_id, profile_key, version, *, namespace=""):
        key = _profile(profile_key)
        scope = _namespace(namespace)
        if self.incognito:
            return None
        with self._tx():
            n = self.db.execute("UPDATE context_tasks SET status='executing',version=version+1,updated_at=? "
                                "WHERE id=? AND profile_key=? AND namespace=? AND version=? AND status='awaiting_confirmation' AND confirm_expires>=?",
                                (self.clock(), task_id, key, scope, version, self.clock())).rowcount
            return self._task(self.db.execute("SELECT * FROM context_tasks WHERE id=? AND profile_key=? AND namespace=?",
                                              (task_id, key, scope)).fetchone()) if n else None

    def claim_execution(self, profile_key, task_id, expected_version, *, namespace=""):
        return self.claim_task(task_id, profile_key, expected_version, namespace=namespace)

    def finish_task(self, task_id, profile_key, version=None, status="succeeded", result_note="", expected_version=None, *, namespace="", **_):
        scope = _namespace(namespace)
        if status not in ("succeeded", "failed", "unknown", "cancelled", "superseded", "expired"):
            raise ValueError("无效的结束状态")
        version = expected_version if expected_version is not None else version
        if version is None:
            raise ValueError("结束任务须提供版本")
        with self._tx():
            allowed = ("executing",) if status in ("succeeded", "failed", "unknown") else OPEN_STATES
            marks = ",".join("?" for _ in allowed)
            n = self.db.execute("UPDATE context_tasks SET status=?,version=version+1,updated_at=?,result_note=? "
                                f"WHERE id=? AND profile_key=? AND namespace=? AND version=? AND status IN ({marks})",
                                (status,self.clock(),self._enc(str(result_note)[:2000],"result_note"),
                                 task_id,_profile(profile_key),scope,version,*allowed)).rowcount
            return self._task(self.db.execute("SELECT * FROM context_tasks WHERE id=? AND profile_key=? AND namespace=?",
                                              (task_id, _profile(profile_key), scope)).fetchone()) if n else None

    def transition_task(self, profile_key, task_id, expected_version, status, *, namespace="", **kwargs):
        scope = _namespace(namespace)
        if status in TERMINAL_STATES:
            return self.finish_task(task_id, profile_key, expected_version, status, namespace=scope, **kwargs)
        task = self.get_task(profile_key, task_id, namespace=scope)
        if not task:
            return None
        return self.save_task(profile_key, task["session_id"], task["action"], kwargs.get("slots", task["slots"]),
                              status, task_id, expected_version, kwargs.get("missing_slot", task["missing_slot"]), namespace=scope)

    def cancel_open_tasks(self, profile_key, *, namespace=""):
        scope = _namespace(namespace)
        with self._tx():
            return self.db.execute("UPDATE context_tasks SET status='cancelled',version=version+1,updated_at=? "
                                   "WHERE profile_key=? AND namespace=? AND status IN ('awaiting_slot','awaiting_confirmation')",
                                   (self.clock(),_profile(profile_key),scope)).rowcount

    def recover_inflight(self, profile_key=None):
        """Call once at voice-runtime startup, never when opening another DB client."""
        with self._tx():
            sql = "UPDATE context_tasks SET status='unknown',version=version+1,updated_at=? WHERE status='executing'"
            args = [self.clock()]
            if profile_key is not None:
                sql += " AND profile_key=?"
                args.append(_profile(profile_key))
            return self.db.execute(sql,args).rowcount

    def today_recall(self, profile_key, limit=20):
        now = datetime.fromtimestamp(self.clock())
        midnight = now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        with self._lock:
            rows = self.db.execute("SELECT * FROM context_turns WHERE profile_key=? AND created_at>=? ORDER BY created_at DESC,id DESC LIMIT ?",
                                   (_profile(profile_key),midnight,max(0,min(int(limit),100)))).fetchall()
        return [self._turn(row) for row in reversed(rows)] if not self.incognito else []

    def recall(self, profile_key, text):
        if not _RECALL_RE.fullmatch(str(text or "").strip()):
            return ""
        rows = self.today_recall(profile_key, limit=8) if "今天" in text else self.recent_turns(profile_key,max_turns=8)
        if not rows:
            return "今天还没有保存你的对话记录。"
        return "今天你说过：" + "；".join(row["user_text"][:100] for row in rows) + "。"

    def view(self, profile_key, *, namespace=""):
        key = _profile(profile_key)
        turns, task = self.recent_turns(key), self.active_task(key,explicit=True, namespace=namespace)
        with self._lock:
            session = self.db.execute("SELECT id FROM context_sessions WHERE profile_key=? AND ended_at IS NULL ORDER BY updated_at DESC LIMIT 1",(key,)).fetchone()
        return {"profile_key":key, "session_id":session[0] if session else None,
                "recent_turns":turns,"current_task":task,"turns":turns,"task":task}

    def _matching_content_locked(self, key, keyword):
        turns = self.db.execute("SELECT id,user_text,decision,spoken,result_note FROM context_turns "
                                "WHERE profile_key=? ORDER BY id", (key,)).fetchall()
        tasks = self.db.execute("SELECT id,slots,result_note FROM context_tasks "
                                "WHERE profile_key=? ORDER BY rowid", (key,)).fetchall()
        turn_ids = tuple(row["id"] for row in turns if not keyword or
                         any(_content_value_matches(row[name], keyword) for name in ("user_text", "spoken", "result_note"))
                         or _context_content_matches(json.loads(row["decision"]), keyword))
        task_ids = tuple(row["id"] for row in tasks if not keyword or
                         _context_content_matches(json.loads(row["slots"]), keyword)
                         or _content_value_matches(row["result_note"], keyword))
        return {"turn_ids": turn_ids, "task_ids": task_ids}

    def matching_content(self, profile_key, keyword):
        """Return only matching row IDs across this profile's task namespaces.

        Matching uses the existing decrypted cursor, ASCII-insensitive literal
        substrings, and explicit all/empty keywords. No original text is returned.
        """
        key, keyword = _profile(profile_key), _content_keyword(keyword)
        with self._tx():
            return self._matching_content_locked(key, keyword)

    def forget_content(self, profile_key, keyword):
        """Atomically remove matching conversation/task rows, retaining sessions.

        A matching executing claim row is intentionally removed too. Finishing
        that old claim must remain a CAS miss; its JSON must never recreate it.
        This does not touch privacy epochs, memory facts, or operational ledgers.
        """
        key, keyword = _profile(profile_key), _content_keyword(keyword)
        with self._tx():
            matching = self._matching_content_locked(key, keyword)
            turns = self.db.executemany("DELETE FROM context_turns WHERE profile_key=? AND id=?",
                                       ((key, rid) for rid in matching["turn_ids"])).rowcount
            tasks = self.db.executemany("DELETE FROM context_tasks WHERE profile_key=? AND id=?",
                                       ((key, rid) for rid in matching["task_ids"])).rowcount
            remaining = self._matching_content_locked(key, keyword)
            if turns != len(matching["turn_ids"]) or tasks != len(matching["task_ids"]) or any(remaining.values()):
                raise RuntimeError("相关上下文内容未能完整清除")
            return {"turns_removed": turns, "tasks_removed": tasks}

    def clear_history(self, profile_key):
        key = _profile(profile_key)
        with self._tx():
            turns = self.db.execute("DELETE FROM context_turns WHERE profile_key=?",(key,)).rowcount
            tasks = self.db.execute("DELETE FROM context_tasks WHERE profile_key=?",(key,)).rowcount
            sessions = self.db.execute("DELETE FROM context_sessions WHERE profile_key=?",(key,)).rowcount
            return {"turns_removed":turns,"tasks_removed":tasks,"sessions_removed":sessions}

    def purge(self):
        cutoff = self.clock()-self.turn_retention_days*86400
        with self._tx():
            turns = self.db.execute("DELETE FROM context_turns WHERE created_at<?",(cutoff,)).rowcount
            tasks = self.db.execute("DELETE FROM context_tasks WHERE updated_at<?",(cutoff,)).rowcount
            self.db.execute("DELETE FROM context_sessions WHERE updated_at<? AND id NOT IN (SELECT session_id FROM context_turns) "
                            "AND id NOT IN (SELECT session_id FROM context_tasks)",(cutoff,))
            return {"turns_removed":turns,"tasks_removed":tasks}

    def delete_profile(self, profile_key):
        with self._tx():
            return bool(self.db.execute("DELETE FROM context_profiles WHERE profile_key=?",(_profile(profile_key),)).rowcount)

    def close(self):
        with self._lock:
            self.db.close()
