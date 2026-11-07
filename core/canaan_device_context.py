"""Task-only persistent context views; authorization stays with the real user.

Namespaces are local server metadata, not device-supplied authority. This module
opens no database, session, model, listener or OS action by itself.
"""
import hashlib
import json

from canaan_user_context import UserContext


def device_task_namespace(client_id, request_id):
    """Stable opaque metadata for one authenticated client/request pair."""
    if any(type(value) is not str or not value for value in (client_id, request_id)):
        raise ValueError("Client and request identities must be nonempty strings")
    payload = json.dumps([client_id, request_id], ensure_ascii=False, separators=(",", ":"))
    return "device:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ScopedTaskStore:
    """Expose only task APIs with a fixed non-desktop namespace."""
    __slots__ = ("_base", "_namespace")

    def __init__(self, base, namespace):
        if (type(namespace) is not str or not namespace or len(namespace) > 256
                or namespace != namespace.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in namespace)):
            raise ValueError("A nonempty local task namespace is required")
        self._base, self._namespace = base, namespace

    @property
    def namespace(self):
        return self._namespace

    @property
    def incognito(self):
        return self._base.incognito

    def _call(self, name, *args, **kwargs):
        if "namespace" in kwargs:
            raise TypeError("The task namespace is fixed by this store")
        return getattr(self._base, name)(*args, namespace=self._namespace, **kwargs)

    def save_task(self, *args, **kwargs):
        return self._call("save_task", *args, **kwargs)

    def get_task(self, *args, **kwargs):
        return self._call("get_task", *args, **kwargs)

    def active_task(self, *args, **kwargs):
        return self._call("active_task", *args, **kwargs)

    def latest_task(self, *args, **kwargs):
        return self._call("latest_task", *args, **kwargs)

    def claim_task(self, *args, **kwargs):
        return self._call("claim_task", *args, **kwargs)

    def claim_execution(self, *args, **kwargs):
        return self._call("claim_execution", *args, **kwargs)

    def finish_task(self, *args, **kwargs):
        return self._call("finish_task", *args, **kwargs)

    def transition_task(self, *args, **kwargs):
        return self._call("transition_task", *args, **kwargs)

    def cancel_open_tasks(self, *args, **kwargs):
        return self._call("cancel_open_tasks", *args, **kwargs)


class TaskScopedUserContext:
    """Reuse the real UserContext's pending/CAS logic against scoped task rows.

    No new UserContext is initialized. Real validity, token, current binding and
    lock are delegated, while the original context.db is never replaced.
    """
    __slots__ = ("_real", "_store")

    def __init__(self, real_context, scoped_store):
        if not isinstance(scoped_store, ScopedTaskStore) or scoped_store._base is not real_context.db:
            raise ValueError("Task storage must wrap this real context's database")
        self._real, self._store = real_context, scoped_store

    @property
    def db(self):
        return self._store

    @property
    def lock(self):
        return self._real.lock

    @property
    def binding(self):
        return self._real.binding

    def valid(self, binding):
        return self._real.valid(binding)

    def token(self):
        return self._real.token()

    def sync(self):
        return self._real.sync()

    prepare_pending = UserContext.prepare_pending
    _prepare_pending = UserContext._prepare_pending
    claim = UserContext.claim
    finish = UserContext.finish
