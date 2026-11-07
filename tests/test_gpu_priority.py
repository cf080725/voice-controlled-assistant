"""显卡让路：后台 7B 用完即卸载、用完后把对话模型预热回来、主人刚说过话时定时总结往后推。"""
import json
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("core", "plugins", "message_tools", ""):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import canaan_gpu_priority as gpu  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gpu._reset_for_tests()
    yield
    gpu._reset_for_tests()


def test_background_model_unloads_but_interactive_models_untouched(monkeypatch):
    import canaan_config as cfg
    monkeypatch.setattr(cfg, "INTENT_COMPACT_MODEL", "intent:1.5b")
    monkeypatch.setattr(cfg, "CHAT_MODEL", "chat:3b")
    monkeypatch.setattr(cfg, "CHAT_FALLBACK_MODEL", "chat:1.5b")
    assert gpu.background_keep_alive("qwen2.5:7b") == 0
    assert gpu.background_keep_alive("chat:3b") is None
    assert gpu.background_keep_alive("intent:1.5b") is None


def test_recent_interaction_window():
    assert not gpu.user_recently_active()
    gpu.mark_interaction(now=100.0)
    assert gpu.seconds_since_interaction(now=150.0) == 50.0
    assert gpu.seconds_since_interaction(now=100.0 + gpu.INTERACTIVE_GRACE_SEC + 1) > gpu.INTERACTIVE_GRACE_SEC


def test_turn_tracer_marks_interaction(tmp_path):
    from canaan_latency import TurnTracer
    TurnTracer(source="kws", log_path=str(tmp_path / "l.jsonl"))
    assert gpu.user_recently_active()


def test_background_done_rewarms_once_and_skips_interactive(monkeypatch):
    import canaan_config as cfg
    monkeypatch.setattr(cfg, "CHAT_MODEL", "chat:3b")
    calls = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(2)

    gpu.register_rewarm(slow)
    gpu.register_rewarm(slow)  # 重复登记只算一次
    assert gpu.background_done("chat:3b") is None  # 对话模型自己用完不用重新预热
    t = gpu.background_done("qwen2.5:7b")
    assert t is not None
    assert gpu.background_done("qwen2.5:7b") is None  # 正在预热时不重复起线程
    gate.set()
    t.join(3)
    assert calls == [1]


def test_rewarm_failure_is_contained():
    def boom():
        raise RuntimeError("ollama down")
    gpu.register_rewarm(boom)
    gpu.background_done("qwen2.5:7b", wait=True)
    assert not gpu._rewarm_running.is_set()


def test_briefing_payload_unloads_7b_and_triggers_rewarm(monkeypatch):
    import canaan_daily_briefing as briefing
    sent = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        content = json.dumps({"speech_text": "根据已采集的消息片段", "todos": [], "highlights": [], "social_memo": []})
        return Resp(json.dumps({"message": {"content": content}, "done_reason": "stop"}).encode("utf-8"))

    rewarmed = []
    monkeypatch.setattr(briefing, "LOCAL_MODELS_CASCADE", ["qwen2.5:7b"])
    monkeypatch.setattr(briefing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gpu, "background_done", lambda model=None, wait=False: rewarmed.append(model))
    content, _ = briefing._call_local_ollama_briefing("消息")
    assert content
    assert sent[0]["keep_alive"] == 0
    assert rewarmed == ["qwen2.5:7b"]


def test_intent_fallback_chain_skips_7b():
    """紧凑意图模型出错时的备用链不再先试两个 7B（8GB 显卡放不下，一句话要等几十秒）。"""
    from canaan_intent import small_fallback_models
    assert small_fallback_models() == ["canaan-desk:1.5b"]
    assert small_fallback_models(["a:14b", "b:3b"]) == ["b:3b"]
    assert small_fallback_models(["qwen2.5:7b"]) == ["qwen2.5:7b"]  # 只有大模型时仍保留一个，不至于没有备用


def test_demo_mode_keeps_chat_model_resident(monkeypatch, tmp_path):
    from canaan_companion import chat_keep_alive
    monkeypatch.setattr(gpu, "DEMO_FLAG", str(tmp_path / "demo_mode.flag"))
    normal = chat_keep_alive()
    assert normal != "24h"
    (tmp_path / "demo_mode.flag").write_text("1", encoding="utf-8")
    assert gpu.demo_mode() and chat_keep_alive() == "24h"


def test_local_service_urls_use_ipv4_loopback():
    """Windows 上 localhost 先试 IPv6，Ollama / Muse / Dots 只听 127.0.0.1：每次请求白等约 2 秒。"""
    import canaan_config
    import canaan_intent
    import canaan_agent_board_data as board
    assert "localhost" not in canaan_config.OLLAMA_API_URL
    assert "localhost" not in canaan_intent.OLLAMA_URL
    assert board._loopback("http://localhost:8787/") == "http://127.0.0.1:8787"
    for url in (board.MUSE_API, board.MUSE_WEB, board.DOTS_API, board.DOTS_WEB):
        assert "//localhost:" not in url
