"""Reachable Gateway task ordering and owner capacity recovery contracts."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest
from canaan_device_limits import DeviceAdmissionLimits
from canaan_device_binding import DeviceBindingError
from canaan_device_gateway import DeviceGatewayError
from test_device_admission_r54 import Environment, OWNER


def ids(rows):
    return [row["request_id"] for row in rows]


@pytest.mark.parametrize("question", [False, True])
@pytest.mark.parametrize("limit", [1, 2])
def test_older_pending_precedes_newer_terminal_before_sql_limit(tmp_path, question, limit):
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(2, 2, 2))
    try:
        env.pipeline.question = question
        pending = env.submit("old-pending")
        env.pipeline.question = False
        terminal = env.submit("new-terminal")
        assert env.confirm(terminal)["state"] == "succeeded"
        stamps = dict(env.broker.db.execute("SELECT request_id,updated_at FROM device_text_requests"))
        assert stamps["new-terminal"] > stamps["old-pending"]
        assert ids(env.broker.list_local(OWNER, limit=limit)) == ["old-pending", "new-terminal"][:limit]
        assert ids(env.route("GET", "/review", token=OWNER)) == ["old-pending", "new-terminal"]
        assert env.get(pending)["state"] == ("awaiting_input" if question else "awaiting_confirmation")
    finally:
        env.close()


def test_owner_can_discover_and_cancel_next_pending_without_old_authority(tmp_path):
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(3, 3, 2))
    try:
        oldest = env.submit("oldest-pending")
        next_old = env.submit("next-pending")
        terminal = env.submit("new-terminal")
        assert env.confirm(terminal)["state"] == "succeeded"
        old_handles = tuple(env.binder._issued)
        env.context.current = SimpleNamespace(**(vars(env.context.current) | {"session_id": "new-context"}))
        for handle in old_handles:
            with pytest.raises(DeviceBindingError) as invalidated:
                env.binder.validate_bound(handle)
            assert invalidated.value.code == "context_changed"
        assert not env.binder._issued and not env.binder._pending_handles
        env.route("POST", "/bindings/" + env.client["id"] + "/revoke", token=OWNER)
        other, other_token = env.clients.pair("nonowner other fixture")
        for token in (env.token, other_token):
            with pytest.raises(DeviceGatewayError) as hidden:
                env.route("GET", "/review", token=token)
            assert hidden.value.status == 403 and hidden.value.code == "local_owner_required"
            with pytest.raises(DeviceGatewayError) as forbidden:
                env.route("POST", "/review/" + oldest["task_id"] + "/cancel",
                    {"expected_version": 2}, token=token)
            assert forbidden.value.status == 403 and forbidden.value.code == "local_owner_required"
        with pytest.raises(DeviceGatewayError) as invalid:
            env.route("GET", "/review", token="invalid-token")
        assert invalid.value.status == 401
        first_visible = env.broker.list_local(OWNER, limit=1)[0]
        assert first_visible["task_id"] == next_old["task_id"]
        assert ids(env.broker.list_local(OWNER, limit=2)) == ["next-pending", "oldest-pending"]
        assert env.route("POST", "/review/" + first_visible["task_id"] + "/cancel",
            {"expected_version": first_visible["confirmation_version"]}, token=OWNER)["state"] == "cancelled"
        next_visible = env.broker.list_local(OWNER, limit=1)[0]
        assert next_visible["task_id"] == oldest["task_id"]
        assert env.route("POST", "/review/" + next_visible["task_id"] + "/cancel",
            {"expected_version": next_visible["confirmation_version"]}, token=OWNER)["state"] == "cancelled"
        assert env.snapshot()["states"] == {"cancelled": 2, "succeeded": 1}
        env.route("POST", "/bindings", {"client_id": env.client["id"]}, token=OWNER)
        assert env.submit("after-owner-recovery")["state"] == "awaiting_confirmation"
        assert env.pipeline.executions == 1
    finally:
        env.close()


@pytest.mark.parametrize("state", ["preparing", "running"])
def test_inflight_pending_precedes_new_terminal(tmp_path, state):
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(2, 2, 2))
    entered, release = threading.Event(), threading.Event()
    def hold(bound):
        if bound.request_id == "old-inflight":
            entered.set()
            assert release.wait(5)
    try:
        if state == "preparing":
            env.pipeline.prepare_hook = hold
        else:
            task = env.submit("old-inflight")
            def execute(prepared, bound):
                hold(bound)
                from canaan_device_requests import DeviceExecutionResult
                return DeviceExecutionResult("succeeded", "fixture")
            env.pipeline.execute_hook = execute
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(env.submit, "old-inflight") if state == "preparing" else pool.submit(env.confirm, task)
            try:
                assert entered.wait(3)
                terminal = env.submit("new-terminal")
                assert env.confirm(terminal)["state"] == "succeeded"
                visible = env.broker.list_local(OWNER, limit=1)
                assert ids(visible) == ["old-inflight"] and visible[0]["state"] == state
            finally:
                release.set()
            future.result(timeout=3)
    finally:
        release.set()
        env.close()
