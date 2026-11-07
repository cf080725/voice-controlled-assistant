# -*- coding: utf-8 -*-
"""
多人记忆（单个 SQLite 文件 data/people.db，只存本机）。

谁在用：说“我是小明”切换到小明；“我是主人 / 换回主人”或 30 分钟没人说话就回到主人。
        没有声纹，口头切换不验证身份（谁说“我是主人”都能切回主人），所以别人的记忆只在本人在用时进提示词。
记什么：每个人自己的长期事实 / 偏好（“我住在合肥”“记住我对花生过敏”），规则抽取，不训练模型。
        主人本人的记忆仍在 data/memory.db（canaan_memory），这里只存其他人的，两边不混。
越来越懂：同类事实（同 key）新的覆盖旧的；每次被用进回答就记一次使用次数和时间，检索时常用的排前面。
保留：没置顶的事实 90 天没被用到就删（canaan_retention 启动时清）；没有事实、90 天没出现的人也删。
查看 / 删除：本人说“你记得我什么 / 忘掉……”；主人在控制中心能看、删所有人的（/api/memory/people）。
无痕模式与主人的记忆共用一个开关：开着时谁的都不记。
"""
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "people.db")
IDLE_RESET_SEC = 1800.0
MAX_FACTS_PER_PERSON = 200
_ALL = ("所有", "全部", "一切", "所有事", "全部的事")

# 口头切换：整句只说身份才算（“我是说……”“我是认真的”不算）
_SWITCH_RE = re.compile(r"^(?:你好|喂|嗨|哈喽)?[，,\s]*(?:我是([一-龥A-Za-z][一-龥A-Za-z0-9 ·.\-]{0,39}?)|现在是([一-龥A-Za-z][一-龥A-Za-z0-9 ·.\-]{0,39}?)在用|切换(?:到)?用户([一-龥A-Za-z][一-龥A-Za-z0-9 ·.\-]{0,39}?))"
                        r"(?:啊|呀|哦|啦)?$")
_JOB_SUFFIX = ("学生", "程序员", "工程师", "老师", "设计师", "医生", "研究生", "本科生", "人")
_OWNER_RE = re.compile(r"^(?:换回|切换回|切回|回到)主人(?:了|吧)?$|^我是主人(?:啊|呀|哦|啦)?$")
_WHOAMI_RE = re.compile(r"^(?:现在是谁在用|我是谁|你知道我是谁吗?|现在是谁)(?:呀|啊)?$")
# 名字里不会出现的字（“我是说”“我是不是”“现在是几点”）
_NOT_NAME_CHARS = set("的了吗呢吧啊呀哦不是说在要想会能个这那么什谁哪几点你我他她它们过着很真太都也就还没有好坏去来对错")
_NOT_NAMES = {"主人", "团团", "故意", "认真", "随便", "开玩笑", "问问", "说说", "男生", "女生", "新手", "学生", "老师",
              "医生", "程序员", "中国人", "外地人", "本地人", "游客", "客人", "一个人", "自己"}


def parse_switch(text):
    """返回 ("owner", None) / ("switch", 名字) / ("whoami", None) / None。"""
    t = re.sub(r"[。.!！?？~～\s]+$", "", (text or "").strip())
    if _OWNER_RE.match(t):
        return "owner", None
    if _WHOAMI_RE.match(t):
        return "whoami", None
    m = _SWITCH_RE.match(t)
    if not m:
        return None
    name = next(g for g in m.groups() if g).strip()
    if name in _NOT_NAMES or name.endswith(_JOB_SUFFIX) or any(ch in _NOT_NAME_CHARS for ch in name):
        return None
    return "switch", name


_STOP_CHARS = set("的了吗呢啊吧我你他她是在有和就都也还么什怎哪个这那一主人谁几多少")


def _bigrams(text):
    t = re.sub(r"\W", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


# 10-24 起 data/people.db 默认加密这两列（canaan_crypto）；别的路径（测试）默认不加密。
ENCRYPTED_COLUMNS = {"name": "people.name", "text": "person_facts.text"}
_AUTO = object()


class PeopleDB:
    def __init__(self, path=DB_PATH, owner_store=None, idle_reset=IDLE_RESET_SEC, clock=time.time, cipher=_AUTO):
        self.path = path
        self.owner_store = owner_store       # 主人的 MemoryStore：共用无痕开关、认出主人的名字
        self.idle_reset = idle_reset
        self.clock = clock
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        import canaan_crypto
        if cipher is _AUTO:
            cipher = canaan_crypto.default_cipher() if os.path.abspath(path) == os.path.abspath(DB_PATH) else None
        self.cipher = cipher
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        canaan_crypto.attach(self.db, cipher, ENCRYPTED_COLUMNS)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS people_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS people(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, last_seen REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS person_facts(
            id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            text TEXT NOT NULL, key TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, last_used REAL NOT NULL, uses INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS person_facts_person ON person_facts(person_id);
        """)
        self.db.execute("INSERT OR IGNORE INTO people_settings(key,value) "
                        "SELECT 'next_person_id',CAST(COALESCE(MAX(id),0)+1 AS TEXT) FROM people")
        self.db.commit()
        if cipher is not None:
            self._encrypt_existing()

    def enc(self, value, column):
        return self.cipher.encrypt(value, ENCRYPTED_COLUMNS[column]) if self.cipher is not None else value

    def _encrypt_existing(self):
        """旧的明文库就地加密，再 VACUUM 覆盖旧页面。"""
        with self._tx():
            n = 0
            for table, col in (("people", "name"), ("person_facts", "text")):
                for rid, value in self.db.execute(
                        f"SELECT id, {col} FROM {table} WHERE {col} NOT LIKE 'enc1:%'").fetchall():
                    self.db.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (self.enc(value, col), rid))
                    n += 1
        if n:
            with self._lock:
                self.db.execute("VACUUM")
            print(f"[🔒] 多人记忆库已加密：{n} 项")

    @contextmanager
    def _tx(self):
        with self._lock:
            outer = not self.db.in_transaction
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                if outer:
                    self.db.commit()
            except BaseException:
                if outer:
                    self.db.rollback()
                raise

    def _setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM people_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO people_settings(key,value) VALUES(?,?)", (key, str(value)))

    # --- 无痕 / 版本（删除后让还在路上的写入作废） ---
    @property
    def incognito(self):
        return bool(getattr(self.owner_store, "incognito", False))

    @incognito.setter
    def incognito(self, value):
        if self.owner_store is not None:
            self.owner_store.incognito = value

    @property
    def privacy_revision(self):
        with self._lock:
            return int(self._setting("privacy_revision", 0))

    def _bump(self):
        self.db.execute("INSERT INTO people_settings(key,value) VALUES('privacy_revision','1') "
                        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")

    # --- 当前是谁 ---
    def _bump_identity(self):
        self.db.execute("INSERT INTO people_settings(key,value) VALUES('identity_revision','1') "
                        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")

    @property
    def identity_revision(self):
        with self._lock:
            return int(self._setting("identity_revision", 0))

    @property
    def switch_revision(self):
        return self.identity_revision

    def owner_name(self):
        try:
            for f in self.owner_store.active_facts():
                if f.get("key") == "name" and str(f.get("text", "")).startswith("主人叫"):
                    return f["text"][3:]
        except Exception:
            pass
        return None

    def active(self):
        """当前在用的人 {"id","name"}；主人返回 None。空闲超过 idle_reset 自动回到主人。"""
        with self._tx():
            pid = int(self._setting("active_person", 0) or 0)
            if not pid:
                return None
            if self.clock() - float(self._setting("active_at", 0)) > self.idle_reset:
                self._set("active_person", 0)
                self._bump_identity()
                return None
            row = self.db.execute("SELECT id,name FROM people WHERE id=?", (pid,)).fetchone()
            if not row:
                self._set("active_person", 0)
                self._bump_identity()
            return dict(row) if row else None

    def snapshot(self):
        """One transaction binds identity and revision, including idle/delete resets."""
        with self._tx():
            person = self.active()
            return {"profile_key": f"person:{person['id']}" if person else "owner",
                    "person_id": person["id"] if person else None,
                    "name": person["name"] if person else self.owner_name() or "主人",
                    "revision": int(self._setting("identity_revision", 0))}

    identity_snapshot = snapshot

    def invalidate_context(self):
        """Invalidate captured confirmations when this person's history is cleared."""
        with self._tx():
            self._bump_identity()
            return self.snapshot()

    def touch(self):
        with self._tx():
            person = self.active()
            pid = person["id"] if person else 0
            if pid:
                now = self.clock()
                self._set("active_at", now)
                self.db.execute("UPDATE people SET last_seen=? WHERE id=?", (now, pid))

    def switch_to(self, name):
        name = " ".join(str(name or "").split())
        if not 1 <= len(name) <= 40 or not re.fullmatch(r"[一-鿿A-Za-z0-9 ·.\-]+", name) or not re.search(r"[一-鿿A-Za-z]", name):
            raise ValueError("名字须为 1–40 字，可包含中文、英文、数字、空格和间隔点")
        if name == "主人" or name == self.owner_name():
            self.switch_owner()
            return None
        with self._tx():
            now = self.clock()
            next_id = max(int(self._setting("next_person_id", 1)),
                          self.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM people").fetchone()[0])
            # 名字加密后每次密文都不一样，UNIQUE / ON CONFLICT 认不出同一个人，按解密后的名字找
            found = self.db.execute("SELECT id FROM people WHERE dec(name,'people.name')=?", (name,)).fetchone()
            if found:
                pid = found[0]
                self.db.execute("UPDATE people SET last_seen=? WHERE id=?", (now, pid))
            else:
                self.db.execute("INSERT INTO people(id,name,created_at,last_seen) VALUES(?,?,?,?)",
                                (next_id, self.enc(name, "name"), now, now))
                pid = next_id
            self._set("next_person_id",next_id+1)
            previous = int(self._setting("active_person", 0) or 0)
            self._set("active_person", pid)
            self._set("active_at", now)
            if previous != pid:
                self._bump_identity()
        return {"id": pid, "name": name}

    def switch_owner(self):
        with self._tx():
            previous = int(self._setting("active_person", 0) or 0)
            self._set("active_person", 0)
            if previous:
                self._bump_identity()

    def store(self, person_id):
        with self._lock:
            row = self.db.execute("SELECT id,name FROM people WHERE id=?", (person_id,)).fetchone()
        return PersonStore(self, row["id"], row["name"]) if row else None

    def active_store(self):
        """当前在用的人的记忆；主人在用时返回 None（调用方用主人自己的 MemoryStore）。"""
        p = self.active()
        return PersonStore(self, p["id"], p["name"]) if p else None

    def command(self, text):
        """规则通道：“我是小明 / 换回主人 / 现在是谁在用”。命中返回决策，否则 None（并顺便续上当前这个人的时间）。"""
        parsed = parse_switch(text)
        if parsed is None:
            self.touch()
            return None
        kind, name = parsed
        current = self.active()
        if kind == "whoami":
            who = current["name"] if current else (self.owner_name() or "主人")
            reply = f"现在是{who}在用。" if current else f"现在是主人{'' if who == '主人' else who}在用。"
        elif kind == "owner" or name == self.owner_name():
            self.switch_owner()
            reply = "好，换回主人了。" if current else "嗯，一直是你在用呀。"
        elif current and current["name"] == name:
            self.touch()
            reply = f"我知道，你是{name}。"
        else:
            self.switch_to(name)
            n = len(self.store(self.active()["id"]).active_facts())
            reply = f"你好{name}，{'我还记得你说过的事' if n else '接下来你跟我说的事我会单独记着'}。" \
                    f"换回主人说“换回主人”。"
        return {"action": "chat", "param": "", "emotion": "pamper", "reply": reply, "_guard": "person_switch"}

    # --- 控制中心（主人）：看所有人、删人、删条 ---
    def overview(self):
        with self._lock:
            active = self.active()
            people = [dict(r) for r in self.db.execute("SELECT id,name,created_at,last_seen FROM people ORDER BY last_seen DESC")]
            for p in people:
                p["active"] = bool(active and active["id"] == p["id"])
                p["facts"] = [dict(r) for r in self.db.execute(
                    "SELECT id,text,key,pinned,uses,last_used,updated_at FROM person_facts WHERE person_id=? "
                    "ORDER BY pinned DESC, updated_at DESC", (p["id"],))]
        return {"incognito": self.incognito, "active": active, "people": people,
                "identity_revision": self.identity_revision}

    def delete_person(self, person_id):
        with self._tx():
            if int(self._setting("active_person", 0) or 0) == person_id:
                self._set("active_person", 0)
                self._bump_identity()
            self.db.execute("DELETE FROM person_facts WHERE person_id=?", (person_id,))
            n = self.db.execute("DELETE FROM people WHERE id=?", (person_id,)).rowcount
            if n:
                self._bump()
        if n:
            from canaan_proactive import forget_profile
            forget_profile(f'person:{person_id}', directory=os.path.dirname(os.path.abspath(self.path)))
        return bool(n)

    def delete_fact(self, person_id, fact_id):
        with self._tx():
            n = self.db.execute("DELETE FROM person_facts WHERE id=? AND person_id=?", (fact_id, person_id)).rowcount
            if n:
                self._bump()
        return bool(n)

    def purge(self, days=90, now=None):
        """数据保留：没置顶、days 天没被用到的事实删掉；没有事实且 days 天没出现的人删掉。返回只有数字的摘要。"""
        now = self.clock() if now is None else now
        cutoff = now - days * 86400
        with self._tx():
            facts = self.db.execute("DELETE FROM person_facts WHERE pinned=0 AND last_used<?", (cutoff,)).rowcount
            active = int(self._setting("active_person", 0) or 0)
            removed_ids = [row[0] for row in self.db.execute(
                "SELECT id FROM people WHERE last_seen<? AND id<>? AND id NOT IN (SELECT person_id FROM person_facts)",
                (cutoff, active))]
            people = self.db.execute(
                "DELETE FROM people WHERE last_seen<? AND id<>? AND id NOT IN (SELECT person_id FROM person_facts)",
                (cutoff, active)).rowcount
            if facts or people:
                self._bump()
        from canaan_proactive import forget_profile
        for person_id in removed_ids:
            forget_profile(f'person:{person_id}', directory=os.path.dirname(os.path.abspath(self.path)))
        return {"people_days": days, "people_facts_removed": facts, "people_removed": people}

    def close(self):
        with self._lock:
            self.db.close()


class PersonStore:
    """某一个人的长期事实，接口与 canaan_memory.MemoryStore 用到的那几个一致（handle_memory_command、陪聊上下文）。
    内部文本和主人的一样以“主人”开头（这里的“主人”指正在说话的人），给模型看时换成名字。"""

    def __init__(self, people, person_id, name):
        self.people, self.person_id, self.name = people, person_id, name

    @property
    def db(self):
        return self.people.db

    @property
    def incognito(self):
        return self.people.incognito

    @incognito.setter
    def incognito(self, value):
        self.people.incognito = value

    @property
    def privacy_revision(self):
        return self.people.privacy_revision

    def add(self, text, key=None, pinned=False, expected_revision=None, **_):
        text = (text or "").strip(" 。，,.!！")
        if not text or len(text) > 80 or self.incognito:
            return None
        with self.people._tx():
            if expected_revision is not None and expected_revision != self.people.privacy_revision:
                return None
            if self.db.execute("SELECT 1 FROM person_facts WHERE person_id=? AND dec(text,'person_facts.text')=?",
                               (self.person_id, text)).fetchone():
                return None
            now = self.people.clock()
            if key:   # 同类事实只留最新的（搬家、换工作）
                self.db.execute("DELETE FROM person_facts WHERE person_id=? AND key=?", (self.person_id, key))
            fid = self.db.execute(
                "INSERT INTO person_facts(person_id,text,key,pinned,created_at,updated_at,last_used) VALUES(?,?,?,?,?,?,?)",
                (self.person_id, self.people.enc(text, "text"), key, int(bool(pinned)), now, now, now)).lastrowid
            # 每人上限：先删最久没用、没置顶的
            self.db.execute("DELETE FROM person_facts WHERE id IN (SELECT id FROM person_facts WHERE person_id=? AND pinned=0 "
                            "ORDER BY last_used DESC, id DESC LIMIT -1 OFFSET ?)", (self.person_id, MAX_FACTS_PER_PERSON))
            return fid

    def active_facts(self):
        with self.people._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM person_facts WHERE person_id=? ORDER BY pinned DESC, updated_at DESC", (self.person_id,))]

    def texts(self):
        return [f["text"] for f in self.active_facts()][::-1]

    def edit_fact(self, fact_id, text):
        text = str(text or "").strip()
        if not 1 <= len(text) <= 80:
            raise ValueError("记忆内容须为 1–80 字")
        if self.incognito:
            raise ValueError("无痕模式下不能写入记忆")
        with self.people._tx():
            if self.incognito:
                raise ValueError("无痕模式下不能写入记忆")
            n = self.db.execute("UPDATE person_facts SET text=?,updated_at=? WHERE id=? AND person_id=?",
                                (self.people.enc(text, "text"),self.people.clock(),fact_id,self.person_id)).rowcount
            if not n:
                raise KeyError("这条记忆不存在")
            self.people._bump()
        return fact_id

    def set_pinned(self, fact_id, pinned):
        with self.people._tx():
            return bool(self.db.execute("UPDATE person_facts SET pinned=? WHERE id=? AND person_id=?",
                                       (int(bool(pinned)),fact_id,self.person_id)).rowcount)

    def delete_fact(self, fact_id):
        return self.people.delete_fact(self.person_id,fact_id)

    def search(self, query, k=5):
        """双字重合 + 单字重合（去掉虚词）打分，使用次数加一点分，置顶的总带上；条数不多时全带上。
        被用到的记一次使用（保留期从这里算）。"""
        qb, qc = _bigrams(query), set(re.sub(r"\W", "", query or "")) - _STOP_CHARS
        facts = self.active_facts()
        scored = []
        for f in facts:
            body = re.sub(r"^主人", "", f["text"])
            overlap = len(qb & _bigrams(body)) + 0.3 * len(qc & set(body))
            if f["pinned"] or overlap or len(facts) <= k:
                scored.append((f["pinned"], overlap + min(f["uses"], 20) * 0.05, f["updated_at"], f, overlap))
        top = sorted(scored, key=lambda x: x[:3], reverse=True)[:k]
        hits = [x[3] for x in top]
        relevant = [x[3]["id"] for x in top if x[0] or x[4]]   # 只是条数少顺带上的不算用到
        if relevant:
            with self.people._tx():
                now = self.people.clock()
                self.db.executemany("UPDATE person_facts SET uses=uses+1, last_used=? WHERE id=?",
                                    [(now, i) for i in relevant])
        return hits

    def context_lines(self, query):
        lines = [f"现在跟你说话的是{self.name}（不是主人），下面是你记得的关于{self.name}的事："]
        for f in self.search(query):
            lines.append(re.sub(r"^主人", self.name, f["text"]))
        return lines

    def matching(self, keyword):
        kw = (keyword or "").strip()
        with self.people._lock:
            if kw in ("",) + _ALL:
                return [dict(r) for r in self.db.execute("SELECT * FROM person_facts WHERE person_id=?", (self.person_id,))]
            pattern = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM person_facts WHERE person_id=? AND dec(text,'person_facts.text') LIKE ? ESCAPE '\\'",
                (self.person_id, pattern))]

    def forget(self, keyword):
        ids = [r["id"] for r in self.matching(keyword)]
        with self.people._tx():
            self.db.executemany("DELETE FROM person_facts WHERE id=?", [(i,) for i in ids])
            if ids:
                self.people._bump()
        return len(ids)

    def forget_pending(self, keyword):
        """“忘掉……”确认后执行的动作：只删这个人的，不碰主人的记忆。"""
        return {"action": "forget_person_memory",
                "param": json.dumps({"person_id": self.person_id, "keyword": keyword}, ensure_ascii=False)}


_PEOPLE = None
_PEOPLE_LOCK = threading.Lock()


def get_people(owner_store=None):
    global _PEOPLE
    with _PEOPLE_LOCK:
        if _PEOPLE is None:
            if owner_store is None:
                try:
                    import canaan_memory
                    owner_store = canaan_memory.get_store()
                except Exception:
                    owner_store = None
            try:
                import canaan_config
                idle = float(getattr(canaan_config, "PEOPLE_IDLE_RESET_SEC", IDLE_RESET_SEC))
            except Exception:
                idle = IDLE_RESET_SEC
            _PEOPLE = PeopleDB(owner_store=owner_store, idle_reset=idle)
        return _PEOPLE
