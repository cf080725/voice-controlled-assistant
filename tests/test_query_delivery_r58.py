"""Finite fake-turn/temp-ledger evidence; no listeners, models or native ports."""
from dataclasses import replace
import hashlib
import io
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from canaan_api_clients import ClientRegistry
from canaan_crypto import Cipher
from canaan_device_binding import DeviceBindingStore, DeviceTextRequestBinder, DeviceBindingError
from canaan_device_requests import DeviceTextRequestBroker, DeviceExecutionResult, PreparedDeviceTurn, DeviceRequestError
from canaan_device_gateway import DeviceTextGateway, DeviceGatewayError
from canaan_user_context import TurnContext
from canaan_query_answers import QueryDocument, QueryFragment, QueryExecutionResult, AnswerEntry, issue_answer, merge_prefix

OWNER = 'local-owner-r58-fake'


class Context:
    def __init__(self):
        self.current = TurnContext('owner', 1, 2, 'fake-session', SimpleNamespace(incognito=False),
                                   None, threading.Event(), 3)
    def sync(self): return self.current
    def valid(self, binding): return binding is not None and not binding.cancelled.is_set() and binding.token == self.current.token


class Pipeline:
    def __init__(self): self.prepares, self.calls, self.results = [], [], []
    def prepare(self, bound):
        self.prepares.append(bound.text)
        return PreparedDeviceTurn({'action': 'run_cmd', 'param': 'fake'}, ['fake'])
    def execute(self, prepared, bound):
        self.calls.append(bound.request_id)
        return self.results.pop(0)


@pytest.fixture
def env(tmp_path):
    registry = ClientRegistry(tmp_path / 'clients', OWNER)
    client, token = registry.pair('synthetic watch')
    context = Context()
    grants = DeviceBindingStore(tmp_path / 'grants.db')
    binder = DeviceTextRequestBinder(registry, context, grants, execution_host_id='fake-host')
    binder.register_local_binding(OWNER, client['id'])
    pipeline, cipher = Pipeline(), Cipher(b'R' * 32)
    path = tmp_path / 'ledger.db'
    broker = DeviceTextRequestBroker(path, binder, pipeline, cipher=cipher)
    value = SimpleNamespace(registry=registry, client=client, token=token, context=context, grants=grants,
                            binder=binder, pipeline=pipeline, cipher=cipher, path=path, broker=broker)
    yield value
    value.broker.close()
    grants.close()


def request(env, *, protocol=2, request_id='req1'):
    return env.broker.submit(env.token, dict(protocol_version=protocol, request_id=request_id, text='查询测试'))


def confirm(env, row):
    version = env.broker.get_local(OWNER, row['task_id'])['confirmation_version']
    return env.broker.confirm_local(OWNER, row['task_id'], expected_version=version)


def answer(step, *, large=False):
    target = 'E盘' if step % 2 else 'C盘'
    full = ('192.0.2.100\n' * 1024) if large else f'{target}剩余{step}GiB。'
    return issue_answer(source='run_cmd', kind='network' if large else 'drive_free', target=target,
                        summary=f'{target}第{step}次剩余{step}GiB', full_text=full,
                        payload={'rows': full.splitlines()}, summary_is_complete=not large)


def document(steps=32, *, large=False):
    return QueryDocument(steps, steps, tuple(range(1, steps+1)),
                         tuple(AnswerEntry(i, answer(i, large=large)) for i in range(1, steps+1)), 'completed')


def execution(doc, *, state='succeeded', reply='都办好了。', continuation=None):
    return DeviceExecutionResult(state, reply, continuation, query_document=doc)


def test_protocol2_opt_in_and_conflict_never_prepare_twice(env):
    first = request(env)
    assert first['protocol_version'] == 2
    assert request(env) == first
    with pytest.raises(DeviceRequestError) as ex:
        request(env, protocol=1)
    assert ex.value.code == 'request_conflict'
    assert env.pipeline.prepares == ['查询测试'] and env.pipeline.calls == []


def test_entire_32_summary_reaches_receipt_not_240(env):
    doc = document()
    assert len(doc.summary_reply()) > 240
    first = request(env, protocol=1)
    env.pipeline.results.append(execution(doc))
    result = confirm(env, first)
    if 'query_document' not in DeviceExecutionResult.__dataclass_fields__:
        assert result['reply'] == doc.summary_reply()
    else:
        assert result['reply'].startswith('旧版摘要')
        row = env.broker._row(first['task_id'])
        assert env.broker._stored_result(row)[1] == doc


def test_protocol2_long_summary_survives_reopen_and_same_id(env):
    doc = document()
    first = request(env)
    env.pipeline.results.append(execution(doc))
    receipt = confirm(env, first)
    assert receipt['reply'] == doc.summary_reply()
    env.broker.close()
    env.broker = DeviceTextRequestBroker(env.path, env.binder, env.pipeline, cipher=env.cipher)
    assert env.broker.get(env.token, first['task_id']) == receipt
    assert request(env) == receipt and env.pipeline.calls == ['req1']


def test_full_pages_reassemble_exact_typed_record_and_remain_read_only(env):
    doc = document(2, large=True)
    row = request(env)
    env.pipeline.results.append(execution(doc))
    confirm(env, row)
    manifest = env.broker.result_manifest(env.token, row['task_id'])
    assert manifest['document_bytes'] > 4096
    pages = [env.broker.result_page(env.token, row['task_id'], manifest['result_revision'], i)
             for i in range(manifest['page_count'])]
    assert ''.join(page['text'] for page in pages) == doc.canonical_record()
    for i, page in enumerate(pages):
        assert set(page) == set(manifest) | {'page_index', 'text', 'page_digest'}
        assert page['page_index'] == i and len(page['text']) <= 1024
        assert len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()) <= 8192
        assert page['page_digest'] == hashlib.sha256(page['text'].encode()).hexdigest()
        assert {k: v for k, v in page.items() if k in manifest} == manifest
    assert hashlib.sha256(''.join(p['text'] for p in pages).encode()).hexdigest() == manifest['document_digest']
    assert env.pipeline.prepares == ['查询测试'] and env.pipeline.calls == ['req1']


@pytest.mark.parametrize('change', ['revocation', 'session', 'grant', 'other_client'])
def test_current_access_is_rechecked_between_every_page(env, change):
    row = request(env)
    env.pipeline.results.append(execution(document(2, large=True)))
    confirm(env, row)
    manifest = env.broker.result_manifest(env.token, row['task_id'])
    env.broker.result_page(env.token, row['task_id'], manifest['result_revision'], 0)
    token = env.token
    if change == 'revocation': env.registry.revoke(env.client['id'])
    elif change == 'session': env.context.current = replace(env.context.current, session_id='other-session')
    elif change == 'grant': env.binder.register_local_binding(OWNER, env.client['id'])
    else:
        client, token = env.registry.pair('other fake')
        env.binder.register_local_binding(OWNER, client['id'])
    with pytest.raises((DeviceBindingError, DeviceRequestError)):
        env.broker.result_page(token, row['task_id'], manifest['result_revision'], 1)
    assert env.pipeline.calls == ['req1']


def test_owner_read_requires_actual_owner_and_unavailable_never_executes(env):
    row = request(env)
    with pytest.raises(DeviceBindingError):
        env.broker.result_manifest(env.token, row['task_id'], local_owner=True)
    with pytest.raises(DeviceRequestError) as ex:
        env.broker.result_manifest(OWNER, row['task_id'], local_owner=True)
    assert ex.value.code == 'result_unavailable' and not env.pipeline.calls


def test_confirmation_prefix_revision_merge_failure_and_stale_pages(env):
    a = AnswerEntry(1, answer(1))
    prefix = QueryDocument(3, 1, (1, 3), (a,), 'confirmation_required')
    row = request(env)
    continuation = PreparedDeviceTurn({'action': 'run_cmd', 'param': 'tail'}, ['tail'])
    env.pipeline.results.append(execution(prefix, state='awaiting_confirmation', reply='请确认待执行部分。', continuation=continuation))
    waiting = confirm(env, row)
    assert waiting['state'] == 'awaiting_confirmation'
    first = env.broker.result_manifest(env.token, row['task_id'])
    fragment = QueryFragment(2, 1, (2,), (), 'failed')
    merged = merge_prefix(prefix, fragment)
    env.pipeline.results.append(execution(merged, state='failed', reply='第二项失败，未执行末项。'))
    failed = confirm(env, row)
    second = env.broker.result_manifest(env.token, row['task_id'])
    assert failed['state'] == 'failed' and merged.entries == prefix.entries
    assert second['result_revision'] == first['result_revision'] + 1
    with pytest.raises(DeviceRequestError) as ex:
        env.broker.result_page(env.token, row['task_id'], first['result_revision'], 0)
    assert ex.value.code == 'stale_result' and env.pipeline.calls == ['req1', 'req1']
    assert env.broker._stored_result(env.broker._row(row['task_id']))[1] == merged


def test_restart_cancel_preserve_prefix_no_execution_reconstruction(env):
    prefix = QueryDocument(2, 1, (1, 2), (AnswerEntry(1, answer(1)),), 'confirmation_required')
    row = request(env)
    env.pipeline.results.append(execution(prefix, state='awaiting_confirmation', continuation=PreparedDeviceTurn({}, ['tail'])))
    confirm(env, row)
    manifest = env.broker.result_manifest(env.token, row['task_id'])
    env.broker.close()
    env.broker = DeviceTextRequestBroker(env.path, env.binder, env.pipeline, cipher=env.cipher)
    assert env.broker.get(env.token, row['task_id'])['state'] == 'unknown'
    assert env.broker.result_manifest(env.token, row['task_id']) == manifest
    assert confirm(env, row)['state'] == 'unknown' and env.pipeline.calls == ['req1']


def test_result_document_encrypted_only_in_original_reply_cell(env):
    doc = document(2)
    row = request(env)
    env.pipeline.results.append(execution(doc))
    confirm(env, row)
    with sqlite3.connect(env.path) as db:
        stored = db.execute('SELECT reply,reply_format,result_revision,state FROM device_text_requests').fetchone()
        columns = {r[1] for r in db.execute('PRAGMA table_info(device_text_requests)')}
    assert stored[0].startswith('enc1:') and doc.full_text() not in stored[0]
    assert stored[1:] == ('query_document_v1', 1, 'succeeded')
    assert not columns & {'document', 'query_document', 'result_json', 'full_result'}
    assert env.broker._stored_result(env.broker._row(row['task_id']))[1] == doc


def test_invalid_json_document_cannot_be_trusted_or_change_success(env):
    assert 'query_document' in DeviceExecutionResult.__dataclass_fields__
    row = request(env)
    env.pipeline.results.append(DeviceExecutionResult('succeeded', '完成。', query_document=document(2).to_record()))
    receipt = confirm(env, row)
    assert receipt['state'] == 'unknown'
    with pytest.raises(DeviceRequestError): env.broker.result_manifest(env.token, row['task_id'])
    assert env.pipeline.calls == ['req1']


def test_snapshot_write_failure_leaves_state_format_doc_revision_same_transaction(env):
    row = request(env)
    env.broker.db.execute("CREATE TRIGGER reject_doc BEFORE UPDATE OF reply_format ON device_text_requests WHEN NEW.reply_format='query_document_v1' BEGIN SELECT RAISE(ABORT,'fake write failure'); END")
    env.broker.db.commit()
    env.pipeline.results.append(execution(document(2)))
    result = confirm(env, row)
    assert result['state'] == 'unknown'
    stored = env.broker._row(row['task_id'])
    assert stored['reply_format'] == 'plain' and stored['result_revision'] == 0
    assert env.pipeline.calls == ['req1']


def test_capabilities_authenticated_read_no_bind_no_rows(env):
    gateway = DeviceTextGateway(env.registry, env.binder, env.broker)
    cap = gateway.route('GET', '/api/device-text/capabilities', {}, token=env.token)
    assert cap == env.broker.capabilities()
    with pytest.raises(DeviceGatewayError): gateway.route('GET', '/api/device-text/capabilities', {}, token='bad')
    assert not env.pipeline.prepares and env.broker.db.execute('SELECT COUNT(*) FROM device_text_requests').fetchone()[0] == 0


@pytest.mark.parametrize('suffix', ['01/0', '1/00', '1/-1', '1/1.0', '１/0', '1/2048'])
def test_gateway_rejects_noncanonical_or_out_of_range_page_paths(env, suffix):
    gateway = DeviceTextGateway(env.registry, env.binder, env.broker)
    row = request(env)
    env.pipeline.results.append(execution(document(2)))
    confirm(env, row)
    with pytest.raises(DeviceGatewayError):
        gateway.route('GET', f"/api/device-text/tasks/{row['task_id']}/result/{suffix}", {}, token=env.token)
    assert env.pipeline.calls == ['req1']


def test_v1_row_migration_keeps_fingerprint_and_existing_history(env):
    row = request(env, protocol=1)
    before = env.broker._row(row['task_id'])['fingerprint']
    env.broker.close()
    with sqlite3.connect(env.path) as db:
        db.execute('ALTER TABLE device_text_requests DROP COLUMN request_protocol')
        db.execute('ALTER TABLE device_text_requests DROP COLUMN reply_format')
        db.execute('ALTER TABLE device_text_requests DROP COLUMN result_revision')
    env.broker = DeviceTextRequestBroker(env.path, env.binder, env.pipeline, cipher=env.cipher)
    stored = env.broker._row(row['task_id'])
    assert stored['request_protocol'] == 1 and stored['fingerprint'] == before
    assert request(env, protocol=1)['task_id'] == row['task_id']
    assert env.pipeline.prepares == ['查询测试']


@pytest.fixture
def http(env, monkeypatch):
    import canaan_api_server as server
    captured = {}
    class FakeServer:
        def __init__(self, address, handler):
            captured['handler'] = handler
            self.server_port = 18989
        def serve_forever(self): raise AssertionError('Must not run server')
    class FakeThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(server, 'ThreadingHTTPServer', FakeServer)
    monkeypatch.setattr(server.threading, 'Thread', FakeThread)
    runtime = server.IntegrationRuntime.__new__(server.IntegrationRuntime)
    runtime.clients = env.registry
    runtime.device_gateway = DeviceTextGateway(env.registry, env.binder, env.broker)
    runtime.voices = SimpleNamespace(start=lambda: None)
    runtime.readiness = None
    runtime.start(port=0, capture=False)
    def call(path, token=env.token, *, headers=None, raw=b''):
        handler = captured['handler'].__new__(captured['handler'])
        handler.command, handler.path = 'GET', path
        handler.headers = {'Host': '127.0.0.1:18989', 'Authorization': 'Bearer ' + token,
                           'Content-Length': str(len(raw)), **(headers or {})}
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        status, output_headers = [], {}
        handler.send_response = lambda value: status.append(value)
        handler.send_header = lambda name, value: output_headers.__setitem__(name, value)
        handler.end_headers = lambda: None
        handler.handle_request()
        body = handler.wfile.getvalue()
        return status[0], json.loads(body), output_headers, body
    return call


def test_actual_http_handler_reads_authenticated_manifest_pages_and_bounds_bytes(env, http):
    row = request(env)
    env.pipeline.results.append(execution(document(2, large=True)))
    confirm(env, row)
    path = f"/api/device-text/tasks/{row['task_id']}/result"
    status, manifest, _, _ = http(path)
    assert status == 200
    for index in range(manifest['page_count']):
        status, page, headers, body = http(path + f"/{manifest['result_revision']}/{index}")
        assert status == 200 and page['task_id'] == row['task_id'] and len(body) <= 8192
        assert int(headers['Content-Length']) == len(body)
    assert http(path, token='bad')[0] == 401
    assert http(path.replace('/tasks/', '/review/'))[0] == 403
    assert http(path.replace('/tasks/', '/review/'), token=OWNER)[0] == 200
    assert env.pipeline.calls == ['req1']


@pytest.mark.parametrize('headers,raw', [({}, b'{}'), ({'Transfer-Encoding': 'chunked'}, b''),
                                        ({'Content-Length': '-1'}, b'')])
def test_actual_http_rejects_get_body_and_query_before_reading_result(env, http, headers, raw):
    assert http('/api/device-text/capabilities', headers=headers, raw=raw)[0] == 400
    assert http('/api/device-text/capabilities?confirmed=', raw=b'')[1]['error'] == 'invalid_query'
    assert not env.pipeline.prepares and not env.pipeline.calls
