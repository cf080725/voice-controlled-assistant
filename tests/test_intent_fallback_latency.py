"""An unavailable primary must not spend the timeout budget on every voice turn."""
import io
import sys
from unittest.mock import Mock

import canaan_intent as intent
import pytest


@pytest.fixture
def client(monkeypatch):
    fallback = Mock()
    fallback.decide.return_value = {"action": "chat", "param": "", "reply": "备用回复", "_model": "backup"}
    fallback.warmup.return_value = "backup"
    c = intent.CompactIntentClient("primary", fallback=fallback)
    monkeypatch.setattr(c, "_query", Mock(side_effect=TimeoutError("primary stalled")))
    return c


def test_consecutive_turns_do_not_repeat_failed_primary_timeout(client, monkeypatch):
    monkeypatch.setattr(intent.time, "monotonic", lambda: 100.0)
    for text in ("有点累", "讲句话", "聊一会儿"):
        assert client.decide(text)["_model"] == "backup"
    assert client._query.call_count == 1
    assert client.fallback.decide.call_count == 3
    assert [c.args[0] for c in client.fallback.decide.call_args_list] == ["有点累", "讲句话", "聊一会儿"]


def test_primary_recovers_after_cooldown(client, monkeypatch):
    monkeypatch.setattr(intent.time, "monotonic", lambda: 100.0)
    client.decide("聊一会儿")
    monkeypatch.setattr(intent.time, "monotonic", lambda: 131.0)
    client._query.side_effect = None
    client._query.return_value = ('{"a":"chat","p":""}', [])
    assert client.decide("再聊会儿")["_model"] == "primary"
    assert client._query.call_count == 2
    assert client.decide("继续")["_model"] == "primary"
    assert client._query.call_count == 3


def test_warmup_fallback_prevents_first_turn_from_retrying_primary(client, monkeypatch):
    monkeypatch.setattr(intent.time, "monotonic", lambda: 100.0)
    assert client.warmup() == "backup"
    assert client.decide("你好")["_model"] == "backup"
    assert client._query.call_count == 1


def test_low_confidence_is_still_confirmed_instead_of_trying_other_models(client):
    client._query.side_effect = None
    client._query.return_value = ('{"a":"lock_screen","p":""}', [])
    result = client.decide("锁屏")
    assert result["action"] == "chat" and result["_pending"]["action"] == "lock_screen"
    client.fallback.decide.assert_not_called()


def test_successful_warmup_does_not_fallback_because_of_windows_console(client, monkeypatch):
    client._query.side_effect = None
    client._query.return_value = '{"a":"chat","p":""}'
    with io.TextIOWrapper(io.BytesIO(), encoding="gbk") as console:
        monkeypatch.setattr(sys, "stdout", console)
        assert client.warmup() == "primary"
        client.fallback.warmup.assert_not_called()
