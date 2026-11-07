"""Production admission contracts with real registry, Binder, Gateway and temporary Cipher/SQLite."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import sqlite3
import threading

import pytest
from canaan_api_clients import ClientRegistry
from canaan_crypto import Cipher
from canaan_device_binding import DeviceBindingStore, DeviceTextRequestBinder
from canaan_device_requests import DeviceTextRequestBroker, PreparedDeviceTurn, DeviceExecutionResult
from canaan_device_gateway import DeviceTextGateway, DeviceGatewayError, PREFIX

OWNER = "proposal-owner-fixture"


class Context:
    def __init__(self):
        self.current = SimpleNamespace(profile_key="owner", session_id="proposal-session",
            token=("owner", 1, 2, 3), store=SimpleNamespace(incognito=False))

    def sync(self):
        return self.current

    def valid(self, current):
        return current is self.current


class Pipeline:
    def __init__(self):
        self.question = False
        self.prepare_hook = self.answer_hook = self.execute_hook = None
        self.prepares = self.answers = self.executions = 0

    def prepare(self, bound):
        self.prepares += 1
        if self.prepare_hook:
            self.prepare_hook(bound)
        return PreparedDeviceTurn({"fixture": True}, question="哪条信息？" if self.question else "")

    def follow_up(self, prepared, bound, text):
        self.answers += 1
        if self.answer_hook:
            self.answer_hook(bound)
        return PreparedDeviceTurn({"fixture": True}, question="还有哪条？")

    def execute(self, prepared, bound):
        self.executions += 1
        return self.execute_hook(prepared, bound) if self.execute_hook else DeviceExecutionResult("succeeded", "fixture")


class Environment:
    def __init__(self, path, *, max_handles=64, limits=None):
        self.path, self.max_handles, self.limits = path, max_handles, limits
        self.clients = ClientRegistry(path / "clients", OWNER)
        self.client, self.token = self.clients.pair("WATCH GT fixture")
        self.context, self.cipher, self.pipeline = Context(), Cipher(b"P" * 32), Pipeline()
        self.open()
        self.binder.register_local_binding(OWNER, self.client["id"])

    def open(self):
        self.store = DeviceBindingStore(self.path / "grants.db")
        self.binder = DeviceTextRequestBinder(self.clients, self.context, self.store,
            execution_host_id="proposal-host", max_handles=self.max_handles)
        kwargs = {"limits": self.limits} if self.limits is not None else {}
        self.broker = DeviceTextRequestBroker(self.path / "ledger.db", self.binder, self.pipeline,
            cipher=self.cipher, **kwargs)
        self.gateway = DeviceTextGateway(self.clients, self.binder, self.broker, owned_store=self.store)

    def route(self, method, suffix, body=None, *, token=None):
        return self.gateway.route(method, PREFIX + suffix, {} if body is None else body,
            token=self.token if token is None else token)

    def submit(self, request_id, *, token=None, text="fixture"):
        return self.route("POST", "/requests", {"protocol_version": 1, "request_id": request_id,
            "text": text}, token=token)

    def get(self, task):
        return self.route("GET", "/tasks/" + task["task_id"])

    def cancel(self, task):
        return self.route("POST", "/tasks/" + task["task_id"] + "/cancel")

    def confirm(self, task):
        view = self.route("GET", "/review/" + task["task_id"], token=OWNER)
        return self.route("POST", "/review/" + task["task_id"] + "/confirm",
            {"expected_version": view["confirmation_version"]}, token=OWNER)

    def answer(self, task, answer_id="a", version=None, text="fixture answer"):
        return self.route("POST", "/tasks/" + task["task_id"] + "/answers",
            {"protocol_version": 1, "answer_id": answer_id,
             "expected_version": task["input_version"] if version is None else version, "text": text})

    def snapshot(self):
        with self.broker.lock:
            return {"states": dict(self.broker.db.execute("SELECT state,COUNT(*) FROM device_text_requests GROUP BY state")),
                "rows": self.broker.db.execute("SELECT COUNT(*) FROM device_text_requests").fetchone()[0],
                "answers": self.broker.db.execute("SELECT COUNT(*) FROM device_text_answers").fetchone()[0],
                "handles": tuple(self.binder._issued), "active": self.broker._active,
                "prepare_active": getattr(self.broker, "_prepare_active", 0),
                "prepares": self.pipeline.prepares, "answer_calls": self.pipeline.answers}

    def close(self):
        self.gateway.close()
        assert self.gateway.wait_closed(3)


def capacity(call, code):
    with pytest.raises(DeviceGatewayError) as caught:
        call()
    assert caught.value.status == 503 and caught.value.code == code
    assert caught.value.public().get("retryable") is True
    assert set(caught.value.public()) == {"error", "message", "retryable"}


def test_default_client_capacity_and_lost_ack(tmp_path):
    env = Environment(tmp_path)
    try:
        tasks = [env.submit(str(index)) for index in range(8)]
        before = env.snapshot()
        capacity(lambda: env.submit("overflow"), "client_capacity")
        assert env.snapshot() == before
        assert env.submit("0") == tasks[0]
        with pytest.raises(DeviceGatewayError) as conflict:
            env.submit("0", text="different")
        assert conflict.value.code == "request_conflict"
        assert env.snapshot() == before
        assert env.confirm(tasks[0])["state"] == "succeeded"
        assert env.submit("overflow")["state"] == "awaiting_confirmation"
    finally:
        env.close()


def test_submit_and_answer_share_prepare_busy_without_mutating_answer(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(3, 4, 1))
    entered, release = threading.Event(), threading.Event()
    def hold(_):
        entered.set()
        assert release.wait(5)
    try:
        env.pipeline.question = True
        task = env.submit("question")
        prior = env.answer(task, "old-answer")
        env.pipeline.prepare_hook = hold
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(env.submit, "held")
            try:
                assert entered.wait(3)
                before = env.snapshot()
                capacity(lambda: env.answer(prior, "new-answer"), "prepare_busy")
                assert env.get(task)["input_version"] == prior["input_version"]
                assert env.snapshot() == before
                replay = env.answer(task, "old-answer")
                assert replay["answer_id"] == "old-answer" and replay["state"] == "awaiting_input"
                with pytest.raises(DeviceGatewayError) as conflict:
                    env.answer(task, "old-answer", text="different")
                assert conflict.value.code == "answer_conflict"
                capacity(lambda: env.submit("blocked-new"), "prepare_busy")
                assert env.snapshot() == before
            finally:
                release.set()
            future.result(timeout=3)
        env.pipeline.prepare_hook = None
        entered.clear()
        release.clear()
        env.pipeline.answer_hook = hold
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(env.answer, prior, "new-answer")
            try:
                assert entered.wait(3)
                capacity(lambda: env.submit("blocked-reverse"), "prepare_busy")
                assert env.answer(prior, "new-answer")["state"] == "preparing"
            finally:
                release.set()
            assert future.result(timeout=3)["state"] == "awaiting_input"
        env.pipeline.answer_hook = None
        assert env.submit("after-release")["state"] == "awaiting_input"
    finally:
        release.set()
        env.close()


def test_handle_capacity_never_evicts_pending_even_via_legacy_binder(tmp_path):
    env = Environment(tmp_path, max_handles=1)
    try:
        first = env.submit("first")
        before = env.snapshot()
        capacity(lambda: env.submit("second"), "handle_capacity")
        assert env.snapshot() == before
        from canaan_device_binding import DeviceBindingError
        with pytest.raises(DeviceBindingError) as full:
            env.binder.bind(env.token, "legacy", "fixture")
        assert full.value.code == "handle_capacity"
        assert env.snapshot() == before
        assert env.confirm(first)["state"] == "succeeded"
        assert env.submit("second")["state"] == "awaiting_confirmation"
    finally:
        env.close()


@pytest.mark.parametrize("field", ["client_pending", "global_pending", "prepare_concurrency"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_limits_reject_non_positive_integers(field, value):
    from canaan_device_limits import DeviceAdmissionLimits
    with pytest.raises(ValueError):
        DeviceAdmissionLimits(**{field: value})


def test_global_capacity_uses_authenticated_clients_and_no_cross_data(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(2, 2, 2))
    try:
        first = env.submit("first")
        other, token = env.clients.pair("Android fixture")
        env.binder.register_local_binding(OWNER, other["id"])
        env.submit("other", token=token)
        before = env.snapshot()
        capacity(lambda: env.submit("overflow"), "pending_capacity")
        assert env.snapshot() == before
        with pytest.raises(DeviceGatewayError) as malformed:
            env.route("POST", "/requests", {"protocol_version": 1, "request_id": "forged",
                "text": "fixture", "client_id": other["id"]})
        assert malformed.value.status == 400
        with pytest.raises(DeviceGatewayError) as invalid:
            env.submit("invalid", token="invalid-token")
        assert invalid.value.status == 401 and env.snapshot() == before
        assert env.cancel(first)["state"] == "cancelled"
        assert env.cancel(first)["state"] == "cancelled"
        env.submit("replacement")
        capacity(lambda: env.submit("still-full"), "pending_capacity")
    finally:
        env.close()


@pytest.mark.parametrize("global_limit", [False, True])
def test_four_simultaneous_new_ids_cannot_overreserve(tmp_path, global_limit):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(4 if global_limit else 2,
        2 if global_limit else 4, 4))
    entered, release = threading.Condition(), threading.Event()
    count = 0
    def hold(_):
        nonlocal count
        with entered:
            count += 1
            entered.notify_all()
        assert release.wait(5)
    env.pipeline.prepare_hook = hold
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(env.submit, "parallel-" + str(index)) for index in range(4)]
            try:
                with entered:
                    assert entered.wait_for(lambda: count == 2, timeout=3)
                assert env.snapshot()["states"] == {"preparing": 2}
                assert env.submit("parallel-0")["state"] == "preparing"
                assert count == 2
            finally:
                release.set()
            successes, errors = [], []
            for future in futures:
                try:
                    successes.append(future.result(timeout=3))
                except DeviceGatewayError as error:
                    errors.append(error)
        assert len(successes) == len(errors) == 2
        assert {error.code for error in errors} == {"pending_capacity" if global_limit else "client_capacity"}
        assert env.snapshot()["rows"] == env.pipeline.prepares == 2
        assert env.broker._prepare_active == env.broker._active == 0
    finally:
        release.set()
        env.close()


def test_cancel_preparing_releases_handle_without_reviving_old_callback(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, max_handles=1, limits=DeviceAdmissionLimits(1, 1, 2))
    entered, release = threading.Event(), threading.Event()
    def hold(bound):
        if bound.request_id == "held":
            entered.set()
            assert release.wait(5)
    env.pipeline.prepare_hook = hold
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(env.submit, "held")
            try:
                assert entered.wait(3)
                old = env.route("GET", "/review", token=OWNER)[0]
                assert env.cancel(old)["state"] == "cancelled"
                assert env.snapshot()["handles"] == ()
                replacement = env.submit("replacement")
                assert replacement["state"] == "awaiting_confirmation"
            finally:
                release.set()
            assert future.result(timeout=3)["state"] == "cancelled"
        assert env.get(old)["state"] == "cancelled"
        assert env.confirm(old)["state"] == "cancelled"
        assert env.confirm(replacement)["state"] == "succeeded"
        assert env.pipeline.executions == 1
        assert env.broker._prepare_active == env.broker._active == 0
    finally:
        release.set()
        env.close()


def test_running_and_continuation_hold_one_pending_reservation(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(1, 1, 1))
    entered, release = threading.Event(), threading.Event()
    def execute(prepared, bound):
        entered.set()
        assert release.wait(5)
        return DeviceExecutionResult("awaiting_confirmation", "next", PreparedDeviceTurn({"remaining": True}))
    env.pipeline.execute_hook = execute
    try:
        task = env.submit("running")
        original_handles = env.snapshot()["handles"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(env.confirm, task)
            try:
                assert entered.wait(3)
                capacity(lambda: env.submit("blocked"), "client_capacity")
                assert env.confirm(task)["state"] == "running"
                with pytest.raises(DeviceGatewayError) as running:
                    env.cancel(task)
                assert running.value.code == "cannot_cancel_running"
            finally:
                release.set()
            assert future.result(timeout=3)["state"] == "awaiting_confirmation"
        assert env.snapshot()["handles"] == original_handles
        capacity(lambda: env.submit("blocked"), "client_capacity")
        env.pipeline.execute_hook = None
        assert env.confirm(task)["state"] == "succeeded"
        env.submit("blocked")
        capacity(lambda: env.submit("still-full"), "client_capacity")
    finally:
        release.set()
        env.close()


def test_unknown_does_not_reprepare_and_terminal_release_is_once(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(1, 1, 1))
    def uncertain(*_):
        raise RuntimeError("fixture uncertain executor")
    try:
        task = env.submit("unknown")
        env.pipeline.execute_hook = uncertain
        assert env.confirm(task)["state"] == "unknown"
        assert env.snapshot()["handles"] == ()
        assert env.submit("unknown")["state"] == "unknown"
        assert env.confirm(task)["state"] == env.cancel(task)["state"] == "unknown"
        assert env.pipeline.prepares == env.pipeline.executions == 1
        env.submit("replacement")
        env.cancel(task)
        capacity(lambda: env.submit("still-full"), "client_capacity")
    finally:
        env.close()


def test_database_insert_and_answer_rollback_leave_no_orphan_reservations(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(1, 1, 1))
    try:
        env.broker.db.execute("CREATE TRIGGER deny_new BEFORE INSERT ON device_text_requests BEGIN SELECT RAISE(ABORT,'fixture'); END")
        env.broker.db.commit()
        before = env.snapshot()
        with pytest.raises(sqlite3.IntegrityError):
            env.submit("rolled-back")
        assert env.snapshot() == before
        assert not env.binder._pending_handles
        env.broker.db.execute("DROP TRIGGER deny_new")
        env.pipeline.question = True
        task = env.submit("rolled-back")
        before = env.snapshot()
        env.broker.db.execute("CREATE TRIGGER deny_answer BEFORE INSERT ON device_text_answers BEGIN SELECT RAISE(ABORT,'fixture'); END")
        env.broker.db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            env.answer(task)
        assert env.snapshot() == before
        assert env.get(task)["input_version"] == task["input_version"]
        env.broker.db.execute("DROP TRIGGER deny_answer")
        assert env.answer(task)["state"] == "awaiting_input"
    finally:
        env.close()


def test_prepare_and_followup_exceptions_release_both_resource_limits(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, max_handles=1, limits=DeviceAdmissionLimits(1, 1, 1))
    def fail(_):
        raise RuntimeError("fixture prepare failure")
    try:
        env.pipeline.prepare_hook = fail
        assert env.submit("failed-prepare")["state"] == "failed"
        assert env.snapshot()["handles"] == ()
        assert env.broker._active == env.broker._prepare_active == 0
        env.pipeline.prepare_hook = None
        env.pipeline.question = True
        task = env.submit("failed-answer")
        env.pipeline.answer_hook = fail
        assert env.answer(task)["state"] == "failed"
        assert env.snapshot()["handles"] == ()
        assert env.broker._active == env.broker._prepare_active == 0
        env.submit("after-failures")
    finally:
        env.close()


def test_revoked_grant_wins_over_dedupe_and_capacity_and_cannot_execute(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(1, 1, 1))
    try:
        task = env.submit("revoked")
        env.route("POST", "/bindings/" + env.client["id"] + "/revoke", token=OWNER)
        before = env.snapshot()
        for request_id in ("revoked", "new-id"):
            with pytest.raises(DeviceGatewayError) as denied:
                env.submit(request_id)
            assert denied.value.code == "grant_missing"
        assert env.snapshot() == before
        assert env.confirm(task)["state"] == "failed"
        assert env.pipeline.executions == 0 and env.snapshot()["handles"] == ()
    finally:
        env.close()


def test_close_drains_prepare_and_reopen_settles_persisted_pending(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, max_handles=1, limits=DeviceAdmissionLimits(1, 1, 1))
    entered, release = threading.Event(), threading.Event()
    def hold(_):
        entered.set()
        assert release.wait(5)
    env.pipeline.prepare_hook = hold
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(env.submit, "persisted")
            try:
                assert entered.wait(3)
                env.gateway.close()
                assert not env.gateway.wait_closed(.01)
                with pytest.raises(DeviceGatewayError) as stopped:
                    env.submit("after-close")
                assert stopped.value.code == "device_gateway_stopping"
            finally:
                release.set()
            task = future.result(timeout=3)
        assert env.gateway.wait_closed(3)
        assert env.broker._prepare_active == env.broker._active == 0
        assert not env.binder._issued and not env.binder._pending_handles
        with sqlite3.connect(tmp_path / "ledger.db") as database:
            assert database.execute("SELECT state FROM device_text_requests").fetchone()[0] == "awaiting_confirmation"
        env.pipeline.prepare_hook = None
        env.open()
        assert env.submit("persisted")["state"] == "unknown"
        assert env.get(task)["state"] == "unknown"
        assert env.pipeline.prepares == 1
        assert env.submit("after-reopen")["state"] == "awaiting_confirmation"
        capacity(lambda: env.submit("still-full"), "client_capacity")
    finally:
        release.set()
        env.close()


def test_commit_failure_rolls_back_new_pending_and_protected_handle(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    env = Environment(tmp_path, max_handles=1, limits=DeviceAdmissionLimits(1, 1, 1))
    try:
        database = env.broker.db
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("CREATE TABLE fixture_parent(id INTEGER PRIMARY KEY)")
        database.execute("CREATE TABLE fixture_child(parent_id INTEGER REFERENCES fixture_parent(id) DEFERRABLE INITIALLY DEFERRED)")
        database.execute("CREATE TRIGGER deny_commit AFTER INSERT ON device_text_requests BEGIN INSERT INTO fixture_child VALUES(42); END")
        database.commit()
        before = env.snapshot()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            env.submit("commit-failed")
        assert not database.in_transaction
        assert env.snapshot() == before
        assert not env.binder._pending_handles
        assert database.execute("SELECT COUNT(*) FROM fixture_child").fetchone()[0] == 0
        database.execute("DROP TRIGGER deny_commit")
        assert env.submit("commit-failed")["state"] == "awaiting_confirmation"
    finally:
        env.close()


def test_context_change_before_insert_is_auth_denial_without_receipt_or_retry_hint(tmp_path, monkeypatch):
    env = Environment(tmp_path)
    current = env.context.current
    original = env.binder.bind
    def change_before_bind(*args, **kwargs):
        env.context.current = SimpleNamespace(**(vars(current) | {"session_id": "changed-session"}))
        return original(*args, **kwargs)
    try:
        before = env.snapshot()
        monkeypatch.setattr(env.binder, "bind", change_before_bind)
        with pytest.raises(DeviceGatewayError) as denied:
            env.submit("session-race")
        assert denied.value.status == 403 and denied.value.code == "context_changed"
        assert denied.value.public() == {"error": "context_changed",
            "message": "设备授权或电脑使用者会话已变化，请在电脑上核对。"}
        assert env.snapshot() == before
        assert not env.binder._pending_handles
        monkeypatch.setattr(env.binder, "bind", original)
        env.context.current = current
        assert env.submit("session-race")["state"] == "awaiting_confirmation"
    finally:
        env.context.current = current
        env.close()
