"""团团本机接口的客户端令牌与权限。

- 每个外部客户端（Claw/AutoClaw、MCP 等）单独配对、单独撤销；登记文件 data/api_clients.json 只存令牌的 SHA-256。
- 本机看板用的主人令牌每次启动在内存里随机生成，不落盘（也可由 CANAAN_API_TOKEN 提供）。
- 能力：status 查看状态 / read 读取指定资料 / propose 提出任务 / ingest 提交采集事件 /
        progress 报告自己的进度（VS Code、编程助手等，只能报状态和标题，不能执行任何动作）/
        confirm 确认执行 / train 授权训练与人工标注 / admin 改设置、导入文件、管理客户端。
- 外部客户端永远拿不到 confirm、train、admin，这三项只属于本机界面里的主人。
- read 必须写明范围：哪些来源的哪些会话、语音转写、知识库里哪些文档或项目文件夹、团团总结哪些来源；没写就是不能读。
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

from canaan_filelock import file_lock

CAPS = ("status", "read", "propose", "ingest", "progress", "confirm", "train", "admin")
EXTERNAL_CAPS = frozenset({"status", "read", "propose", "ingest", "progress"})
DEFAULT_EXTERNAL_CAPS = ("status", "propose")
from canaan_message_priority import SOURCES as _SOURCE_NAMES
SOURCES = tuple(_SOURCE_NAMES)   # 跟着版本走：公开版只有 feishu / claw
LEGACY_NAME = "旧共享令牌（只能看状态，请重新配对）"


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_scope(value, label):
    if not isinstance(value, dict) or set(value) - {"sources", "conversations"}:
        raise ValueError(f"{label} 范围须为 {{sources, conversations}}")
    sources, conversations = value.get("sources"), value.get("conversations")
    if not isinstance(sources, list) or not sources or any(s not in SOURCES for s in sources):
        raise ValueError(f"{label} 须列出来源：" + " / ".join(SOURCES))
    if not isinstance(conversations, list) or not conversations or not all(isinstance(c, str) and c.strip() for c in conversations):
        raise ValueError(f"{label} 须列出会话名；整个来源都开放须明确写 [\"*\"]")
    return {"sources": sorted(set(sources)), "conversations": sorted({c.strip() for c in conversations})}


def validate_resources(resources):
    if resources is None:
        return {}
    if not isinstance(resources, dict) or set(resources) - {"messages", "voice", "knowledge", "summary"}:
        raise ValueError("resources 只能包含 messages / voice / knowledge / summary")
    out = {}
    for key in ("messages", "voice"):
        if resources.get(key) is not None:
            out[key] = _validate_scope(resources[key], key)
    # knowledge：{"documents": [文档路径或项目文件夹]}；summary：{"sources": [来源]}。
    # 旧登记里的 true 仍按“整块开放”读，但新配对必须写明范围
    if resources.get("knowledge") is not None:
        out["knowledge"] = _validate_list(resources["knowledge"], "knowledge", "documents")
    if resources.get("summary") is not None:
        out["summary"] = _validate_list(resources["summary"], "summary", "sources")
        if any(s not in SOURCES for s in out["summary"]["sources"]):
            raise ValueError("summary 的来源只能是 " + " / ".join(SOURCES))
    return out


def _validate_list(value, label, field):
    if isinstance(value, bool):
        raise ValueError(f"{label} 须写明范围，如 {{\"{field}\": [...]}}，不再接受整块 true / false")
    if not isinstance(value, dict) or set(value) != {field}:
        raise ValueError(f"{label} 范围须为 {{{field}}}")
    items = value[field]
    if not isinstance(items, list) or not items or not all(isinstance(i, str) and i.strip() for i in items):
        raise ValueError(f"{label}.{field} 须列出至少一项")
    return {field: sorted({i.strip() for i in items})}


def _norm_path(path):
    return str(path).replace("\\", "/").rstrip("/").lower()


class Principal:
    def __init__(self, client_id, name, caps, resources=None, owner=False):
        self.id, self.name, self.owner = client_id, name, owner
        self.caps = frozenset(CAPS) if owner else frozenset(caps) & EXTERNAL_CAPS
        self.resources = deepcopy(resources) if resources else {}

    def can(self, cap):
        return cap in self.caps

    def _scope_allows(self, key, source, conversation):
        if self.owner:
            return True
        scope = self.resources.get(key) if self.can("read") else None
        if not scope or source not in scope["sources"]:
            return False
        return "*" in scope["conversations"] or conversation in scope["conversations"]

    def allows_message(self, source, conversation):
        return self._scope_allows("messages", source, conversation)

    def allows_voice(self, source, conversation):
        return self._scope_allows("voice", source, conversation)

    def allows(self, key):
        """knowledge / summary 这类整块资料。"""
        return self.owner or (self.can("read") and bool(self.resources.get(key)))

    def summary_sources(self):
        """能让团团总结的来源；旧登记里的 summary: true 视为全部来源。"""
        if self.owner:
            return list(SOURCES)
        scope = self.resources.get("summary") if self.can("read") else None
        if scope is True:
            return list(SOURCES)
        return list(scope["sources"]) if isinstance(scope, dict) else []

    def allows_document(self, source):
        """知识库按文档或项目文件夹授权；引用和检索结果都按这个过滤。"""
        if self.owner:
            return True
        scope = self.resources.get("knowledge") if self.can("read") else None
        if scope is True:
            return True
        if not isinstance(scope, dict):
            return False
        doc = _norm_path(source)
        return any(doc == d or doc.startswith(d + "/") for d in map(_norm_path, scope["documents"]))

    def message_sources(self):
        if self.owner:
            return list(SOURCES)
        scope = self.resources.get("messages") if self.can("read") else None
        return scope["sources"] if scope else []


class ClientRegistry:
    def __init__(self, directory, owner_token):
        self.path = Path(directory) / "api_clients.json"
        self.owner_token = owner_token
        self.lock = threading.RLock()
        self._stamp = None
        self._clients = []

    def _load(self, *, fresh=False):
        with self.lock:
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                self._clients, self._stamp = [], None
                return self._clients
            stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if fresh or stamp != self._stamp:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._clients = data.get("clients", []) if isinstance(data, dict) else []
                self._stamp = stamp
            return self._clients

    def _save(self, clients):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"version": 1, "clients": clients}, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(self.path)
            self._stamp = None

    def authenticate(self, token):
        if not isinstance(token, str) or not token:
            return None
        if self.owner_token and hmac.compare_digest(token, self.owner_token):
            return Principal("owner", "本机主人", CAPS, owner=True)
        digest = _hash(token)
        for client in self._load():
            if hmac.compare_digest(client.get("token_sha256", ""), digest) and not client.get("revoked_at"):
                return Principal(client["id"], client.get("name", ""), client.get("caps", []), client.get("resources"))
        return None

    def pair(self, name, caps=None, resources=None):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("客户端名称须为 1–60 字")
        caps = list(DEFAULT_EXTERNAL_CAPS if caps is None else caps)
        if not isinstance(caps, list) or any(c not in CAPS for c in caps):
            raise ValueError("未知权限；可选 status / read / propose / ingest / progress")
        forbidden = set(caps) - EXTERNAL_CAPS
        if forbidden:
            raise ValueError("外部客户端不能获得确认执行、授权训练或管理权限：" + "、".join(sorted(forbidden)))
        resources = validate_resources(resources)
        if "read" in caps and not resources:
            raise ValueError("read 权限须明确选定可读的资料范围")
        if resources and "read" not in caps:
            raise ValueError("选了资料范围就要同时给 read 权限")
        token = secrets.token_urlsafe(32)
        record = {"id": uuid.uuid4().hex[:12], "name": name.strip(), "token_sha256": _hash(token),
                  "caps": sorted(set(caps), key=CAPS.index), "resources": resources,
                  "created_at": time.time(), "revoked_at": None}
        with self.lock, file_lock(self.path):
            self._save(self._load(fresh=True) + [record])
        return self.public(record), token

    def revoke(self, client_id):
        with self.lock, file_lock(self.path):
            clients = [dict(c) for c in self._load(fresh=True)]
            hit = next((c for c in clients if c["id"] == client_id), None)
            if hit is None:
                raise KeyError("客户端不存在")
            if not hit.get("revoked_at"):
                hit["revoked_at"] = time.time()
                self._save(clients)
            return self.public(hit)

    def list(self):
        return [self.public(c) for c in self._load()]

    @staticmethod
    def public(record):
        return deepcopy({k: v for k, v in record.items() if k != "token_sha256"})

    def migrate_legacy(self, token_file):
        """旧版所有客户端共用 data/integration_token.txt 的全权限令牌。
        平滑迁移：登记成只能看状态的客户端，原客户端照常连得上，但要重新配对才有其他权限。"""
        token_file = Path(token_file)
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if len(token) < 24:
            return None
        digest = _hash(token)
        with self.lock, file_lock(self.path):
            clients = self._load(fresh=True)
            if any(c.get("token_sha256") == digest for c in clients):
                return None
            record = {"id": "legacy-shared", "name": LEGACY_NAME, "token_sha256": digest, "caps": ["status"],
                      "resources": {}, "created_at": time.time(), "revoked_at": None, "legacy": True}
            self._save([c for c in clients if c.get("id") != "legacy-shared"] + [record])
            return self.public(record)
