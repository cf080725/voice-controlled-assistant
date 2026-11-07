import json
import sqlite3
import pytest
from test_query_delivery_r58 import env, request, confirm, document, execution, answer
from canaan_query_answers import QueryDocument, AnswerEntry


def test_presentation_cache_is_bounded_bytes_only_and_manifest_copy(env):
    for number in range(3):
        row = request(env, request_id='cache-'+str(number))
        env.pipeline.results.append(execution(document(2, large=True)))
        confirm(env, row)
        manifest = env.broker.result_manifest(env.token, row['task_id'])
        manifest['document_digest'] = 'bad-external-mutation'
        again = env.broker.result_manifest(env.token, row['task_id'])
        assert again['document_digest'] != manifest['document_digest']
        assert len(env.broker._result_cache) <= 2
        assert sum(sum(map(len, value[1])) for value in env.broker._result_cache.values()) <= 4194304
        assert all(type(page) is bytes for value in env.broker._result_cache.values() for page in value[1])
    assert len(env.broker._result_cache) == 2 and len(env.pipeline.calls) == 3
    env.broker.close()
    assert not env.broker._result_cache


def test_cache_hit_still_checks_live_metadata_and_current_access(env, monkeypatch):
    row = request(env)
    env.pipeline.results.append(execution(document(2)))
    confirm(env, row)
    manifest = env.broker.result_manifest(env.token, row['task_id'])
    def unexpected_full_restore(*args): raise AssertionError('Cache hit decrypted/restored the full document')
    monkeypatch.setattr(env.broker, '_stored_result', unexpected_full_restore)
    assert env.broker.result_page(env.token, row['task_id'], manifest['result_revision'], 0)['page_index'] == 0
    env.registry.revoke(env.client['id'])
    with pytest.raises(Exception): env.broker.result_page(env.token, row['task_id'], manifest['result_revision'], 0)
    assert env.pipeline.calls == ['req1']


def test_current_committed_revision_from_another_connection_invalidates_cached_snapshot(env):
    prefix = QueryDocument(2, 1, (1, 2), (AnswerEntry(1, answer(1)),), 'confirmation_required')
    row = request(env)
    env.broker._set(row['task_id'], 'awaiting_confirmation', '请确认。', query_document=prefix)
    old = env.broker.result_manifest(env.token, row['task_id'])
    completed = document(2)
    wrapper = json.dumps(dict(schema='canaan.device.query-reply.v1', reply=completed.summary_reply(),
                               document=completed.to_record(), result_revision=2), ensure_ascii=False)
    with sqlite3.connect(env.path) as connection:
        connection.execute('UPDATE device_text_requests SET reply=?,result_revision=2 WHERE id=?',
                           (env.broker._codec.encode(wrapper, 'reply'), row['task_id']))
    new = env.broker.result_manifest(env.token, row['task_id'])
    assert new['result_revision'] == 2 and new['document_digest'] != old['document_digest']
    with pytest.raises(Exception) as ex: env.broker.result_page(env.token, row['task_id'], 1, 0)
    assert getattr(ex.value, 'code', None) == 'stale_result'
    assert not env.pipeline.calls


def test_write_rollback_drops_cache_then_reloads_original_revision(env):
    prefix = QueryDocument(2, 1, (1, 2), (AnswerEntry(1, answer(1)),), 'confirmation_required')
    row = request(env)
    env.broker._set(row['task_id'], 'awaiting_confirmation', '请确认。', query_document=prefix)
    old = env.broker.result_manifest(env.token, row['task_id'])
    assert len(env.broker._result_cache) == 1
    env.broker.db.execute("CREATE TRIGGER reject_revision BEFORE UPDATE ON device_text_requests WHEN NEW.result_revision>OLD.result_revision BEGIN SELECT RAISE(ABORT,'fake rollback'); END")
    env.broker.db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        env.broker._set(row['task_id'], 'succeeded', '完成。', query_document=document(2))
    env.broker.db.rollback()
    assert not env.broker._result_cache
    assert env.broker.result_manifest(env.token, row['task_id']) == old
    assert not env.pipeline.calls
