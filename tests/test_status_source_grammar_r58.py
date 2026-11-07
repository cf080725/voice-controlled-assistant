"""Status source completeness: grammatical obligations are not evaluations."""
import copy
from types import SimpleNamespace as NS
import pytest
import canaan_intent as ci
import canaan_turn_pipeline as turns
from canaan_query_context import trusted_query_bindings,step_query_scope,current_query
from canaan_action_framework.actions.system_actions import SystemStatusAction
from canaan_action_framework.models import ActionContext
import canaan_action_framework.actions.system_actions as system

OBLIGATIONS = ('得改','该改','得排','该排','需改','须改','应改','愿改','欲改','敢改','肯改','待改',
               '不得改','不该改','无需改','未需改','不愿改','不敢改','不要改','还得改','应当改','必须改')

@pytest.mark.parametrize('suffix', OBLIGATIONS)
@pytest.mark.parametrize('shape', ('system_status','multi_action'))
def test_obligation_predicate_cannot_silently_drop_other_source(suffix,shape):
    text = 'CPU温度多少度，这份表格看起来' + suffix
    step={'action':'system_status','param':''}
    proposal={'action':shape,'param':'' if shape=='system_status' else [step], '_conf':.99,'_pconf':.99,
              'query_binding':{'trusted':True},'owned_text':'not authority','query':['temperature_cpu',[]]}
    fixed = ci.apply_command_guard(text,copy.deepcopy(proposal),require_confidence=True)
    assert fixed['action']=='chat' and not fixed.get('_pending')
    calls=[]
    def model(source):calls.append(source);return copy.deepcopy(proposal)
    state=turns.decide(text,None,None,[ci.fast_match_intent],model,lambda item:item,
        lambda source,item,is_llm:ci.apply_command_guard(source,item,require_confidence=is_llm))
    assert state['decision']['action']=='chat' and not state['decision'].get('_pending')
    assert trusted_query_bindings(text,[step]) == ()
    assert all(source==text for source in calls)

@pytest.mark.parametrize('connector',('接下来','紧接着','随后','接着','之后'))
def test_explicit_next_source_remains_unresolved(connector):
    text='CPU温度多少度，'+connector+'风扇听起来有些尖锐'
    proposal={'action':'system_status','param':'','_conf':.99,'_pconf':.99}
    assert ci.apply_command_guard(text,proposal,require_confidence=True)['action']=='chat'
    assert trusted_query_bindings(text,[proposal]) == ()

@pytest.mark.parametrize('text', (
    'CPU温度多少度，风扇听起来有些尖锐','我这会儿有些惴惴不安，CPU温度多少度',
    'CPU温度多少度，画面看起来很清晰','CPU温度多少度，这身穿着看起来很得体',
    'CPU温度多少度，这个安排看起来不得当','CPU温度多少度，这幅装饰看起来很应景',
    'CPU温度多少度，玩偶看起来有些可爱','CPU温度多少度，这个支架看起来很可靠',
    'CPU温度多少度，响声听起来悦耳','CPU温度多少度，毯子摸起来柔软',
))
def test_real_evaluation_preserves_rule_route_value_and_full_authority(text,monkeypatch):
    def no_model(*args):pytest.fail('Descriptive background added a model call')
    state=turns.decide(text,None,None,[ci.fast_match_intent],no_model,lambda item:item,
        lambda source,item,is_llm:ci.apply_command_guard(source,item,require_confidence=is_llm))
    assert state['decision']['action']=='system_status' and state['exec_source']=='rule'
    binding,=trusted_query_bindings(text,[state['decision']])
    assert binding.original_text==text and binding.query==('temperature_cpu',())
    reads=[]
    def temperatures():reads.append('temperature');return {'coretemp':[NS(label='CPU',current=62.5)]}
    monkeypatch.setattr(system,'psutil',NS(sensors_temperatures=temperatures))
    context=ActionContext('system_status',raw_param='',user_text=text)
    with step_query_scope(binding,step_index=0):
        assert current_query(context) is binding
        result=SystemStatusAction().execute(context)
    assert current_query(context) is None and context.user_text==text
    assert result.success and result.detail['temperature_c']==62.5 and reads==['temperature']
