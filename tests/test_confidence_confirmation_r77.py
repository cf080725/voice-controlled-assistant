"""Current model parsing, consent queue and executor stages; HTTP and actors are synthetic."""
import json
from types import SimpleNamespace

import pytest
import canaan_config as cfg
import canaan_events as events
import canaan_intent as ci
import canaan_os_executor as executor
import canaan_turn_pipeline as turns
from canaan_action_framework import ActionRegistry, BaseAction, ActionResult, PermissionLevel, VerificationResult
from canaan_execution_guard import execution_guard
from canaan_device_turn import prepare_hub_turn, TextTurnAborted


class Response:
    def __init__(self, body):
        self.body, self.closed = body, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self):
        return json.dumps(self.body, ensure_ascii=False).encode('utf-8')


class Flow:
    def __init__(self, actors, requests, responses, scripts):
        self.actors, self.requests, self.responses, self.scripts = actors, requests, responses, scripts
        self.clock = 100.0
        self.pending = ci.PendingConfirmation(ttl=30, clock=lambda: self.clock)
        self.memory = ci.ConversationMemory()
        self.callbacks = []
        self.history = []
        self.client = ci.create_compact_intent_client(cfg.INTENT_COMPACT_MODEL, local_only=True)

    def response(self, text='把音量调高五档', *, action='volume_up', param='5', fault='nan'):
        raw = json.dumps(dict(a=action, p=param), ensure_ascii=False, separators=(',', ':'))
        token = dict(token=raw, bytes=list(raw.encode('utf-8')), logprob=-.01)
        if fault == 'nan': token['logprob'] = float('nan')
        elif fault == 'inf': token['logprob'] = float('inf')
        elif fault == 'positive': token['logprob'] = .01
        elif fault == 'boolean': token['logprob'] = True
        elif fault == 'missing': token.pop('logprob')
        elif fault == 'wrong_text': token.update(token='x'*len(raw), bytes=list(('x'*len(raw)).encode()))
        elif fault == 'partial': token.update(token=raw[:8], bytes=list(raw[:8].encode()))
        elif fault == 'invalid_bytes': token['bytes'] = [True]
        elif fault not in ('valid', 'absent'): raise AssertionError(fault)
        self.scripts[text] = dict(message=dict(content=raw), logprobs=[] if fault == 'absent' else [token])
        return text

    def run(self, text, *, model_path=False, expected=None):
        kind, pending = (None, None) if model_path else self.pending.resolve(text, expected_item=expected)
        state = turns.decide(text, kind, pending, [] if model_path else [ci.fast_match_intent],
            lambda value: self.client.decide(value, on_action=self.callbacks.append),
            lambda value: ci.resolve_references(value, self.memory), ci.apply_command_guard,
            executor.cancel_reply, planner=self.client.complete_partial)
        decision = state['decision']
        result = None
        if decision['action'] == 'chat':
            if decision.get('_pending'):
                self.pending.set(decision['_pending'], state['exec_source'], state['exec_user_text'])
        else:
            result = executor.CanaanOSExecutor.execute(decision['action'], decision.get('param', ''),
                source=state['exec_source'], confirmed=state['confirmed'], user_text=state['exec_user_text'])
            if result.get('error_code') == 'CONFIRM_REQUIRED':
                self.pending.set(result.get('pending_decision') or decision, state['exec_source'], state['exec_user_text'])
        self.history.append(dict(text=text, model_path=model_path,
            action=decision['action'], param=decision.get('param'), guard=decision.get('_guard'),
            confidence=decision.get('_conf'), source=state['exec_source'], confirmed=state['confirmed'],
            original_text=state['exec_user_text'], pending=self.pending_summary(),
            result=None if result is None else dict(success=result.get('success'), code=result.get('error_code')),
            model_request_count=len(self.requests), actor_count=len(self.actors)))
        return state, result

    def pending_summary(self):
        item = self.pending.peek()
        if item is None:
            return None
        return dict(action=item['decision']['action'], param=item['decision'].get('param'),
            source=item['source'], text=item['user_text'], expires=item['expires'])

    def stale_boundary(self, expected):
        # Reach the real shared Hub boundary, which rejects before any channels
        # or storage access when this expected prompt has expired/replaced.
        binding = object()
        hub = SimpleNamespace(_context_valid=lambda value: value is binding)
        session = SimpleNamespace(device=True, pending=self.pending, binding=binding)
        try:
            return prepare_hub_turn(hub, '确认', binding=binding, ports={}, session=session, expected_pending=expected)
        except TextTurnAborted as exc:
            self.history.append(dict(shared_hub_boundary_rejection=exc.kind,
                pending=self.pending_summary(), model_request_count=len(self.requests), actor_count=len(self.actors)))
            raise


@pytest.fixture
def flow(monkeypatch, tmp_path):
    actors, requests, responses, scripts = [], [], [], {}
    hooks = []

    class SyntheticActor(BaseAction):
        def __init__(self, name, sensitive=False):
            self.name = name
            self.permission_level = PermissionLevel.SENSITIVE if sensitive else PermissionLevel.NORMAL_MUTATION
            self.confirm_even_from_rule = sensitive
            self.after_execute = None

        def pre_execute(self, context):
            hooks.append(('pre', self.name))

        def execute(self, context):
            actors.append(dict(action=self.name, param=context.param_value(), source=context.source,
                confirmed=context.confirmed, text=context.user_text))
            if self.after_execute:
                self.after_execute()
            return ActionResult(success=True, action=self.name, message='合成动作完成。')

        def verify(self, context):
            hooks.append(('verify', self.name))
            return VerificationResult(passed=True, verified=False, gate_name='SYNTHETIC_ACTOR')

        def post_execute(self, context, result):
            hooks.append(('post', self.name))

    registry = {name: SyntheticActor(name, name == 'close_app') for name in ('volume_up', 'volume_down', 'close_app')}
    monkeypatch.setattr(ActionRegistry, '_actions', registry)
    monkeypatch.setattr(ActionRegistry, '_alias_map', {name:name for name in registry})
    monkeypatch.setattr(ActionRegistry, '_middlewares', [])
    import canaan_action_framework.action_registry as registry_module
    monkeypatch.setattr(registry_module, 'AUDIT_LOG_PATH', str(tmp_path / 'audit.jsonl'))
    monkeypatch.delenv('CANAAN_DRY_RUN', raising=False)
    monkeypatch.setattr(ci, '_find_application', lambda name: 'notepad' if name in ('记事本', 'notepad') else '')
    monkeypatch.setattr(ci, '_known_application', lambda name: name in ('记事本', 'notepad'))
    monkeypatch.setattr(executor, 'time', SimpleNamespace(sleep=lambda seconds: None))
    published = []  # 只在测试里记下执行器发出的状态事件，不进真实分发队列
    monkeypatch.setattr(events, 'publish', lambda kind, *args, **kwargs: published.append((kind, *args, kwargs)))
    monkeypatch.setattr(events, 'published', published, raising=False)

    def request(req, **kwargs):
        payload = json.loads(req.data)
        assert req.full_url == 'http://127.0.0.1:11434/api/chat'
        assert payload['model'] == cfg.INTENT_COMPACT_MODEL == 'canaan-chat:3b'
        assert payload['options']['num_ctx'] == cfg.SHARED_NUM_CTX == 8192
        assert payload['format'] == ci.COMPACT_SCHEMA and payload['logprobs'] is True
        text = payload['messages'][-1]['content']
        requests.append(dict(text=text, **kwargs))
        assert text in scripts, 'Unexpected extra model classification'
        response = Response(scripts[text]);responses.append(response)
        return response

    monkeypatch.setattr(ci.urllib.request, 'urlopen', request)
    result = Flow(actors, requests, responses, scripts)
    result.hooks, result.registry = hooks, registry
    yield result
    assert all(response.closed for response in responses)
    (tmp_path / 'flow.json').write_text(json.dumps(dict(history=result.history, actors=actors,
        hooks=hooks, requests=requests, callbacks=result.callbacks, events=events.published,
        responses_closed=all(response.closed for response in responses)),
        ensure_ascii=False, indent=2, allow_nan=False)+'\n',encoding='utf-8')


@pytest.mark.parametrize('fault',['nan','inf','positive','boolean','missing','wrong_text','partial','invalid_bytes','absent'])
def test_unverifiable_model_output_requires_consent_before_real_dispatch(flow,fault):
    text=flow.response(fault=fault);state,result=flow.run(text,model_path=True)
    assert result is None and state['decision']['action']=='chat'
    assert state['decision']['_guard']=='low_confidence' and state['decision']['_conf'] is None
    item=flow.pending.peek()
    assert item and item['decision']['action']=='volume_up' and item['decision']['param']=='5'
    assert not flow.actors and not flow.hooks and not flow.callbacks and not events.published
    assert [request['text'] for request in flow.requests]==[text]


@pytest.mark.parametrize('consent',['确认','我确认','好','我确认执行'])
def test_consent_resumes_exact_original_without_another_model_request(flow,consent):
    text=flow.response();flow.run(text,model_path=True);item=flow.pending.peek()
    state,result=flow.run(consent,expected=item)
    assert result['success'] and state['confirmed'] and state['intent_source']=='confirm'
    assert flow.actors==[dict(action='volume_up',param='5',source='llm',confirmed=True,text=text)]
    assert len(flow.requests)==1 and flow.callbacks==[] and flow.pending.peek() is None
    assert flow.pending.resolve(consent,expected_item=item)==(None,None)


@pytest.mark.parametrize('cancel',['取消','算了','不用了'])
def test_cancellation_does_not_dispatch_or_requery(flow,cancel):
    text=flow.response();flow.run(text,model_path=True);item=flow.pending.peek()
    state,result=flow.run(cancel,expected=item)
    assert result is None and state['intent_source']=='cancel' and not state['confirmed']
    assert not flow.actors and not flow.hooks and len(flow.requests)==1 and flow.pending.peek() is None


def test_valid_metadata_still_executes_normal_mutation_once(flow):
    text=flow.response(fault='valid');state,result=flow.run(text,model_path=True)
    assert result['success'] and state['decision']['action']=='volume_up'
    assert flow.actors==[dict(action='volume_up',param='5',source='llm',confirmed=False,text=text)]
    assert flow.callbacks==['volume_up'] and not flow.pending.has_pending() and len(flow.requests)==1


@pytest.mark.parametrize('fault',['nan','valid'])
def test_unspecified_step_count_does_not_preserve_a_model_invented_number(flow,fault):
    text=flow.response('把声音调大一点',param='50',fault=fault)
    state,result=flow.run(text,model_path=True)
    decision=state['decision']
    if fault=='nan':
        assert result is None and not flow.actors
        assert flow.pending.peek()['decision']['param']==''
        _,result=flow.run('确认',expected=flow.pending.peek())
    else:
        assert decision['param']==''
    assert result['success'] and flow.actors[0]['param']=='' and flow.actors[0]['text']==text
    assert len(flow.actors)==1 and len(flow.requests)==1


def test_expired_expected_prompt_is_rejected_by_actual_shared_hub_boundary(flow):
    flow.run(flow.response(),model_path=True);item=flow.pending.peek();flow.clock+=30.01
    with pytest.raises(TextTurnAborted,match='过期或已变更'):
        flow.stale_boundary(item)
    assert not flow.actors and not flow.hooks and len(flow.requests)==1


def test_old_expected_prompt_does_not_consume_its_replacement(flow):
    flow.run(flow.response(),model_path=True);old=flow.pending.peek()
    flow.pending.set(dict(action='volume_down',param='3'),'rule','把音量调低三档')
    current=flow.pending.peek()
    with pytest.raises(TextTurnAborted,match='过期或已变更'):
        flow.stale_boundary(old)
    assert flow.pending.peek() is current and not flow.actors
    _,result=flow.run('确认',expected=current)
    assert result['success'] and flow.actors==[dict(action='volume_down',param='3',source='rule',confirmed=True,text='把音量调低三档')]
    assert len(flow.requests)==1


def test_execution_revocation_after_consent_stops_before_any_actor_hook(flow):
    flow.run(flow.response(),model_path=True);item=flow.pending.peek()
    with execution_guard(lambda:False):
        _,result=flow.run('确认',expected=item)
    assert result['error_code']=='EXECUTION_REVOKED' and not result['success']
    assert not flow.actors and not flow.hooks and flow.pending.peek() is None
    assert not any(event[0]=='success' for event in events.published)


def test_revocation_after_synthetic_effect_does_not_run_verify_or_claim_success(flow):
    flow.run(flow.response(),model_path=True);item=flow.pending.peek();valid=[True]
    flow.registry['volume_up'].after_execute=lambda:valid.__setitem__(0,False)
    with execution_guard(lambda:valid[0]):
        _,result=flow.run('确认',expected=item)
    assert result['error_code']=='EXECUTION_REVOKED' and not result['success']
    assert len(flow.actors)==1 and flow.hooks==[('pre','volume_up')]
    assert result['detail']['execution_started'] and result['detail']['effects_may_have_occurred']
    assert not any(event[0]=='success' for event in events.published)


@pytest.mark.parametrize('cancel_tail',[False,True])
def test_rule_plan_confirmation_preserves_only_unexecuted_tail(flow,cancel_tail):
    text='把声音调大一点，然后关闭记事本，然后把音量调低一点'
    state,first=flow.run(text)
    assert state['decision']['action']=='multi_action' and first['error_code']=='CONFIRM_REQUIRED'
    assert [row['action'] for row in flow.actors]==['volume_up']
    item=flow.pending.peek();remaining=json.loads(item['decision']['param'])
    assert [row['action'] for row in remaining]==['close_app','volume_down']
    _,last=flow.run('取消' if cancel_tail else '确认',expected=item)
    if cancel_tail:
        assert last is None and [row['action'] for row in flow.actors]==['volume_up']
    else:
        assert last['success'] and [row['action'] for row in flow.actors]==['volume_up','close_app','volume_down']
        assert flow.actors[1]['confirmed'] is True and flow.actors[2]['confirmed'] is False
    assert flow.pending.peek() is None and not flow.requests
