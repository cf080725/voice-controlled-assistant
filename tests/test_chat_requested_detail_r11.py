"""Requested answer budgets use fake chunks; no model or private context is read."""

import canaan_companion as companion
import pytest


def _fake_stream(monkeypatch, prompt, *, configured=2, chunks=(), guard=False):
    monkeypatch.setattr(companion._cfg, "CHAT_MAX_SENTENCES", configured)
    chat = companion.CompanionChat()
    monkeypatch.setattr(
        chat, "_stream", lambda *args, **kwargs: companion.ChatStream(iter(chunks), guard=guard)
    )
    return chat.stream(prompt)


@pytest.mark.parametrize("prompt", [
    "请把半小时分三段安排学习。",
    "请分成三段安排半小时学习。",
    "请分为三步讲解。",
    "给三个方法。",
    "给我三个建议。",
    "帮我列出3条要点。",
])
@pytest.mark.parametrize("guard", [False, True])
def test_three_requested_parts_survive_a_short_opener(monkeypatch, prompt, guard):
    pieces = ["可以，按这个安排。", "先预习十分钟。", "再练习十分钟。", "最后复习十分钟。"]
    stream = _fake_stream(monkeypatch, prompt, chunks=pieces, guard=guard)
    assert "".join(stream.iter_text()) == "".join(pieces)
    assert stream.full_text == "".join(pieces)


@pytest.mark.parametrize("prompt,budget", [
    ("用两步说明。", 3),
    ("给出2个方法。", 3),
    ("介绍四个办法。", 5),
    ("提供4条建议。", 5),
    ("请给出五个方法。", 6),
    ("分5段回答。", 6),
    ("给我三个简单的方法。", 4),
    ("给我三种方法。", 4),
    ("不要分三段，给我两个方法。", 3),
])
def test_only_bounded_requested_parts_expand_budget(monkeypatch, prompt, budget):
    assert _fake_stream(monkeypatch, prompt).max_sentences == budget


@pytest.mark.parametrize("prompt", [
    "怎么安排30分钟学习？",
    "我有3本书，应该先看哪本？",
    "给我三本书的建议。",
    "三步上篮怎么练？",
    "先看两个方法，再讨论。",
    "不要分三段。",
    "不分三段。",
    "不用三步。",
    "请不要分成四段。",
    "不必给我三个建议。",
    "不要只用一句话。",
    "不用一句话，讲详细些。",
    "给我1个方法。",
    "给我六个方法。",
    "给我30个方法。",
    "给我3.5个方法。",
    "给我十三个方法。",
    "你好，今天很累。",
])
def test_normal_numbers_and_negated_requests_keep_configured_budget(monkeypatch, prompt):
    assert _fake_stream(monkeypatch, prompt).max_sentences == 2


@pytest.mark.parametrize("configured", [0, 2, 9])
@pytest.mark.parametrize("instruction,budget", [
    ("只用一句话", 1),
    ("用两句话回答", 2),
    ("最多3句", 3),
    ("不超过两句话", 2),
])
def test_explicit_sentence_limit_has_priority(monkeypatch, configured, instruction, budget):
    stream = _fake_stream(
        monkeypatch, f"给我三个建议，{instruction}。", configured=configured,
        chunks=["先喝水。", "再休息。", "最后散步。", "记得早点睡。"],
    )
    assert stream.max_sentences == budget
    assert "".join(stream.iter_text()) == "".join(
        ["先喝水。", "再休息。", "最后散步。", "记得早点睡。"][:budget]
    )


@pytest.mark.parametrize("prompt", ["今天很累。", "给我三个建议。"])
def test_unlimited_configuration_stays_unlimited_without_sentence_limit(monkeypatch, prompt):
    assert _fake_stream(monkeypatch, prompt, configured=0).max_sentences == 0


def test_large_configuration_is_not_lowered_by_detail_rule(monkeypatch):
    assert _fake_stream(monkeypatch, "给我三个建议。", configured=9).max_sentences == 9


def test_detail_budget_does_not_read_ahead_before_first_output(monkeypatch):
    consumed = []

    def chunks():
        consumed.append("first")
        yield "可以，按这个安排。"
        consumed.append("second")
        yield "先预习十分钟。"

    stream = _fake_stream(monkeypatch, "分三段安排半小时学习。", chunks=chunks())
    output = stream.iter_text()
    assert next(output) == "可以，按这个安排。"
    assert consumed == ["first"]
    output.close()
