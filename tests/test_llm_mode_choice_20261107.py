"""首次安装三选一（本机模型 / 云端 API / 只用规则）以及 canaan_config 按选择开关大模型。网络、密钥存储都是替身。"""
import importlib
import json
from types import SimpleNamespace

import pytest

import canaan_bootstrap as boot
import canaan_config
import canaan_llm_provider as llm


def answers(*values):
    it = iter(values)
    return lambda prompt="": next(it)


class FakeLLM:
    KNOWN_BASE_URLS = llm.KNOWN_BASE_URLS

    def __init__(self, cfg=None, fail_test=False):
        self.cfg, self.keys, self.fail_test, self.calls = dict(cfg or {}), {}, fail_test, []
        outer = self

        class Store:
            def set(self, name, key):
                outer.keys[name] = key
        self.SecretStore = Store

    def load_config(self):
        return json.loads(json.dumps(self.cfg))

    def save_config(self, cfg):
        self.cfg = json.loads(json.dumps(cfg))

    def provider(self, name, cfg=None):
        return llm.provider(name, cfg if cfg is not None else self.cfg)

    def complete(self, p, messages, **kwargs):
        self.calls.append(p)
        if self.fail_test:
            raise RuntimeError("401")
        return "你好呀"


@pytest.mark.parametrize("tier,expected", [("8gb", "local"), ("6gb", "local"), ("4gb", "api")])
def test_default_follows_vram(tier, expected):
    out = []
    assert boot.ask_mode(tier, 0, ask=answers(""), out=out.append) == expected
    assert any("推荐" in line for line in out)


def test_invalid_choice_asks_again():
    out = []
    assert boot.ask_mode("8gb", 8192, ask=answers("9", "abc", "3"), out=out.append) == "rules"
    assert out.count("请输入 1、2 或 3") == 2


def test_cloud_setup_uses_one_provider_for_chat_and_intent_and_keeps_memory_local():
    fake, out = FakeLLM(), []
    name = boot.setup_cloud(ask=answers("1", ""), ask_secret=answers("sk-test"), out=out.append, llm=fake)
    assert name == "deepseek"
    assert fake.cfg["providers"]["deepseek"] == {"kind": "openai", "model": "deepseek-chat"}
    assert fake.cfg["chat"] == fake.cfg["intent"] == "deepseek" and fake.cfg["mode"] == "api"
    assert fake.cfg["send_memory"] is False
    assert fake.keys == {"deepseek": "sk-test"}                # 密钥只进加密存储，不进配置文件
    assert "sk-test" not in json.dumps(fake.cfg)
    assert fake.calls and fake.calls[0]["base_url"] == llm.KNOWN_BASE_URLS["deepseek"]


def test_cloud_setup_keeps_existing_memory_choice_and_reports_bad_key():
    fake, out = FakeLLM({"send_memory": True}, fail_test=True), []
    assert boot.setup_cloud(ask=answers("6", "gpt-x"), ask_secret=answers("k"), out=out.append, llm=fake) == "openai"
    assert fake.cfg["providers"]["openai"]["model"] == "gpt-x" and fake.cfg["send_memory"] is True
    assert any("暂时不通" in line for line in out)


def test_custom_endpoint_needs_http_url_and_model():
    fake = FakeLLM()
    assert boot.setup_cloud(ask=answers("8", "ftp://x"), ask_secret=answers(""), out=lambda *_: None, llm=fake) is None
    assert boot.setup_cloud(ask=answers("8", "https://relay.example/v1", ""), ask_secret=answers(""),
                            out=lambda *_: None, llm=fake) is None
    assert boot.setup_cloud(ask=answers("8", "https://relay.example/v1", "m1"), ask_secret=answers(""),
                            out=lambda *_: None, llm=fake) == "custom"
    assert fake.cfg["providers"]["custom"] == {"kind": "openai", "base_url": "https://relay.example/v1", "model": "m1"}


def test_no_key_skips_the_test_call(monkeypatch):
    monkeypatch.delenv("CANAAN_DEEPSEEK_API_KEY", raising=False)
    fake = FakeLLM()
    boot.setup_cloud(ask=answers("1", ""), ask_secret=answers(""), out=lambda *_: None, llm=fake)
    assert fake.calls == [] and fake.keys == {}


def test_local_and_rules_reset_cloud_roles():
    fake = FakeLLM({"chat": "deepseek", "intent": "deepseek", "providers": {"deepseek": {"model": "m"}}})
    boot.save_mode("rules", llm=fake)
    assert fake.cfg["mode"] == "rules" and fake.cfg["chat"] == fake.cfg["intent"] == "local"
    assert "deepseek" in fake.cfg["providers"]                 # 服务配置留着，之后改回 api 不用重填


@pytest.fixture
def reload_config(monkeypatch):
    def run(cfg, tier):
        monkeypatch.setattr(llm, "load_config", lambda path=None: cfg)
        import canaan_tiers
        monkeypatch.setattr(canaan_tiers, "detect_vram_mb", lambda: {"8gb": 8192, "6gb": 6144, "4gb": 0}[tier])
        return importlib.reload(canaan_config)
    yield run
    monkeypatch.undo()
    importlib.reload(canaan_config)


def test_api_mode_without_gpu_still_talks_through_the_cloud(reload_config):
    cfg = {"mode": "api", "chat": "deepseek", "intent": "deepseek", "providers": {"deepseek": {"model": "deepseek-chat"}}}
    c = reload_config(cfg, "4gb")
    assert c.ACTIVE_TIER == "4gb" and c.LLM_MODE == "api"
    assert c.LLM_ENABLED is True and c.COMPANION_ENABLED is True and c.LOCAL_LLM_ENABLED is False


def test_api_mode_on_a_big_gpu_does_not_load_local_models(reload_config):
    cfg = {"mode": "api", "chat": "qwen", "intent": "qwen", "providers": {"qwen": {"model": "qwen-plus"}}}
    c = reload_config(cfg, "8gb")
    assert c.LLM_ENABLED is True and c.LOCAL_LLM_ENABLED is False


def test_rules_mode_turns_every_model_off(reload_config):
    c = reload_config({"mode": "rules"}, "8gb")
    assert c.LLM_ENABLED is False and c.LOCAL_LLM_ENABLED is False and c.COMPANION_ENABLED is False


def test_no_config_keeps_the_vram_tier(reload_config):
    c = reload_config({}, "8gb")
    assert c.LLM_MODE == "local" and c.LLM_ENABLED is True and c.LOCAL_LLM_ENABLED is True
    c = reload_config({}, "4gb")
    assert c.LLM_ENABLED is False and c.LOCAL_LLM_ENABLED is False


def test_noninteractive_bootstrap_never_asks_for_a_key(monkeypatch, tmp_path):
    fake = FakeLLM()
    monkeypatch.setattr(llm, "load_config", fake.load_config)
    monkeypatch.setattr(llm, "save_config", fake.save_config)
    monkeypatch.setattr(boot, "ensure_files", lambda manifest, check: True)
    monkeypatch.setattr(boot, "ensure_ollama", lambda *a, **k: pytest.fail("4GB 不该装本机模型"))
    monkeypatch.setattr(boot, "setup_cloud", lambda *a, **k: pytest.fail("不能交互时不该问密钥"))
    monkeypatch.setattr(canaan_config, "ACTIVE_TIER", "4gb")
    monkeypatch.setattr(boot.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert boot.main([]) == 0
    assert fake.cfg["mode"] == "rules"
