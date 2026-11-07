"""Production owner recovery after grant/context invalidation; no candidate loader."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

def environment(path):
    from canaan_device_limits import DeviceAdmissionLimits
    from test_device_admission_r54 import Environment
    env = Environment(path, limits=DeviceAdmissionLimits(1, 1, 2))
    env.pipeline.question = True
    return env

def denied(call, status, code):
    import pytest
    from canaan_device_gateway import DeviceGatewayError
    with pytest.raises(DeviceGatewayError) as caught:
        call()
    assert (caught.value.status, caught.value.code) == (status, code)
    return caught.value.public()

def owner_cancel(env, task):
    from test_device_admission_r54 import OWNER
    view = env.route('GET', '/review/' + task['task_id'], token=OWNER)
    return env.route('POST', '/review/' + task['task_id'] + '/cancel', {'expected_version': view['confirmation_version']}, token=OWNER)

def test_revoked_grant_pending_is_recoverable_through_owner_gateway(tmp_path):
    from test_device_admission_r54 import OWNER
    env = environment(tmp_path)
    try:
        old = env.submit('old')
        env.route('POST', '/bindings/' + env.client['id'] + '/revoke', token=OWNER)
        denied(lambda: env.cancel(old), 403, 'grant_missing')
        denied(lambda: env.submit('new'), 403, 'grant_missing')
        env.route('POST', '/bindings', {'client_id': env.client['id']}, token=OWNER)
        busy = denied(lambda: env.submit('new'), 503, 'client_capacity')
        assert owner_cancel(env, old)['state'] == 'cancelled'
        assert owner_cancel(env, old)['state'] == 'cancelled'
        assert env.submit('new')['state'] == 'awaiting_input'
        denied(lambda: env.submit('old'), 409, 'request_conflict')
        assert env.pipeline.prepares == 2 and env.pipeline.executions == 0
    finally:
        env.close()

def test_changed_context_pending_is_recoverable_without_restoring_context(tmp_path):
    from test_device_admission_r54 import OWNER
    env = environment(tmp_path)
    try:
        old = env.submit('old')
        env.context.current = SimpleNamespace(**vars(env.context.current) | {'session_id': 'new-session'})
        denied(lambda: env.cancel(old), 403, 'context_changed')
        env.route('POST', '/bindings', {'client_id': env.client['id']}, token=OWNER)
        denied(lambda: env.cancel(old), 409, 'binding_changed')
        denied(lambda: env.submit('new'), 503, 'client_capacity')
        assert owner_cancel(env, old)['state'] == 'cancelled'
        assert env.submit('new')['state'] == 'awaiting_input'
        assert env.pipeline.prepares == 2 and env.pipeline.executions == 0
    finally:
        env.close()

def test_context_race_invalidates_answer_handle_but_owner_can_release_capacity(tmp_path, monkeypatch):
    from test_device_admission_r54 import OWNER
    env = environment(tmp_path)
    try:
        old = env.submit('old')
        original = env.binder.validate_bound

        def switch_then_validate(handle):
            env.context.current = SimpleNamespace(**vars(env.context.current) | {'session_id': 'raced-session'})
            return original(handle)
        monkeypatch.setattr(env.binder, 'validate_bound', switch_then_validate)
        denied(lambda: env.answer(old), 403, 'context_changed')
        monkeypatch.setattr(env.binder, 'validate_bound', original)
        assert not env.binder._issued and (not env.binder._pending_handles)
        assert env.snapshot()['answers'] == env.pipeline.answers == 0
        assert env.snapshot()['states'] == {'awaiting_input': 1}
        env.route('POST', '/bindings', {'client_id': env.client['id']}, token=OWNER)
        denied(lambda: env.submit('new'), 503, 'client_capacity')
        assert owner_cancel(env, old)['state'] == 'cancelled'
        assert env.submit('new')['state'] == 'awaiting_input'
        assert len(env.binder._pending_handles) == 1
    finally:
        env.close()

def test_revoked_client_pending_does_not_permanently_starve_other_clients(tmp_path):
    from test_device_admission_r54 import OWNER
    env = environment(tmp_path)
    try:
        old = env.submit('old')
        env.clients.revoke(env.client['id'])
        denied(lambda: env.cancel(old), 401, 'authentication_required')
        client, token = env.clients.pair('other fixture')
        env.route('POST', '/bindings', {'client_id': client['id']}, token=OWNER)
        denied(lambda: env.submit('new', token=token), 503, 'pending_capacity')
        view = env.route('GET', '/review/' + old['task_id'], token=OWNER)
        denied(lambda: env.route('POST', '/review/' + old['task_id'] + '/cancel', {'expected_version': view['confirmation_version']}, token=token), 403, 'local_owner_required')
        assert owner_cancel(env, old)['state'] == 'cancelled'
        assert env.submit('new', token=token)['state'] == 'awaiting_input'
        assert env.pipeline.executions == 0
    finally:
        env.close()

def test_two_prepare_slots_are_shared_with_answer_and_busy_is_nonmutating(tmp_path):
    from canaan_device_limits import DeviceAdmissionLimits
    from test_device_admission_r54 import Environment
    env = Environment(tmp_path, limits=DeviceAdmissionLimits(3, 3, 2))
    env.pipeline.question = True
    entered, release = (threading.Condition(), threading.Event())
    count = 0

    def hold(bound):
        nonlocal count
        with entered:
            count += 1
            entered.notify_all()
        assert release.wait(5)
    try:
        question = env.submit('question')
        env.pipeline.prepare_hook = hold
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(env.submit, 'held-' + str(index)) for index in range(2)]
            try:
                with entered:
                    assert entered.wait_for(lambda: count == 2, timeout=3)
                before = env.snapshot()
                denied(lambda: env.answer(question), 503, 'prepare_busy')
                assert env.snapshot() == before
                assert env.submit('held-0')['state'] == 'preparing'
                assert env.snapshot() == before
            finally:
                release.set()
            assert all((future.result(timeout=3)['state'] == 'awaiting_input' for future in futures))
        assert env.broker._active == env.broker._prepare_active == 0
    finally:
        release.set()
        env.close()
