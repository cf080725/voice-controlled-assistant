"""Synthetic standard-library-only ABI and resource proof."""
from contextvars import copy_context
import hashlib
import json
import pytest
import canaan_query_answers as q
OBSERVATIONS = {}


def answer(target='E', value=100, **changes):
    fields = dict(source='run_cmd', kind='drive_free', target=target,
                  summary=f'{target}盘余{value}GiB。', full_text=f'{target}盘剩余{value}GiB。',
                  payload={'drive': target, 'free_bytes': value * 1024 ** 3}, summary_is_complete=True)
    return q.issue_answer(**(fields | changes))


def fragment(steps, expected, values, stop):
    collector = q.AnswerCollector(segment_steps=steps, expected_query_steps=expected)
    for index, value in enumerate(values, 1):
        with collector.step(index, value.source if value else 'open_app') as step:
            if value:
                assert q.publish_answer(value)
            step.commit()
    return collector.finish(stop)


def test_all_32_samples_order_and_lossless_canonical_record_roundtrip():
    doc = q.merge_prefix(None, fragment(32, tuple(range(1,33)), [answer('E' if i%2 else 'C',1000+i) for i in range(32)], 'completed'))
    assert [e.answer.payload_json for e in doc.entries] == [answer('E' if i%2 else 'C',1000+i).payload_json for i in range(32)]
    assert q.from_trusted_record(doc.to_record()) == doc
    assert json.loads(''.join(p.text for p in doc.pages())) == doc.to_record()
    assert doc.compact_reply(24) is None
    assert all(str(1000+i) in doc.summary_reply() for i in range(32))


def test_confirmation_prefix_keeps_queries_and_nonquery_offset_through_resume():
    prefix = q.merge_prefix(None, fragment(5, (1,4), [answer(),None], 'confirmation_required'))
    assert prefix.completed_steps == 2
    resumed = q.merge_prefix(prefix, fragment(3, (2,), [None,answer('C',200),None], 'completed'))
    assert resumed.completed_steps == resumed.plan_steps == 5
    assert [e.step for e in resumed.entries] == [1,4]
    assert resumed.compact_reply(24) == 'E盘余100GiB，C盘余200GiB。'


def test_empty_future_query_prefix_and_mutation_only_suffix_are_not_lost():
    empty = q.merge_prefix(None, fragment(4,(3,),[None], 'confirmation_required'))
    assert not empty.entries and empty.completed_steps == 1
    middle = q.merge_prefix(empty, fragment(3,(2,),[None,answer()], 'confirmation_required'))
    end = q.merge_prefix(middle, fragment(1,(),[None], 'completed'))
    assert end.completed_steps == 4 and end.entries[0].step == 3
    assert q.merge_prefix(None, fragment(2,(),[None,None], 'completed')) is None


def test_failure_discards_current_answer_but_keeps_prior_answers():
    collector = q.AnswerCollector(segment_steps=3,expected_query_steps=(1,2,3))
    with collector.step(1,'run_cmd') as step:
        q.publish_answer(answer())
        step.commit()
    with collector.step(2,'run_cmd'):
        q.publish_answer(answer('C',200))
    doc = q.merge_prefix(None, collector.finish('failed'))
    assert len(doc.entries) == 1 and doc.completed_steps == 1 and doc.stop_reason == 'failed'
    with pytest.raises(q.AnswerError):
        q.merge_prefix(doc, fragment(2,(1,2),[answer(),answer()], 'completed'))


def test_resume_rejects_changed_suffix_length_or_query_positions():
    prefix = q.merge_prefix(None, fragment(4,(1,4),[answer()], 'confirmation_required'))
    for suffix in (fragment(2,(2,),[], 'failed'), fragment(3,(1,),[], 'failed')):
        with pytest.raises(q.AnswerError): q.merge_prefix(prefix,suffix)


def test_plain_json_flags_and_subclasses_cannot_issue_carrier_or_answers():
    fake = {'query_document': {'answers': [100]}, 'success':True}
    assert q.get_query_document(fake) is None and not q.publish_answer(fake)
    class Fake(q.QueryExecutionResult): pass
    doc = q.merge_prefix(None, fragment(1,(1,),[answer()], 'completed'))
    assert q.get_query_document(Fake(fake,doc)) is None
    with pytest.raises(q.AnswerError): q.QueryExecutionResult(fake,doc.to_record())


def test_json_only_sees_original_keys_and_hub_clone_preserves_typed_object():
    doc = q.merge_prefix(None,fragment(1,(1,),[answer()], 'completed'))
    result = q.QueryExecutionResult({'success':True,'detail':{'ordinary':'JSON'}},doc)
    assert json.loads(json.dumps(result)) == {'success':True,'detail':{'ordinary':'JSON'}}
    assert q.get_query_document({**result}) is None
    cloned = q.clone_execution_result(result,message='original')
    assert q.get_query_document(cloned) is doc
    assert json.loads(json.dumps(cloned))['message'] == 'original'


def test_records_are_independent_and_strict():
    doc=q.merge_prefix(None,fragment(1,(1,),[answer()], 'completed'))
    data=doc.to_record()
    data['answers'][0]['payload']['free_bytes']=5
    assert doc.entries[0].answer.payload_json != q.canonical_json(data['answers'][0]['payload'])
    for record in (doc.to_record() | {'external':True}, doc.to_record() | {'completed_steps':True}):
        with pytest.raises(q.AnswerError): q.from_trusted_record(record)
    record=doc.to_record()
    record['answers'][0]['step']=True
    with pytest.raises(q.AnswerError): q.from_trusted_record(record)


def test_nested_and_late_context_and_precreated_scope_do_not_duplicate():
    outer=q.AnswerCollector(segment_steps=1,expected_query_steps=(1,))
    stale=outer.step(1,'run_cmd')
    with outer.step(1,'run_cmd') as step:
        copied=copy_context()
        inner=q.merge_prefix(None,fragment(1,(1,),[answer('C',200)], 'completed'))
        q.publish_answer(answer())
        step.commit()
    with pytest.raises(q.AnswerError): copied.run(q.publish_answer,answer())
    with pytest.raises(q.AnswerError):
        with stale: pass
    assert inner.entries[0].answer.target == 'C'
    assert len(outer.finish('completed').entries)==1


def test_32_times_1024_ipv4_complete_typed_data_fits_2mib_and_pages():
    # Deliberately includes 1024 addresses per step, beyond table header overhead.
    addresses=[f'255.255.{252+i//256}.{i%256}' for i in range(1024)]
    values=[answer(kind='ipv4_table',target=f'IPv4 sample {i+1}',
                   summary='255.255.252.0等1024个地址。',
                   full_text='本机IPv4地址：'+'、'.join(addresses),
                   payload={'ipv4_addresses':addresses,'interface_preference_selected':False},summary_is_complete=False)
            for i in range(32)]
    doc=q.merge_prefix(None,fragment(32,tuple(range(1,33)),values,'completed'))
    raw=doc.canonical_record().encode('utf-8')
    pages=doc.pages()
    envelope={'task_id':'t'*128,'request_id':'r'*128,'result_revision':2**63-1,
              'digest':hashlib.sha256(raw).hexdigest(),'content':'record','schema':'canaan.result.page.v2'}
    sizes=[len(q.encode_page(page,envelope=envelope)) for page in pages]
    assert len(raw)<=q.DOCUMENT_BYTES and len(pages)<=q.MAX_PAGES and max(sizes)<=q.PAGE_JSON_BYTES
    assert ''.join(page.text for page in pages)==doc.canonical_record()
    assert all(len(e.answer.payload_json)>10000 for e in doc.entries)
    OBSERVATIONS['max_ipv4']={'steps':32,'addresses_each':1024,'document_utf8_bytes':len(raw),
                             'pages':len(pages),'max_encoded_page_bytes':max(sizes),'record_roundtrip':True}


def test_unicode_scalar_utf16_and_actual_json_escaping_bounds():
    # Non-BMP scalars count once per page and twice in the wire reply budget.
    text=('😀中\\\"\n\t\b\f\r\x01')*100
    doc=q.merge_prefix(None,fragment(1,(1,),[answer(full_text=text,payload={'unicode':text})],'completed'))
    pages=doc.pages()
    assert all(len(p.text)<=1024 and len(q.encode_page(p))<=8192 for p in pages)
    assert ''.join(p.text for p in pages)==doc.canonical_record()
    assert q.utf16_units('😀'*2048)==4096 and q.utf16_units('😀'*2049)==4098
    assert q.encode_page(q.QueryPage(0,1,'\x01'*1024),envelope={'task_id':'中'*128,'request_id':'😀'*128,'digest':'f'*64,'result_revision':2**63-1})
    with pytest.raises(q.AnswerError): q.encode_page(q.QueryPage(0,1,'x'),envelope={'huge':'x'*8192})
    with pytest.raises(q.AnswerError): answer(full_text='\ud800')
    OBSERVATIONS['unicode']={'astral_reply_2048_units':4096,'control_scalar_max_page_bytes':len(q.encode_page(q.QueryPage(0,1,'\x01'*1024),envelope={'task_id':'中'*128,'request_id':'😀'*128,'digest':'f'*64,'result_revision':2**63-1}))}


def test_exact_document_byte_limit_and_one_over_fail_without_truncation():
    base=answer(payload={'padding':''})
    initial=q.merge_prefix(None,fragment(1,(1,),[base],'completed'))
    overhead=len(initial.canonical_record().encode('utf-8'))
    count=q.DOCUMENT_BYTES-overhead
    doc=q.merge_prefix(None,fragment(1,(1,),[answer(payload={'padding':'x'*count})],'completed'))
    assert len(doc.canonical_record().encode('utf-8'))==q.DOCUMENT_BYTES
    pages=doc.pages()
    assert len(pages)<=q.MAX_PAGES and ''.join(p.text for p in pages)==doc.canonical_record()
    with pytest.raises(q.AnswerError):
        q.merge_prefix(None,fragment(1,(1,),[answer(payload={'padding':'x'*(count+1)})],'completed'))
    OBSERVATIONS['exact_document_limit']={'utf8_bytes':q.DOCUMENT_BYTES,'pages':len(pages),'one_byte_over_rejected':True}


def test_incomplete_row_summary_is_explicit_and_never_claims_compact_completeness():
    doc=q.merge_prefix(None,fragment(1,(1,),[answer(summary='首地址等1024个地址。',summary_is_complete=False)],'completed'))
    assert doc.compact_reply(24) is None
    assert doc.summary_reply()=='首地址等1024个地址。'
