"""A remaining-RAM question must reach the real read-only actor and measured answer."""
import json
from types import SimpleNamespace as NS

import pytest

import canaan_intent as ci
import canaan_status_query as sq
import canaan_turn_pipeline as turns
import canaan_os_executor as executor
import canaan_query_answers as answers
from canaan_action_framework import ActionRegistry
from canaan_action_framework.actions import system_actions as system
from canaan_query_context import trusted_query_bindings, current_query, step_query_scope
from canaan_action_framework.models import ActionContext


POSITIVE = [
    "电脑还剩多少内存？", "电脑现在还剩多少内存", "这台电脑还有多少内存？",
    "本机剩余多少内存", "系统目前还有多少内存", "内存还剩多少？",
    "内存剩余多少", "内存还有多少", "现在内存还剩多少", "可用内存有多少",
    "剩余内存有多少？", "查一下可用内存", "帮我看看剩余内存",
    "查看电脑剩余内存", "电脑的可用内存是多少？", "电脑还有多少可用内存？",
    "帮我查一下当前可用内存", "现在还有多少运行内存", "告诉我可用内存",
]
NEGATIVE = [
    "内存", "内存有什么作用？", "内存是什么意思？", "内存有多大？", "总内存多少？",
    "电脑还剩多少内存才够用？", "电脑还剩多少内存才不卡？",
    "如何查询电脑还剩多少内存？", "怎么查看可用内存？", "教程：电脑还剩多少内存",
    "不要查电脑还剩多少内存", "别看可用内存", "如果电脑还剩多少内存",
    "朋友说电脑还剩多少内存", "手机还剩多少内存？", "手表还剩多少内存？",
    "网上搜索电脑还剩多少内存", "搜索“电脑还剩多少内存”", "“电脑还剩多少内存？”",
    "打开文件“电脑还剩多少内存.txt”",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_specific_remaining_memory_wording_is_selected(text):
    assert sq.query_kind(text) == ("memory_available", ())


@pytest.mark.parametrize("text", POSITIVE)
def test_rule_guard_and_query_binding_agree_on_the_original_question(text):
    def no_model(*args):
        pytest.fail("A complete supported memory query was sent to the model")
    state = turns.decide(text, None, None, [ci.fast_match_intent], no_model, lambda value: value,
        lambda source, decision, llm: ci.apply_command_guard(source, decision, require_confidence=llm))
    decision = state["decision"]
    assert decision["action"] == "system_status" and decision["param"] == ""
    assert state["exec_source"] == "rule" and state["exec_user_text"] == text
    assert not state["confirmed"] and not decision.get("_pending")
    binding, = trusted_query_bindings(text, [decision])
    assert binding.original_text == text and binding.query == ("memory_available", ())
    assert binding.identity == ("specific", "memory_available", ())
    assert "_conf" not in decision and "_pconf" not in decision


@pytest.mark.parametrize("text", NEGATIVE)
def test_topics_other_devices_literals_and_instruction_data_are_not_local_ram_queries(text):
    assert sq.query_kind(text) != ("memory_available", ())
    decision = ci.fast_match_intent(text)
    assert not decision or decision["action"] != "system_status", decision


@pytest.fixture
def memory_actor(monkeypatch):
    actor = system.SystemStatusAction()
    monkeypatch.setattr(ActionRegistry, "_actions", {"system_status": actor})
    monkeypatch.setattr(ActionRegistry, "_alias_map", {name: "system_status" for name in
        ("system_status", *actor.aliases)})
    monkeypatch.setattr(ActionRegistry, "_middlewares", [])
    monkeypatch.setattr(ActionRegistry, "_audit_log", lambda *args, **kwargs: None)
    reads = []
    state = dict(total=16*1024**3, available=3*1024**3)
    def memory():
        reads.append("virtual_memory")
        return NS(**state)
    def wrong_read(*args, **kwargs):
        pytest.fail("Remaining RAM queried an unrelated CPU/battery/disk/process provider")
    monkeypatch.setattr(system, "psutil", NS(virtual_memory=memory, cpu_percent=wrong_read,
        sensors_battery=wrong_read, disk_usage=wrong_read, process_iter=wrong_read))
    return state, reads


def execute(text="电脑还剩多少内存？"):
    decision = ci.apply_command_guard(text, ci.fast_match_intent(text))
    assert decision["action"] == "system_status", decision
    result = executor.CanaanOSExecutor.execute("system_status", "", source="rule", user_text=text)
    return decision, result


def test_exact_rule_registry_executor_answer_document_and_speech_use_measured_bytes(memory_actor):
    state, reads = memory_actor
    decision, result = execute()
    assert result["success"] and reads == ["virtual_memory"]
    document = answers.get_query_document(result)
    assert document is not None and len(document.entries) == 1 and document.completed_steps == 1
    answer = document.entries[0].answer
    payload = json.loads(answer.payload_json)
    assert answer.source == "system_status" and answer.kind == "memory_available"
    assert answer.target == "可用内存"
    assert payload == dict(query="memory_available", complete=True,
        available_bytes=state["available"], total_bytes=state["total"])
    assert str(state["available"]) in answer.full_text
    spoken = turns.spoken_result("system_status", decision, result, "rule")
    assert "可用内存3.0GiB" in spoken and "CPU" not in spoken and "占用" not in spoken
    assert "亲爱的" not in spoken


def test_each_fresh_question_reads_fresh_memory_without_cached_or_generated_amounts(memory_actor):
    state, reads = memory_actor
    _, first = execute()
    state["available"] = 512*1024**2
    decision, second = execute()
    assert reads == ["virtual_memory", "virtual_memory"]
    first_payload = json.loads(answers.get_query_document(first).entries[0].answer.payload_json)
    second_payload = json.loads(answers.get_query_document(second).entries[0].answer.payload_json)
    assert first_payload["available_bytes"] == 3*1024**3
    assert second_payload["available_bytes"] == 512*1024**2
    assert "512.0MiB" in turns.spoken_result("system_status", decision, second, "rule")


@pytest.mark.parametrize("available,expected", [
    (0, "0字节"), (1, "1字节"), (1023, "1023字节"), (1024, "1.0KiB"),
    (2**20, "1.0MiB"), (3*2**30, "3.0GiB"), (2**40, "1.0TiB"),
    (2**50, "1.0PiB"), (2**60, "1.0EiB"), (2**64-1, "16.0EiB"),
])
def test_available_amounts_keep_exact_bytes_and_short_human_readable_speech(available, expected):
    result = sq.answer_query(("memory_available", ()), NS(virtual_memory=lambda: NS(total=max(1, available), available=available)))
    assert result.success and result.detail["available_bytes"] == available
    assert expected in result.speech and len(result.speech) <= 24
    assert str(available) in result.message


INVALID = [None, True, False, "123", -1, 1.0, float("nan"), float("inf"), 2**64]


@pytest.mark.parametrize("field", ["total", "available"])
@pytest.mark.parametrize("invalid", INVALID)
def test_invalid_byte_counters_are_unavailable_and_never_publish_an_answer(memory_actor, field, invalid):
    state, reads = memory_actor
    state[field] = invalid
    _, result = execute()
    assert result["success"] is False and result["error_code"] == "STATUS_UNAVAILABLE"
    document = answers.get_query_document(result)
    assert document is None or not document.entries
    assert reads == ["virtual_memory"]


@pytest.mark.parametrize("total,available", [(0,0),(100,101),(1000,2000)])
def test_impossible_memory_totals_or_remaining_amounts_fail(total, available):
    result = sq.answer_query(("memory_available", ()), NS(virtual_memory=lambda: NS(total=total, available=available)))
    assert not result.success and result.error_code == "STATUS_UNAVAILABLE"
    assert result.detail == dict(query="memory_available", complete=False)


@pytest.mark.parametrize("fields", [dict(total=10), dict(available=5), {}])
def test_missing_memory_fields_do_not_fall_back_to_percent(fields):
    result = sq.answer_query(("memory_available", ()), NS(virtual_memory=lambda: NS(percent=50, **fields)))
    assert not result.success and result.error_code == "STATUS_UNAVAILABLE"


def test_provider_failure_is_explicit_and_not_retried():
    calls = []
    def fail():
        calls.append(1)
        raise OSError("Synthetic unavailable sensor")
    result = sq.answer_query(("memory_available", ()), NS(virtual_memory=fail))
    assert result.error_code == "STATUS_UNAVAILABLE" and calls == [1]
    assert "Synthetic unavailable sensor" not in result.message


@pytest.mark.parametrize("mode", ["before", "during", "timeout", "backwards"])
def test_cancellation_and_time_budget_never_publish_a_reading(mode):
    instant, calls = [10.0], []
    def read():
        calls.append(1)
        if mode == "timeout":
            instant[0] += 3
        if mode == "backwards":
            instant[0] -= 1
        return NS(total=100, available=50)
    result = sq.answer_query(("memory_available", ()), NS(virtual_memory=read),
        clock=lambda: instant[0], should_stop=lambda: mode == "before" or mode == "during" and bool(calls))
    expected = "STATUS_CANCELLED" if mode in ("before","during") else "STATUS_TIMEOUT" if mode == "timeout" else "STATUS_INCOMPLETE"
    assert not result.success and result.error_code == expected
    assert calls == ([] if mode == "before" else [1])
    assert "available_bytes" not in result.detail


def test_extra_query_targets_cannot_change_the_memory_sensor():
    calls = []
    result = sq.answer_query(("memory_available", ("some other machine",)), NS(virtual_memory=lambda: calls.append(1)))
    assert result.error_code == "STATUS_UNAVAILABLE" and calls == []


@pytest.mark.parametrize("tail", ["把音量调到", "然后随便处理点事情", "接着把那份文件处理好"])
def test_an_unresolved_second_request_cannot_be_replaced_by_just_the_memory_read(tail):
    text = "电脑还剩多少内存，" + tail
    proposal = dict(action="system_status", param="", _conf=.99, _pconf=.99)
    guarded = ci.apply_command_guard(text, proposal, require_confidence=True)
    assert guarded["action"] == "chat"
    assert trusted_query_bindings(text, [proposal]) == ()


def test_original_memory_query_repairs_an_unrelated_synthetic_model_command():
    decision = ci.apply_command_guard("电脑还剩多少内存？", dict(action="run_cmd",
        param="Get-NetIPAddress -AddressFamily IPv4 | Get-NetIPAddress -AddressFamily IPv6",
        _conf=.99, _pconf=.99), require_confidence=True)
    assert decision["action"] == "system_status" and decision["param"] == "" and not decision.get("_pending")
    assert not answers.get_query_document(decision)
    assert decision["_guard_rule"] == "query_domain"
    assert decision["_model_observation"]["action"] == "run_cmd"
    assert "_conf" not in decision and "_pconf" not in decision


def test_source_scope_is_not_overridden_by_a_different_text_or_client_query_metadata(memory_actor):
    decision = ci.fast_match_intent("电脑还剩多少内存？")
    binding, = trusted_query_bindings("电脑还剩多少内存？", [decision])
    context = ActionContext("system_status", raw_param="", source="rule", user_text="CPU温度多少度")
    with step_query_scope(binding, step_index=0):
        with pytest.raises(ValueError):
            current_query(context)
