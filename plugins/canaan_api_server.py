"""Authenticated loopback API and local control page; no message-to-command bridge."""
import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import argparse
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from canaan_message_priority import SOURCES
from canaan_device_gateway import DeviceGatewayError, authenticate_credential

ROOT = Path(__file__).resolve().parent.parent


def load_token(directory=None):
    """主人令牌（本机看板用，全部权限）：每次启动在内存里随机生成，不落盘；也可由 CANAAN_API_TOKEN 提供。
    旧版 data/integration_token.txt 不再是主人令牌，启动时降级登记为只能看状态的客户端。"""
    token = os.environ.get("CANAAN_API_TOKEN", "").strip()
    if token:
        if len(token) < 24:
            raise ValueError("CANAAN_API_TOKEN 至少需要 24 字符")
        return token
    return secrets.token_urlsafe(32)


# 每个接口需要的能力；没列出的接口一律只给主人
ROUTE_CAPS = {
    ("GET", "/api/actions"): "status", ("GET", "/api/status"): "status", ("GET", "/api/tasks"): "status", ("GET", "/api/tasks/*"): "status",
    ("GET", "/api/messages"): "read", ("GET", "/api/messages/inbox"): "read",
    ("GET", "/api/messages/status"): "status", ("GET", "/api/voice/jobs"): "read", ("GET", "/api/voice/jobs/*"): "read",
    ("GET", "/api/voice/status"): "status", ("GET", "/api/messages/summary"): "status",
    ("GET", "/api/knowledge"): "read", ("GET", "/api/agents"): "status", ("GET", "/api/connections"): "status",
    ("GET", "/api/agents/*/*"): "propose",
    ("POST", "/api/tasks"): "propose", ("POST", "/api/tasks/*/cancel"): "propose", ("POST", "/api/tasks/*/confirm"): "confirm",
    ("POST", "/api/messages/summary/run"): "propose", ("POST", "/api/messages/summary"): "propose",
    ("POST", "/api/messages"): "ingest", ("POST", "/api/voice/jobs"): "ingest",
    ("POST", "/api/voice/jobs/*/review"): "train", ("POST", "/api/messages/priority"): "train", ("POST", "/api/training/export"): "train",
    ("POST", "/api/knowledge/search"): "read", ("POST", "/api/knowledge/answer"): "read", ("POST", "/api/feedback"): "propose",
    ("POST", "/api/progress"): "progress",
    ("POST", "/api/device-text/requests"): "propose",
    ("GET", "/api/device-text/capabilities"): "propose",
    ("GET", "/api/device-text/tasks/*/attachments"): "propose",
    ("GET", "/api/device-text/tasks/*/attachments/*"): "propose",
    ("GET", "/api/device-text/tasks/*/attachments/*/*"): "propose",
    ("GET", "/api/device-text/review/*/attachments"): "confirm",
    ("GET", "/api/device-text/review/*/attachments/*"): "confirm",
    ("GET", "/api/device-text/review/*/attachments/*/*"): "confirm",
    ("GET", "/api/device-text/tasks/*/result"): "propose",
    ("GET", "/api/device-text/tasks/*/result/*/*"): "propose",
    ("GET", "/api/device-text/review/*/result"): "confirm",
    ("GET", "/api/device-text/review/*/result/*/*"): "confirm",
    ("GET", "/api/device-text/tasks/*"): "propose",
    ("POST", "/api/device-text/tasks/*/answers"): "propose",
    ("POST", "/api/device-text/tasks/*/cancel"): "propose",
    ("GET", "/api/device-text/review"): "confirm",
    ("GET", "/api/device-text/review/*"): "confirm",
    ("POST", "/api/device-text/review/*/confirm"): "confirm",
    ("POST", "/api/device-text/review/*/cancel"): "confirm",
}
AGENT_ACTIONS = ("openmuse_task", "opendots_task", "multiagent_task", "opendots_memory")


def _git_commit(root=ROOT):
    """不调子进程：直接读 .git 里的 HEAD；安装包里没有 .git 时读 build_info.json（构建时写入）。"""
    try:
        info = json.loads((root / "build_info.json").read_text(encoding="utf-8"))
        return str(info.get("commit", ""))[:12] or "unknown"
    except (OSError, ValueError):
        pass
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[5:]
        path = root / ".git" / ref
        if path.exists():
            return path.read_text(encoding="utf-8").strip()[:12]
        for line in (root / ".git" / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:12]
    except OSError:
        pass
    return "unknown"


def policy_version():
    """接口权限表 + 客户端能力表的指纹：权限规则一改，这个值就变。"""
    import hashlib
    from canaan_api_clients import CAPS, EXTERNAL_CAPS
    blob = json.dumps({"routes": sorted(f"{m} {p} {c}" for (m, p), c in ROUTE_CAPS.items()),
                       "caps": list(CAPS), "external": sorted(EXTERNAL_CAPS)}, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


_HEALTH = None
_LOADED_AT = time.time()


def runtime_source_digest():
    import hashlib
    sources = ('core/canaan_desk_os_hub.py', 'core/canaan_asr.py', 'core/canaan_asr_prefetch.py',
               'core/canaan_voice_capture.py', 'core/canaan_intent.py', 'core/canaan_pending_bargein.py',
               'core/canaan_conversation_time.py', 'core/canaan_memory.py', 'core/canaan_retention.py',
               'core/canaan_pending_voice_worker.py', 'core/canaan_chatapp_executor.py', 'core/canaan_chatapp_uia.py',
               'core/canaan_user_context.py', 'core/canaan_context_store.py', 'core/canaan_people.py',
               'core/canaan_learning.py', 'core/canaan_companion.py', 'core/canaan_clock_math.py', 'core/tuantuan_voice_engine.py',
               'core/canaan_config.py', 'core/canaan_action_framework/actions/chatapp_action.py',
               'plugins/canaan_api_server.py', 'shells/canaan_control.html', 'shells/canaan_device_tasks.js',
               'core/canaan_proactive.py', 'core/canaan_device_binding.py', 'core/canaan_device_requests.py',
               'core/canaan_device_turn.py', 'core/canaan_device_payloads.py', 'core/canaan_device_context.py',
               'core/canaan_execution_guard.py', 'plugins/canaan_device_gateway.py',
               'core/canaan_asr_loader.py', 'core/canaan_public_help.py', 'core/canaan_persona.py')
    digest = hashlib.sha256()
    for source in sources:
        path = ROOT / source
        if path.exists():
            digest.update(source.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


# Snapshot at import, not at the first health request after files may have changed.
_RUNTIME_BUILD_ID = runtime_source_digest()


def health_info(readiness=None):
    """公开版本标识和语音就绪布尔值；独立接口服务不代表桌面语音可用。"""
    global _HEALTH
    if _HEALTH is None:
        import hashlib
        import canaan_config as cfg
        config_digest = hashlib.sha256((ROOT / "core" / "canaan_config.py").read_bytes()).hexdigest()[:12]
        _HEALTH = {"service": "canaan-integrations", "version": 2, "edition": cfg.EDITION, "build": _git_commit(),
                   "policy_version": policy_version(), "config": config_digest,
                   "models": {"intent": cfg.INTENT_COMPACT_MODEL, "chat": cfg.CHAT_MODEL, "summary": cfg.MESSAGE_SUMMARY_MODEL}}
        _HEALTH.update(runtime_build_id=_RUNTIME_BUILD_ID, loaded_at=_LOADED_AT)
    fields = ("attached", "ui_ready", "asr_ready", "wake_ready", "listening_enabled", "microphone_ready")
    voice = dict.fromkeys((*fields, "game_mode", "ready"), False)
    if readiness is not None:
        try:
            snapshot = readiness()
            if isinstance(snapshot, dict):
                for key in voice:
                    voice[key] = snapshot.get(key) is True
                voice["ready"] = all(voice[key] for key in fields) and snapshot.get("game_mode") is False
        except Exception:
            pass  # 不把异常、路径、录音内容等写进无需认证的健康接口。
    return {**_HEALTH, "voice_runtime": voice}


def required_cap(method, path):
    if (method, path) in ROUTE_CAPS:
        return ROUTE_CAPS[(method, path)]
    parts = path.split("/")
    for (m, pattern), cap in ROUTE_CAPS.items():
        p = pattern.split("/")
        if m == method and "*" in p and len(p) == len(parts) and all(a == "*" or a == b for a, b in zip(p, parts)):
            return cap
    return "admin"


class IntegrationRuntime:
    def __init__(self, directory=None, token=None, tasks=None, messages=None, knowledge=None, learning=None, generate=None, notify=None, transcriber=None, memory=None, readiness=None, people=None, context=None, switch_user=None, clear_context=None, context_store=None, device_factory=None):
        from canaan_integration_tasks import IntegrationTasks
        from canaan_message_center import MessageCenter
        self.directory = Path(directory or ROOT / "data")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.token = token or load_token(self.directory)
        from canaan_api_clients import ClientRegistry
        self.clients = ClientRegistry(self.directory, self.token)
        self.clients.migrate_legacy(self.directory / "integration_token.txt")
        from canaan_local_lock import acquire_file_lock
        self._lease = acquire_file_lock(self.directory / "integration.lock")
        self.tasks = tasks or IntegrationTasks(self.directory / "integration_tasks.db")
        self.settings_path = self.directory / "message_settings.json"
        settings = json.loads(self.settings_path.read_text(encoding="utf-8")) if self.settings_path.exists() else {}
        self.messages = messages or MessageCenter(self.directory / "messages.db", settings, self._send)
        self.messages.update_settings(self.messages.settings)
        from canaan_voice_messages import VoiceMessages
        from canaan_feishu_service import FeishuService
        self.voices = VoiceMessages(self.messages, transcriber)
        self.messages.audio_ingest = self.voices.submit
        self.feishu = FeishuService(self.directory, self.messages)
        self.notify = notify
        self.readiness = readiness
        self._people = people
        self.context_provider = context
        self.switch_user = switch_user
        self.clear_context = clear_context
        self._context_store = context_store
        self._knowledge, self._learning, self._memory = knowledge, learning, memory
        self.generate = generate or self._generate
        self.server = None
        self.summary_lock = threading.RLock()
        self.summary_stop = threading.Event()
        self.summary_settings_path = self.directory / "message_summary_settings.json"
        self.summary_settings = {"enabled": False, "interval_seconds": 900,
                                 "sources": [s for s in ("chatapp", "imapp") if s in SOURCES] or ["feishu"]}
        if self.summary_settings_path.exists():
            self.summary_settings = self.validated_summary_settings(json.loads(self.summary_settings_path.read_text(encoding="utf-8")))
        previous = self.tasks.list("daily_briefing")
        self.summary_last_check = previous[0]["created_at"] if previous else 0
        self.summary_error = ""
        self.device_gateway = None
        self.device_initialization_error = None
        if device_factory is not None:
            try:
                gateway = device_factory(self)
                if not callable(getattr(gateway, "route", None)) or not callable(getattr(gateway, "close", None)):
                    raise TypeError("Invalid device gateway")
                self.device_gateway = gateway
            except Exception as ex:
                # Existing integrations remain usable; never substitute the
                # action-only task dispatcher for an unavailable text gateway.
                self.device_initialization_error = type(ex).__name__

    @property
    def knowledge(self):
        if self._knowledge is None:
            from canaan_knowledge import get_knowledge
            self._knowledge = get_knowledge()
        return self._knowledge

    @property
    def memory(self):
        if self._memory is None:
            from canaan_memory import get_store
            self._memory = get_store()
        return self._memory

    def _memory_route(self, method, path, query, body):
        """长期记忆管理：只给本机主人（这些路由不在 ROUTE_CAPS 里，外部客户端一律 403）。"""
        parts = path.split("/")
        if method == "GET" and path == "/api/memory":
            store = self.people.active_store() or self.memory
            if store is self.memory:
                return self.memory.overview(query.get("q", [""])[0])
            needle = query.get('q', [''])[0]
            return {'incognito': store.incognito, 'facts': store.matching(needle), 'episodes': []}
        if method == "POST" and path == "/api/memory/incognito":
            self.memory.incognito = bool(body.get("on"))
            return {"incognito": self.memory.incognito}
        if path == "/api/memory/people" or path.startswith("/api/memory/people/"):
            return self._people_route(method, parts, body)
        if method == "POST" and len(parts) == 6 and parts[3] in ("facts", "episodes"):
            store = self.people.active_store() or self.memory
            try:
                item_id = int(parts[4])
            except ValueError:
                raise ValueError("记忆编号须为整数")
            op = parts[5]
            if parts[3] == "episodes" and op == "delete":
                if not hasattr(store, 'delete_episode') or not store.delete_episode(item_id):
                    raise KeyError("这条记忆不存在")
                return {"deleted": item_id}
            if parts[3] == "facts" and op == "delete":
                if not store.delete_fact(item_id):
                    raise KeyError("这条记忆不存在")
                return {"deleted": item_id}
            if parts[3] == "facts" and op == "update":
                return {"id": store.edit_fact(item_id, body.get("text"))}
            if parts[3] == "facts" and op == "pin":
                if not store.set_pinned(item_id, bool(body.get("pinned"))):
                    raise KeyError("这条记忆不存在")
                return {"id": item_id, "pinned": bool(body.get("pinned"))}
        raise KeyError("接口不存在")

    @property
    def people(self):
        if getattr(self, "_people", None) is None:
            from canaan_people import PeopleDB
            self._people = PeopleDB(str(self.directory / 'people.db'), owner_store=self.memory)
        return self._people

    def _profile_key(self):
        return self.people.snapshot()['profile_key']

    def context_view(self):
        if self.context_provider is not None:
            return self.context_provider()
        if self._context_store is None:
            from canaan_context_store import ContextStore
            self._context_store = ContextStore(str(self.directory / 'context.db'))
        snapshot = self.people.snapshot()
        return {**self._context_store.view(snapshot['profile_key']), 'active': snapshot}

    def _switch_person(self, name=None):
        if self.switch_user is not None:
            self.switch_user(name)
        elif name is None:
            self.people.switch_owner()
        else:
            self.people.switch_to(name)
        return self.people.snapshot()

    def _people_route(self, method, parts, body=None):
        """多人记忆（canaan_people）：主人在控制中心看所有人的、删人、删条、切回主人。"""
        # /api/memory/people  /api/memory/people/owner  /api/memory/people/<id>/delete  /api/memory/people/<id>/facts/<fid>/delete
        if method == "GET" and len(parts) == 4:
            return self.people.overview()
        if method == "POST" and len(parts) == 5 and parts[4] == "owner":
            self._switch_person()
            return {"active": None}
        if method == 'POST' and len(parts) == 5 and parts[4] == 'switch':
            name = (body or {}).get('name')
            if not isinstance(name, str) or not name.strip():
                raise ValueError('请填写使用者姓名')
            self._switch_person(name.strip())
            return {'active': self.people.active(), 'identity_revision': self.people.snapshot()['revision']}
        try:
            ids = [int(parts[4])] + ([int(parts[6])] if len(parts) == 8 else [])
        except (ValueError, IndexError):
            raise ValueError("编号须为整数")
        if method == "POST" and len(parts) == 6 and parts[5] == "delete":
            was_active = (self.people.active() or {}).get('id') == ids[0]
            if not self.people.delete_person(ids[0]):
                raise KeyError("这个人不存在")
            if was_active:
                self._switch_person()
            if self._context_store is None:
                self.context_view()
            if self._context_store is not None:
                self._context_store.delete_profile(f'person:{ids[0]}')
            self.learning.delete_profile(f'person:{ids[0]}')
            return {"deleted": ids[0]}
        if method == "POST" and len(parts) == 8 and parts[5] == "facts" and parts[7] == "delete":
            if not self.people.delete_fact(*ids):
                raise KeyError("这条记忆不存在")
            return {"deleted": ids[1]}
        raise KeyError("接口不存在")

    @property
    def learning(self):
        if self._learning is None:
            from canaan_learning import FeedbackLearning
            self._learning = FeedbackLearning(self.directory / 'learning.db')
        return self._learning

    def _generate(self, question, context):
        from canaan_companion import CompanionChat
        stream = CompanionChat().stream(question, context=context)
        answer = "".join(stream.iter_text())
        if stream.error:
            raise RuntimeError("本地模型试答失败")
        return answer

    def _learning_generator(self, profile_key):
        def generate(question, context):
            from canaan_learning import profile_scope
            with profile_scope(profile_key):
                return self.generate(question, context)
        return generate

    def _send(self, target, text):
        from canaan_task_manager import GLOBAL_TASK_MANAGER
        from canaan_action_framework import ActionRegistry
        # Automated forwarding never overrides the existing sensitive-content gate.
        with GLOBAL_TASK_MANAGER.os_exec_lock:
            # 自动转发是主人在看板里自己打开的设置（默认关），视为事先确认；语音里的发聊天软件仍然每条都要确认。
            # 涉及钱、账号、验证码的内容和以前一样不自动转发（不算确认，会停在待确认）
            try:
                from canaan_action_framework.actions.chatapp_action import ChatAppSendAction
            except ImportError:   # 公开版不带聊天软件发送
                return {"success": False, "error_code": "UNSUPPORTED", "message": "这个版本不含聊天软件转发"}
            pre_confirmed = not ChatAppSendAction.RISKY_CONTENT_RE.search(text or "")
            return ActionRegistry.dispatch("chatapp_send", {"contact": target, "message": text}, source="rule",
                                           confirmed=pre_confirmed).to_dict()

    def validated_summary_settings(self, values):
        if not isinstance(values, dict) or set(values) - {"enabled", "interval_seconds", "sources"}:
            raise ValueError("未知总结设置")
        settings = {**self.summary_settings, **values}
        if not isinstance(settings["enabled"], bool):
            raise ValueError("enabled 须为布尔值")
        if type(settings["interval_seconds"]) is not int or not 60 <= settings["interval_seconds"] <= 86400:
            raise ValueError("总结间隔须为 60–86400 秒的整数")
        sources = settings["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 4 or any(s not in SOURCES for s in sources) or len(set(sources)) != len(sources):
            raise ValueError("sources 须为 " + " / ".join(SOURCES) + " 中不重复的来源")
        return settings.copy()

    def summary_watermark(self, sources, now):
        with self.messages.lock:
            return self.messages.db.execute("SELECT coalesce(max(m.id),0)+coalesce(sum(r.revision),0) FROM messages m LEFT JOIN message_revisions r ON m.id=r.message_id WHERE source IN (%s) AND timestamp>=?" % ",".join("?" for _ in sources), (*sources, now - 86400)).fetchone()[0]

    def run_summary(self, sources=None, origin="manual", now=None):
        with self.summary_lock:
            sources = self.validated_summary_settings({"sources": self.summary_settings["sources"] if sources is None else sources})["sources"]
            jobs = self.tasks.list("daily_briefing")
            running = next((job for job in jobs if job["state"] in ("queued", "running")), None)
            if running:
                return running
            now = time.time() if now is None else now
            job = self.tasks.submit("daily_briefing", {"sources": sources, "origin": origin,
                                    "message_watermark": self.summary_watermark(sources, now)})
            self.summary_last_check = now
            self.summary_error = ""
            return job

    def summary_tick(self, now=None):
        with self.summary_lock:
            now = time.time() if now is None else now
            settings = self.summary_settings
            if not settings["enabled"] or now < self.summary_last_check + settings["interval_seconds"]:
                return None
            try:  # 主人 3 分钟内说过话：定时总结往后推，不在对话中途占显卡（手动总结不受影响）
                import canaan_gpu_priority
                if canaan_gpu_priority.user_recently_active() or canaan_gpu_priority.demo_mode():
                    return None
            except ImportError:
                pass
            jobs = self.tasks.list("daily_briefing")
            if jobs and jobs[0]["state"] in ("queued", "running"):
                return None
            watermark = self.summary_watermark(settings["sources"], now)
            previous = jobs[0]["param"] if jobs and isinstance(jobs[0]["param"], dict) else {}
            if jobs and jobs[0]["state"] == "succeeded" and previous.get("sources") == settings["sources"] and previous.get("message_watermark") == watermark and now - jobs[0]["updated_at"] < 86400:
                self.summary_last_check = now
                return None
            return self.run_summary(settings["sources"], "scheduled", now)

    def summary_snapshot(self):
        with self.summary_lock:
            jobs = self.tasks.list("daily_briefing")
            last_success = next((job for job in jobs if job["state"] == "succeeded"), None)
            result = last_success["result"].get("detail") if last_success else None
            path = self.directory / "daily_briefing_latest.json"
            if path.exists():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(saved, dict) and saved.get("success") and (not result or saved.get("generated_at", "") > result.get("generated_at", "")):
                        result = saved
                except (OSError, ValueError):
                    pass
            return {"settings": self.summary_settings.copy(), "task": jobs[0] if jobs else None,
                    "result": result, "scheduler_error": self.summary_error,
                    "next_check_at": self.summary_last_check + self.summary_settings["interval_seconds"] if self.summary_settings["enabled"] else None}

    # ---- 按客户端权限裁剪返回内容（主人不裁剪） ----
    @staticmethod
    def _task_view(task, p):
        if p.owner or task.get("client_id") == p.id:
            return task
        return {k: task.get(k) for k in ("id", "action", "state", "created_at", "updated_at")} | {"redacted": True}

    def _read_messages(self, after, limit, p):
        if p.owner:
            return self.messages.list_messages(after, limit)
        out = []
        for _ in range(50):  # 跳过无权读取的会话，保证客户端用最后一条 id 能继续往后翻
            batch = self.messages.list_messages(after, 200)
            out += [m for m in batch if p.allows_message(m["source"], m["conversation"])]
            if len(out) >= limit or len(batch) < 200:
                break
            after = batch[-1]["id"]
        return out[:limit]

    def _inbox(self, source, priority, search, limit, p):
        if p.owner:
            return self.messages.inbox(source, priority, search, limit)
        sources = [source] if source else p.message_sources()
        messages = []
        for s in sources:
            if s in p.message_sources():
                found = self.messages.inbox(s, priority, search, 200)["messages"]
                messages += [m for m in found if p.allows_message(m["source"], m["conversation"])]
        messages.sort(key=lambda m: (int(m["importance"]["priority"][1]), -m["importance"]["score"], -m["timestamp"]))
        counts = {pr: sum(m["importance"]["priority"] == pr for m in messages) for pr in ("P0", "P1", "P2")}
        return {"messages": messages[:limit], "counts": counts, "matched": len(messages), "window_hours": 24, "scoped": True}

    def _summary_view(self, snapshot, p):
        if p.owner:
            return snapshot
        result = snapshot.get("result")
        covered = (result or {}).get("sources") or list(SOURCES)  # 说不清总结了哪些来源时按全部来源算
        if p.allows("summary") and set(covered) <= set(p.summary_sources()):
            task = snapshot.get("task")
            return {**snapshot, "task": self._task_view(task, p) if task else None}
        task = snapshot.get("task")
        return {**snapshot, "result": None, "task": self._task_view(task, p) if task else None, "content_withheld": True}

    def _client_summary_sources(self, p, sources):
        allowed = p.summary_sources()
        if not allowed:
            raise PermissionError("该客户端没有被授权让团团总结消息")
        sources = allowed if sources is None else sources
        if not isinstance(sources, list) or not sources or any(s not in allowed for s in sources):
            raise PermissionError("只能总结被授权的来源：" + "、".join(allowed))
        return self.validated_summary_settings({"sources": sources})["sources"]

    def _owns_agent_task(self, p, provider, agent_task_id):
        """只接受已确认、成功提交任务的结构化 provider/task_id；不从正文猜归属。"""
        if provider not in ("muse", "dots") or not agent_task_id:
            return False
        for task in self.tasks.list():
            if (task.get("client_id") != p.id or not task.get("approved")
                    or task.get("state") != "succeeded" or task["action"] not in AGENT_ACTIONS):
                continue
            result = task.get("result") or {}
            if not result.get("success"):
                continue
            detail = result.get("detail") or {}
            if not isinstance(detail, dict):
                continue
            if task["action"] == "multiagent_task":
                binding = detail.get(provider + "_task") or {}
                if isinstance(binding, dict) and binding.get("success") and binding.get("task_id") == agent_task_id:
                    return True
            elif (task["action"] == {"muse": "openmuse_task", "dots": "opendots_task"}[provider]
                  and detail.get("provider") == provider and detail.get("task_id") == agent_task_id):
                return True
        return False

    PROGRESS_KINDS = ("progress", "waiting", "done", "failed", "info")

    def _progress(self, body, p):
        """VS Code、编程助手等报告自己的进度 → 统一提示中心。只收状态和标题，不能执行任何动作；
        外部客户端的来源一律用它配对时的名字（不能冒充别的应用），也不能发“聊天消息”类提示。"""
        kind = body.get("kind")
        if kind not in self.PROGRESS_KINDS:
            raise ValueError("kind 须为 progress / waiting / done / failed / info")
        title = body.get("title", "")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            raise ValueError("title 须为 1–80 字")
        progress = body.get("progress")
        if progress is not None and (type(progress) not in (int, float) or not 0 <= progress <= 100):
            raise ValueError("progress 须为 0–100")
        key = body.get("key")
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 64):
            raise ValueError("key 须为 1–64 字符")
        source = (body.get("source") or "本机") if p.owner else (p.name or "外部客户端")
        import canaan_notice_center
        nid = canaan_notice_center.default().post(str(source)[:24], kind, title=title.strip(), progress=progress,
                                                  key=f"api:{p.id}:{key}" if key else None, openable=False)
        return {"accepted": nid is not None, "id": nid}

    def _need(self, p, key):
        if not p.allows(key):
            raise PermissionError(f"该客户端没有被授权读取 {key}")

    def route(self, method, path, query, body, principal=None, *, credential=None):
        if path == "/api/device-text" or path.startswith("/api/device-text/"):
            # This branch must precede the legacy local-owner default below.
            if authenticate_credential(self.clients, credential) is None:
                raise DeviceGatewayError(401, "authentication_required", "请先验证设备身份。")
            if query:
                raise DeviceGatewayError(400, "invalid_query", "请通过请求正文提交这项任务。")
            gateway = getattr(self, "device_gateway", None)
            if gateway is None:
                raise DeviceGatewayError(503, "device_gateway_unavailable", "电脑端文字入口尚未就绪。")
            return gateway.route(method, path, body, token=credential)
        from canaan_api_clients import CAPS, Principal
        p = principal or Principal("owner", "本机主人", CAPS, owner=True)
        cap = required_cap(method, path)
        if not p.can(cap):
            raise PermissionError(f"该客户端没有 {cap} 权限")
        if path == "/api/memory" or path.startswith("/api/memory/"):
            return self._memory_route(method, path, query, body)
        if method == 'POST' and path == '/api/context/clear':
            if self.clear_context is not None:
                return self.clear_context()
            self.context_view()
            if self._context_store is None:
                raise RuntimeError('会话保存暂不可用')
            result = self._context_store.clear_history(self._profile_key())
            self.people.invalidate_context()
            return result
        if method == "GET":
            if path == '/api/context':
                return self.context_view()
            if path == "/api/actions":
                return self.tasks.registry.list_actions()
            if path == "/api/clients":
                return self.clients.list()
            if path == "/api/status":  # 统一状态事件的当前快照：只有状态、动作名、步骤号
                import canaan_events
                return canaan_events.default_tracker().view()
            if path == "/api/tasks":
                return [self._task_view(t, p) for t in self.tasks.list()]
            if path.startswith("/api/tasks/"):
                return self._task_view(self.tasks.get(path.rsplit("/", 1)[-1]), p)
            if path == "/api/messages":
                return self._read_messages(int(query.get("after", [0])[0]), int(query.get("limit", [100])[0]), p)
            if path == "/api/messages/inbox":
                return self._inbox(query.get("source", [None])[0], query.get("priority", [None])[0],
                                   query.get("search", [""])[0], int(query.get("limit", [50])[0]), p)
            if path == "/api/messages/status" and not p.owner:
                result = self.messages.status()
                return {k: v for k, v in result.items() if k != "settings"}
            if path == "/api/messages/status":
                from canaan_connections import listener_status
                result = self.messages.status()
                listener = listener_status(self.directory)
                state = listener["state"]
                result["sources"]["feishu"] = listener.get("error") or {"connected": "长连接已连通", "connecting": "正在连接", "stopped": "监听已停止", "offline": "监听当前离线", "not_configured": "尚未配置监听", "error": "监听状态文件无法读取"}.get(state, state)
                result["sources"].setdefault("claw", "已收到客户端事件" if "claw" in result["by_source"] else "等待客户端提交事件")
                return {**result, "settings": self.messages.settings.copy(), "desktop_available": self.notify is not None}
            if path == "/api/voice/jobs":
                jobs = self.voices.list(int(query.get("limit", [50])[0]))
                return [j for j in jobs if p.allows_voice(j.get("source"), j.get("conversation"))]
            if path == "/api/voice/status":
                status = self.voices.status()
                return status if p.owner else {k: v for k, v in status.items() if k != "settings"}
            if path.startswith("/api/voice/jobs/"):
                job = self.voices.get(path.rsplit("/", 1)[-1])
                if not p.allows_voice(job.get("source"), job.get("conversation")):
                    raise PermissionError("该客户端没有被授权读取这条语音")
                return job
            if path == "/api/messages/summary":
                return self._summary_view(self.summary_snapshot(), p)
            if path == "/api/knowledge":
                self._need(p, "knowledge")
                return [d for d in self.knowledge.documents() if p.allows_document(d["source"])]
            if path == "/api/feedback":
                return self.learning.list(profile_key=self._profile_key())
            if path == "/api/agents":
                from canaan_agent_board_data import fetch_muse, fetch_dots, counts
                agents = {"muse": fetch_muse(), "dots": fetch_dots()}
                if p.owner:
                    return agents
                # 外部客户端只看在线与否和各状态数量，不看任务标题、进度说明和结果
                return {k: {"online": v["online"], "counts": counts(v["tasks"]), "at": v["at"], "redacted": True} for k, v in agents.items()}
            if path == "/api/connections":
                from canaan_connections import snapshot
                result = snapshot(self.directory)
                if not p.owner:  # 外部客户端只看接入方式，不看本机路径和采集配置
                    return {"sources": result["sources"], "api": result["api"], "agents": result["agents"],
                            "claw": {k: result["claw"][k] for k in ("mode", "protocols", "confirmation")},
                            "feishu": {"listener": {"state": result["feishu"]["listener"].get("state")}}}
                result["feishu"]["management"] = self.feishu.snapshot()
                result["feishu"]["text_only"] = False
                return result
            if path == "/api/notices/settings":       # 不在 ROUTE_CAPS 里：只有主人
                import canaan_config
                import canaan_notice_center
                import canaan_toast_listener
                return {**canaan_notice_center.load_settings(), "toast": canaan_toast_listener.status(),
                        "retention_days": int(getattr(canaan_config, "CONVERSATION_RETENTION_DAYS", 14))}
            if path.startswith("/api/agents/"):
                from canaan_agent_board_data import agent_detail
                parts = path.split("/")
                if len(parts) == 5:
                    if not p.owner and not self._owns_agent_task(p, parts[3], parts[4]):
                        raise PermissionError("只能读自己提出、经主人确认的智能体任务")
                    return agent_detail(parts[3], parts[4])
        if method == "POST":
            if path == "/api/clients":
                record, token = self.clients.pair(body.get("name"), body.get("caps"), body.get("resources"))
                return {**record, "token": token, "note": "令牌只显示这一次，请填进该客户端的配置"}
            if path.startswith("/api/clients/") and path.endswith("/revoke"):
                return self.clients.revoke(path.split("/")[-2])
            if path == "/api/voice/jobs":
                return self.voices.submit(body)
            if path.startswith("/api/voice/jobs/") and path.endswith("/review"):
                return self.voices.review(path.split("/")[-2], body)
            if path.startswith("/api/voice/jobs/") and path.endswith("/retry"):
                return self.voices.retry(path.split("/")[-2])
            if path == "/api/connections":
                from canaan_connections import save
                return save(self.directory, body)
            if path == "/api/messages/summary/run":
                if p.owner:
                    return self.run_summary(body.get("sources"))
                # 外部客户端触发总结和提出其他任务一样：只能总结被授权的来源，等主人在本机确认后才跑
                sources = self._client_summary_sources(p, body.get("sources"))
                return self.tasks.submit("daily_briefing", {"sources": sources, "origin": f"client:{p.id}"},
                                         client_id=p.id, require_confirmation=True)
            if path == "/api/messages/summary/settings":
                with self.summary_lock:
                    settings = self.validated_summary_settings(body)
                    temp = self.summary_settings_path.with_suffix(".tmp")
                    temp.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
                    temp.replace(self.summary_settings_path)
                    self.summary_settings = settings
                return settings
            if path == "/api/messages/summary":
                self._need(p, "summary")
                sources = body.get("sources") if p.owner else self._client_summary_sources(p, body.get("sources"))
                if not p.owner:
                    return self.tasks.submit("daily_briefing", {"sources": sources, "origin": f"client:{p.id}"},
                                             client_id=p.id, require_confirmation=True)
                from message_tools.canaan_daily_briefing import generate_executive_briefing
                return generate_executive_briefing(use_local_fallback=True, sources=sources,
                                                  message_db=self.directory / "messages.db")
            if path == "/api/tasks":
                if p.owner:
                    return self.tasks.submit(body.get("action"), body.get("param"), request_id=body.get("request_id"))
                action_name = body.get("action")
                if not isinstance(action_name, str):
                    raise ValueError("action 须为动作名字符串")
                act = self.tasks.registry.get_action(action_name)
                if act and act.name == "daily_briefing":
                    param = body.get("param") or {}
                    if not isinstance(param, dict):
                        raise ValueError("总结参数须为对象")
                    sources = self._client_summary_sources(p, param.get("sources"))
                    return self.tasks.submit("daily_briefing", {"sources": sources, "origin": f"client:{p.id}"},
                                             client_id=p.id, require_confirmation=True, request_id=body.get("request_id"))
                if act and act.name == "knowledge_query":
                    raise PermissionError("请使用按文档授权的 /api/knowledge/search 或 /api/knowledge/answer")
                # 外部客户端提出的任务一律等主人在本机看板确认，只读动作也一样（结果可能含隐私）
                return self.tasks.submit(body.get("action"), body.get("param"), client_id=p.id,
                                         require_confirmation=True, request_id=body.get("request_id"))
            if path.startswith("/api/tasks/"):
                parts = path.split("/")
                if len(parts) == 5 and parts[-1] in ("confirm", "cancel"):
                    if not p.owner and self.tasks.get(parts[-2]).get("client_id") != p.id:
                        raise PermissionError("只能取消自己提出的任务")
                    return getattr(self.tasks, parts[-1])(parts[-2])
            if path == "/api/messages":
                return self.messages.ingest(body)
            if path == "/api/messages/priority":
                return self.messages.review_priority(body.get("id"), body.get("priority"), body.get("reason"))
            if path == "/api/training/export":
                from canaan_training_data import export_dataset, export_voice_dataset
                if body.get("kind") == "voice":
                    return export_voice_dataset(self.voices, self.directory, body.get("consent"))
                if body.get("kind", "priority") != "priority":
                    raise ValueError("未知训练数据类型")
                return export_dataset(self.messages, self.directory, body.get("consent"))
            if path == "/api/messages/settings":
                with self.messages.lock:
                    settings = self.messages.validated_settings(body)
                    temp = self.settings_path.with_suffix(".tmp")
                    temp.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
                    temp.replace(self.settings_path)
                    self.messages.update_settings(settings)
                return settings
            if path == "/api/messages/retry":
                if not isinstance(body.get("id"), int):
                    raise ValueError("id 须为整数")
                return {"queued": self.messages.retry(body["id"])}
            if path == "/api/knowledge/import":
                if not isinstance(body.get("path"), str):
                    raise ValueError("须提供文档路径")
                return self.knowledge.import_file(body["path"])
            if path == "/api/knowledge/search":
                self._need(p, "knowledge")
                if p.owner:
                    return self.knowledge.search(body.get("question"), body.get("limit", 5))
                return self.knowledge.search(body.get("question"), body.get("limit", 5), allowed=p.allows_document)
            if path == "/api/knowledge/answer":
                self._need(p, "knowledge")
                if p.owner:
                    return self.knowledge.answer(body.get("question"))
                return self.knowledge.answer(body.get("question"), allowed=p.allows_document)
            if path == "/api/progress":
                return self._progress(body, p)
            if path == "/api/notices/settings":       # 以下三个只有主人
                import canaan_notice_center
                return canaan_notice_center.save_settings(canaan_notice_center.validated_settings(body))
            if path == "/api/notices/toast_access":
                import canaan_toast_listener
                return {"status": canaan_toast_listener.request_access()}
            if path == "/api/retention/run":
                import canaan_retention
                return canaan_retention.run()
            if path == "/api/diagnostics/export":        # 只有主人：生成诊断包（不含原话 / 消息 / 记忆），打开所在文件夹
                import canaan_diagnostics
                zpath, names = canaan_diagnostics.build()
                if body.get("open", True):
                    try:
                        os.startfile(os.path.dirname(zpath))
                    except Exception:
                        pass
                return {"path": zpath, "files": names}
            if path == "/api/feedback":
                return self.learning.record(body.get("kind"), body.get("question", ""), body.get("correction"), profile_key=self._profile_key())
            if path == "/api/feedback/evaluate":
                profile_key = self._profile_key()
                return self.learning.validate_and_activate(body.get("id"), body.get("cases"), self._learning_generator(profile_key), profile_key=profile_key)
            if path == "/api/feedback/disable":
                self.learning.deactivate(body.get("id"), profile_key=self._profile_key())
                return {"disabled": True}
        raise KeyError("接口不存在")

    def start(self, port=None, capture=True):
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, value, html=False, content_type=None):
                data = value if html or content_type else json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type or ("text/html; charset=utf-8" if html else "application/json; charset=utf-8"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except ConnectionError:
                    pass  # 页面关闭不改变已保存的任务结果，也不尝试再次回复。

            def handle_request(self):
                parsed = urllib.parse.urlsplit(self.path)
                if self.headers.get("Host") not in (f"127.0.0.1:{runtime.port}", f"localhost:{runtime.port}"):
                    return self.reply(403, {"error": "invalid_host"})
                origin = self.headers.get("Origin")
                if origin and origin not in (f"http://127.0.0.1:{runtime.port}", f"http://localhost:{runtime.port}"):
                    return self.reply(403, {"error": "invalid_origin"})
                if self.command == "GET" and parsed.path == "/":
                    return self.reply(200, (ROOT / "shells" / "canaan_control.html").read_bytes(), html=True)
                if self.command == "GET" and parsed.path == "/canaan_device_tasks.js":
                    asset = ROOT / "shells" / "canaan_device_tasks.js"
                    if not asset.is_file():
                        return self.reply(404, {"error": "asset_unavailable"})
                    return self.reply(200, asset.read_bytes(),
                                      content_type="application/javascript; charset=utf-8")
                if self.command == "GET" and parsed.path == "/health":
                    return self.reply(200, health_info(runtime.readiness))
                auth = self.headers.get("Authorization", "")
                principal = authenticate_credential(runtime.clients, auth[7:]) if auth.startswith("Bearer ") else None
                if principal is None:
                    return self.reply(401, {"error": "authentication_required"})
                try:
                    body = {}
                    if self.command == "POST":
                        length = int(self.headers.get("Content-Length", 0))
                        if not 0 < length <= 65536:
                            return self.reply(413, {"error": "body_size"})
                        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                            return self.reply(415, {"error": "JSON required"})
                        self.connection.settimeout(10)
                        body = json.loads(self.rfile.read(length))
                        if not isinstance(body, dict):
                            raise ValueError("请求体须为对象")
                    if parsed.path == "/api/device-text" or parsed.path.startswith("/api/device-text/"):
                        if parsed.query:
                            raise DeviceGatewayError(400, "invalid_query", "请通过请求正文提交这项任务。")
                        if self.command == 'GET':
                            # HTTPMessage.get returns only the first duplicate.
                            # Inspect every field before routing any device GET.
                            get_all = getattr(self.headers, 'get_all', None)
                            if callable(get_all):
                                lengths = get_all('Content-Length', [])
                                encodings = get_all('Transfer-Encoding', [])
                            else:
                                # Dict headers are used by finite fake handlers.
                                length = self.headers.get('Content-Length')
                                encoding = self.headers.get('Transfer-Encoding')
                                lengths = [] if length is None else [length]
                                encodings = [] if encoding is None else [encoding]
                            if encodings or len(lengths) > 1 or lengths and lengths != ['0']:
                                raise DeviceGatewayError(400, 'invalid_body', '读取结果时请求正文必须为空。')
                        result = runtime.route(self.command, parsed.path, urllib.parse.parse_qs(parsed.query), body,
                                               principal, credential=auth[7:])
                    else:
                        result = runtime.route(self.command, parsed.path, urllib.parse.parse_qs(parsed.query), body, principal)
                    self.reply(200, result)
                except DeviceGatewayError as ex:
                    self.reply(ex.status, ex.public())
                except PermissionError as ex:
                    self.reply(403, {"error": "forbidden", "detail": str(ex)[:200], "client": principal.name})
                except (ValueError, TypeError, FileNotFoundError) as ex:
                    self.reply(400, {"error": str(ex)[:300]})
                except KeyError as ex:
                    self.reply(404, {"error": str(ex)})
                except Exception as ex:
                    self.reply(500, {"error": f"服务执行失败：{type(ex).__name__}"})

            do_GET = handle_request
            do_POST = handle_request

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", int(port if port is not None else os.environ.get("CANAAN_API_PORT", "8789"))), Handler)
        except Exception:
            self.stop()
            raise
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, name="canaan-api", daemon=True).start()
        self.voices.start()
        if capture:
            from canaan_message_center import VisibleMessageSource
            from canaan_message_connectors import configured_collector
            import canaan_config
            # 读聊天软件 / 聊天软件 窗口里可见消息只在自用版里；公开版不碰第三方客户端的界面
            visible = VisibleMessageSource() if canaan_config.EDITION == "personal" else None
            try:
                self.messages.start(visible, collector=configured_collector(self.directory, self.messages))
            except Exception:
                self.stop()
                raise
        def summarize():
            # 新消息提示每秒查一次（原来跟着 5 秒一轮，最坏晚 5 秒才弹）；飞书监听检查和定时总结仍是 5 秒一轮
            tick = 0
            while not self.summary_stop.wait(1):
                tick += 1
                try:
                    self.messages.notify_once(self.notify)
                except Exception as ex:
                    self.messages.mark_health("runtime", error=f"后台检查失败：{type(ex).__name__}")
                if tick % 5:
                    continue
                try:
                    if capture:
                        self.feishu.tick()
                except Exception as ex:
                    self.messages.mark_health("feishu", error=f"监听检查失败：{type(ex).__name__}")
                try:
                    self.summary_tick()
                except Exception as ex:
                    self.summary_error = f"自动总结检查失败：{type(ex).__name__}"
        self.summary_thread = threading.Thread(target=summarize, name="canaan-summary-scheduler", daemon=True)
        self.summary_thread.start()
        return self

    def dashboard_url(self):
        return f"http://127.0.0.1:{self.port}/#token={self.token}"

    def stop(self):
        gateway = getattr(self, "device_gateway", None)
        if gateway is not None:
            try:
                gateway.close()
            except Exception as ex:
                self.device_initialization_error = type(ex).__name__
        self.summary_stop.set()
        if getattr(self, "summary_thread", None):
            self.summary_thread.join(timeout=3)
        self.messages.stop()
        self.voices.stop()
        self.feishu.stop()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self._lease.close()


def manage_clients(args):
    """不用启动服务也能配对 / 撤销 / 查看客户端；正在运行的服务下一次请求就会读到变化。"""
    from canaan_api_clients import ClientRegistry
    registry = ClientRegistry(ROOT / "data", owner_token=None)
    if args.pair:
        resources = json.loads(args.resources) if args.resources else None
        caps = args.caps.split(",") if args.caps else None
        record, token = registry.pair(args.pair, caps, resources)
        print(json.dumps(record, ensure_ascii=False, indent=1))
        print(f"\n令牌（只显示这一次，填进该客户端配置的 CANAAN_API_TOKEN）：\n{token}")
    elif args.revoke:
        print(json.dumps(registry.revoke(args.revoke), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(registry.list(), ensure_ascii=False, indent=1))


def main():
    parser = argparse.ArgumentParser(description="团团本地 API、消息监听与控制中心")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-capture", action="store_true")
    parser.add_argument("--pair", metavar="名称", help="给一个外部客户端单独发令牌，默认权限 status,propose")
    parser.add_argument("--caps", help="逗号分隔：status,read,propose,ingest,progress（外部客户端不能有 confirm/train/admin）；VS Code、编程助手只要 progress")
    parser.add_argument("--resources", help='read 的范围，JSON，如 {"messages":{"sources":["feishu"],"conversations":["工作群"]},"summary":{"sources":["feishu"]},"knowledge":{"documents":["D:/项目/课题"]}}')
    parser.add_argument("--revoke", metavar="客户端ID")
    parser.add_argument("--list-clients", action="store_true")
    args = parser.parse_args()
    if args.pair or args.revoke or args.list_clients:
        return manage_clients(args)
    runtime = IntegrationRuntime().start(args.port, capture=not args.no_capture)
    print(f"团团接口已启动：http://127.0.0.1:{runtime.port}\n本机看板（含本次启动的主人令牌，勿转发）：{runtime.dashboard_url()}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        runtime.stop()


if __name__ == "__main__":
    main()
