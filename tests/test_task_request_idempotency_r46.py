"""R46 request retry contract; stdlib loads, fake actions, temporary SQLite only.

Run with --noconftest so repository-wide runtime fixtures are never imported.
The API class is compiled from its AST without its module startup imports, and
is allocated without __init__/start: route and scope logic remain the real code.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("r46_integration_tasks", ROOT / "plugins/canaan_integration_tasks.py")
task_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_module)
IntegrationTasks = task_module.IntegrationTasks


class Registry:
    actions = {
        name: types.SimpleNamespace(name=name, permission_level=types.SimpleNamespace(value=permission))
        for name, permission in (("read", "safe_readonly"), ("write", "dangerous"),
                                 ("daily_briefing", "safe_readonly"), ("knowledge_query", "safe_readonly"))
    }

    @classmethod
    def get_action(cls, name):
        return cls.actions.get("read" if name == "read_alias" else name)


class FakeExecutor:
    def __init__(self, blocked=False, success=True):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.success = success
        if not blocked:
            self.release.set()

    def __call__(self, action, param, approved):
        self.calls.append((action, param, approved))
        self.started.set()
        assert self.release.wait(5), "test did not release its fake executor"
        return {"success": self.success, "message": "fake result"}


@pytest.fixture
def make_tasks(tmp_path):
    opened = []

    def make(name="tasks.db", executor=None):
        executor = executor or FakeExecutor()
        tasks = IntegrationTasks(tmp_path / name, Registry, executor)
        opened.append((tasks, executor))
        return tasks, executor

    yield make
    for tasks, executor in opened:
        executor.release.set()
    for tasks, _ in opened:
        for worker in tuple(threading.enumerate()):
            if worker.name.startswith("canaan-task-"):
                worker.join(5)
                assert not worker.is_alive(), "fake execution thread leaked"
        tasks.db.close()


def wait_state(tasks, task_id, states=("succeeded", "failed")):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = tasks.get(task_id)
        if task["state"] in states:
            return task
        time.sleep(.005)
    pytest.fail("temporary fake task did not reach expected state")


def count_starts(tasks):
    starts = []
    start = tasks._start

    def record(task_id):
        starts.append(task_id)
        start(task_id)

    tasks._start = record
    return starts


def test_reordered_nested_objects_reuse_confirmed_current_task(make_tasks):
    tasks, executor = make_tasks()
    starts = count_starts(tasks)
    first = tasks.submit("read", {"b": [False, {"y": 2, "x": "a"}], "a": None},
                         client_id="watch", require_confirmation=True, request_id="retry-1")
    replay = tasks.submit("read_alias", {"a": None, "b": [False, {"x": "a", "y": 2}]},
                          client_id="watch", require_confirmation=True, request_id="retry-1")
    assert replay["id"] == first["id"]
    assert replay["state"] == "awaiting_confirmation"
    assert starts == [] and executor.calls == []
    tasks.confirm(first["id"])
    done = wait_state(tasks, first["id"])
    replay = tasks.submit("read", first["param"], client_id="watch", require_confirmation=True, request_id="retry-1")
    assert replay == done
    assert starts == [first["id"]]
    assert executor.calls == [("read", first["param"], True)]
    assert len(tasks.list()) == 1


def test_running_and_completed_retries_do_not_schedule_or_execute_again(make_tasks):
    executor = FakeExecutor(blocked=True)
    tasks, _ = make_tasks(executor=executor)
    starts = count_starts(tasks)
    first = tasks.submit("read", [1, True, "1"], request_id="readonly")
    assert executor.started.wait(5)
    replay = tasks.submit("read", [1, True, "1"], request_id="readonly")
    assert replay["id"] == first["id"] and replay["state"] == "running"
    executor.release.set()
    done = wait_state(tasks, first["id"])
    assert tasks.submit("read", [1, True, "1"], request_id="readonly") == done
    assert starts == [first["id"]]
    assert executor.calls == [("read", [1, True, "1"], False)]


@pytest.mark.parametrize("old,new", [
    (True, 1), (False, 0), (1, 1.0), ("1", 1), (None, "null"),
    ([1, 2], [2, 1]), ([1], {"0": 1}), ({"a": True}, {"a": 1}),
])
def test_same_key_changed_json_payload_conflicts_without_mutation(make_tasks, old, new):
    tasks, executor = make_tasks()
    first = tasks.submit("write", old, request_id="private-request")
    with pytest.raises(ValueError, match="request_id.*(冲突|conflict)") as error:
        tasks.submit("write", new, request_id="private-request")
    assert "private-request" not in str(error.value)
    assert tasks.list() == [first] and executor.calls == []


@pytest.mark.parametrize("changed", [{"action": "read"}, {"require_confirmation": True}])
def test_same_key_changed_action_or_confirmation_policy_conflicts(make_tasks, changed):
    tasks, executor = make_tasks()
    first = tasks.submit("write", {"secret": "never echoed"}, request_id="policy")
    with pytest.raises(ValueError, match="request_id.*(冲突|conflict)") as error:
        tasks.submit(**({"action": "write", "param": {"secret": "never echoed"}, "request_id": "policy"} | changed))
    assert "never echoed" not in str(error.value)
    assert tasks.list() == [first] and executor.calls == []


@pytest.mark.parametrize("request_id", ["", "   ", "x" * 129, 1, True, [], {}, b"key"])
def test_invalid_request_ids_are_value_errors_without_writes(make_tasks, request_id):
    tasks, executor = make_tasks()
    with pytest.raises(ValueError, match="request_id"):
        tasks.submit("read", request_id=request_id)
    assert tasks.list() == [] and executor.calls == []


@pytest.mark.parametrize("client_id", ["", " ", "x" * 129, 1, True, [], {}])
def test_keyed_invalid_client_namespace_is_rejected(make_tasks, client_id):
    tasks, executor = make_tasks()
    with pytest.raises(ValueError, match="client_id"):
        tasks.submit("read", client_id=client_id, request_id="key")
    assert tasks.list() == [] and executor.calls == []


@pytest.mark.parametrize("param", [float("nan"), float("inf"), {1: "a"}, (1, 2), {"x": object()}])
def test_keyed_non_json_or_ambiguous_types_fail_safely(make_tasks, param):
    tasks, executor = make_tasks()
    with pytest.raises(ValueError):
        tasks.submit("read", param, request_id="invalid-json")
    assert tasks.list() == [] and executor.calls == []


def test_cyclic_json_and_non_boolean_keyed_policy_fail_safely(make_tasks):
    tasks, executor = make_tasks()
    cycle = []
    cycle.append(cycle)
    for param, policy in ((cycle, False), (None, 1)):
        with pytest.raises(ValueError):
            tasks.submit("read", param, require_confirmation=policy, request_id="invalid")
    assert tasks.list() == [] and executor.calls == []


def test_no_key_keeps_independent_submissions_and_legacy_parameter_handling(make_tasks):
    tasks, executor = make_tasks()
    one = tasks.submit("write", (1, 2))
    two = tasks.submit("write", (1, 2), request_id=None)
    assert one["id"] != two["id"]
    assert one["param"] == two["param"] == [1, 2]
    assert len(tasks.list()) == 2 and executor.calls == []


def test_distinct_request_keys_create_distinct_tasks_for_same_client(make_tasks):
    tasks, executor = make_tasks()
    one = tasks.submit("write", "same payload", client_id="watch", request_id="one")
    two = tasks.submit("write", "same payload", client_id="watch", request_id="two")
    assert one["id"] != two["id"]
    assert len(tasks.list()) == 2 and executor.calls == []


def test_no_key_readonly_submissions_each_execute_once(make_tasks):
    tasks, executor = make_tasks()
    one = tasks.submit("read", "same payload")
    two = tasks.submit("read", "same payload")
    assert one["id"] != two["id"]
    assert wait_state(tasks, one["id"])["state"] == "succeeded"
    assert wait_state(tasks, two["id"])["state"] == "succeeded"
    assert executor.calls == [("read", "same payload", False), ("read", "same payload", False)]


def test_max_length_key_is_accepted_and_oversized_param_cannot_consume_key(make_tasks):
    tasks, executor = make_tasks()
    key = "k" * 128
    with pytest.raises(ValueError):
        tasks.submit("write", "x" * 16001, request_id=key)
    assert tasks.list() == []
    first = tasks.submit("write", "allowed", request_id=key)
    assert tasks.submit("write", "allowed", request_id=key)["id"] == first["id"]
    assert executor.calls == []


@pytest.mark.parametrize("args", [
    {"request_id": "\ud800"}, {"request_id": "key", "client_id": "\ud800"},
    {"request_id": "key", "param": "\ud800"}, {"request_id": "key", "param": {"\ud800": "value"}},
])
def test_invalid_unicode_is_safe_value_error_and_rolls_back_before_execution(make_tasks, args):
    tasks, executor = make_tasks()
    with pytest.raises(ValueError):
        tasks.submit("read", **args)
    assert tasks.list() == [] and executor.calls == []
    assert tasks.db.execute("SELECT count(*) FROM task_requests").fetchone()[0] == 0
    assert not tasks.db.in_transaction


def test_owner_and_client_namespaces_are_distinct_and_persistent(make_tasks):
    tasks, executor = make_tasks()
    owner = tasks.submit("write", request_id="same")
    client = tasks.submit("write", client_id="owner", request_id="same")
    other = tasks.submit("write", client_id="watch", request_id="same")
    restored, restored_executor = make_tasks()
    for client_id, original in ((None, owner), ("owner", client), ("watch", other)):
        assert restored.submit("write", client_id=client_id, request_id="same")["id"] == original["id"]
    assert len({owner["id"], client["id"], other["id"]}) == 3
    assert len(restored.list()) == 3
    assert executor.calls == restored_executor.calls == []


@pytest.mark.parametrize("state", ["cancelled", "succeeded", "failed", "unknown"])
def test_reopen_retry_of_finished_cancelled_or_unknown_never_reschedules(make_tasks, state):
    tasks, executor = make_tasks()
    first = tasks.submit("write", request_id="reopen")
    tasks.db.execute("UPDATE tasks SET state=?, result=? WHERE id=?", (state, '{"message":"saved"}', first["id"]))
    tasks.db.commit()
    restored, restored_executor = make_tasks()
    starts = count_starts(restored)
    replay = restored.submit("write", request_id="reopen")
    restored.confirm(first["id"])
    assert replay["id"] == first["id"] and replay["state"] == state
    assert replay["result"] == {"message": "saved"}
    assert starts == [] and executor.calls == restored_executor.calls == []


@pytest.mark.parametrize("old_state", ["queued", "running"])
def test_interrupted_task_is_unknown_and_retry_cannot_reexecute(make_tasks, old_state):
    tasks, executor = make_tasks()
    first = tasks.submit("write", request_id="interrupted")
    tasks.db.execute("UPDATE tasks SET state=? WHERE id=?", (old_state, first["id"]))
    tasks.db.commit()
    restored, restored_executor = make_tasks()
    replay = restored.submit("write", request_id="interrupted")
    assert replay["id"] == first["id"] and replay["state"] == "unknown"
    assert executor.calls == restored_executor.calls == []


def test_request_map_failure_rolls_back_task_and_leaves_key_retryable(make_tasks):
    tasks, executor = make_tasks()
    tasks.db.execute("CREATE TRIGGER reject_request BEFORE INSERT ON task_requests BEGIN SELECT RAISE(ABORT, 'injected map failure'); END")
    tasks.db.commit()
    with pytest.raises(sqlite3.DatabaseError, match="injected map failure"):
        tasks.submit("read", request_id="atomic")
    assert tasks.list() == [] and executor.calls == []
    assert tasks.db.execute("SELECT count(*) FROM task_requests").fetchone()[0] == 0
    assert not tasks.db.in_transaction
    tasks.db.execute("DROP TRIGGER reject_request")
    tasks.db.commit()
    accepted = tasks.submit("write", request_id="atomic")
    assert accepted["state"] == "awaiting_confirmation" and len(tasks.list()) == 1


@pytest.mark.parametrize("connections", [1, 2])
def test_concurrent_duplicate_submissions_create_and_schedule_exactly_once(make_tasks, connections):
    executor = FakeExecutor(blocked=True)
    one, _ = make_tasks(executor=executor)
    two = make_tasks(executor=executor)[0] if connections == 2 else one
    starts = [count_starts(one)] + ([count_starts(two)] if connections == 2 else [])
    barrier = threading.Barrier(12)

    def submit(index):
        barrier.wait(5)
        return (one if index % 2 else two).submit("read", {"battery": [1, True]}, client_id="watch", request_id="concurrent")

    with ThreadPoolExecutor(max_workers=12) as pool:
        replies = list(pool.map(submit, range(12)))
    assert len({reply["id"] for reply in replies}) == 1
    assert executor.started.wait(5)
    assert sum(map(len, starts)) == 1
    executor.release.set()
    done = wait_state(one, replies[0]["id"])
    assert done["state"] == "succeeded"
    assert executor.calls == [("read", {"battery": [1, True]}, False)]
    assert len(one.list()) == 1


def test_concurrent_changed_payload_has_one_winner_one_conflict(make_tasks):
    one, _ = make_tasks()
    two, _ = make_tasks()
    barrier = threading.Barrier(2)

    def submit(item):
        tasks, value = item
        barrier.wait(5)
        try:
            return tasks.submit("write", value, request_id="race")
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(submit, ((one, True), (two, 1))))
    assert replies.count("conflict") == 1
    assert len(one.list()) == 1


def test_concurrent_initialization_serializes_legacy_schema_migration(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, action TEXT, param TEXT, state TEXT, result TEXT, approved INTEGER, created_at REAL, updated_at REAL)")
    barrier = threading.Barrier(6)

    def open_legacy(_):
        barrier.wait(5)
        return IntegrationTasks(path, Registry, FakeExecutor())

    with ThreadPoolExecutor(max_workers=6) as pool:
        instances = list(pool.map(open_legacy, range(6)))
    try:
        first = instances[0].submit("write", request_id="legacy")
        assert instances[-1].submit("write", request_id="legacy")["id"] == first["id"]
    finally:
        for tasks in instances:
            tasks.db.close()


@pytest.fixture
def api(monkeypatch):
    """Compile real route/scope code; exclude startup and all external imports."""
    clients = types.ModuleType("canaan_api_clients")
    clients.CAPS = ("status", "read", "propose", "confirm", "train", "admin")
    clients.EXTERNAL_CAPS = frozenset(("status", "read", "propose"))
    clients.SOURCES = ("feishu", "claw")
    tree = ast.parse((ROOT / "plugins/canaan_api_clients.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Principal"]
    exec(compile(tree, "canaan_api_clients.py", "exec"), clients.__dict__)
    monkeypatch.setitem(sys.modules, "canaan_api_clients", clients)
    namespace = {"SOURCES": clients.SOURCES, "ROOT": ROOT, "json": json, "time": time, "Path": Path}
    tree = ast.parse((ROOT / "plugins/canaan_api_server.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if
                 isinstance(node, ast.ClassDef) and node.name == "IntegrationRuntime" or
                 isinstance(node, ast.FunctionDef) and node.name == "required_cap" or
                 isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("ROUTE_CAPS", "AGENT_ACTIONS") for t in node.targets)]
    exec(compile(tree, "canaan_api_server.py", "exec"), namespace)
    return namespace["IntegrationRuntime"], clients.Principal, clients.CAPS


@pytest.fixture
def route_env(api, make_tasks):
    Runtime, Principal, caps = api
    runtime = object.__new__(Runtime)
    runtime.tasks, executor = make_tasks()
    runtime.summary_settings = {"enabled": False, "interval_seconds": 900, "sources": ["feishu"]}
    owner = Principal("owner", "owner", caps, owner=True)
    watch = Principal("watch", "watch", ("status", "propose", "read"), {"summary": {"sources": ["feishu"]}})
    return runtime, owner, watch, executor


def test_api_owner_and_authenticated_clients_forward_keys_without_body_identity(route_env, api):
    runtime, owner, watch, executor = route_env
    _, Principal, _ = api
    other = Principal("other", "other", ("status", "propose"))
    body = {"action": "write", "param": {"a": 1}, "request_id": "api-retry", "client_id": "other", "request_namespace": "owner"}
    client = runtime.route("POST", "/api/tasks", {}, body, watch)
    assert runtime.route("POST", "/api/tasks", {}, body | {"client_id": "watch"}, watch)["id"] == client["id"]
    owner_task = runtime.route("POST", "/api/tasks", {}, body, owner)
    assert runtime.route("POST", "/api/tasks", {}, body, owner)["id"] == owner_task["id"]
    other_task = runtime.route("POST", "/api/tasks", {}, body, other)
    assert len({client["id"], owner_task["id"], other_task["id"]}) == 3
    assert client["client_id"] == "watch" and other_task["client_id"] == "other"
    assert owner_task["client_id"] is None
    assert client["state"] == "awaiting_confirmation" and executor.calls == []


def test_api_daily_briefing_retry_uses_authorized_transformed_payload(route_env):
    runtime, _, watch, executor = route_env
    body = {"action": "daily_briefing", "param": {"sources": ["feishu"], "origin": "spoof", "irrelevant": 1}, "request_id": "briefing"}
    first = runtime.route("POST", "/api/tasks", {}, body, watch)
    replay = runtime.route("POST", "/api/tasks", {}, body | {"param": {"sources": ["feishu"]}}, watch)
    assert replay["id"] == first["id"]
    assert first["param"] == {"sources": ["feishu"], "origin": "client:watch"}
    assert first["state"] == "awaiting_confirmation" and executor.calls == []
    with pytest.raises(PermissionError):
        runtime.route("POST", "/api/tasks", {}, body | {"param": {"sources": ["claw"]}}, watch)
    assert len(runtime.tasks.list()) == 1


@pytest.mark.parametrize("action", ["write", "read", "daily_briefing"])
def test_api_invalid_key_is_safe_value_error(route_env, action):
    runtime, _, watch, executor = route_env
    param = {"sources": ["feishu"]} if action == "daily_briefing" else "a"
    body = {"action": action, "param": param, "request_id": ""}
    with pytest.raises(ValueError, match="request_id"):
        runtime.route("POST", "/api/tasks", {}, body, watch)
    assert runtime.tasks.list() == [] and executor.calls == []


def test_api_daily_briefing_changed_authorized_sources_conflict(route_env, api):
    runtime, _, _, executor = route_env
    _, Principal, _ = api
    watch = Principal("watch", "watch", ("status", "read", "propose"),
                      {"summary": {"sources": ["feishu", "claw"]}})
    body = {"action": "daily_briefing", "param": {"sources": ["feishu"]}, "request_id": "briefing"}
    first = runtime.route("POST", "/api/tasks", {}, body, watch)
    with pytest.raises(ValueError, match="request_id.*(冲突|conflict)"):
        runtime.route("POST", "/api/tasks", {}, body | {"param": {"sources": ["claw"]}}, watch)
    assert runtime.tasks.list() == [first] and executor.calls == []


def test_api_retry_does_not_relax_scope_confirmation_or_capabilities(route_env, api):
    runtime, _, watch, executor = route_env
    _, Principal, _ = api
    body = {"action": "read", "request_id": "safe-read"}
    task = runtime.route("POST", "/api/tasks", {}, body, watch)
    assert task["state"] == "awaiting_confirmation" and executor.calls == []
    with pytest.raises(PermissionError):
        runtime.route("POST", f'/api/tasks/{task["id"]}/confirm', {}, {}, watch)
    no_cap = Principal("watch", "watch", ("status",))
    with pytest.raises(PermissionError):
        runtime.route("POST", "/api/tasks", {}, body, no_cap)
    with pytest.raises(PermissionError):
        runtime.route("POST", "/api/tasks", {}, {"action": "knowledge_query", "request_id": "knowledge"}, watch)
    no_scope = Principal("watch", "watch", ("propose",))
    with pytest.raises(PermissionError):
        runtime.route("POST", "/api/tasks", {}, {"action": "daily_briefing", "request_id": "summary"}, no_scope)
    assert len(runtime.tasks.list()) == 1 and executor.calls == []
