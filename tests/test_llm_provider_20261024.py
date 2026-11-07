"""10-24：接外部大模型 API（OpenAI 兼容 / Claude）。全部用假的 HTTP 和假的 SDK 客户端，不联网、不花钱。"""
import io
import json
import os
import urllib.error

import canaan_companion as cc
import canaan_crypto
import canaan_llm_provider as llm
import pytest


class SSE(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sse(*pieces, done=True):
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': p}}]}, ensure_ascii=False)}\n\n" for p in pieces]
    if done:
        lines.append("data: [DONE]\n\n")
    return SSE("".join(lines).encode("utf-8"))


OPENAI = {"name": "deepseek", "kind": "openai", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}


def test_openai_compatible_stream_request_and_parse():
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["headers"], seen["body"] = req.full_url, dict(req.header_items()), json.loads(req.data)
        return sse("你好", "呀")

    out = list(llm.stream_chat(OPENAI, [{"role": "user", "content": "hi"}], key="sk-test", stop=list("abcde"),
                               temperature=0.6, opener=opener))
    assert out == ["你好", "呀"]
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["body"]["stream"] is True and seen["body"]["model"] == "deepseek-chat"
    assert len(seen["body"]["stop"]) == 4 and seen["body"]["temperature"] == 0.6


def test_openai_http_error_becomes_provider_error():
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error":"bad key"}'))

    with pytest.raises(llm.ProviderError, match="401"):
        list(llm.stream_chat(OPENAI, [{"role": "user", "content": "hi"}], key="x", opener=opener))


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(llm, "api_key", lambda name, secrets=None: None)
    with pytest.raises(llm.ProviderError, match="--set-key deepseek"):
        list(llm.stream_chat(OPENAI, [{"role": "user", "content": "hi"}]))


class FakeStream:
    def __init__(self, texts, stop_reason="end_turn"):
        self.text_stream, self._stop = iter(texts), stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return type("M", (), {"stop_reason": self._stop})()


class FakeClaude:
    def __init__(self, texts, stop_reason="end_turn"):
        self.calls, self.texts, self.stop_reason = [], texts, stop_reason
        self.messages = self

    def stream(self, **kw):
        self.calls.append(kw)
        return FakeStream(self.texts, self.stop_reason)


CLAUDE = {"name": "claude", "kind": "anthropic", "model": "claude-opus-5-5"}


def test_claude_uses_sdk_stream_with_system_split_and_no_prefill():
    client = FakeClaude(["在呢", "。"])
    msgs = [{"role": "system", "content": "你是团团"}, {"role": "assistant", "content": "早"},
            {"role": "user", "content": "你好"}, {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "[pamper] 开场"}]
    assert list(llm.stream_chat(CLAUDE, msgs, key="k", stop=["\n\n", " ", "主人："], anthropic_client=client)) == ["在呢", "。"]
    kw = client.calls[0]
    assert kw["system"] == "你是团团" and kw["model"] == "claude-opus-5-5"
    assert kw["messages"] == [{"role": "user", "content": "你好\n在吗"}]   # 开头 assistant、结尾预填都去掉
    assert kw["stop_sequences"] == ["主人："] and "temperature" not in kw   # 纯空白的停止串 Claude 不收
    assert kw["output_config"] == {"effort": "low"}


def test_claude_refusal_is_reported_so_local_model_takes_over():
    with pytest.raises(llm.ProviderError, match="拒绝"):
        list(llm.stream_chat(CLAUDE, [{"role": "user", "content": "x"}], key="k",
                             anthropic_client=FakeClaude([], stop_reason="refusal")))


def test_secret_store_encrypts_and_binds_name(tmp_path):
    c = canaan_crypto.Cipher(os.urandom(32))
    s = llm.SecretStore(str(tmp_path / "s.db"), cipher=c)
    s.set("deepseek", "sk-very-secret")
    assert s.get("deepseek") == "sk-very-secret" and s.names() == ["deepseek"]
    s.db.commit()
    assert b"sk-very-secret" not in open(tmp_path / "s.db", "rb").read()
    raw = s.db.execute("SELECT value FROM secrets").fetchone()[0]
    s.db.execute("INSERT INTO secrets VALUES('kimi',?,0)", (raw,))          # 挪到别的名字下解不开
    with pytest.raises(Exception):
        s.get("kimi")
    assert s.delete("deepseek") and s.get("deepseek") is None


def test_env_key_wins(monkeypatch):
    monkeypatch.setenv("CANAAN_DEEPSEEK_API_KEY", "sk-env")
    assert llm.api_key("deepseek") == "sk-env"


def test_config_roles_and_known_base_urls(tmp_path):
    p = tmp_path / "c.json"
    llm.save_config({"chat": "deepseek", "providers": {"deepseek": {"kind": "openai", "model": "deepseek-chat"}}}, str(p))
    cfg = llm.load_config(str(p))
    assert llm.role_provider("chat", cfg) == "deepseek"
    assert llm.provider("deepseek", cfg)["base_url"] == llm.KNOWN_BASE_URLS["deepseek"]
    assert llm.role_provider("chat", {"chat": "local"}) is None
    assert llm.role_provider("chat", {"chat": "nope"}) is None
    assert llm.send_memory(cfg) is False


def _chat(monkeypatch, cfg, cloud_pieces=None, cloud_error=None):
    monkeypatch.setattr(llm, "load_config", lambda path=None: cfg)
    sent = []

    def fake_stream_chat(p, messages, **kw):
        sent.append(messages)
        if cloud_error:
            raise cloud_error
        yield from cloud_pieces or []

    monkeypatch.setattr(llm, "stream_chat", fake_stream_chat)
    chat = cc.CompanionChat(model="canaan-chat:3b", fallback_model="canaan-desk:1.5b")
    return chat, sent


CFG = {"chat": "deepseek", "providers": {"deepseek": {"kind": "openai", "model": "deepseek-chat"}}}


def test_companion_uses_cloud_first_and_local_as_backup(monkeypatch):
    chat, sent = _chat(monkeypatch, CFG, cloud_pieces=["[pamper] 好呀"])
    assert chat.model == "provider:deepseek" and chat.fallback_model == "canaan-chat:3b"
    assert "".join(chat._chunks([{"role": "user", "content": "x"}])) == "[pamper] 好呀"
    assert chat.used_model == "provider:deepseek"


def test_cloud_failure_falls_back_to_local_model(monkeypatch):
    chat, _ = _chat(monkeypatch, CFG, cloud_error=llm.ProviderError("断网"))
    local = []
    monkeypatch.setattr(chat, "_chunks_one", (lambda orig: lambda m, msgs, *a, **k: (
        orig(m, msgs, *a, **k) if chat.is_cloud(m) else (local.append(m) or iter(["本机回答"]))))(chat._chunks_one))
    assert "".join(chat._chunks([{"role": "user", "content": "x"}])) == "本机回答"
    assert local == ["canaan-chat:3b"]


def test_memory_is_not_sent_to_cloud_by_default(monkeypatch):
    chat, sent = _chat(monkeypatch, CFG, cloud_pieces=["[pamper] 嗯"])
    monkeypatch.setattr(cc, "load_persona_prompt", lambda: "人设")
    cs = chat._stream("我住哪", context="【关于主人】主人住在合肥")
    "".join(cs.iter_text())
    assert sent and all("合肥" not in json.dumps(m, ensure_ascii=False) for m in sent)
    assert sent[0][0]["content"] == "人设"


def test_memory_is_sent_when_owner_opts_in(monkeypatch):
    chat, sent = _chat(monkeypatch, dict(CFG, send_memory=True), cloud_pieces=["[pamper] 嗯"])
    monkeypatch.setattr(cc, "load_persona_prompt", lambda: "人设")
    "".join(chat._stream("我住哪", context="【关于主人】主人住在合肥").iter_text())
    assert "合肥" in json.dumps(sent[0], ensure_ascii=False)


def test_cloud_is_not_warmed_up(monkeypatch):
    chat, sent = _chat(monkeypatch, CFG)
    monkeypatch.setattr(chat, "_chunks_one", lambda m, *a, **k: iter([]))
    assert chat.warmup() == "canaan-chat:3b" and sent == []


def test_cli_add_use_and_list(tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("llm_cli", os.path.join(os.path.dirname(__file__), "..", "tools", "llm_provider.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(llm, "CONFIG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(llm.load_config, "__defaults__", (str(tmp_path / "c.json"),))
    monkeypatch.setattr(llm.save_config, "__defaults__", (str(tmp_path / "c.json"),))
    monkeypatch.setattr(llm, "SecretStore", lambda: type("S", (), {"names": lambda self: []})())
    assert cli.main(["--add", "deepseek", "--model", "deepseek-chat"]) == 0
    assert cli.main(["--use-chat", "deepseek"]) == 0
    assert llm.load_config(str(tmp_path / "c.json"))["chat"] == "deepseek"
    assert cli.main(["--list"]) == 0
    assert "deepseek: openai deepseek-chat" in capsys.readouterr().out
