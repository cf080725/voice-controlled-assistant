"""Production Hub recording/ASR methods against versioned in-memory ports.

No Hub, executor, ASR, audio-device, model, learning or database module is
initialized.  ClarifyContext's own atomic-consumption tests live separately;
this suite tests transporting its snapshot across the real Hub methods.
"""
import ast
import contextlib
import ctypes
from pathlib import Path
import queue
import re
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.request

import numpy as np
import pytest

import canaan_turn_pipeline


ROOT = Path(__file__).resolve().parents[1]


def method(name, namespace):
    source = ROOT / "core/canaan_desk_os_hub.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CanaanDeskOSHub")
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[name]


class Trace:
    source = "offline_clarification"
    def __init__(self, **kwargs):
        self.data = {}
    def set(self, key, value):
        self.data[key] = value
    def mark(self, *args):
        pass
    def finish(self, **kwargs):
        self.data.update(kwargs)


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, **unused):
        self.target, self.args, self.kwargs = target, args, kwargs or {}
    def start(self):
        self.target(*self.args, **self.kwargs)


class QuestionPort:
    """Fake of the CI snapshot/matches/consume contract, including no-question."""
    def __init__(self, now):
        self.now, self.generation, self.scope, self.item = now, 0, ("owner", "session-1"), None
    def set(self, text="提醒我喝水"):
        self.generation += 1
        self.item = {"text": text, "guard": "test_missing_time", "question": "什么时候提醒？", "expires": self.now[0] + 600}
    def clear(self):
        self.generation += 1
        self.item = None
    def peek(self):
        return dict(self.item) if self.item and self.now[0] <= self.item["expires"] else None
    def snapshot(self):
        return (self.scope, self.generation, self.item["expires"]) if self.peek() else None
    def matches(self, expected):
        return self.snapshot() == expected
    def consume(self, expected):
        if expected is None or not self.matches(expected):
            return None
        result = self.peek()
        self.clear()
        return result


@pytest.fixture
def voice(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A real native, audio, model, notification or storage port was reached")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace())
    monkeypatch.setattr(ctypes, "WinDLL", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    for name, module in {
        "canaan_events": SimpleNamespace(publish=lambda *a, **k: None),
        "canaan_learning": SimpleNamespace(profile_scope=lambda *a: contextlib.nullcontext()),
        "canaan_os_executor": SimpleNamespace(cancel_reply=lambda *a: "已取消。"),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    now = [100.0]
    questions = QuestionPort(now)
    questions.set()
    token = ["owner", 0, 0, 0]
    binding = SimpleNamespace(token=tuple(token), profile_key="owner", privacy_revision=0,
                              cancelled=threading.Event(), store=object(),
                              memory=SimpleNamespace(messages=lambda: [], add_turn=lambda *a, **k: None))
    calls, models, spoken, merges, finished = [], [], [], [], []
    pending = SimpleNamespace(item=None)
    pending.peek = lambda: pending.item
    pending.has_pending = lambda: pending.item is not None
    pending.last_dropped = lambda: None
    pending.classify = lambda text, expected_item=None: (("confirm", pending.item)
        if text == "确认" and pending.item is not None and (expected_item is None or expected_item is pending.item) else (None, None))
    class VoiceHub(SimpleNamespace):
        def __setattr__(self, name, value):
            # The old rule hook takes one text argument. Keep that hook (and
            # tests replacing it mid-turn) behind the current scoped contract.
            if name == "_context_rule":
                original = value
                value = lambda text, **scope: original(text)
            super().__setattr__(name, value)

    h = VoiceHub(clarify=questions, ui_queue=queue.Queue(), root=SimpleNamespace(), asr_model=None,
        is_processing=False, is_speaking=False, is_recording_command=False, turn_cancelled=False,
        _turn_local=threading.local(), pending=pending, followup_until=105.0, ambient_noise_floor=.001,
        _capture_context=lambda: binding, _context_token=lambda: tuple(token),
        _context_valid=lambda b: not b.cancelled.is_set() and b.token == tuple(token),
        _speaker_ok=lambda *a: True, _context_rule=lambda *a, **scope: None, _notice_query=lambda *a: None,
        _proactive_command=lambda *a: None, wait_ack=lambda: None, start_ack=lambda *a: None,
        fast_match_intent=lambda *a: None, speak=lambda text, *a, **k: spoken.append(text),
        log_conversation=lambda *a, **k: None, _voice_output_busy=lambda: False)
    h.resolve_pending = lambda text, expected_item=None: pending.classify(text, expected_item)
    h.query_canaan_os = lambda text, **k: models.append(text) or {"action": "set_reminder", "param": text, "_model": "synthetic-selected-3b"}
    asr_hook, task_hook, asr_result = [None], [None], [("二十分钟后", "fake_asr")]
    def transcribe(*args, **kwargs):
        if asr_hook[0]:
            asr_hook[0]()
        return asr_result[0]
    def merge(answer, ctx, expected=Ellipsis, stale_result=None):
        merges.append((answer, expected))
        expected = ctx.snapshot() if expected is Ellipsis else expected
        if not ctx.matches(expected):
            return stale_result
        item = ctx.consume(expected)
        if expected is not None and item is None:
            return stale_result
        if answer in ("截个图", "打开计算器", "取消"):
            return None
        return f"{item['text']}，{answer}" if item else None
    def task(*args, **kwargs):
        if task_hook[0]:
            task_hook[0]()
        return "synthetic-task", False
    clock = SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0])
    namespace = dict(np=np, re=re, time=clock,
        threading=SimpleNamespace(Thread=ImmediateThread, local=threading.local), TurnTracer=Trace,
        WAKE_PREFIX_RE=re.compile(r"^团团[,， ]*"), people_command_of=lambda *a: None,
        is_noise_utterance=lambda text: False,
        fact_store_of=lambda *a: None, extract_facts=lambda *a: [], SLOW_ACTIONS=set(),
        clarify_merge=merge, remember_clarify=lambda *a: False, draft_fill=lambda *a: None,
        handle_memory_command=lambda *a: None, resend_request=lambda *a: None,
        resolve_references=lambda decision, *a: decision, apply_command_guard=lambda text, decision, **k: decision,
        canaan_turn_pipeline=canaan_turn_pipeline,
        canaan_contact_match=SimpleNamespace(correct_decision=lambda d: d), keeps_pending=lambda *a: False,
        describe_action=lambda *a: "synthetic", canaan_config=SimpleNamespace(COMPANION_ENABLED=False),
        canaan_gamemode=SimpleNamespace(active=lambda: False), TRIVIAL_ACTIONS={"chat", "set_reminder", "screenshot", "open_app"},
        EMOTION_CONFIG={"confident": {"title": "test"}, "tease": {"title": "test"}},
        failure_detail=lambda text, *a: text, transcribe=transcribe,
        GLOBAL_TASK_MANAGER=SimpleNamespace(check_and_create_task=task, os_exec_lock=threading.RLock(),
            notify_step=lambda *a: None, notify_finish=lambda *a: finished.append(a)),
        CanaanOSExecutor=SimpleNamespace(execute=lambda action, param, **kwargs:
            calls.append((action, param, kwargs)) or {"success": True, "message": "synthetic", "speech_override": "synthetic"}))
    for name in ("prepare_text_identity", "prepare_text_turn", "execute_text_turn", "process_audio_data"):
        setattr(h, name, method(name, namespace).__get__(h))
    h._pending_voice_context_token = method("_pending_voice_context_token", namespace).__get__(h)
    h._pending_voice_clarification_active = method("_pending_voice_clarification_active", namespace).__get__(h)
    h._pending_voice_clarification_filter = method("_pending_voice_clarification_filter", namespace).__get__(h)
    return SimpleNamespace(h=h, questions=questions, now=now, token=token, binding=binding, calls=calls,
                           models=models, spoken=spoken, merges=merges, asr_hook=asr_hook, task_hook=task_hook,
                           namespace=namespace, forbidden=forbidden, asr_result=asr_result, finished=finished)


def turn(f, expected=Ellipsis):
    trace = Trace()
    kwargs = {} if expected is Ellipsis else {"expected_clarification": expected}
    f.h.process_audio_data(np.zeros(3200, np.float32), tracer=trace,
                           expected_context_token=f.binding.token, **kwargs)
    assert not any(event[0] == "status" and event[1][0] == "error" for event in f.h.ui_queue.queue), list(f.h.ui_queue.queue)
    return trace


def replace_same(f):
    f.questions.clear()
    f.questions.set()


@pytest.mark.parametrize("mutation", ["new_question", "same_text", "clear", "expires", "scope"])
def test_question_changed_during_asr_never_consumes_or_executes_short_answer(voice, mutation):
    f = voice
    expected = f.questions.snapshot()
    def change():
        if mutation == "new_question": f.questions.set("提醒我开会")
        elif mutation == "same_text": replace_same(f)
        elif mutation == "clear": f.questions.clear()
        elif mutation == "expires": f.now[0] += 601
        else: f.questions.scope = ("owner", "session-2")
    f.asr_hook[0] = change
    turn(f, expected)
    assert not f.models and not f.calls and not f.merges
    if mutation in ("new_question", "same_text", "scope"):
        assert f.questions.peek() is not None


def test_asr_entry_itself_captures_question_when_legacy_voice_caller_omits_snapshot(voice):
    f = voice
    f.asr_hook[0] = lambda: f.questions.set("提醒我开会")
    turn(f)
    assert not f.models and not f.calls and not f.merges
    assert f.questions.peek()["text"] == "提醒我开会"


def test_audio_started_without_question_cannot_fill_one_created_during_asr(voice):
    f = voice
    f.questions.clear()
    f.asr_hook[0] = lambda: f.questions.set("提醒我开会")
    turn(f, None)
    assert not f.models and not f.calls and not f.merges
    assert f.questions.peek()["text"] == "提醒我开会"


def test_replacement_after_asr_before_merge_does_not_clear_new_question(voice):
    f = voice
    f.task_hook[0] = lambda: f.questions.set("提醒我开会")
    turn(f, f.questions.snapshot())
    assert not f.models and not f.calls
    assert f.questions.peek()["text"] == "提醒我开会"
    assert len(f.finished) == 1 and f.finished[0][1] is False


def test_replacement_inside_atomic_consume_does_not_fall_through_to_model(voice):
    f = voice
    original = f.questions.consume
    def replaced(expected):
        f.questions.set("提醒我开会")
        return original(expected)
    f.questions.consume = replaced
    turn(f, f.questions.snapshot())
    assert not f.models and not f.calls
    assert f.questions.peek()["text"] == "提醒我开会"
    assert len(f.finished) == 1 and f.finished[0][1] is False


@pytest.mark.parametrize("change", ["cancel", "binding", "turn_owner"])
@pytest.mark.parametrize("boundary", ["task_created", "before_model", "after_model"])
def test_created_task_cannot_outlive_its_cancelled_owner_at_processing_boundaries(voice, change, boundary):
    f = voice
    expected = f.questions.snapshot()
    def invalidate():
        if change == "cancel": f.h.turn_cancelled = True
        elif change == "binding": f.binding.cancelled.set()
        else: f.h._active_turn_owner = object()
    if boundary == "task_created":
        f.task_hook[0] = invalidate
    elif boundary == "before_model":
        f.h._context_rule = lambda text: invalidate()
    else:
        original = f.h.query_canaan_os
        def model(text, **kwargs):
            result = original(text, **kwargs)
            invalidate()
            return result
        f.h.query_canaan_os = model
    turn(f, expected)
    assert len(f.models) == (1 if boundary == "after_model" else 0)
    assert not f.calls
    assert len(f.finished) == 1 and f.finished[0][1] is False
    if boundary == "task_created":
        assert not f.merges and f.questions.snapshot() == expected


@pytest.mark.parametrize("text,action", [("截个图", "screenshot"), ("打开计算器", "open_app"), ("取消", "chat")])
def test_new_complete_command_or_cancel_consumes_matching_question_normally(voice, text, action):
    f = voice
    f.asr_result[0] = (text, "fake_asr")
    f.h.fast_match_intent = lambda _: {"action": action, "param": "", "reply": "synthetic"}
    turn(f, f.questions.snapshot())
    assert not f.models and f.calls[0][0] == action
    assert f.questions.peek() is None


def test_prefetched_asr_is_discarded_if_its_question_changed_during_resolution(voice):
    f = voice
    ticket = SimpleNamespace(discard=Mock())
    def resolve(*args, **kwargs):
        f.questions.set("提醒我开会")
        return "二十分钟后", "fake_prefetch"
    ticket.resolve = resolve
    f.h.process_audio_data(np.zeros(3200, np.float32), tracer=Trace(), asr_ticket=ticket,
        expected_context_token=f.binding.token, expected_clarification=f.questions.snapshot())
    ticket.discard.assert_called_once()
    assert not f.models and not f.calls and f.questions.peek()["text"] == "提醒我开会"


def test_already_stale_question_is_rejected_before_resolving_prefetch(voice):
    f = voice
    expected = f.questions.snapshot()
    f.questions.set("提醒我开会")
    ticket = SimpleNamespace(discard=Mock(), resolve=f.forbidden)
    f.h.process_audio_data(np.zeros(3200, np.float32), tracer=Trace(), asr_ticket=ticket,
        expected_context_token=f.binding.token, expected_clarification=expected)
    ticket.discard.assert_called_once()
    assert not f.models and not f.calls and f.questions.peek()["text"] == "提醒我开会"


@pytest.mark.parametrize("change", ["person", "privacy", "owner_turn"])
def test_owner_or_privacy_change_still_discards_old_audio(voice, change):
    f = voice
    def changed():
        if change == "person": f.binding.cancelled.set()
        elif change == "privacy": f.token[2] += 1
        else: f.h._active_turn_owner = object()
    f.asr_hook[0] = changed
    turn(f, f.questions.snapshot())
    assert not f.models and not f.calls and f.questions.peek() is not None


def test_valid_question_snapshot_is_consumed_once_before_synthetic_execution(voice):
    f = voice
    snapshot = f.questions.snapshot()
    turn(f, snapshot)
    assert f.models == ["提醒我喝水，二十分钟后"]
    assert [(a, p) for a, p, _ in f.calls] == [("set_reminder", "提醒我喝水，二十分钟后")]
    assert f.merges == [("二十分钟后", snapshot)] and f.questions.peek() is None
    turn(f, snapshot)
    assert len(f.calls) == 1 and len(f.models) == 1


def test_explicit_no_question_allows_an_ordinary_voice_turn_when_still_absent(voice):
    f = voice
    f.questions.clear()
    turn(f, None)
    assert f.models == ["二十分钟后"] and len(f.calls) == 1


def test_existing_pending_expected_item_confirmation_still_uses_original_task(voice):
    f = voice
    item = {"decision": {"action": "set_reminder", "param": "original reminder"}, "source": "rule", "user_text": "original"}
    f.h.pending.item = item
    f.h.process_audio_data(None, tracer=Trace(), text="确认", expected_pending=item,
                           expected_context_token=f.binding.token)
    assert not f.models and not f.merges
    assert f.calls[0][0:2] == ("set_reminder", "original reminder")
    assert f.calls[0][2]["confirmed"] is True and f.questions.peek() is not None


class Endpointer:
    kind = "fake"
    def __init__(self, frames=4): self.chunks, self.frames = [], frames
    def feed(self, chunk):
        self.chunks.append(chunk)
        return "end" if len(self.chunks) >= self.frames else "speech"
    def is_valid_utterance(self, **kwargs): return True
    def endpoint_wait_ms(self): return 0
    def record_ms(self): return 400
    def get_audio(self, **kwargs): return np.concatenate(self.chunks)


def record(f, on_read=None, *, near_end_filtered=False, waveform_hook=None, deliver=False, frames=4, setup=None):
    finished = threading.Event()
    finished.set()
    f.h._speech_playback_session = {"state": "finished", "finished_at": 98.0, "finished_event": finished}
    if setup:
        setup()
    f.h._begin_asr_prefetch = Mock(return_value=None)
    f.h._merge_wait = lambda stream, endpointer, state, **kwargs: state
    f.h.process_audio_data = Mock(wraps=f.h.process_audio_data if deliver else None)
    def endpointer(**kwargs):
        endpoint = Endpointer(frames)
        original = endpoint.get_audio
        def audio(**kwargs):
            result = original(**kwargs)
            if waveform_hook:
                waveform_hook()
            return result
        endpoint.get_audio = audio
        return endpoint
    namespace = {**f.namespace, "create_endpointer": endpointer}
    f.h.record_followup = method("record_followup", namespace).__get__(f.h)
    class Stream:
        def read(self, frames):
            if on_read:
                on_read()
            return np.full((frames, 1), .05, np.float32), False
    f.h.record_followup(Stream(), np.full(1600, .05, np.float32), near_end_filtered=near_end_filtered)


def test_record_followup_transports_question_snapshot_and_identity(voice):
    f = voice
    expected = f.questions.snapshot()
    record(f)
    assert f.h.process_audio_data.call_args.kwargs["expected_clarification"] == expected
    assert f.h.process_audio_data.call_args.kwargs["expected_context_token"] == f.binding.token


@pytest.mark.parametrize("mutation", ["new_question", "same_text", "none_to_question", "expired"])
def test_recording_cannot_rebind_first_audio_to_a_later_question(voice, mutation):
    f = voice
    if mutation == "none_to_question": f.questions.clear()
    def changed():
        if mutation == "same_text": replace_same(f)
        elif mutation == "expired": f.now[0] += 601
        else: f.questions.set("提醒我开会")
    record(f, changed)
    f.h.process_audio_data.assert_not_called()
    assert not f.h.is_recording_command


def test_filtered_first_frame_cannot_be_rebound_after_question_replacement(voice):
    f = voice
    event = threading.Event()
    event.set()
    f.h._speech_playback_session = {"state": "finished", "finished_at": 98.0, "finished_event": event}
    assert f.h._pending_voice_clarification_filter(np.full(1600, .05, np.float32)) is not None
    f.questions.set("提醒我开会")
    record(f, near_end_filtered=True)
    f.h.process_audio_data.assert_not_called()
    assert f.questions.peek()["text"] == "提醒我开会"


def test_final_waveform_handoff_carries_old_snapshot_for_processing_to_reject(voice):
    f = voice
    expected = f.questions.snapshot()
    record(f, waveform_hook=lambda: f.questions.set("提醒我开会"), deliver=True)
    f.h.process_audio_data.assert_called_once()
    assert f.h.process_audio_data.call_args.kwargs["expected_clarification"] == expected
    assert not f.models and not f.calls and f.questions.peek()["text"] == "提醒我开会"


def test_question_version_reads_are_bounded_instead_of_querying_each_audio_frame(voice):
    f = voice
    f.questions.snapshot = Mock(wraps=f.questions.snapshot)
    record(f, frames=20)
    f.h.process_audio_data.assert_called_once()
    assert f.questions.snapshot.call_count <= 6


@pytest.mark.parametrize("frames", [4, 20])
@pytest.mark.parametrize("persistent", [False, True])
def test_all_question_reads_are_bounded_and_audio_still_reaches_handoff(voice, frames, persistent):
    f = voice
    if persistent:
        context = real_clarification(f)
        slots = {"text": "提醒我喝水", "guard": "test_missing_time", "question": "什么时候提醒？"}
        store = SimpleNamespace(active_task=Mock(return_value={"id": "task-1", "version": 1,
                                "session_id": None, "slots": slots}), finish_task=f.forbidden)
        context.store = store
    else:
        context = f.questions
        store = None
    context.peek = Mock(wraps=context.peek)
    context.snapshot = Mock(wraps=context.snapshot)
    record(f, frames=frames)
    f.h.process_audio_data.assert_called_once()
    np.testing.assert_array_equal(f.h.process_audio_data.call_args.args[0], np.full(1600 * frames, .05, np.float32))
    assert context.snapshot.call_count <= 6
    assert context.peek.call_count <= 7
    if store:
        assert store.active_task.call_count <= 7


def test_captured_short_answer_keeps_reference_tail_filtering_for_every_frame(voice):
    f = voice
    frames = []
    listener = SimpleNamespace(bargein_audio=np.zeros(0, np.float32))
    def feed(audio, **kwargs):
        assert kwargs == {"playback_silent": True}
        frames.append(audio.copy())
        listener.bargein_audio = audio * .5
    listener.feed = feed
    def prepare():
        f.h._speech_playback_session["finished_at"] = f.now[0] - .1
        f.h._clarify_voice_listener = listener
        f.h._clarify_voice_session = f.h._speech_playback_session
        f.h._clarify_voice_context = f.binding.token
        f.h._clarify_voice_question = f.questions.snapshot()
        f.h._clarify_voice_question_session = f.h._speech_playback_session
        f.h._clarify_voice_question_context = f.binding.token
    record(f, frames=20, setup=prepare)
    f.h.process_audio_data.assert_called_once()
    assert len(frames) == 20
    np.testing.assert_array_equal(f.h.process_audio_data.call_args.args[0], np.full(1600 * 20, .025, np.float32))


@pytest.mark.parametrize("change", ["speaking", "cancelled", "ack"])
def test_captured_slot_does_not_bypass_playback_or_ack_gates(voice, change):
    f = voice
    def changed():
        if change == "speaking": f.h.is_speaking = True
        elif change == "cancelled": f.h._speech_playback_session["cancelled"] = True
        else: f.h._ack_playback_session = {"state": "active"}
    record(f, changed)
    f.h.process_audio_data.assert_not_called()
    assert not f.h.is_recording_command


@pytest.mark.parametrize("prefetch", [False, True])
def test_asr_stop_polling_does_not_query_persisted_question_every_callback(voice, prefetch):
    f = voice
    expected = f.questions.snapshot()
    f.questions.snapshot = Mock(wraps=f.questions.snapshot)
    def resolve(*args, should_stop, **kwargs):
        for _ in range(50):
            assert not should_stop()
        return "二十分钟后", "fake_asr"
    kwargs = {}
    if prefetch:
        kwargs["asr_ticket"] = SimpleNamespace(resolve=resolve, discard=Mock())
    else:
        f.namespace["transcribe"] = resolve
    f.h.process_audio_data(np.zeros(3200, np.float32), tracer=Trace(),
        expected_context_token=f.binding.token, expected_clarification=expected, **kwargs)
    assert len(f.calls) == 1 and f.questions.snapshot.call_count <= 10


def real_clarification(f):
    """Load CI's actual version/consumption contract without its client imports."""
    source = ROOT / "core/canaan_intent.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
             and node.name in ("ClarifyContext", "clarify_merge")]
    nodes += [node for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "_ANSWER_FIRST_GUARDS" for target in node.targets)]
    namespace = {"threading": threading, "re": re, "time": SimpleNamespace(monotonic=lambda: f.now[0]),
                 "_DRAFT_CANCEL_RE": re.compile(r"^取消$"), "_CANCEL_RE": re.compile(r"^取消$"),
                 "fast_match_intent": lambda text: f.h.fast_match_intent(text), "is_noise_utterance": lambda _: False}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    context = namespace["ClarifyContext"](ttl=600, clock=lambda: f.now[0])
    # Generic missing-slot wording isolates the voice revision contract.  The
    # reminder parser/body/time integration is tested by its separate owner.
    context.set("提醒我喝水", "test_missing_time", "什么时候提醒？")
    f.h.clarify = f.questions = context
    f.namespace["clarify_merge"] = namespace["clarify_merge"]
    return context


def test_real_ci_and_hub_complete_one_matching_question_once(voice):
    f = voice
    context = real_clarification(f)
    expected = context.snapshot()
    turn(f, expected)
    assert f.models == ["提醒我喝水，二十分钟后"] and len(f.calls) == 1
    assert context.peek() is None
    turn(f, expected)
    assert len(f.calls) == 1


@pytest.mark.parametrize("change", ["new", "same", "session", "reset", "expiry"])
def test_real_ci_revision_is_preserved_through_actual_hub_asr(change, voice):
    f = voice
    context = real_clarification(f)
    expected = context.snapshot()
    def mutation():
        if change == "session": context.bind("owner", "new-session")
        elif change == "reset": context.reset_local()
        elif change == "expiry": f.now[0] += 601
        else:
            if change == "same": context.clear()
            context.set("提醒我喝水" if change == "same" else "提醒我开会", "test_missing_time", "什么时候提醒？")
    f.asr_hook[0] = mutation
    turn(f, expected)
    assert not f.models and not f.calls
    if change in ("new", "same", "session"):
        assert context.peek() is not None


@pytest.mark.parametrize("text,action", [("打开计算器", "open_app"), ("取消", "chat")])
def test_real_ci_consumed_new_command_remains_distinct_from_stale(voice, text, action):
    f = voice
    context = real_clarification(f)
    f.asr_result[0] = (text, "fake_asr")
    f.h.fast_match_intent = lambda _: {"action": action, "param": "", "reply": "synthetic"}
    turn(f, context.snapshot())
    assert not f.models and f.calls[0][0] == action and context.peek() is None


def test_real_ci_consume_race_cannot_fall_through_as_an_ordinary_model_turn(voice):
    f = voice
    context = real_clarification(f)
    original = context.consume
    def changed(expected):
        context.set("提醒我开会", "test_missing_time", "什么时候提醒开会？")
        return original(expected)
    context.consume = changed
    turn(f, context.snapshot())
    assert not f.models and not f.calls and context.peek()["text"] == "提醒我开会"
