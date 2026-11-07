"""Local device grants and opaque request bindings, without transport or execution.

Only a trusted local caller may construct this adapter and its dependencies.  The
SQLite store is local authorization state, not a remotely callable interface.
Issued handles live only in this binder instance; they are not serializable
authorization and must be revalidated immediately before a later side effect.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time


class DeviceBindingError(PermissionError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeviceTextRequest:
    protocol_version: int
    request_id: str
    text: str

    def __post_init__(self):
        if type(self.protocol_version) is not int or self.protocol_version not in (1, 2):
            raise ValueError("Unsupported device request protocol version")
        if (not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128
                or self.request_id != self.request_id.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in self.request_id)):
            raise ValueError("request_id must be 1–128 characters without surrounding whitespace or control characters")
        if (not isinstance(self.text, str) or not 1 <= len(self.text) <= 16000
                or not self.text.strip() or "\x00" in self.text):
            raise ValueError("text must contain 1–16000 characters of nonempty text without NUL")

    @classmethod
    def from_body(cls, body):
        if not isinstance(body, dict) or set(body) != {"protocol_version", "request_id", "text"}:
            raise ValueError("Device request body must contain only protocol_version, request_id and text")
        return cls(body["protocol_version"], body["request_id"], body["text"])


@dataclass(frozen=True)
class BoundDeviceTextRequest:
    """Server-internal request facts. Constructing this value grants no authority."""
    handle: str = field(repr=False)
    request_id: str
    text: str = field(repr=False)
    client_id: str
    binding_id: str
    grant_revision: int
    profile_key: str
    session_id: str
    execution_host_id: str


@dataclass(frozen=True)
class _Grant:
    client_id: str
    binding_id: str
    revision: int
    execution_host_id: str
    profile_key: str
    session_id: str
    token_json: str
    active: bool


class DeviceBindingStore:
    """Persistent local grants; never stores a token or the raw request text."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._closed = False
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS device_bindings (
                client_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL UNIQUE,
                revision INTEGER NOT NULL, execution_host_id TEXT NOT NULL,
                profile_key TEXT NOT NULL, session_id TEXT NOT NULL,
                token_json TEXT NOT NULL, active INTEGER NOT NULL,
                updated_at REAL NOT NULL)""")

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def get(self, client_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM device_bindings WHERE client_id=?", (client_id,)).fetchone()
            if row is None:
                return None
            return _Grant(*(row[key] for key in ("client_id", "binding_id", "revision", "execution_host_id",
                                                "profile_key", "session_id", "token_json")), bool(row["active"]))

    def replace(self, client_id, *, execution_host_id, profile_key, session_id, token_json):
        """Called only after the binder has authenticated a local owner."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                old = self.get(client_id)
                grant = _Grant(client_id, secrets.token_urlsafe(24), old.revision + 1 if old else 1,
                               execution_host_id, profile_key, session_id, token_json, True)
                self._db.execute("""INSERT INTO device_bindings VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(client_id) DO UPDATE SET binding_id=excluded.binding_id,
                    revision=excluded.revision, execution_host_id=excluded.execution_host_id,
                    profile_key=excluded.profile_key, session_id=excluded.session_id,
                    token_json=excluded.token_json, active=excluded.active, updated_at=excluded.updated_at""",
                    (grant.client_id, grant.binding_id, grant.revision, grant.execution_host_id,
                     grant.profile_key, grant.session_id, grant.token_json, 1, time.time()))
                self._db.commit()
                return grant
            except BaseException:
                self._db.rollback()
                raise

    def revoke(self, client_id, *, expected_binding_id=None):
        with self._lock, self._db:
            sql = "UPDATE device_bindings SET active=0,revision=revision+1,updated_at=? WHERE client_id=? AND active=1"
            args = [time.time(), client_id]
            if expected_binding_id is not None:
                sql += " AND binding_id=?"
                args.append(expected_binding_id)
            return self._db.execute(sql, args).rowcount == 1


@dataclass(frozen=True)
class _ContextSnapshot:
    binding: object = field(repr=False)
    profile_key: str
    session_id: str
    token_json: str


@dataclass(frozen=True)
class _Issued:
    request: BoundDeviceTextRequest
    context: _ContextSnapshot


class DeviceTextRequestBinder:
    """Bind an authenticated device to a locally approved host and user session.

    `context_port` wraps the real UserContext.sync/valid, never its display JSON.
    `lookup_active_client`, if supplied, is a trusted local registry adapter that
    returns a fresh public client record (or None), not a device-supplied value.
    No method approves or executes the natural-language request itself.
    """
    def __init__(self, clients, context_port, binding_store, *, execution_host_id,
                 lookup_active_client=None, max_handles=1024):
        if (not isinstance(execution_host_id, str) or not execution_host_id.strip()
                or execution_host_id != execution_host_id.strip() or len(execution_host_id) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in execution_host_id)):
            raise ValueError("A stable local execution_host_id is required")
        if not callable(getattr(context_port, "sync", None)) or not callable(getattr(context_port, "valid", None)):
            raise ValueError("context_port must provide UserContext.sync and UserContext.valid")
        if type(max_handles) is not int or max_handles < 1:
            raise ValueError("max_handles must be a positive integer")
        self._clients, self._context, self._store = clients, context_port, binding_store
        self._host = execution_host_id
        self._lookup = lookup_active_client or self._registry_lookup
        self._max_handles = max_handles
        self._issued = OrderedDict()
        self._pending_handles = set()
        self._lock = threading.RLock()
        self._terminal_validator = None

    def configure_terminal_validator(self, validator):
        """Install one trusted construction-time reader, never request data."""
        if not callable(validator):
            raise TypeError("A trusted terminal target validator is required")
        with self._lock:
            if self._terminal_validator is validator:
                return
            if self._terminal_validator is not None or self._issued:
                raise RuntimeError("The terminal target policy cannot change after configuration or handle issuance")
            self._terminal_validator = validator

    def _registry_lookup(self, client_id):
        matches = [record for record in self._clients.list() if record.get("id") == client_id]
        return matches[0] if len(matches) == 1 else None

    def _active_client(self, client_id):
        if not isinstance(client_id, str) or not client_id or client_id == "owner":
            raise DeviceBindingError("client_unavailable", "An active external client with propose permission is required")
        record = self._lookup(client_id)
        caps = record.get("caps") if isinstance(record, dict) else None
        if (not isinstance(record, dict) or record.get("id") != client_id or record.get("revoked_at")
                or record.get("owner") or not isinstance(caps, (list, tuple, set, frozenset)) or "propose" not in caps):
            raise DeviceBindingError("client_unavailable", "The device client was revoked or no longer has propose permission")
        return record

    def require_local_owner(self, owner_token, *, capability="confirm"):
        principal = self._clients.authenticate(owner_token) if isinstance(owner_token, str) else None
        if principal is None or not principal.owner or not principal.can(capability):
            raise DeviceBindingError("local_owner_required", "A freshly authenticated local owner is required")

    def _device_principal(self, token):
        principal = self._clients.authenticate(token) if isinstance(token, str) else None
        if principal is None:
            raise DeviceBindingError("invalid_token", "The device token is invalid or revoked")
        if principal.owner:
            raise DeviceBindingError("external_client_required", "A device request must use its own external client token")
        if not principal.can("propose"):
            raise DeviceBindingError("client_unavailable", "The device client requires propose permission")
        self._active_client(principal.id)
        return principal

    def _snapshot(self, binding):
        try:
            key, session = binding.profile_key, binding.session_id
            token = binding.token
            if (not isinstance(key, str) or not key.strip()
                    or not isinstance(session, str) or not session.strip()
                    or getattr(binding.store, "incognito", False)):
                raise DeviceBindingError("context_unavailable", "A persistent local user session is required")
            if not isinstance(token, (tuple, list)) or len(token) != 4 or token[0] != key:
                raise DeviceBindingError("context_unavailable", "The local context token is unavailable")
            token_json = json.dumps(token, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            return _ContextSnapshot(binding, key, session, token_json)
        except (AttributeError, TypeError, ValueError) as exc:
            raise DeviceBindingError("context_unavailable", "The local user context cannot be bound") from exc

    @staticmethod
    def _same_context(first, second):
        return (first.profile_key, first.session_id, first.token_json) == (second.profile_key, second.session_id, second.token_json)

    def _capture_context(self):
        first = self._snapshot(self._context.sync())
        if not self._context.valid(first.binding):
            raise DeviceBindingError("context_changed", "The local user context has changed")
        second = self._snapshot(self._context.sync())
        if (not self._same_context(first, second) or not self._context.valid(first.binding)
                or not self._context.valid(second.binding)):
            raise DeviceBindingError("context_changed", "The local user session changed while binding")
        return first

    def _recheck_context(self, snapshot):
        current = self._capture_context()
        if not self._same_context(snapshot, current) or not self._context.valid(snapshot.binding):
            raise DeviceBindingError("context_changed", "The original local user session is no longer valid")
        return current

    def _grant(self, client_id, context, *, binding_id=None, revision=None):
        grant = self._store.get(client_id)
        if grant is None or not grant.active:
            raise DeviceBindingError("grant_missing", "The device has no active local binding grant")
        if grant.execution_host_id != self._host:
            raise DeviceBindingError("execution_host_changed", "This grant belongs to a different execution host")
        if (binding_id is not None and grant.binding_id != binding_id
                or revision is not None and grant.revision != revision):
            raise DeviceBindingError("grant_changed", "The local binding grant has been replaced")
        if (grant.profile_key, grant.session_id, grant.token_json) != (context.profile_key, context.session_id, context.token_json):
            raise DeviceBindingError("context_changed", "The device grant belongs to a different local user session")
        return grant

    def register_local_binding(self, owner_token, client_id):
        with self._lock:
            self.require_local_owner(owner_token, capability="admin")
            self._active_client(client_id)
            context = self._capture_context()
            self.require_local_owner(owner_token, capability="admin")
            self._active_client(client_id)
            grant = self._store.replace(client_id, execution_host_id=self._host,
                                        profile_key=context.profile_key, session_id=context.session_id,
                                        token_json=context.token_json)
            try:
                self._recheck_context(context)
                self.require_local_owner(owner_token, capability="admin")
                self._active_client(client_id)
                self._grant(client_id, context, binding_id=grant.binding_id, revision=grant.revision)
            except BaseException:
                self._store.revoke(client_id, expected_binding_id=grant.binding_id)
                raise
            return grant.binding_id

    def revoke_local_binding(self, owner_token, client_id):
        with self._lock:
            self.require_local_owner(owner_token, capability="admin")
            return self._store.revoke(client_id)

    def bind_body(self, token, body):
        request = DeviceTextRequest.from_body(body)
        return self.bind(token, request.request_id, request.text)

    def bind(self, token, request_id, text, *, preserve_pending=False):
        """Broker reservations reject overflow and cannot be FIFO-evicted."""
        return self._resolve_request(token, request_id, text, issue_handle=True,
                                     preserve_pending=preserve_pending)

    def inspect_request(self, token, request_id, text):
        """Authenticate request facts for polling/dedupe without allocating a handle.

        The returned empty handle is deliberately not executable authority. This
        keeps repeated reads from evicting a pending task's valid issued handle.
        """
        return self._resolve_request(token, request_id, text, issue_handle=False)

    def _resolve_request(self, token, request_id, text, *, issue_handle, preserve_pending=False):
        request = DeviceTextRequest(1, request_id, text)
        with self._lock:
            principal = self._device_principal(token)
            context = self._capture_context()
            grant = self._grant(principal.id, context)
            self._recheck_context(context)
            self._active_client(principal.id)
            self._grant(principal.id, context, binding_id=grant.binding_id, revision=grant.revision)
            if issue_handle and len(self._issued) >= self._max_handles:
                evictable = next((value for value in self._issued if value not in self._pending_handles), None)
                if preserve_pending or evictable is None:
                    raise DeviceBindingError("handle_capacity", "Pending request handle capacity is full")
                self._issued.pop(evictable)
            handle = secrets.token_urlsafe(32) if issue_handle else ""
            issued = BoundDeviceTextRequest(handle, request.request_id, request.text, principal.id,
                                           grant.binding_id, grant.revision, context.profile_key,
                                           context.session_id, self._host)
            if issue_handle:
                self._issued[handle] = _Issued(issued, context)
                if preserve_pending:
                    self._pending_handles.add(handle)
            return issued

    def validate_bound(self, handle):
        with self._lock:
            if not isinstance(handle, str) or handle not in self._issued:
                raise DeviceBindingError("invalid_handle", "The request handle was not issued by this binder instance")
            issued = self._issued[handle]
            request = issued.request
            try:
                self._active_client(request.client_id)
                self._recheck_context(issued.context)
                self._grant(request.client_id, issued.context, binding_id=request.binding_id, revision=request.grant_revision)
                self._active_client(request.client_id)
                self._grant(request.client_id, issued.context, binding_id=request.binding_id, revision=request.grant_revision)
            except BaseException:
                self._issued.pop(handle, None)
                self._pending_handles.discard(handle)
                raise
            return request

    def release_bound(self, handle):
        """Forget one process-local handle; never changes a persistent grant."""
        with self._lock:
            if isinstance(handle, str):
                self._issued.pop(handle, None)
                self._pending_handles.discard(handle)

    def validate_terminal_transition(self, handle, before_token, after_token, after_session_id, *, expected_target):
        """Validate one approved terminal control; never renew the old grant.

        The caller must first validate the server-typed control receipt. Only
        this final check permits an expected new/absent session or incognito.
        Ordinary bind/inspect/validate remain subject to their original grant.
        """
        with self._lock:
            issued = self._issued.get(handle) if isinstance(handle, str) else None
            if issued is None:
                raise DeviceBindingError("invalid_handle", "The original request handle is unavailable")
            validator = self._terminal_validator
            if validator is None:
                raise DeviceBindingError("invalid_transition", "No trusted terminal target reader was configured")
            before = json.dumps(before_token, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            after = json.dumps(after_token, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if (not isinstance(before_token, (tuple, list)) or len(before_token) != 4
                    or not isinstance(after_token, (tuple, list)) or len(after_token) != 4
                    or before != issued.context.token_json
                    or (after == before and after_session_id == issued.context.session_id)):
                raise DeviceBindingError("invalid_transition", "The terminal transition does not match the original context")
            request = issued.request
            def check():
                self._active_client(request.client_id)
                grant = self._store.get(request.client_id)
                if (grant is None or not grant.active or grant.binding_id != request.binding_id
                        or grant.revision != request.grant_revision or grant.execution_host_id != self._host
                        or grant.profile_key != issued.context.profile_key or grant.session_id != issued.context.session_id
                        or grant.token_json != issued.context.token_json):
                    raise DeviceBindingError("grant_changed", "The original device grant changed during the terminal operation")
                current = self._context.sync()
                token = json.dumps(current.token, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                if (not self._context.valid(current) or token != after or current.session_id != after_session_id
                        or not isinstance(current.profile_key, str) or not current.profile_key
                        or current.token[0] != current.profile_key):
                    raise DeviceBindingError("context_changed", "The actual terminal context differs from its completion receipt")
                try:
                    if validator(self._context, current, expected_target) is not True:
                        raise ValueError("The trusted target reader did not authorize the actual state")
                except BaseException as error:
                    raise DeviceBindingError("context_changed", "The actual terminal state differs from its approved target") from error
            check()
            check()
