"""Exercise hub startup methods without importing Tk, models, or user audio."""
import ast
import os
import queue
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def startup(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "core" / "canaan_desk_os_hub.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CanaanDeskOSHub")
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef)
               and node.name in {"_start_integrations", "_transcribe_message", "async_load_models"}]
    isolated = ast.ClassDef(name="Hub", bases=[], keywords=[], body=methods, decorator_list=[])
    threads, runtimes, transcriptions = [], [], []
    transcribed = threading.Event()

    def thread_factory(*args, **kwargs):
        worker = threading.Thread(*args, **kwargs)
        threads.append(worker)
        return worker

    def transcribe(audio, sense_voice=None, whisper=None, command=True):
        transcriptions.append((sense_voice, whisper, command))
        transcribed.set()
        return ("synthetic recognized text", "stub") if sense_voice is not None or whisper is not None else ("", "none")

    class Runtime:
        def __init__(self, notify, transcriber, readiness=None, memory=None, people=None, context=None,
                     switch_user=None, clear_context=None, context_store=None, device_factory=None):
            self.transcriber = transcriber
            self.readiness = readiness
            self.memory, self.people = memory, people
            self.context, self.switch_user = context, switch_user
            self.clear_context = clear_context
            self.context_store = context_store
            self.device_factory = device_factory
            self.voices = SimpleNamespace(stop_event=threading.Event())
            self.started = threading.Event()
            runtimes.append(self)

        def start(self):
            self.started.set()
            return self

    api = ModuleType("canaan_api_server")
    api.IntegrationRuntime = Runtime
    monkeypatch.setitem(sys.modules, "canaan_api_server", api)
    namespace = {
        "threading": SimpleNamespace(Thread=thread_factory), "transcribe": transcribe,
        "time": time, "os": SimpleNamespace(environ={"CANAAN_SUPERVISED": "1"}, path=os.path),
        "canaan_config": SimpleNamespace(TTS_MODE="cpu"),
        "canaan_gamemode": SimpleNamespace(active=lambda: False),
        "CanaanWakeWordDetector": lambda: object(), "WAKE_WORD": "fixture",
        "print": lambda *args, **kwargs: None,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[isolated], type_ignores=[])), str(source), "exec"), namespace)
    hub = namespace["Hub"]()
    hub.root, hub.ui_queue = SimpleNamespace(), queue.Queue()
    hub.sense_voice, hub.asr_model = None, None
    hub._asr_ready, hub._asr_load_error = threading.Event(), ""
    hub.voice_readiness = lambda: {"attached": True, "asr_ready": hub.sense_voice is not None or hub.asr_model is not None}
    hub.profile, hub.people = object(), object()
    hub.context_view = lambda: {"available": True, "active_profile": "owner"}
    hub.switch_person = lambda name=None: {"active_profile": name or "owner"}
    hub.clear_context = lambda: {"cleared": True}
    hub.context_store = object()
    wake_started = threading.Event()
    hub.kws_audio_loop = wake_started.set
    hub.warmup_ollama = hub.prewarm_tts_cache = hub._tts_keep_warm = hub._start_proactive = lambda: None
    hub._release_asr_load_memory = lambda: None
    hub._start_integrations()
    threads[-1].join(1)
    runtime = hub.root.canaan_integration_runtime
    assert runtime.memory is hub.profile and runtime.people is hub.people
    assert runtime.context() == {"available": True, "active_profile": "owner"}
    assert runtime.switch_user("访客") == {"active_profile": "访客"}
    assert runtime.clear_context() == {"cleared": True}
    assert runtime.context_store is hub.context_store
    yield SimpleNamespace(hub=hub, runtime=runtime, namespace=namespace, threads=threads,
                          transcribed=transcribed, transcriptions=transcriptions,
                          wake_started=wake_started)
    runtime.voices.stop_event.set()
    for worker in threads:
        worker.join(2)


def callback_worker(startup):
    entered, finished = threading.Event(), threading.Event()
    result = {}

    def run():
        entered.set()
        try:
            result["value"] = startup.runtime.transcriber(object())
        except Exception as error:
            result["error"] = error
        finally:
            finished.set()

    worker = threading.Thread(target=run, daemon=True)
    startup.threads.append(worker)
    worker.start()
    assert entered.wait(1)
    return worker, finished, result


def models(monkeypatch, startup, sense, whisper):
    funasr = ModuleType("funasr")
    funasr.AutoModel = sense
    monkeypatch.setitem(sys.modules, "funasr", funasr)
    for name in ("modelscope", "modelscope.hub"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    snapshot = ModuleType("modelscope.hub.snapshot_download")
    snapshot.snapshot_download = lambda *args, **kwargs: "synthetic-model-path"
    monkeypatch.setitem(sys.modules, "modelscope.hub.snapshot_download", snapshot)
    startup.namespace["WhisperModel"] = whisper


def failed_model(*args, **kwargs):
    raise RuntimeError("synthetic model load failure")


def test_wake_model_alone_does_not_publish_voice_ready(startup, monkeypatch):
    loading, release = threading.Event(), threading.Event()

    def delayed_sense(**kwargs):
        loading.set()
        assert release.wait(1)
        return object()

    models(monkeypatch, startup, delayed_sense, failed_model)
    startup.hub.async_load_models()
    try:
        assert loading.wait(1)
        statuses = []
        while not startup.hub.ui_queue.empty():
            kind, value = startup.hub.ui_queue.get_nowait()
            if kind == "status":
                statuses.append(value)
        assert not any(stage == "ready" for stage, _ in statuses), "Wake-model load advertised readiness before ASR or microphone"
    finally:
        release.set()


def test_callback_waits_for_delayed_real_engine_without_blocking_runtime(startup, monkeypatch):
    loading, release = threading.Event(), threading.Event()
    sense_engine = object()

    def delayed_sense(*args, **kwargs):
        loading.set()
        assert release.wait(1)
        return sense_engine

    models(monkeypatch, startup, delayed_sense, failed_model)
    startup.hub.async_load_models()
    assert loading.wait(1)
    worker, finished, result = callback_worker(startup)
    try:
        assert not startup.transcribed.wait(.03), "ASR was called with no loaded engine"
        assert startup.runtime.started.is_set()
        from canaan_asr import ASR_LOCK
        assert ASR_LOCK.acquire(blocking=False)
        ASR_LOCK.release()
    finally:
        release.set()
    assert finished.wait(1)
    worker.join(1)
    assert result["value"] == ("synthetic recognized text", "stub")
    assert startup.transcriptions == [(sense_engine, None, False)]


def test_sensevoice_success_is_ready_before_whisper_finishes(startup, monkeypatch):
    whisper_loading, release = threading.Event(), threading.Event()
    sense_engine = object()

    def delayed_whisper(*args, **kwargs):
        whisper_loading.set()
        assert release.wait(1)
        return object()

    models(monkeypatch, startup, lambda **kwargs: sense_engine, delayed_whisper)
    startup.hub.async_load_models()
    assert whisper_loading.wait(1)
    worker, finished, result = callback_worker(startup)
    try:
        assert finished.wait(.3), "A usable SenseVoice engine waited for Whisper"
        assert result["value"] == ("synthetic recognized text", "stub")
        assert startup.transcriptions == [(sense_engine, None, False)]
    finally:
        release.set()
    worker.join(1)


def test_sensevoice_success_whisper_failure_keeps_asr_and_wake_startup(startup, monkeypatch):
    sense_engine = object()
    models(monkeypatch, startup, lambda **kwargs: sense_engine, failed_model)
    loader_index = len(startup.threads)
    startup.hub.async_load_models()
    startup.threads[loader_index].join(1)
    worker, finished, result = callback_worker(startup)
    assert finished.wait(1)
    worker.join(1)
    assert result["value"] == ("synthetic recognized text", "stub")
    assert startup.wake_started.wait(.3), "Whisper failure stopped the usable ASR startup"


def test_both_engines_fail_reports_model_loading_failure_not_empty_audio(startup, monkeypatch):
    models(monkeypatch, startup, failed_model, failed_model)
    loader_index = len(startup.threads)
    startup.hub.async_load_models()
    startup.threads[loader_index].join(1)
    worker, finished, result = callback_worker(startup)
    assert finished.wait(1)
    worker.join(1)
    assert isinstance(result.get("error"), ValueError)
    assert "加载失败" in str(result["error"])
    assert not startup.transcriptions


def test_whisper_success_after_sensevoice_failure_is_usable(startup, monkeypatch):
    whisper_engine = object()
    models(monkeypatch, startup, failed_model, lambda *args, **kwargs: whisper_engine)
    loader_index = len(startup.threads)
    startup.hub.async_load_models()
    startup.threads[loader_index].join(1)
    worker, finished, result = callback_worker(startup)
    assert finished.wait(1)
    worker.join(1)
    assert result["value"] == ("synthetic recognized text", "stub")
    assert startup.transcriptions == [(None, whisper_engine, False)]


def test_engine_loading_timeout_is_explicit(startup):
    ticks = iter((0, 181))
    startup.namespace["time"] = SimpleNamespace(monotonic=lambda: next(ticks))
    worker, finished, result = callback_worker(startup)
    assert finished.wait(1)
    worker.join(1)
    assert isinstance(result.get("error"), ValueError)
    assert "加载超时" in str(result["error"])
    assert not startup.transcriptions


def test_stop_ends_engine_wait_without_transcription(startup):
    worker, finished, result = callback_worker(startup)
    startup.runtime.voices.stop_event.set()
    assert finished.wait(.5)
    worker.join(1)
    assert isinstance(result.get("error"), ValueError)
    assert "关闭" in str(result["error"])
    assert not startup.transcriptions


def test_closed_runtime_never_uses_an_already_ready_engine(startup):
    startup.hub.sense_voice = object()
    startup.hub._asr_ready.set()
    startup.runtime.voices.stop_event.set()
    worker, finished, result = callback_worker(startup)
    assert finished.wait(.5)
    worker.join(1)
    assert isinstance(result.get("error"), ValueError)
    assert "关闭" in str(result["error"])
    assert not startup.transcriptions
