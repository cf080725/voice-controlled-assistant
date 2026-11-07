"""Cold startup warms the real prompt and reuses a working fallback."""
import io
import json
import sys

import canaan_companion as cc
import pytest


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        yield json.dumps({"message": {"content": "你好。"}, "done": True}).encode()


def test_successful_warmup_is_not_lost_on_windows_gbk_console(monkeypatch):
    monkeypatch.setattr(cc.urllib.request, "urlopen", lambda *a, **k: Response())
    with io.TextIOWrapper(io.BytesIO(), encoding="gbk") as console:
        monkeypatch.setattr(sys, "stdout", console)
        chat = cc.CompanionChat(model="primary", fallback_model="backup")
        assert chat.warmup() == "primary"


def test_warmup_prefills_real_system_prompt_without_private_retrieval(monkeypatch):
    requests = []

    def request(req, timeout):
        requests.append((json.loads(req.data), timeout))
        return Response()

    monkeypatch.setattr(cc.urllib.request, "urlopen", request)
    monkeypatch.setattr(cc, "load_persona_prompt", lambda: "你是测试助手，先给出具体答案。")
    chat = cc.CompanionChat(timeout=3)
    monkeypatch.setattr(chat, "build_messages", lambda *a, **k: pytest.fail("Warmup queried private retrieval"))
    assert chat.warmup() == chat.model
    assert requests[0][0]["messages"][0] == {"role": "system", "content": cc.load_persona_prompt()}
    assert requests[0][0]["options"]["num_predict"] == 1
    assert requests[0][1] >= 60
    assert "".join(chat._chunks_one(chat.model, [{"role": "user", "content": "你好"}])) == "你好。"
    assert requests[1][1] == 3, "Cold-start budget leaked into ordinary requests"


@pytest.mark.parametrize("fallback", [None, "primary"])
def test_unavailable_warmup_skips_missing_or_duplicate_fallback(monkeypatch, fallback):
    called = []

    def request(req, timeout):
        called.append(json.loads(req.data)["model"])
        raise OSError("synthetic failure")

    monkeypatch.setattr(cc.urllib.request, "urlopen", request)
    assert cc.CompanionChat(model="primary", fallback_model=fallback).warmup() is None
    assert called == ["primary"]


def test_first_turn_reuses_warm_fallback_then_retries_primary_after_cooldown(monkeypatch):
    calls = []
    clock = [100.]

    def request(req, timeout):
        model = json.loads(req.data)["model"]
        calls.append(model)
        if model == "primary" and clock[0] < 200:
            raise TimeoutError("primary unavailable")
        return Response()

    monkeypatch.setattr(cc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cc.urllib.request, "urlopen", request)
    chat = cc.CompanionChat(model="primary", fallback_model="backup")
    assert chat.warmup() == "backup"
    calls.clear()
    assert "".join(chat._chunks([])) == "你好。"
    assert calls == ["backup"], "A known failed model delayed the first real reply"
    clock[0] = 300.
    calls.clear()
    assert "".join(chat._chunks([])) == "你好。"
    assert calls == ["primary"] and chat.used_model == "primary"


def test_runtime_failure_also_reuses_healthy_fallback(monkeypatch):
    calls = []

    def request(req, timeout):
        model = json.loads(req.data)["model"]
        calls.append(model)
        if model == "primary":
            raise OSError("synthetic unavailable primary")
        return Response()

    monkeypatch.setattr(cc.urllib.request, "urlopen", request)
    chat = cc.CompanionChat(model="primary", fallback_model="backup")
    assert "".join(chat._chunks([])) == "你好。"
    assert "".join(chat._chunks([])) == "你好。"
    assert calls == ["primary", "backup", "backup"]


def test_fallback_failure_during_cooldown_can_recover_to_primary(monkeypatch):
    calls = []

    def request(req, timeout):
        model = json.loads(req.data)["model"]
        calls.append(model)
        if model == "backup":
            raise OSError("synthetic unavailable backup")
        return Response()

    monkeypatch.setattr(cc.urllib.request, "urlopen", request)
    chat = cc.CompanionChat(model="primary", fallback_model="backup")
    chat._primary_retry_at = cc.time.monotonic() + 30
    assert "".join(chat._chunks([])) == "你好。"
    assert calls == ["backup", "primary"]


def test_partial_answer_is_not_replaced_with_a_second_model(monkeypatch):
    calls = []

    class PartialResponse(Response):
        def __iter__(self):
            yield json.dumps({"message": {"content": "第一段。"}}).encode()
            raise OSError("synthetic connection failure after text")

    def request(req, timeout):
        calls.append(json.loads(req.data)["model"])
        return PartialResponse()

    monkeypatch.setattr(cc.urllib.request, "urlopen", request)
    output = cc.CompanionChat(model="primary", fallback_model="backup")._chunks([])
    assert next(output) == "第一段。"
    with pytest.raises(OSError):
        next(output)
    assert calls == ["primary"]
