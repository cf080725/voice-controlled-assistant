# -*- coding: utf-8 -*-
"""
长期记忆（单个 SQLite 文件 data/memory.db，只存本机）。

表：
    facts     关于主人的事实：text, category, source_turn, created_at, updated_at, superseded_by, pinned
    episodes  每次会话的摘要：summary, started_at, ended_at
    *_fts     FTS5（trigram 分词，中文子串可搜）
    *_vec     sqlite-vec 向量索引（bge-small-zh-v1.5，512 维，CPU 计算，不占显存）

写入：
    - “记住……”：直接写入并置顶（pinned）
    - 规则抽取的明确事实（我叫/我住在/我喜欢……），同类 key 覆盖旧值（旧值标记 superseded_by）
    - 会话结束（5 分钟无对话）后，后台用陪聊模型（JSON schema 约束输出）：
        1) 写会话摘要；2) 抽取候选事实；3) 对每条候选与相似旧事实比较，给出 ADD / UPDATE / DELETE / NOOP（Mem0 式）
读取：向量检索 + 全文检索，RRF 融合，取前 5 条事实 + 前 2 段摘要，总长不超过约 400 token（600 字）
删除：“忘掉 X” 确认后硬删除（行、向量、全文索引），并清除本地对话记录里含 X 的行
无痕模式：开启后不写入任何记忆和对话记录
加密（10-24 起，data/memory.db 默认开）：facts.text、episodes.summary 用 canaan_crypto 加密（AES-256-GCM，密钥 DPAPI 保护）。
    加密时全文索引和 sqlite-vec 不写任何内容（它们只能存明文）；向量加密后存在 *_emb 表里，检索时在内存里算余弦相似度，
    三字 / 双字重合也在内存里算。旧的明文库第一次打开时就地加密并 VACUUM。别的路径（知识库、测试）默认不加密。
"""
import os
import re
import json
import time
import sqlite3
from contextlib import contextmanager
import threading
import urllib.request

from canaan_conversation_time import record_timestamps

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "memory.db")
LEGACY_PROFILE = os.path.join(DATA_DIR, "user_profile.json")
CONV_LOG = os.path.join(BASE_DIR, "logs", "conversations.jsonl")
EMBED_DIR = os.path.join(BASE_DIR, "models", "bge-small-zh-v1.5")
EMBED_DIM = 512
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"


# ----------------------------------------------------------------------------
# 嵌入（CPU）
# ----------------------------------------------------------------------------

class Embedder:
    _lock = threading.Lock()

    def __init__(self, model_dir=EMBED_DIR):
        self.model_dir = model_dir
        self._tok = self._model = None

    def available(self):
        return os.path.exists(os.path.join(self.model_dir, "config.json"))

    def _load(self):
        if self._model is None:
            import torch
            from transformers import AutoTokenizer, AutoModel
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 2)))
            self._tok = AutoTokenizer.from_pretrained(self.model_dir)
            self._model = AutoModel.from_pretrained(self.model_dir).eval()

    def encode(self, texts, query=False):
        import torch
        with self._lock:
            self._load()
            if query:
                texts = [QUERY_INSTRUCTION + t for t in texts]
            batch = self._tok(texts, padding=True, truncation=True, max_length=128, return_tensors="pt")
            with torch.no_grad():
                out = self._model(**batch).last_hidden_state[:, 0]
            out = torch.nn.functional.normalize(out, dim=-1)
            return [v.tolist() for v in out]


_default_embedder = None
_default_embedder_lock = threading.Lock()


def default_embedder():
    """Share the read-only CPU retrieval model across stores in this process."""
    global _default_embedder
    with _default_embedder_lock:
        if _default_embedder is None:
            _default_embedder = Embedder()
        return _default_embedder


# ----------------------------------------------------------------------------
# 存储
# ----------------------------------------------------------------------------

_STOP = set("的了吗呢啊吧我你他她是在有和就都也还么什么怎么哪个这那一个主人")


def _bigrams(text):
    t = re.sub(r"\W", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1) if not (t[i] in _STOP and t[i + 1] in _STOP)}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _as_time_text(value):
    """把 epoch 数字（秒或毫秒）转成 "%Y-%m-%d %H:%M:%S"，其余原样返回。

    日期列是 TEXT，早期写入方（Dots 桥的老版本直接插 updated_at/now）把数字放了进来，
    这些行会被 [5:10] 之类的日期切片读成乱码，也会打乱按时间排序。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
        num = float(value)
        if num > 1e11:  # 毫秒
            num /= 1000.0
        if num < 1e8:  # 1973 年以前，不像时间戳（更可能是 YYYYMMDD 之类的写法）
            return value
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(num))
    return value


def _day_label(value):
    """日期列 → “10月13日”；没有像样日期就返回空串，由调用方省掉前缀。"""
    text = _as_time_text(value) or ""
    if len(text) < 10:
        return ""
    return f"{text[5:10].replace('-', '月')}日"


def _trigrams(text):
    t = re.sub(r"\W", "", text or "")
    return {t[i:i + 3] for i in range(len(t) - 2)}


# 加密的列：列名 → 附加数据（AAD）。查询结果里这些列名会自动解密。
ENCRYPTED_COLUMNS = {"text": "facts.text", "summary": "episodes.summary"}
_AUTO = object()


class MemoryStore:
    def __init__(self, path=DB_PATH, embedder=None, migrate=True, cipher=_AUTO):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.embedder = embedder if embedder is not None else default_embedder()
        if cipher is _AUTO:
            cipher = None
            if os.path.abspath(path) == os.path.abspath(DB_PATH):
                import canaan_crypto
                cipher = canaan_crypto.default_cipher()
        self.cipher = cipher
        self._emb_cache = {}
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self.db = sqlite3.connect(path, check_same_thread=False)
        import canaan_crypto
        canaan_crypto.attach(self.db, cipher, ENCRYPTED_COLUMNS)
        self.db.execute("PRAGMA secure_delete=ON")
        self.vec = self.embedder.available() if cipher is not None else self._load_vec()
        self._init_schema()
        if cipher is not None:
            self._encrypt_existing()
        if migrate and not self.incognito:
            self._migrate_legacy()

    # --- 加密 ---
    def _enc(self, text, column):
        return self.cipher.encrypt(text, ENCRYPTED_COLUMNS[column]) if self.cipher is not None else text

    def _encrypt_existing(self):
        """旧的明文库：就地加密两列、补加密向量、清空只能存明文的索引，再 VACUUM 把旧页面覆盖掉。"""
        with self._lock:
            plain = {t: self.db.execute(f"SELECT id, {c} FROM {t} WHERE {c} NOT LIKE 'enc1:%'").fetchall()
                     for t, c in (("facts", "text"), ("episodes", "summary"))}
            leftovers = any(self.db.execute(f"SELECT 1 FROM {t}_fts LIMIT 1").fetchone() for t in ("facts", "episodes"))
            legacy_vec = [r[0] for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('facts_vec','episodes_vec')")]
            if not (plain["facts"] or plain["episodes"] or leftovers or legacy_vec):
                return
            with self._transaction():
                for table, col in (("facts", "text"), ("episodes", "summary")):
                    for rid, value in plain[table]:
                        self.db.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (self._enc(value, col), rid))
                        self._store_emb(table, rid, value)
                    # DELETE 只给 FTS5 记删除标记，旧的分词数据还留在内部表里；整张删掉重建才干净
                    self.db.execute(f"DROP TABLE IF EXISTS {table}_fts")
                    col = "text" if table == "facts" else "summary"
                    self.db.execute(f"CREATE VIRTUAL TABLE {table}_fts USING fts5({col}, tokenize='trigram')")
            for name in legacy_vec:
                try:
                    self._load_vec()
                    self.db.execute(f"DROP TABLE {name}")
                except Exception as e:
                    print(f"[!] 旧向量表 {name} 没删掉（{type(e).__name__}），里面可能还有明文向量")
            self.db.commit()
            self.db.execute("VACUUM")
            print(f"[🔒] 记忆库已加密：事实 {len(plain['facts'])} 条、会话摘要 {len(plain['episodes'])} 段")

    def _drop_indexes(self, table, rid):
        """删一行时连带删掉它的全文 / 向量索引（加密时只有加密向量）。"""
        if self.cipher is not None:
            self._drop_emb(table, rid)
            return
        self.db.execute(f"DELETE FROM {table}_fts WHERE rowid=?", (rid,))
        if self.vec:
            self.db.execute(f"DELETE FROM {table}_vec WHERE rowid=?", (rid,))

    def _store_emb(self, table, rid, text):
        if self.cipher is None or not self.vec:
            return
        try:
            import numpy as np
            vec = np.asarray(self.embedder.encode([text])[0], np.float32)
        except Exception:
            return
        blob = self.cipher.encrypt_bytes(vec.tobytes(), f"{table}_emb")
        self.db.execute(f"INSERT OR REPLACE INTO {table}_emb(id, data) VALUES(?,?)", (rid, blob))
        self._emb_cache.pop(table, None)

    def _drop_emb(self, table, rid):
        if self.cipher is not None:
            self.db.execute(f"DELETE FROM {table}_emb WHERE id=?", (rid,))
            self._emb_cache.pop(table, None)

    def _embeddings(self, table):
        """{id: 向量}，解密后只放内存。"""
        cached = self._emb_cache.get(table)
        if cached is None:
            import numpy as np
            cached = {rid: np.frombuffer(self.cipher.decrypt_bytes(blob, f"{table}_emb"), np.float32)
                      for rid, blob in self.db.execute(f"SELECT id, data FROM {table}_emb")}
            self._emb_cache[table] = cached
        return cached

    @property
    def incognito(self):
        # 每次从数据库读，API 与 hub 分别持有连接时也能共享开关。
        with self._lock:
            row = self.db.execute("SELECT value FROM memory_settings WHERE key='incognito'").fetchone()
            return bool(row and row[0] == "1")

    @incognito.setter
    def incognito(self, value):
        with self._transaction():
            changed = self.incognito != bool(value)
            self.db.execute("INSERT OR REPLACE INTO memory_settings(key,value) VALUES('incognito',?)",
                            ("1" if value else "0",))
            if changed:
                self._invalidate_background()

    @contextmanager
    def _transaction(self):
        """共享 SQLite 写锁下检查版本并修改；嵌套操作不提前提交。"""
        with self._lock:
            outer = self._transaction_depth == 0
            if outer and not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
                if outer:
                    self.db.commit()
            except BaseException:
                if outer:
                    self.db.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def _commit(self):
        if self._transaction_depth == 0:
            self.db.commit()

    @property
    def privacy_revision(self):
        with self._lock:
            row = self.db.execute("SELECT value FROM memory_settings WHERE key='privacy_revision'").fetchone()
            return int(row[0]) if row else 0

    @property
    def resume_after(self):
        with self._lock:
            row = self.db.execute("SELECT value FROM memory_settings WHERE key='resume_after'").fetchone()
            return float(row[0]) if row else 0.0

    def _invalidate_background(self):
        self.db.execute("INSERT INTO memory_settings(key,value) VALUES('privacy_revision','1') "
                        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
        self.db.execute("INSERT OR REPLACE INTO memory_settings(key,value) VALUES('resume_after',?)", (str(time.time()),))

    def _write_allowed(self, expected_revision):
        return not self.incognito and (expected_revision is None or expected_revision == self.privacy_revision)

    def observe_origin(self, origin, expected_revision):
        """固定后台任务第一次观察到的版本；跨轮询和重启不重新授权旧任务。"""
        if not isinstance(origin, str) or not origin or len(origin) > 512:
            raise ValueError("任务来源格式错误")
        with self._transaction():
            if not self._write_allowed(expected_revision):
                return None
            self.db.execute("INSERT OR IGNORE INTO background_origins(origin,revision) VALUES(?,?)",
                            (origin, expected_revision))
            return self.db.execute("SELECT revision FROM background_origins WHERE origin=?", (origin,)).fetchone()[0]

    def _load_vec(self):
        try:
            import sqlite_vec
            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
            self.db.enable_load_extension(False)
            return self.embedder.available()
        except Exception as e:
            print(f"[!] 向量索引不可用（{e}），只用全文检索")
            return False

    def _init_schema(self):
        with self._lock:
            c = self.db
            c.executescript("""
            CREATE TABLE IF NOT EXISTS memory_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS facts(
                id INTEGER PRIMARY KEY, text TEXT NOT NULL, category TEXT, key TEXT, source_turn TEXT,
                created_at TEXT, updated_at TEXT, superseded_by INTEGER, pinned INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS episodes(
                id INTEGER PRIMARY KEY, summary TEXT NOT NULL, started_at TEXT, ended_at TEXT, origin TEXT);
            CREATE TABLE IF NOT EXISTS forgotten_origins(origin TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS background_origins(origin TEXT PRIMARY KEY, revision INTEGER NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, tokenize='trigram');
            CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(summary, tokenize='trigram');
            """)
            if "origin" not in {r[1] for r in c.execute("PRAGMA table_info(episodes)")}:
                c.execute("ALTER TABLE episodes ADD COLUMN origin TEXT")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS episodes_origin ON episodes(origin) WHERE origin IS NOT NULL")
            if self.cipher is not None:
                c.execute("CREATE TABLE IF NOT EXISTS facts_emb(id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
                c.execute("CREATE TABLE IF NOT EXISTS episodes_emb(id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
            elif self.vec:
                c.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(embedding float[{EMBED_DIM}])")
                c.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS episodes_vec USING vec0(embedding float[{EMBED_DIM}])")
            c.commit()

    def _migrate_legacy(self):
        """把旧的 data/user_profile.json 导入（只做一次，导入后改名为 .migrated）。"""
        if not os.path.exists(LEGACY_PROFILE):
            return
        try:
            with open(LEGACY_PROFILE, encoding="utf-8") as f:
                facts = json.load(f).get("facts", [])
            for x in facts:
                self.add(x.get("text", ""), key=x.get("key"), category=x.get("key") or "legacy",
                         source_turn="user_profile.json", created_at=x.get("time"))
            os.replace(LEGACY_PROFILE, LEGACY_PROFILE + ".migrated")
            print(f"[🧠] 已从 user_profile.json 迁移 {len(facts)} 条记忆")
        except Exception as e:
            print(f"[!] 记忆迁移失败: {e}")

    # --- 向量 ---
    def _embed(self, text, query=False):
        if not self.vec:
            return None
        return json.dumps(self.embedder.encode([text], query=query)[0])

    # --- 事实 ---
    def active_facts(self):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM facts WHERE superseded_by IS NULL ORDER BY pinned DESC, updated_at DESC")]

    def texts(self):
        return [f["text"] for f in self.active_facts()][::-1]

    def add(self, text, key=None, category="fact", pinned=False, source_turn="", created_at=None, expected_revision=None):
        """写入一条事实；同 key 覆盖旧值（旧值 superseded_by 指向新值）；完全相同的文本不重复写。返回新 id 或 None。"""
        text = (text or "").strip(" 。，,.!！")
        if not text or len(text) > 80 or self.incognito:
            return None
        with self._transaction():
            if not self._write_allowed(expected_revision):
                return None
            if self.db.execute("SELECT 1 FROM facts WHERE dec(text,'facts.text')=? AND superseded_by IS NULL",
                               (text,)).fetchone():
                return None
            ts = _as_time_text(created_at) or _now()
            cur = self.db.execute(
                "INSERT INTO facts(text,category,key,source_turn,created_at,updated_at,pinned) VALUES(?,?,?,?,?,?,?)",
                (self._enc(text, "text"), category, key, source_turn, ts, ts, int(pinned)))
            fid = cur.lastrowid
            if self.cipher is not None:
                self._store_emb("facts", fid, text)
            else:
                self.db.execute("INSERT INTO facts_fts(rowid,text) VALUES(?,?)", (fid, text))
                emb = self._embed(text)
                if emb:
                    self.db.execute("INSERT INTO facts_vec(rowid,embedding) VALUES(?,?)", (fid, emb))
            if key:
                self.db.execute("UPDATE facts SET superseded_by=? WHERE key=? AND id<>? AND superseded_by IS NULL",
                                (fid, key, fid))
            self._commit()
            return fid

    def update(self, fid, new_text, created_at=None, expected_revision=None):
        """事实有新版本：写入新事实，旧事实标记被取代（保留历史，便于回答“以前…现在…”）。"""
        with self._transaction():
            if not self._write_allowed(expected_revision):
                return None
            old = self.db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
            if not old:
                return None
            nid = self.add(new_text, key=old["key"], category=old["category"], pinned=bool(old["pinned"]),
                           created_at=created_at, expected_revision=expected_revision)
            if nid:
                self.db.execute("UPDATE facts SET superseded_by=? WHERE id=?", (nid, fid))
                self._commit()
            return nid

    def retire(self, fid, expected_revision=None):
        """事实已不成立（如“我不住合肥了”）：标记为被取代（取代者 0），不再出现在检索结果里。"""
        with self._transaction():
            if not self._write_allowed(expected_revision):
                return False
            changed = self.db.execute("UPDATE facts SET superseded_by=0 WHERE id=?", (fid,)).rowcount
            self._commit()
            return bool(changed)

    # --- 看板里按条管理（主人在本机操作；删除是硬删除，改写不留旧版本） ---
    def delete_fact(self, fid):
        with self._transaction():
            n = self.db.execute("DELETE FROM facts WHERE id=?", (fid,)).rowcount
            self._drop_indexes("facts", fid)
            self.db.execute("UPDATE facts SET superseded_by=0 WHERE superseded_by=?", (fid,))
            if n:
                self._invalidate_background()
            self._commit()
            return bool(n)

    def delete_episode(self, eid):
        with self._transaction():
            row = self.db.execute("SELECT origin FROM episodes WHERE id=?", (eid,)).fetchone()
            if row and row[0]:
                self.db.execute("INSERT OR IGNORE INTO forgotten_origins(origin) VALUES(?)", (row[0],))
            n = self.db.execute("DELETE FROM episodes WHERE id=?", (eid,)).rowcount
            self._drop_indexes("episodes", eid)
            if n:
                self._invalidate_background()
            self._commit()
            return bool(n)

    def edit_fact(self, fid, new_text):
        """主人在看板里改写一条：写新内容（保留分类、置顶），旧内容硬删除，不当作“以前的版本”留着。"""
        new_text = (new_text or "").strip()
        if not new_text or len(new_text) > 80:
            raise ValueError("记忆内容须为 1–80 字")
        if self.incognito:
            raise ValueError("无痕模式下不能写入记忆")
        with self._transaction():
            if self.incognito:
                raise ValueError("无痕模式下不能写入记忆")
            old = self.db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
            if not old:
                raise KeyError("这条记忆不存在")
            if old["text"] == new_text:
                return fid
            nid = self.add(new_text, category=old["category"], pinned=bool(old["pinned"]))
            if nid is None:   # 已经有一条一模一样的，留那条
                nid = self.db.execute("SELECT id FROM facts WHERE dec(text,'facts.text')=? AND superseded_by IS NULL",
                                      (new_text.strip(" 。，,.!！"),)).fetchone()[0]
            elif old["key"]:
                self.db.execute("UPDATE facts SET key=? WHERE id=?", (old["key"], nid))
                self._commit()
            self.delete_fact(fid)
        return nid

    def set_pinned(self, fid, pinned):
        with self._lock:
            n = self.db.execute("UPDATE facts SET pinned=? WHERE id=?", (1 if pinned else 0, fid)).rowcount
            self._commit()
            return bool(n)

    def overview(self, query="", limit=200):
        """看板用：当前有效的事实（置顶在前）+ 最近的会话摘要（含“做过的任务”）。"""
        q = (query or "").strip()
        facts = [f for f in self.active_facts() if not q or q in f["text"]][:limit]
        with self._lock:
            eps = []
            for r in self.db.execute(
                    "SELECT id,summary,started_at,ended_at FROM episodes "
                    "WHERE (?='' OR dec(summary,'episodes.summary') LIKE ?) ORDER BY id DESC LIMIT ?",
                    (q, f"%{q}%", limit)):
                row = dict(r)
                row["started_at"] = _as_time_text(row["started_at"])
                row["ended_at"] = _as_time_text(row["ended_at"])
                eps.append(row)
        return {"incognito": self.incognito,
                "facts": [{k: f[k] for k in ("id", "text", "category", "pinned", "updated_at")} for f in facts],
                "episodes": eps}

    def matching(self, keyword):
        kw = (keyword or "").strip()
        with self._lock:
            if kw in ("", "所有", "全部", "一切", "所有事", "全部的事"):
                return [dict(r) for r in self.db.execute("SELECT * FROM facts")]
            pattern = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            return [dict(r) for r in self.db.execute("SELECT * FROM facts WHERE dec(text,'facts.text') LIKE ? ESCAPE '\\'",
                                                     (pattern,))]

    def forget(self, keyword, scrub_log=True):
        """硬删除：事实行、全文索引、向量，以及会话摘要与本地对话记录里含关键词的内容。返回删除的事实数。"""
        kw = (keyword or "").strip()
        with self._transaction():
            rows = self.matching(kw)
            self._invalidate_background()
            for r in rows:
                self.db.execute("DELETE FROM facts WHERE id=?", (r["id"],))
                self._drop_indexes("facts", r["id"])
            if kw and kw not in ("所有", "全部", "一切", "所有事", "全部的事"):
                pattern = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                eps = [e[0] for e in self.db.execute(
                    "SELECT id FROM episodes WHERE dec(summary,'episodes.summary') LIKE ? ESCAPE '\\'", (pattern,))]
            else:
                eps = [e[0] for e in self.db.execute("SELECT id FROM episodes")]
            for eid in eps:
                origin = self.db.execute("SELECT origin FROM episodes WHERE id=?", (eid,)).fetchone()[0]
                if origin:
                    self.db.execute("INSERT OR IGNORE INTO forgotten_origins(origin) VALUES(?)", (origin,))
                self.db.execute("DELETE FROM episodes WHERE id=?", (eid,))
                self._drop_indexes("episodes", eid)
            self._commit()
        with self._lock:
            self.db.execute("VACUUM")
        if scrub_log and kw:
            scrub_conversation_log(None if kw in ("所有", "全部", "一切", "所有事", "全部的事") else kw)
        return len(rows)

    # --- 会话摘要 ---
    def add_episode(self, summary, started_at=None, ended_at=None, expected_revision=None, origin=None):
        summary = (summary or "").strip()
        if not summary or self.incognito:
            return None
        with self._transaction():
            if not self._write_allowed(expected_revision):
                return None
            if origin and (self.db.execute("SELECT 1 FROM forgotten_origins WHERE origin=?", (origin,)).fetchone()
                           or self.db.execute("SELECT 1 FROM episodes WHERE origin=?", (origin,)).fetchone()):
                return None
            cur = self.db.execute("INSERT INTO episodes(summary,started_at,ended_at,origin) VALUES(?,?,?,?)",
                                  (self._enc(summary, "summary"), _as_time_text(started_at) or _now(),
                                   _as_time_text(ended_at) or _now(), origin))
            eid = cur.lastrowid
            if self.cipher is not None:
                self._store_emb("episodes", eid, summary)
            else:
                self.db.execute("INSERT INTO episodes_fts(rowid,summary) VALUES(?,?)", (eid, summary))
                emb = self._embed(summary)
                if emb:
                    self.db.execute("INSERT INTO episodes_vec(rowid,embedding) VALUES(?,?)", (eid, emb))
            self._commit()
            return eid

    # --- 检索 ---
    def _hybrid(self, table, text_col, query, k, extra_where="", embed_query=None):
        """向量 + 全文，RRF 融合（k=60）。返回 [(id, score)]。"""
        scores = {}
        if self.cipher is not None:
            return self._hybrid_encrypted(table, text_col, query, k, extra_where, embed_query)
        with self._lock:
            if self.vec and self.db.execute(f"SELECT 1 FROM {table}_vec LIMIT 1").fetchone():
                q = embed_query() if embed_query is not None else self._embed(query, query=True)
                for rank, r in enumerate(self.db.execute(
                        f"SELECT rowid FROM {table}_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?", (q, k * 4))):
                    scores[r[0]] = scores.get(r[0], 0) + 1 / (60 + rank)
            # trigram 分词：按查询里每个 3 字窗口做 OR 匹配（中文没有空格分词）
            terms = []
            for run in re.findall(r"\w{3,}", query):
                for i in range(len(run) - 2):
                    if run[i:i + 3] not in terms:
                        terms.append(run[i:i + 3])
            fts_q = " OR ".join('"' + t.replace('"', "") + '"' for t in terms[:24])
            if fts_q:
                try:
                    for rank, r in enumerate(self.db.execute(
                            f"SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ? ORDER BY rank LIMIT ?", (fts_q, k * 4))):
                        scores[r[0]] = scores.get(r[0], 0) + 1 / (60 + rank)
                except sqlite3.OperationalError:
                    pass
            # 双字重合（表很小，直接在内存里算）：补上短词（“猫”“生日”）的召回
            qb = _bigrams(query)
            if qb:
                rows = self.db.execute(f"SELECT id, {text_col} FROM {table}").fetchall()
                overlap = sorted(((len(qb & _bigrams(r[1])), r[0]) for r in rows), reverse=True)
                for rank, (n, rid) in enumerate([x for x in overlap if x[0] > 0][:k * 4]):
                    scores[rid] = scores.get(rid, 0) + 1 / (60 + rank)
            ranked = sorted(scores.items(), key=lambda x: -x[1])
            if extra_where:
                ok = {r[0] for r in self.db.execute(f"SELECT id FROM {table} WHERE {extra_where}")}
                ranked = [x for x in ranked if x[0] in ok]
        return ranked[:k]

    def _hybrid_encrypted(self, table, text_col, query, k, extra_where="", embed_query=None):
        """加密库：向量 / 三字（代替全文索引）/ 双字三路排名都在内存里算，RRF 融合方式和明文库一样。"""
        scores = {}

        def fuse(ranked_ids):
            for rank, rid in enumerate(ranked_ids[:k * 4]):
                scores[rid] = scores.get(rid, 0) + 1 / (60 + rank)

        with self._lock:
            where = f" WHERE {extra_where}" if extra_where else ""
            rows = self.db.execute(f"SELECT id, {text_col} FROM {table}{where}").fetchall()
            if not rows:
                return []
            ids = {r[0] for r in rows}
            embs = self._embeddings(table) if self.vec else {}
            embs = {rid: v for rid, v in embs.items() if rid in ids}
            if embs:
                import numpy as np
                q = embed_query() if embed_query is not None else self._embed(query, query=True)
                if q:
                    qv = np.asarray(json.loads(q) if isinstance(q, str) else q, np.float32)
                    fuse(sorted(embs, key=lambda rid: -float(np.dot(embs[rid], qv))))
            for grams in (_trigrams, _bigrams):
                qg = grams(query)
                if qg:
                    overlap = sorted(((len(qg & grams(r[1])), r[0]) for r in rows), reverse=True)
                    fuse([rid for n, rid in overlap if n > 0])
        return sorted(scores.items(), key=lambda x: -x[1])[:k]

    def search(self, query, k_facts=5, k_episodes=2, budget_chars=600):
        """返回 (facts, episodes)：facts 为 [{id,text,created_at}]，置顶事实总会带上（计入条数）。"""
        query_embedding = None

        def embed_query():
            nonlocal query_embedding
            if query_embedding is None:
                query_embedding = self._embed(query, query=True)
            return query_embedding

        with self._lock:
            pinned = [dict(r) for r in self.db.execute(
                "SELECT id,text,created_at FROM facts WHERE pinned=1 AND superseded_by IS NULL ORDER BY updated_at DESC LIMIT 2")]
            hits = self._hybrid("facts", "text", query, k_facts, "superseded_by IS NULL", embed_query=embed_query)
            facts = list(pinned)
            for fid, _ in hits:
                if len(facts) >= k_facts:
                    break
                if all(f["id"] != fid for f in facts):
                    r = self.db.execute("SELECT id,text,created_at FROM facts WHERE id=?", (fid,)).fetchone()
                    if r:
                        facts.append(dict(r))
            eps = []
            for eid, _ in self._hybrid("episodes", "summary", query, k_episodes, embed_query=embed_query):
                r = self.db.execute("SELECT id,summary,ended_at FROM episodes WHERE id=?", (eid,)).fetchone()
                if r:
                    eps.append(dict(r))
        used, out_f, out_e = 0, [], []
        for f in facts:
            if used + len(f["text"]) > budget_chars:
                break
            out_f.append(f)
            used += len(f["text"]) + 8
        for e in eps:
            if used + len(e["summary"]) > budget_chars:
                break
            out_e.append(e)
            used += len(e["summary"]) + 8
        return out_f, out_e

    def context_lines(self, query):
        """给陪聊提示词用的【关于主人】文本行（带日期，便于回答“什么时候”）。"""
        facts, eps = self.search(query)
        lines = []
        for f in facts:
            day = _day_label(f["created_at"])
            lines.append(f"{day}：{f['text']}" if day else f["text"])
        for e in eps:
            day = _day_label(e["ended_at"])
            lines.append(f"{day}的聊天：{e['summary']}" if day else f"聊天：{e['summary']}")
        return lines


def recent_conversation(path=None, max_turns=4, within=7200.0, now=None, not_before=0.0):
    """跨重启接着聊：读本地对话记录最后几轮（只要 within 秒以内的），返回 [(原话, 决策 dict)]。
    只读文件末尾一小段；时间格式不对或读不到就当没有。"""
    path = path or CONV_LOG
    now = time.time() if now is None else now
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - 64 * 1024)
            f.seek(start)
            lines = f.read().decode("utf-8", errors="replace").splitlines()
            if start:
                lines = lines[1:]  # 从中间开始读，第一行可能是半行
    except OSError:
        return []
    out = []
    import canaan_crypto
    for line in lines[-50:]:
        try:
            rec = canaan_crypto.open_record(json.loads(line))
            stamps = record_timestamps(rec, clock=time)
        except Exception:   # 坏行、时间不对、密钥解不开：都当没有
            continue
        if (stamps and all(ts > not_before and 0 <= now - ts <= within for ts in stamps)
                and isinstance(rec.get("user"), str) and rec["user"].strip()):
            out.append((rec["user"], {"action": rec.get("action") or "chat", "param": rec.get("param") or "",
                                      "reply": rec.get("spoken") or ""}))
    return out[-max_turns:]


def scrub_conversation_log(keyword, path=None):
    """从本地对话记录中删除含关键词的行（keyword=None 时清空）。"""
    path = path or CONV_LOG
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    def mentions(line):
        """加密的行要解开看；解不开的行留着（删错比漏删更糟：这一行可能根本不含关键词）。"""
        if keyword in line:
            return True
        try:
            import canaan_crypto
            return keyword in json.dumps(canaan_crypto.open_record(json.loads(line)), ensure_ascii=False)
        except Exception:
            return False
    keep = [] if keyword is None else [l for l in lines if not mentions(l)]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(keep)
    os.replace(tmp, path)
    return len(lines) - len(keep)


# ----------------------------------------------------------------------------
# 会话结束后的整理（摘要 + Mem0 式增删改）
# ----------------------------------------------------------------------------

def _shared_num_ctx():
    """和陪聊同一个模型同一个上下文长度（canaan_config.SHARED_NUM_CTX），免得 Ollama 重载。"""
    try:
        import canaan_config
        return getattr(canaan_config, "SHARED_NUM_CTX", 4096)
    except Exception:
        return 4096


def _llm_json(model, prompt, schema, num_predict=300, timeout=60):
    from canaan_companion import chat_keep_alive
    payload = {"model": model, "stream": False, "keep_alive": chat_keep_alive(), "format": schema,
               "messages": [{"role": "user", "content": prompt}],
               "options": {"temperature": 0, "num_predict": num_predict, "num_ctx": _shared_num_ctx()}}
    req = urllib.request.Request(OLLAMA_CHAT, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(json.loads(r.read().decode("utf-8"))["message"]["content"])


# ---------------------------------------------------------------------------
# 回答“你记得我……吗”：要求模型先原样抄出依据，再由代码核对依据确实在记忆里、回答确实来自依据；
# 核对不过就说不记得（小模型常拿相关但不对题的记忆硬答，如问奶奶叫什么、记忆里只有奶奶的年龄）
# ---------------------------------------------------------------------------
NO_MEMORY_REPLY = "这个我不记得，你好像没跟我说过。"

EVIDENCE_QA_PROMPT = """你是主人的语音助手。下面是你记得的关于主人的事（前面是日期）：
{memory}
今天是 {today}。主人问：{q}
先在上面的记忆里找能【直接】回答这个问题的那一句，原样抄到 evidence（只抄一句；只是相关、不能直接回答就留空）。
evidence 不为空时 known=true，用一句话回答（answer），答案必须来自 evidence；同一件事有新旧两个说法时，以日期更晚的为准。
evidence 为空时 known=false，answer 写“不知道，你没跟我说过”。
只输出 JSON：{{"evidence": "...", "known": true 或 false, "answer": "..."}}"""

EVIDENCE_QA_SCHEMA = {"type": "object", "properties": {"evidence": {"type": "string"}, "known": {"type": "boolean"},
                                                       "answer": {"type": "string"}},
                      "required": ["evidence", "known", "answer"]}

# 第二版：依据可以是多句（跨会话推理、新旧说法对比都需要几条一起看），措辞不再要求“一句就能直接回答”
EVIDENCE_QA_PROMPT_MULTI = """你是主人的语音助手。下面是你记得的关于主人的事（前面是日期）：
{memory}
今天是 {today}。主人问：{q}
把回答这个问题要用到的记忆原样抄进 evidence 列表（可以抄多句；同一件事有新旧说法时都抄上，回答以日期更晚的为准）。
如果记忆里只提到相关的人或事、但没有问的那个具体信息（比如问名字而记忆里没有名字），evidence 留空列表，known=false，answer 写“不知道，你没跟我说过”。
否则 known=true，用一句话回答（answer），答案只能来自 evidence。
只输出 JSON：{{"evidence": ["..."], "known": true 或 false, "answer": "..."}}"""

EVIDENCE_QA_SCHEMA_MULTI = {"type": "object",
                            "properties": {"evidence": {"type": "array", "items": {"type": "string"}},
                                           "known": {"type": "boolean"}, "answer": {"type": "string"}},
                            "required": ["evidence", "known", "answer"]}

# 第二道核对：只问一件事（这句记忆里有没有问题要的那个信息），小模型做单一判断比边找边答可靠
VERIFY_PROMPT = """问题：{q}
记忆：{line}
从这句记忆里原样摘出回答这个问题的那几个字（问名字就摘名字，问哪个/哪里就摘具体是哪个，问时间就摘时间，问多少就摘数量）。
这句记忆里没有问的那个信息（只是提到相关的人或事）就输出空字符串。
只输出 JSON，span 里直接写摘出的字，例如问“我的猫叫什么”、记忆是“主人的猫叫团子”时输出 {{"span": "团子"}}"""
VERIFY_SCHEMA = {"type": "object", "properties": {"span": {"type": "string"}}, "required": ["span"]}

_STOP_CHARS = set("你我他她它的了是在有吗呢吧啊呀哦嗯就都也还和跟与对说过主人这那个一些么什哪谁几多少怎样")


def _norm(s):
    return re.sub(r"[\s，,。.!！?？~～：:；;“”\"'、（）()\-]", "", s or "")


def _bigram_set(s):
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def _evidence_line(evidence, memory_lines):
    """依据在哪条记忆里（双字重合 ≥60%），找不到返回 None（模型编的依据）。"""
    eb = _bigram_set(evidence)
    if not eb:
        return None
    best, best_line = 0.0, None
    for line in memory_lines:
        lb = _bigram_set(line)
        r = len(eb & lb) / len(eb)
        if r > best:
            best, best_line = r, line
    return best_line if best >= 0.6 else None


def _answer_supported(answer, question, line):
    """回答里去掉问题本身已有的字和虚词后，剩下的内容要在依据所在的那条记忆里出现。"""
    nums_a = [int(x) for x in re.findall(r"\d+", answer or "")]
    nums_l = {int(x) for x in re.findall(r"\d+", line or "")}
    if nums_a and all(n in nums_l for n in nums_a):
        return True
    rest = [ch for ch in _norm(answer) if ch not in _norm(question) and ch not in _STOP_CHARS and not ch.isdigit()]
    if not rest:
        return True  # 只是复述/是否类回答（“对，养了”），依据存在即可
    hit = sum(1 for ch in rest if ch in (line or ""))
    return hit / len(rest) >= 0.5


def answer_from_memory(model, question, memory_lines, today="", check_evidence=True, verify=False):
    """据检索到的记忆回答问题。返回 {"known","answer","evidence","reason"}。
    check_evidence=False 时等同旧做法（只看模型的 known）。"""
    try:
        obj = _llm_json(model, EVIDENCE_QA_PROMPT.format(memory="\n".join(memory_lines) or "（无）", q=question,
                                                         today=today or time.strftime("%Y年%m月%d日")),
                        EVIDENCE_QA_SCHEMA, num_predict=320)
    except (ValueError, KeyError):
        return {"known": False, "answer": NO_MEMORY_REPLY, "evidence": "", "reason": "bad_json"}
    known, ans, ev = bool(obj.get("known")), (obj.get("answer") or "").strip(), (obj.get("evidence") or "").strip()
    if not known:
        return {"known": False, "answer": ans or NO_MEMORY_REPLY, "evidence": ev, "reason": "model"}
    if check_evidence:
        line = _evidence_line(ev, memory_lines)
        if line is None:
            return {"known": False, "answer": NO_MEMORY_REPLY, "evidence": ev, "reason": "no_evidence"}
        if not _answer_supported(ans, question, line):
            return {"known": False, "answer": NO_MEMORY_REPLY, "evidence": ev, "reason": "answer_not_in_evidence"}
        if verify:
            v = _llm_json(model, VERIFY_PROMPT.format(q=question, line=line), VERIFY_SCHEMA, num_predict=40)
            span = _norm(v.get("span") or "")
            body = _norm(line.split("：", 1)[-1]) if "：" in line else _norm(line)
            # 摘出的答案要真在记忆里，且不能只是问题里已有的字（“老公叫什么”→“老公”）
            novel = [ch for ch in span if ch not in _norm(question) and ch not in _STOP_CHARS]
            if not span or (span not in _norm(line) and span not in body) or not novel:
                return {"known": False, "answer": NO_MEMORY_REPLY, "evidence": ev, "reason": "verify_no"}
    return {"known": True, "answer": ans, "evidence": ev, "reason": "ok"}


def answer_from_memory_multi(model, question, memory_lines, today="", check_evidence=True, verify=False):
    """第二版：依据可以是多句。核对：每句依据都在记忆里；回答去掉问题已有字后的内容出现在这些依据所在的记忆里。"""
    try:
        obj = _llm_json(model, EVIDENCE_QA_PROMPT_MULTI.format(memory="\n".join(memory_lines) or "（无）", q=question,
                                                               today=today or time.strftime("%Y年%m月%d日")),
                        EVIDENCE_QA_SCHEMA_MULTI, num_predict=400)
    except (ValueError, KeyError):
        return {"known": False, "answer": NO_MEMORY_REPLY, "evidence": [], "reason": "bad_json"}
    known, ans = bool(obj.get("known")), (obj.get("answer") or "").strip()
    ev = [e.strip() for e in (obj.get("evidence") or []) if isinstance(e, str) and e.strip()]
    if not known:
        return {"known": False, "answer": ans or NO_MEMORY_REPLY, "evidence": ev, "reason": "model"}
    if check_evidence:
        lines = [_evidence_line(e, memory_lines) for e in ev]
        lines = [l for l in lines if l]
        if not lines:
            return {"known": False, "answer": NO_MEMORY_REPLY, "evidence": ev, "reason": "no_evidence"}
        if not _answer_supported(ans, question, "\n".join(lines)):
            return {"known": False, "answer": NO_MEMORY_REPLY, "evidence": ev, "reason": "answer_not_in_evidence"}
        if (verify == "slot" and _SLOT_Q_RE.search(question) or verify is True) and \
                not _span_check(model, question, "\n".join(lines)):
            return {"known": False, "answer": NO_MEMORY_REPLY, "evidence": ev, "reason": "verify_no"}
    return {"known": True, "answer": ans, "evidence": ev, "reason": "ok"}


# 问具体某一项（名字、哪个、哪里、多少、什么牌子……）的问题才需要摘答案核对；“为什么/是不是/怎么样”类不需要
_SLOT_Q_RE = re.compile(r"叫什么|名字|是谁|哪个|哪家|哪所|哪位|哪里|哪儿|什么牌子|多少|几[点号岁个月年天次]|什么时候|什么颜色|多大|多久|几")


def _span_check(model, question, text):
    """让模型从依据里原样摘出问题要的那几个字；摘不出、摘出的不在依据里、或只是问题里已有的字 → 不通过。"""
    try:
        v = _llm_json(model, VERIFY_PROMPT.format(q=question, line=text), VERIFY_SCHEMA, num_predict=40)
    except (ValueError, KeyError):
        return False
    span = re.sub(r"[.…。]", "", _norm(v.get("span") or ""))
    novel = [ch for ch in span if ch not in _norm(question) and ch not in _STOP_CHARS]
    t = _norm(text)
    in_text = bool(span) and sum(1 for ch in span if ch in t) / len(span) >= 0.8
    return in_text and bool(novel)


SUMMARY_PROMPT = """下面是主人和语音助手的一段对话。用一两句话写出这次对话里主人说了什么、做了什么（只写事实，不写助手的话，不超过60字）。
{dialog}
只输出 JSON：{{"summary": "..."}}"""

EXTRACT_PROMPT = """下面是主人对语音助手说的话。找出其中关于主人本人、值得长期记住的事实：身份、住处、工作学习、家人朋友、宠物、喜好厌恶、计划安排、健康、习惯。
每条写成以“主人”开头的一句短话（如“主人养了一只叫团子的猫”）。闲聊、情绪、对助手的指令不算。没有就输出空列表。
{dialog}
只输出 JSON：{{"facts": ["..."]}}"""

OPS_PROMPT = """你在维护关于主人的长期记忆。已有记忆：
{existing}
新信息：{new}
判断应该怎么处理新信息：
- ADD：新的事实，与已有记忆都不冲突
- UPDATE：与某条已有记忆说的是同一件事但内容变了（如搬家、换工作、改了喜好），用新信息替换它（给出 id）
- DELETE：新信息表明某条已有记忆不再成立，且没有替代内容（给出 id）
- NOOP：已有记忆已经包含这条信息
只输出 JSON：{{"op": "ADD|UPDATE|DELETE|NOOP", "id": 已有记忆的 id 或 0}}"""

OPS_SCHEMA = {"type": "object", "properties": {"op": {"type": "string", "enum": ["ADD", "UPDATE", "DELETE", "NOOP"]},
                                               "id": {"type": "integer"}}, "required": ["op", "id"]}


class MemoryManager:
    """记录本次会话的对话；会话结束后在后台整理成摘要与事实。"""

    def __init__(self, store, model="canaan-chat:3b", idle_sec=300.0, clock=time.time,
                 stale_flush_sec=3600.0):
        self.store = store
        self.model = model
        self.idle_sec = idle_sec
        self.clock = clock
        self.stale_flush_sec = stale_flush_sec
        self.turns = []
        self.started = None
        self.first_at = None
        self.revision = None
        self.last = 0.0
        self._lock = threading.Lock()

    def on_turn(self, user, reply, expected_revision=None, defer_consolidation=False):
        """Buffer a turn; voice callers can leave idle consolidation to tick().

        A return after an idle gap then retains both parts in the same buffer,
        rather than starting model work alongside the current voice request.
        """
        if self.store.incognito:
            return
        with self._lock:
            revision = self.store.privacy_revision
            if expected_revision is not None and expected_revision != revision:
                return False
            if self.turns and self.revision != revision:
                self.turns, self.started = [], None
            now = self.clock()
            if self.turns and now - self.last > self.idle_sec and not defer_consolidation:
                self._close_locked(background=True)
            # 语音侧把整理交给 tick()，而演示/游戏模式会长期不叫 tick()：攒着的轮次只在内存里，
            # 进程一退就永久丢了。超过 stale_flush_sec 就自己收尾，把丢失窗口压回一小时。
            if self.turns and self.first_at is not None and self.stale_flush_sec \
                    and now - self.first_at > self.stale_flush_sec:
                self._close_locked(background=True)
            if not self.turns:
                self.started = _now()
                self.first_at = now
                self.revision = revision
            self.turns.append((user, reply))
            self.last = now

    def tick(self):
        """定时调用：会话空闲超过 idle_sec 就整理。"""
        with self._lock:
            if self.turns and self.clock() - self.last > self.idle_sec:
                self._close_locked(background=True)

    def close(self, background=False):
        with self._lock:
            return self._close_locked(background)

    def _close_locked(self, background):
        turns, started, revision = self.turns, self.started, self.revision
        self.turns, self.started, self.first_at, self.revision = [], None, None, None
        if not turns:
            return None
        if background:
            threading.Thread(target=self.consolidate, args=(turns, started), kwargs={"expected_revision": revision}, daemon=True).start()
            return None
        return self.consolidate(turns, started, expected_revision=revision)

    def consolidate(self, turns, started=None, ended=None, expected_revision=None):
        """摘要 + 抽取候选事实 + 逐条 ADD/UPDATE/DELETE/NOOP。返回操作记录（便于评测）。"""
        revision = self.store.privacy_revision if expected_revision is None else expected_revision

        def stale():
            return self.store.incognito or self.store.privacy_revision != revision

        if stale():
            return []
        dialog = "\n".join(f"主人：{u}" + (f"\n助手：{r}" if r else "") for u, r in turns)
        user_only = "\n".join(f"主人：{u}" for u, _ in turns)
        ops = []
        try:
            s = _llm_json(self.model, SUMMARY_PROMPT.format(dialog=dialog),
                          {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]})
            if stale():
                return []
            self.store.add_episode(s.get("summary", ""), started, ended, expected_revision=revision)
        except Exception as e:
            print(f"[!] 会话摘要失败: {e}")
        if stale():
            return []
        try:
            cand = _llm_json(self.model, EXTRACT_PROMPT.format(dialog=user_only),
                             {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
                              "required": ["facts"]}).get("facts", [])
        except Exception as e:
            print(f"[!] 事实抽取失败: {e}")
            cand = []
        for fact in cand[:8]:
            if stale():
                return []
            fact = (fact or "").strip()
            if not fact:
                continue
            similar, _ = self.store.search(fact, k_facts=4, k_episodes=0)
            existing = "\n".join(f"[{f['id']}] {f['text']}" for f in similar) or "（无）"
            try:
                d = _llm_json(self.model, OPS_PROMPT.format(existing=existing, new=fact), OPS_SCHEMA, num_predict=40)
            except Exception:
                d = {"op": "ADD", "id": 0}
            op, fid = d.get("op", "ADD"), int(d.get("id") or 0)
            if stale():
                return []
            ids = {f["id"] for f in similar}
            if op == "UPDATE" and fid in ids:
                self.store.update(fid, fact, created_at=ended, expected_revision=revision)
            elif op == "DELETE" and fid in ids:
                self.store.retire(fid, expected_revision=revision)
            elif op == "NOOP":
                pass
            else:
                op = "ADD"
                self.store.add(fact, category="session", source_turn=started or "", created_at=ended, expected_revision=revision)
            if stale():
                return []
            ops.append({"fact": fact, "op": op, "id": fid})
        return ops


_STORE = None


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = MemoryStore()
    return _STORE
