"""Authenticated text-task routes over the existing loopback API.

This object opens no listener. Raw bearer credentials are authenticated again;
neither a Principal argument nor request-body identity grants device authority.
"""
from pathlib import Path
from contextlib import closing
import sqlite3
import threading
import uuid

from canaan_device_binding import DeviceBindingError, DeviceBindingStore, DeviceTextRequestBinder
from canaan_device_requests import DeviceRequestError, DeviceTextRequestBroker
from canaan_execution_guard import execution_guard


PREFIX = "/api/device-text"


def authenticate_credential(clients, token):
    """Malformed credentials or registry data never become local-owner access."""
    try:
        return clients.authenticate(token)
    except (TypeError, ValueError):
        return None


class DeviceGatewayError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def public(self):
        result = {"error": self.code, "message": self.message}
        if self.code in ("client_capacity", "pending_capacity", "prepare_busy", "handle_capacity"):
            result["retryable"] = True
        return result


def execution_host_id(directory):
    """Logical local installation identity; no hardware fingerprint or probe."""
    path = Path(directory) / "device_host.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("CREATE TABLE IF NOT EXISTS execution_host(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        database.execute("INSERT OR IGNORE INTO execution_host VALUES(1,?)", (uuid.uuid4().hex,))
        value = database.execute("SELECT value FROM execution_host WHERE id=1").fetchone()[0]
        if type(value) is not str or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Invalid stored execution host identity")
    return value


class DeviceTextGateway:
    def __init__(self, clients, binder, broker, *, pipeline=None, owned_store=None):
        if broker.binder is not binder:
            raise ValueError("The gateway and broker must share their binder")
        self.clients, self.binder, self.broker = clients, binder, broker
        self.pipeline, self._store = pipeline, owned_store
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._lifecycle = threading.Condition(threading.RLock())
        self._active_routes = 0

    @classmethod
    def for_hub(cls, runtime, hub, *, admission_limits=None):
        from canaan_device_turn import DeviceTurnAdapter
        context = getattr(hub, "user_context", None)
        if context is None or not callable(getattr(context, "sync", None)) or not callable(getattr(context, "valid", None)):
            raise ValueError("A real desktop user context is required")
        store = DeviceBindingStore(runtime.directory / "device_bindings.db")
        broker = pipeline = None
        try:
            binder = DeviceTextRequestBinder(runtime.clients, context, store,
                                             execution_host_id=execution_host_id(runtime.directory))
            pipeline = DeviceTurnAdapter(hub, binder)
            context_store = getattr(hub, "context_store", getattr(context, "db", None))
            broker = DeviceTextRequestBroker(runtime.directory / "device_text_requests.db", binder, pipeline,
                                            cipher=getattr(context_store, "cipher", None), limits=admission_limits)
            return cls(runtime.clients, binder, broker, pipeline=pipeline, owned_store=store)
        except BaseException:
            if pipeline is not None:
                pipeline.close()
            if broker is not None:
                broker.close()
            store.close()
            raise

    @staticmethod
    def _body(body, fields):
        if type(body) is not dict or set(body) != set(fields):
            raise DeviceGatewayError(400, "invalid_body", "请求内容不完整或包含额外字段。")

    @staticmethod
    def _mapped(error):
        if isinstance(error, DeviceBindingError):
            if error.code == "handle_capacity":
                return DeviceGatewayError(503, error.code, "设备任务暂忙，请稍后保留原编号重试。")
            status = 401 if error.code == "invalid_token" else 403
            return DeviceGatewayError(status, error.code, "设备授权或电脑使用者会话已变化，请在电脑上核对。")
        if isinstance(error, DeviceRequestError):
            capacity = error.code in ("client_capacity", "pending_capacity", "prepare_busy")
            status = 404 if error.code == "not_found" else 503 if error.code == "closed" or capacity else 409
            messages = {"not_found": "没有找到这项设备任务。", "stale_input_version": "问题已变化，请查看最新问题。",
                        "stale_confirmation": "待确认内容已变化，请重新核对。", "answer_conflict": "这个回答编号已经用于其他内容。",
                        "request_conflict": "这个请求编号已经用于其他内容或会话。", "cannot_cancel_running": "任务已经开始，请在电脑上查看。"}
            if capacity:
                messages[error.code] = "设备任务暂忙，请稍后保留原编号重试。"
            return DeviceGatewayError(status, error.code, messages.get(error.code, "这项任务暂时不能处理，请在电脑上核对。"))
        return DeviceGatewayError(400, "invalid_request", "请求格式不正确，请保留原编号并核对内容。")

    def route(self, method, path, body, *, token):
        principal = authenticate_credential(self.clients, token)
        if principal is None:
            raise DeviceGatewayError(401, "authentication_required", "请先验证设备身份。")
        if not path.startswith(PREFIX + "/"):
            raise DeviceGatewayError(404, "not_found", "没有找到这个入口。")
        with self._lifecycle:
            if self._stop.is_set():
                raise DeviceGatewayError(503, "device_gateway_stopping", "电脑端文字入口正在关闭。")
            self._active_routes += 1
        try:
            return self._route(method, path, body, token=token)
        finally:
            with self._lifecycle:
                self._active_routes -= 1
                self._lifecycle.notify_all()

    def _route(self, method, path, body, *, token):
        parts = path[len(PREFIX) + 1:].split("/")
        try:
            # Closing the gateway revokes every subsequent real dispatch in
            # an in-flight route; the adapter also feeds supported stop events.
            with execution_guard(lambda: not self._stop.is_set()):
                if method == 'GET' and parts == ['capabilities']:
                    self._body(body, ())
                    return self.broker.capabilities()
                if method == 'GET' and len(parts) in (3, 4, 5) and parts[0] in ('tasks', 'review') and parts[2] == 'attachments':
                    self._body(body, ())
                    local_owner = parts[0] == 'review'
                    if len(parts) == 3:
                        return self.broker.attachment_catalog(token, parts[1], local_owner=local_owner)
                    if len(parts) == 4:
                        return self.broker.attachment_manifest(token, parts[1], parts[3], local_owner=local_owner)
                    index = parts[4]
                    if (not index.isascii() or not index.isdecimal() or len(index) > 4
                            or len(index) > 1 and index.startswith('0')):
                        raise ValueError('Noncanonical attachment chunk index')
                    return self.broker.attachment_chunk(token, parts[1], parts[3], int(index), local_owner=local_owner)
                if method == 'GET' and len(parts) in (3, 5) and parts[0] in ('tasks', 'review') and parts[2] == 'result':
                    self._body(body, ())
                    local_owner = parts[0] == 'review'
                    if len(parts) == 3:
                        return self.broker.result_manifest(token, parts[1], local_owner=local_owner)
                    revision, index = parts[3:]
                    if (not revision.isascii() or not revision.isdecimal() or revision.startswith('0')
                            or len(revision) > 16 or not index.isascii() or not index.isdecimal()
                            or len(index) > 4 or len(index) > 1 and index.startswith('0')):
                        raise ValueError('Noncanonical result page path')
                    return self.broker.result_page(token, parts[1], int(revision), int(index), local_owner=local_owner)
                if method == "POST" and parts == ["requests"]:
                    return self.broker.submit(token, body)
                if len(parts) in (2, 3) and parts[0] == "tasks":
                    task_id = parts[1]
                    if method == "GET" and len(parts) == 2:
                        self._body(body, ())
                        return self.broker.get(token, task_id)
                    if method == "POST" and parts[2:] == ["answers"]:
                        return self.broker.follow_up(token, task_id, body)
                    if method == "POST" and parts[2:] == ["cancel"]:
                        self._body(body, ())
                        return self.broker.cancel(token, task_id)
                if method == "GET" and parts == ["review"]:
                    self._body(body, ())
                    return self.broker.list_local(token)
                if len(parts) in (2, 3) and parts[0] == "review":
                    task_id = parts[1]
                    if method == "GET" and len(parts) == 2:
                        self._body(body, ())
                        return self.broker.get_local(token, task_id)
                    if method == "POST" and parts[2:] in (["confirm"], ["cancel"]):
                        self._body(body, ("expected_version",))
                        action = self.broker.confirm_local if parts[2] == "confirm" else self.broker.cancel_local
                        return action(token, task_id, expected_version=body["expected_version"])
                if method == "POST" and parts == ["bindings"]:
                    self._body(body, ("client_id",))
                    binding_id = self.binder.register_local_binding(token, body["client_id"])
                    return {"client_id": body["client_id"], "binding_id": binding_id, "bound": True}
                if method == "POST" and len(parts) == 3 and parts[0] == "bindings" and parts[2] == "revoke":
                    self._body(body, ())
                    return {"revoked": self.binder.revoke_local_binding(token, parts[1])}
        except (DeviceBindingError, DeviceRequestError, ValueError, TypeError) as ex:
            raise self._mapped(ex) from None
        raise DeviceGatewayError(404, "not_found", "没有找到这个入口。")

    def close(self):
        with self._close_lock:
            with self._lifecycle:
                if self._stop.is_set():
                    return
                self._stop.set()
            try:
                if self.pipeline is not None:
                    self.pipeline.close()
            finally:
                with self._lifecycle:
                    idle = self._active_routes == 0
                if not idle or not self.broker.wait_idle(0) or not self._finish_close():
                    threading.Thread(target=self._drain, name="device-text-drain", daemon=True).start()

    def _drain(self):
        with self._lifecycle:
            self._lifecycle.wait_for(lambda: self._active_routes == 0)
        while not self._closed.is_set():
            self.broker.wait_idle()
            if self._finish_close():
                return

    def _finish_close(self):
        try:
            self.broker.close()
        except DeviceRequestError as ex:
            if ex.code == "busy":
                return False
            raise
        if self._store is not None:
            self._store.close()
        self._closed.set()
        return True

    def wait_closed(self, timeout=None):
        return self._closed.wait(timeout)
