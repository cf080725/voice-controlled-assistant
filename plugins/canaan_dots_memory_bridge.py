# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Bidirectional Knowledge and Memory Bridge
Synchronizes long-term memory between Canaan (memory.db) and OpenDots (opendots.sqlite).

Architectural strengths:
  - MemGPT / Letta: Tiered memory hierarchy (working context, episodic memory, archival storage)
  - ElizaOS: Continuous evaluators and memory digestion
  - Privacy Shield: PII scrub before cross-agent knowledge propagation
"""
import os
import time
import json
import sqlite3
import logging
import threading
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

logger = logging.getLogger("canaan_dots_bridge")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CANAAN_DB = os.path.join(DATA_DIR, "memory.db")
DOTS_DB = os.environ.get("CANAAN_DOTS_DB", os.path.join(os.environ.get("CANAAN_DOTS_DIR", os.path.join(os.path.dirname(BASE_DIR), "dots")), "data", "opendots.sqlite"))
from canaan_agent_board_data import DOTS_API


class _NoEmbeddings:
    def available(self):
        return False


class CanaanDotsMemoryBridge:
    """
    Bidirectional memory and knowledge synchronizer between Canaan and OpenDots.
    """

    _lock = threading.Lock()

    def __init__(self, canaan_db_path=CANAAN_DB, dots_db_path=DOTS_DB, poll_interval=120.0, enabled=False):
        self.canaan_db_path = canaan_db_path
        self.dots_db_path = dots_db_path
        self.poll_interval = poll_interval
        self.enabled = enabled is True
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _sync_allowed(self):
        if not self.enabled or not os.path.exists(self.canaan_db_path):
            return False
        try:
            with sqlite3.connect(self.canaan_db_path) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_settings'").fetchone():
                    return True  # 旧数据库没有无痕开关；仍需显式 enabled=True
                row = db.execute("SELECT value FROM memory_settings WHERE key='incognito'").fetchone()
                return not (row and row[0] == "1")
        except sqlite3.Error:
            return False

    def _open_store(self):
        from canaan_memory import MemoryStore
        # 独立连接经过同一写入闸门；桥接不额外加载嵌入模型或迁移私人档案。
        return MemoryStore(path=self.canaan_db_path, embedder=_NoEmbeddings(), migrate=False)

    # ------------------------------------------------------------------------
    # 1. Canaan Facts -> OpenDots Memories (Archival Sync)
    # ------------------------------------------------------------------------

    def sync_canaan_to_dots(self) -> int:
        """
        Reads active, confirmed facts from Canaan memory.db and syncs them
        to OpenDots memories repository with deduplication.
        """
        if not self._sync_allowed():
            return 0

        synced_count = 0
        store = None
        try:
            store = self._open_store()
            revision = store.privacy_revision
            cur_c = store.db.cursor()
            cur_c.execute(
                "SELECT id, text, category, pinned FROM facts WHERE superseded_by IS NULL ORDER BY id ASC"
            )
            canaan_facts = cur_c.fetchall()

            if not canaan_facts:
                return 0

            existing_texts = set()
            if os.path.exists(self.dots_db_path):
                conn_d = sqlite3.connect(self.dots_db_path)
                cur_d = conn_d.cursor()
                cur_d.execute("SELECT text FROM memories")
                existing_texts = {row[0].strip() for row in cur_d.fetchall() if row[0]}
                conn_d.close()

            from canaan_privacy_shield import PrivacyShield

            for fid, text, cat, pinned in canaan_facts:
                if not text:
                    continue
                clean_text, _ = PrivacyShield.mask_text(text.strip())
                if clean_text in existing_texts:
                    continue
                if store.incognito or store.privacy_revision != revision:
                    break

                # If testing with custom DB path, write to sqlite directly for test isolation
                if self.dots_db_path != DOTS_DB:
                    success = self._push_to_dots_sqlite(clean_text)
                else:
                    success = self._push_to_dots_api(clean_text)
                    if not success and os.path.exists(self.dots_db_path):
                        success = self._push_to_dots_sqlite(clean_text)

                if success:
                    existing_texts.add(clean_text)
                    synced_count += 1

            if synced_count > 0:
                logger.info(f"[MemoryBridge] Synced {synced_count} facts from Canaan to OpenDots.")
        except Exception as e:
            logger.error("[MemoryBridge] Error syncing Canaan to Dots: %s", type(e).__name__)
        finally:
            if store is not None:
                store.db.close()

        return synced_count

    def _push_to_dots_api(self, text: str) -> bool:
        try:
            payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{DOTS_API}/api/memories",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                return resp.status in (200, 201)
        except Exception:
            return False

    def _push_to_dots_sqlite(self, text: str):
        try:
            import uuid
            conn = sqlite3.connect(self.dots_db_path)
            conn.execute(
                "INSERT INTO memories (id, text, createdAt) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), text, int(time.time() * 1000))
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning("[MemoryBridge] SQLite direct insert fallback error: %s", type(e).__name__)
            return False

    # ------------------------------------------------------------------------
    # 2. OpenDots Tasks & Artifacts -> Canaan Episodes (Knowledge Ingestion)
    # ------------------------------------------------------------------------

    def sync_dots_to_canaan(self) -> int:
        """
        Reads completed research tasks and summaries from OpenDots
        and ingests them into Canaan memory episodes.
        """
        if not self._sync_allowed() or not os.path.exists(self.dots_db_path):
            return 0

        synced_count = 0
        store = None
        try:
            store = self._open_store()
            revision = store.privacy_revision
            conn_d = sqlite3.connect(self.dots_db_path)
            cur_d = conn_d.cursor()
            cur_d.execute(
                "SELECT id, prompt, status, updatedAt FROM tasks"
            )
            completed_tasks = cur_d.fetchall()
            conn_d.close()

            if not completed_tasks:
                return 0

            now = int(time.time())
            for tid, prompt, status, updated_at in completed_tasks:
                origin = f"agent:dots:{tid}"
                task_revision = store.observe_origin(origin, revision)
                if task_revision is None or status not in ('completed', 'succeeded', 'done'):
                    continue
                updated = float(updated_at or 0)
                if updated > 1e11:  # Dots 的毫秒时间戳。
                    updated /= 1000
                if store.resume_after and updated <= store.resume_after:
                    continue
                summary = f"[OpenDots 虚拟同事完成任务]: {prompt}"
                from canaan_privacy_shield import PrivacyShield
                summary = PrivacyShield.mask_text(summary)[0]
                started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated or now))
                ended = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                if store.add_episode(summary, started, ended, expected_revision=task_revision, origin=origin):
                    synced_count += 1

            if synced_count > 0:
                logger.info(f"[MemoryBridge] Ingested {synced_count} completed tasks from OpenDots into Canaan.")
        except Exception as e:
            logger.error("[MemoryBridge] Error syncing Dots to Canaan: %s", type(e).__name__)
        finally:
            if store is not None:
                store.db.close()

        return synced_count

    # ------------------------------------------------------------------------
    # 3. Unified Cross-Agent Memory Query
    # ------------------------------------------------------------------------

    def query_unified(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Performs hybrid query across both Canaan and OpenDots memories.
        """
        if not self._sync_allowed():
            return []
        results = []

        # 1. Canaan Store query
        try:
            if self.canaan_db_path == CANAAN_DB:
                from canaan_memory import get_store
                store = get_store()
            else:
                from canaan_memory import MemoryStore
                store = MemoryStore(path=self.canaan_db_path, migrate=False)
            search_out = store.search(query, k_facts=limit, k_episodes=2)
            if isinstance(search_out, tuple):
                facts, episodes = search_out
            elif isinstance(search_out, dict):
                facts = search_out.get("facts", [])
                episodes = search_out.get("episodes", [])
            else:
                facts, episodes = [], []

            for f in facts:
                results.append({
                    "source": "canaan_fact",
                    "text": f.get("text", "") if isinstance(f, dict) else str(f),
                    "score": 1.0
                })
            for ep in episodes:
                results.append({
                    "source": "canaan_episode",
                    "text": ep.get("summary", "") if isinstance(ep, dict) else str(ep),
                    "score": 0.8
                })
        except Exception as e:
            logger.warning(f"[MemoryBridge] Canaan search error: {e}")

        # 2. OpenDots memories query
        if os.path.exists(self.dots_db_path):
            try:
                conn_d = sqlite3.connect(self.dots_db_path)
                cur_d = conn_d.cursor()
                keywords = [k for k in query.split() if len(k) > 1]
                if keywords:
                    like_clauses = " OR ".join(["text LIKE ?" for _ in keywords])
                    params = [f"%{k}%" for k in keywords]
                    cur_d.execute(f"SELECT text FROM memories WHERE {like_clauses} LIMIT 3", params)
                    for row in cur_d.fetchall():
                        results.append({
                            "source": "opendots_knowledge",
                            "text": row[0],
                            "score": 0.9
                        })
                conn_d.close()
            except Exception as e:
                logger.warning(f"[MemoryBridge] Dots search error: {e}")

        return results[:limit]

    # ------------------------------------------------------------------------
    # 4. Background Sync Daemon
    # ------------------------------------------------------------------------

    def start_background_sync(self):
        """Starts periodic synchronization daemon."""
        if not self._sync_allowed():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="MemoryBridgeWorker")
            self._thread.start()
            logger.info("[MemoryBridge] Started background sync daemon.")

    def stop_background_sync(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_loop(self):
        self.sync_canaan_to_dots()
        self.sync_dots_to_canaan()

        while not self._stop_event.is_set():
            if self._stop_event.wait(self.poll_interval):
                break
            self.sync_canaan_to_dots()
            self.sync_dots_to_canaan()


_bridge_instance: Optional[CanaanDotsMemoryBridge] = None

def get_memory_bridge() -> CanaanDotsMemoryBridge:
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = CanaanDotsMemoryBridge()
    return _bridge_instance
