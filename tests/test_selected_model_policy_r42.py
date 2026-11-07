"""Selected local models stay selected on faults; HTTP, ports and speech are fakes."""
import ast
import copy
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import canaan_companion as cc
import canaan_config as cfg
import canaan_intent as ci
import canaan_llm_provider as llm
import canaan_pet_chat as pet_chat


ROOT = Path(__file__).resolve().parents[1]
SELECTED = "canaan-chat:3b"
LOCAL_URL = "http://127.0.0.1:11434/api/chat"


class Response:
    def __init__(self, pieces=(), content=None, error=None):
        self.pieces, self.content, self.error = pieces, content, error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.closed = True

    def read(self):
        return json.dumps({"message": {"content": self.content}}).encode()

    def __iter__(self):
        for piece in self.pieces:
            yield json.dumps({"message": {"content": piece}}).encode()
        if self.error:
            raise self.error
        yield b'{"done":true}'


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    # No socket/HTTP is permitted, even if an old fallback unexpectedly probes.
    monkeypatch.setattr(llm, "load_config", lambda path=None: {})
    monkeypatch.setattr(cc, "load_persona_prompt", lambda: "测试助手。")
    monkeypatch.setattr(cc.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Unstubbed HTTP"))
    probes = []

    class Probe:
        def __init__(self, host, port, *a, **k):
            self.port = port

        def is_up(self, *a, **k):
            probes.append(self.port)
            return True

        def mark_down(self):
            pass

    monkeypatch.setattr(ci, "PortProbe", Probe)
    monkeypatch.setattr(cfg, "CHAT_MODEL", SELECTED)
    monkeypatch.setattr(cfg, "INTENT_COMPACT_MODEL", SELECTED)
    yield probes


def hub_clients(config=cfg):
    """Compile only the real model construction block, never the GUI module."""
    source = ROOT / "core" / "canaan_desk_os_hub.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CanaanDeskOSHub")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")

    def assigns(node, attr):
        return any(isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                   and n.attr == attr and isinstance(n.value, ast.Name) and n.value.id == "self"
                   for n in ast.walk(node))

    start = next(i for i, n in enumerate(init.body) if assigns(n, "intent_client"))
    end = next(i for i, n in enumerate(init.body) if assigns(n, "companion"))
    body = copy.deepcopy(init.body[start:end + 1])
    fn = ast.FunctionDef(name="construct", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")],
                         kwonlyargs=[], kw_defaults=[], defaults=[]), body=body, decorator_list=[])
    ns = {"canaan_config": config, "CompanionChat": cc.CompanionChat, "OllamaIntentClient": ci.OllamaIntentClient}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(source), "exec"), ns)
    hub = SimpleNamespace()
    ns["construct"](hub)
    return hub


def requests_with(monkeypatch, responder):
    sent = []

    def request(req, **kwargs):
        payload = json.loads(req.data)
        sent.append((req.full_url, payload, kwargs))
        return responder(req.full_url, payload, len(sent))

    monkeypatch.setattr(cc.urllib.request, "urlopen", request)
    return sent


def query_hub(hub):
    source = ROOT / "core" / "canaan_desk_os_hub.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CanaanDeskOSHub")
    method = copy.deepcopy(next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "query_canaan_os"))
    ns = {"NULL_TRACER": SimpleNamespace(), "canaan_config": cfg,
          "canaan_gamemode": SimpleNamespace(active=lambda: False)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), "exec"), ns)
    hub.memory = SimpleNamespace(messages=lambda: [])
    return ns["query_canaan_os"](hub, "聊一下今天的心情")


def fail_selected_else_answer(url, payload, _):
    if payload.get("model") == SELECTED:
        raise TimeoutError("synthetic selected-model failure")
    return Response(pieces=['{"action":"chat","param":"","reply":"另一模型回答"}'],
                    content='{"action":"chat","param":"","reply":"商业服务回答"}')


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "unknown_action"])
def test_hub_intent_failure_does_not_probe_or_request_another_model(monkeypatch, no_real_services, failure):
    def response(url, payload, _):
        if payload.get("model") != SELECTED:
            return fail_selected_else_answer(url, payload, _)
        if failure == "timeout":
            raise TimeoutError("synthetic selected-model failure")
        return Response(content="broken json" if failure == "invalid_json" else '{"a":"invented","p":""}')

    sent = requests_with(monkeypatch, response)
    hub = hub_clients()
    original = hub.intent_client.model, hub.intent_client.working_model
    for _ in range(2):
        result = hub.intent_client.decide("聊一下今天的心情")
        assert result["action"] == "chat" and result["_model"] == "fallback"
    assert [(url, p.get("model")) for url, p, _ in sent] == [(LOCAL_URL, SELECTED)] * 2
    assert no_real_services == [], "Selected compact intent entered a legacy service probe"
    assert (hub.intent_client.model, hub.intent_client.working_model) == original


def test_hub_failed_intent_warmup_next_turn_retries_only_selected_model(monkeypatch, no_real_services):
    def response(url, payload, number):
        if number == 1:
            raise OSError("synthetic cold-start failure")
        return Response(content='{"a":"chat","p":""}')

    sent = requests_with(monkeypatch, response)
    hub = hub_clients()
    assert hub.intent_client.warmup() is None
    assert hub.intent_client.decide("继续聊聊")["_model"] == SELECTED
    assert [(u, p.get("model")) for u, p, _ in sent] == [(LOCAL_URL, SELECTED)] * 2
    assert no_real_services == []
    assert hub.intent_client.model == hub.intent_client.working_model == SELECTED
    assert all(p["options"]["num_ctx"] == cfg.SHARED_NUM_CTX and p["keep_alive"] == "24h" for _, p, _ in sent)


def test_hub_failed_intent_returns_unavailable_instead_of_canned_chat(monkeypatch):
    sent = requests_with(monkeypatch, fail_selected_else_answer)
    result = query_hub(hub_clients())
    assert result["action"] == "chat" and result["_guard"] == "model_unavailable"
    assert result["_model"] == "fallback" and "暂时不可用" in result["reply"]
    assert [(u, p["model"]) for u, p, _ in sent] == [(LOCAL_URL, SELECTED)]


def test_explicit_cloud_intent_is_preserved(monkeypatch, no_real_services):
    result = {"action": "chat", "param": "", "reply": "明确选择的云服务回答", "_model": "provider:user-choice"}
    monkeypatch.setattr(ci.CompactIntentClient, "_cloud_decision", lambda *a, **k: result)
    assert query_hub(hub_clients()) == result
    assert not no_real_services


def test_explicit_other_intent_model_and_legacy_mode_are_preserved(monkeypatch):
    custom = SimpleNamespace(INTENT_COMPACT_MODEL="user-chosen:4b", CHAT_MODEL="user-chat:2b", CHAT_FALLBACK_MODEL="")
    selected = hub_clients(custom)
    assert selected.intent_client.model == "user-chosen:4b"
    assert selected.companion.model == "user-chat:2b"
    custom.INTENT_COMPACT_MODEL = ""
    assert isinstance(hub_clients(custom).intent_client, ci.OllamaIntentClient)


def test_hub_chat_faults_do_not_switch_model_or_url(monkeypatch):
    sent = requests_with(monkeypatch, fail_selected_else_answer)
    chat = hub_clients().companion
    for _ in range(2):
        with pytest.raises(TimeoutError):
            list(chat._chunks([]))
    assert [(u, p.get("model")) for u, p, _ in sent] == [(LOCAL_URL, SELECTED)] * 2
    assert chat.model == chat.used_model == SELECTED


def test_hub_failed_chat_warmup_next_turn_keeps_selected_model(monkeypatch):
    sent = requests_with(monkeypatch, fail_selected_else_answer)
    chat = hub_clients().companion
    assert chat.warmup() is None
    assert chat.model == chat.used_model == SELECTED
    sent.clear()
    monkeypatch.setattr(cc.urllib.request, "urlopen", lambda req, **k: (
        sent.append((req.full_url, json.loads(req.data), k)) or Response(["同一模型恢复了。"])))
    assert "".join(chat._chunks([])) == "同一模型恢复了。"
    assert [(u, p["model"]) for u, p, _ in sent] == [(LOCAL_URL, SELECTED)]
    assert sent[0][1]["options"]["num_ctx"] == cfg.SHARED_NUM_CTX
    assert sent[0][1]["keep_alive"] == cc.chat_keep_alive()


def test_standalone_pet_uses_configured_model_without_hidden_backup(monkeypatch):
    sent = requests_with(monkeypatch, fail_selected_else_answer)
    chat = pet_chat.PetChat(settings={"provider": "local"})._get_companion()
    assert chat.model == SELECTED and not chat.fallback_model
    with pytest.raises(TimeoutError):
        list(chat._chunks([]))
    assert [(u, p.get("model")) for u, p, _ in sent] == [(LOCAL_URL, SELECTED)]


@pytest.mark.parametrize("environment, expected", [({}, ""), ({"CANAAN_CHAT_FALLBACK_MODEL": "manual:2b"}, "manual:2b")])
def test_chat_fallback_default_is_empty_but_explicit_environment_is_preserved(environment, expected):
    source = ROOT / "core" / "canaan_config.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "CHAT_FALLBACK_MODEL" for t in n.targets)]
    ns = {"os": SimpleNamespace(environ=environment)}
    exec(compile(ast.Module(body=assignments, type_ignores=[]), str(source), "exec"), ns)
    assert ns["CHAT_FALLBACK_MODEL"] == expected


def test_explicit_chat_fallback_remains_available(monkeypatch):
    def response(url, payload, _):
        if payload["model"] == SELECTED:
            raise TimeoutError("synthetic selected-model failure")
        return Response(["用户明确选的备用回答。"])

    sent = requests_with(monkeypatch, response)
    custom = SimpleNamespace(INTENT_COMPACT_MODEL=SELECTED, CHAT_MODEL=SELECTED, CHAT_FALLBACK_MODEL="chosen-backup:2b")
    chat = hub_clients(custom).companion
    assert "".join(chat._chunks([])) == "用户明确选的备用回答。"
    assert [p["model"] for _, p, _ in sent] == [SELECTED, "chosen-backup:2b"]


def test_explicit_cloud_provider_keeps_selected_local_backup(monkeypatch):
    provider = {"chat": "test-cloud", "providers": {"test-cloud": {"kind": "openai", "model": "chosen-cloud"}}}
    monkeypatch.setattr(llm, "load_config", lambda path=None: provider)
    calls = []

    def cloud(*a, **k):
        calls.append("explicit-cloud")
        raise llm.ProviderError("synthetic cloud outage")
        yield

    monkeypatch.setattr(llm, "stream_chat", cloud)
    sent = requests_with(monkeypatch, lambda *a: Response(["用户所选本机模型回答。"]))
    chat = hub_clients().companion
    assert chat.model == "provider:test-cloud" and chat.fallback_model == SELECTED
    assert "".join(chat._chunks([])) == "用户所选本机模型回答。"
    assert calls == ["explicit-cloud"]
    assert [(u, p["model"]) for u, p, _ in sent] == [(LOCAL_URL, SELECTED)]


def test_explicit_cloud_warmup_keeps_available_provider_first(monkeypatch):
    provider = {"chat": "test-cloud", "providers": {"test-cloud": {"kind": "openai", "model": "chosen-cloud"}}}
    monkeypatch.setattr(llm, "load_config", lambda path=None: provider)
    calls = []

    def cloud(*a, **k):
        calls.append("explicit-cloud")
        yield "用户明确选择的云服务回答。"

    monkeypatch.setattr(llm, "stream_chat", cloud)
    sent = requests_with(monkeypatch, lambda *a: Response(["本地备用回答。"]))
    chat = hub_clients().companion
    assert chat.warmup() == SELECTED
    assert [(u, p["model"]) for u, p, _ in sent] == [(LOCAL_URL, SELECTED)]
    assert not calls, "Cloud providers must not be contacted just to warm up"
    sent.clear()

    assert "".join(chat._chunks([])) == "用户明确选择的云服务回答。"
    assert calls == ["explicit-cloud"]
    assert not sent, "Warming a backup must not bypass the selected cloud provider"


def test_explicit_cloud_fault_after_warmup_keeps_real_failure_backoff(monkeypatch):
    provider = {"chat": "test-cloud", "providers": {"test-cloud": {"kind": "openai", "model": "chosen-cloud"}}}
    monkeypatch.setattr(llm, "load_config", lambda path=None: provider)
    now = [1000.0]
    monkeypatch.setattr(cc.time, "monotonic", lambda: now[0])
    calls = []

    def cloud(*a, **k):
        calls.append("explicit-cloud")
        raise llm.ProviderError("synthetic cloud outage")
        yield

    monkeypatch.setattr(llm, "stream_chat", cloud)
    sent = requests_with(monkeypatch, lambda *a: Response(["本地备用回答。"]))
    chat = hub_clients().companion
    assert chat.warmup() == SELECTED
    sent.clear()

    assert "".join(chat._chunks([])) == "本地备用回答。"
    assert calls == ["explicit-cloud"]
    assert chat.warmup() == SELECTED
    assert "".join(chat._chunks([])) == "本地备用回答。"
    assert calls == ["explicit-cloud"], "A real cloud failure should retain its retry delay"
    now[0] += 31
    assert "".join(chat._chunks([])) == "本地备用回答。"
    assert calls == ["explicit-cloud", "explicit-cloud"]
    assert [p["model"] for _, p, _ in sent] == [SELECTED] * 4


def test_partial_explicit_cloud_stream_does_not_restart_on_local_backup(monkeypatch):
    provider = {"chat": "test-cloud", "providers": {"test-cloud": {"kind": "openai", "model": "chosen-cloud"}}}
    monkeypatch.setattr(llm, "load_config", lambda path=None: provider)

    def cloud(*a, **k):
        yield "云端已经输出的句子。"
        raise llm.ProviderError("synthetic broken cloud stream")

    monkeypatch.setattr(llm, "stream_chat", cloud)
    sent = requests_with(monkeypatch, lambda *a: pytest.fail("Partial cloud answer restarted locally"))
    chat = hub_clients().companion
    chunks = chat._chunks([])
    assert next(chunks) == "云端已经输出的句子。"
    with pytest.raises(llm.ProviderError):
        next(chunks)
    assert not sent and chat.used_model == "provider:test-cloud"


def test_already_spoken_ack_cannot_hide_model_unavailable_as_asr_error():
    source = ROOT / "core" / "canaan_desk_os_hub.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    fn = copy.deepcopy(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_llm"))
    failure = {"action": "chat", "param": "", "_model": "fallback", "_guard": "model_unavailable",
               "reply": "当前选择的指令模型暂时不可用，请稍后再试。"}
    ns = {"self": SimpleNamespace(query_canaan_os=lambda *a, **k: dict(failure)),
          "tracer": SimpleNamespace(set=Mock()), "ack_state": {"played": True}, "_on_action": Mock(),
          "finish_cancelled_turn": Mock(return_value=False)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(source), "exec"), ns)
    assert ns["_llm"]("测试") == failure
    ns["finish_cancelled_turn"].assert_called_once_with()


@pytest.mark.parametrize("fallback", [None, "chosen-backup:2b"])
def test_partial_local_stream_never_restarts_on_another_model(monkeypatch, fallback):
    sent = requests_with(monkeypatch, lambda *a: Response(["已经输出的第一句。"], error=OSError("synthetic broken stream")))
    chat = cc.CompanionChat(model=SELECTED, fallback_model=fallback, guard=False)
    chunks = chat._chunks([])
    assert next(chunks) == "已经输出的第一句。"
    with pytest.raises(OSError):
        next(chunks)
    assert [p["model"] for _, p, _ in sent] == [SELECTED]
    assert chat.model == chat.used_model == SELECTED


def speech_hub(monkeypatch, stream):
    """Compile real completion logic; replace memory, captions and voice with fakes."""
    source = ROOT / "core" / "canaan_desk_os_hub.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CanaanDeskOSHub")
    methods = [copy.deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name in {"speak_companion", "create_companion_stream", "finish_companion_stream",
                              "_capture_context", "_context_valid"}]
    trace = SimpleNamespace(set=Mock())
    ns = {"NULL_TRACER": trace, "threading": threading, "canaan_config": SimpleNamespace(CHAT_OPENER=False),
          "fact_store_of": lambda h: SimpleNamespace(texts=lambda: []), "build_context": lambda **k: "",
          "EMOTION_CONFIG": {"pamper": {"title": "测试"}}, "remember_clarify": lambda *a: False}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), str(source), "exec"), ns)
    hub = SimpleNamespace(memory=SimpleNamespace(recent_actions=lambda: [], add_turn=Mock()),
                          companion=SimpleNamespace(stream=lambda *a, **k: stream, used_model=SELECTED),
                          ui_queue=queue.Queue(), speak=Mock(), set_pending=Mock(),
                          _speech_interrupted=lambda: False)
    hub.create_companion_stream = ns["create_companion_stream"].__get__(hub)
    hub.finish_companion_stream = ns["finish_companion_stream"].__get__(hub)
    hub._capture_context = ns["_capture_context"].__get__(hub)
    hub._context_valid = ns["_context_valid"].__get__(hub)

    def playback(texts, emotion, on_segment, **kwargs):
        for piece in texts:
            on_segment(piece)
        return {"interrupted": False, "failed": False}

    hub.engine = SimpleNamespace(pipeline=SimpleNamespace(speak_stream=playback))
    hub._speak_with = lambda invoke, tracer: invoke()
    return hub, lambda: ns["speak_companion"](hub, "测试话题", [], fallback_reply="这是旧的猜测回复。")


@pytest.mark.parametrize("pieces", [[], ["已经播出的句子。"]])
def test_hub_generation_failure_is_explicit_and_does_not_repeat_old_reply(monkeypatch, pieces):
    def broken():
        yield from pieces
        raise OSError("synthetic broken stream")

    stream = cc.ChatStream(broken(), guard=False)
    hub, speak = speech_hub(monkeypatch, stream)
    reply = speak()
    assert "这是旧的猜测回复" not in reply
    if pieces:
        assert reply == "".join(pieces)
        hub.speak.assert_not_called()
    else:
        assert "暂时不可用" in reply
        hub.speak.assert_called_once()
    assert hub.memory.add_turn.call_args.args[1]["reply"] == reply
    hub.set_pending.assert_not_called()


def test_hub_empty_stream_reports_unavailable_instead_of_successful_chat(monkeypatch):
    stream = cc.ChatStream(iter([]), guard=False)
    hub, speak = speech_hub(monkeypatch, stream)
    assert "暂时不可用" in speak()
    hub.set_pending.assert_not_called()


def test_failed_partial_answer_cannot_offer_an_action(monkeypatch):
    def broken():
        yield "要不要我帮你搜一下课程？"
        raise OSError("synthetic broken stream")

    hub, speak = speech_hub(monkeypatch, cc.ChatStream(broken(), guard=False))
    assert speak() == "要不要我帮你搜一下课程？"
    hub.set_pending.assert_not_called()
