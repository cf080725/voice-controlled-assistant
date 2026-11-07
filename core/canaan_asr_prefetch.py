"""Bounded, audio-only SenseVoice speculation during endpoint silence.

The recorder may call ``start(ep.get_audio(), sense_voice)`` after at least
300 ms of silence. Discard that ticket if speech resumes. The final ASR worker
calls ``resolve(ticket, final_audio, sense_voice)`` before its normal ASR path.
Only an identical immutable waveform and the identical model can produce a hit.
This module never invokes Whisper, intent handling, or any application action.
"""
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from canaan_asr import ASR_LOCK, to_mono_f32, transcribe_sensevoice


@dataclass
class _Outcome:
    text: str = ""
    error: str = ""
    discarded: bool = False
    duration_ms: float = 0.0
    inference_ms: float = 0.0


@dataclass(frozen=True, eq=False)
class PrefetchTicket:
    """One recognition attempt; the snapshot cannot be changed or made writable."""

    audio: np.ndarray
    samples: int
    sha256: str
    model: object
    created_at: float
    done: threading.Event = field(default_factory=threading.Event)
    _outcome: _Outcome = field(default_factory=_Outcome, repr=False)

    @property
    def error(self):
        return self._outcome.error

    @property
    def duration_ms(self):
        """Worker scheduling plus inference, excluding endpoint waiting."""
        return self._outcome.duration_ms

    @property
    def inference_ms(self):
        return self._outcome.inference_ms


class ASRPrefetch:
    """At most one running daemon task, with no backlog and no ASR-lock wait.

    ``resolve`` returns ``(text, "sensevoice")`` on a valid successful hit,
    otherwise ``None`` so the caller can retain its existing ASR fallback.
    Matching unfinished work is awaited once; it is never started again here.
    ``max_age_sec`` bounds a wait for a hung model and invalidates stale results.
    No audio or recognition text is logged.
    """

    def __init__(self, *, transcriber=None, asr_lock=None, max_age_sec=3.0, clock=None):
        if max_age_sec <= 0:
            raise ValueError("max_age_sec must be positive")
        self._transcribe = transcriber if transcriber is not None else transcribe_sensevoice
        self._asr_lock = asr_lock if asr_lock is not None else ASR_LOCK
        self._max_age_sec = float(max_age_sec)
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._ticket = None
        self._busy = False

    def _valid(self, ticket):
        return (
            ticket is self._ticket
            and not ticket._outcome.discarded
            and self._clock() - ticket.created_at < self._max_age_sec
        )

    def start(self, audio, sense_voice):
        """Copy audio and submit immediately, or skip when another task is active."""
        if sense_voice is None:
            return None
        with self._lock:
            if self._busy:
                return None
            normalized = to_mono_f32(audio)
            if not len(normalized):
                return None
            # A bytes owner makes setflags(write=True) fail too; a writable
            # ndarray with write=False alone could be made writable again.
            data = normalized.tobytes(order="C")
            snapshot = np.frombuffer(data, dtype=np.float32)
            ticket = PrefetchTicket(
                audio=snapshot, samples=len(snapshot), sha256=hashlib.sha256(data).hexdigest(),
                model=sense_voice, created_at=self._clock(),
            )
            if self._ticket is not None:
                self._ticket._outcome.discarded = True
            self._ticket = ticket
            self._busy = True
            worker = threading.Thread(target=self._run, args=(ticket,), name="canaan-asr-prefetch", daemon=True)
            try:
                worker.start()
            except Exception:
                self._busy = False
                ticket._outcome.error = "thread_start_failed"
                ticket.done.set()
                return None
            return ticket

    def _run(self, ticket):
        acquired = False
        started = None
        try:
            acquired = self._asr_lock.acquire(blocking=False)
            if not acquired:
                ticket._outcome.error = "lock_busy"
                return
            with self._lock:
                if not self._valid(ticket):
                    ticket._outcome.error = "discarded_or_expired"
                    return
            started = time.perf_counter()
            text = self._transcribe(ticket.model, ticket.audio)
            if isinstance(text, str) and text.strip():
                ticket._outcome.text = text.strip()
            else:
                ticket._outcome.error = "empty_result"
        except Exception as exc:
            ticket._outcome.error = type(exc).__name__
        finally:
            if started is not None:
                ticket._outcome.inference_ms = (time.perf_counter() - started) * 1000
            if acquired:
                self._asr_lock.release()
            with self._lock:
                ticket._outcome.duration_ms = max(0, self._clock() - ticket.created_at) * 1000
                self._busy = False
                ticket.done.set()

    def discard(self, ticket=None):
        """Invalidate a turn without cancelling or reusing its running inference."""
        with self._lock:
            if self._ticket is not None and (ticket is None or ticket is self._ticket):
                self._ticket._outcome.discarded = True
                self._ticket = None

    def resolve(self, ticket, final_audio, sense_voice, *, should_stop=None):
        """Return only an exact successful current ticket; never run inference."""
        if ticket is None:
            return None
        if should_stop is not None and should_stop():
            self.discard(ticket)
            return None
        with self._lock:
            if not self._valid(ticket):
                return None
        final = to_mono_f32(final_audio)
        matches = (
            sense_voice is ticket.model
            and len(final) == ticket.samples
            and hashlib.sha256(final.tobytes(order="C")).hexdigest() == ticket.sha256
        )
        if not matches:
            self.discard(ticket)
            return None
        # Never hold the manager mutex while waiting: continued speech, a new
        # turn, or shutdown can invalidate this ticket concurrently.
        while not ticket.done.is_set():
            if should_stop is not None and should_stop():
                self.discard(ticket)
                return None
            with self._lock:
                if not self._valid(ticket):
                    return None
                remaining = self._max_age_sec - (self._clock() - ticket.created_at)
            ticket.done.wait(min(.05, max(0, remaining)))
        if should_stop is not None and should_stop():
            self.discard(ticket)
            return None
        with self._lock:
            if not self._valid(ticket) or ticket.error or not ticket._outcome.text:
                return None
            return ticket._outcome.text, "sensevoice"


_REQUEST_QUOTE_HEAD_RE = re.compile(r"(?:搜索|查一下|查下|搜一下|搜下|查|搜|输入|写入|记住|打开|启动|播放|把|将)\s*")
_MESSAGE_CHANNEL = r"(?:(?:用|通过|在)?聊天软件(?:上|里)?\s*)?"
_MESSAGE_REQUEST_RE = re.compile(
    _MESSAGE_CHANNEL + r"(?:给|跟|向|对)[^，,。.!！?？：:\"“”'‘’「」『』]{1,12}?"
    r"(?:发(?:送)?(?:一条|一则|个|条|句)?(?:聊天软件(?:消息)?|消息|短信|信息)?(?:说)?|说(?:一声)?)"
    r"[：:，,\s]*(?P<body>.*)", re.S,
)
_DELIMITED_RELAY_REQUEST_RE = re.compile(
    _MESSAGE_CHANNEL + r"(?:告诉|通知|提醒|转告)[^，,。.!！?？：:\"“”'‘’「」『』\s]{1,12}"
    r"[：:，,\s]+(?:说(?:一声)?[：:，,\s]*)?(?P<body>.*)", re.S,
)
_MISSING_DESTINATION_RE = re.compile(
    r"(?:(?:把|将)\S{1,40}|(?:屏幕)?亮度|(?:系统)?音量|声音|窗口|文件(?:夹)?|目录)"
    r"(?:调到|调整到|设置为|设为|改成|改为|降低到|增加到|切换到|移动到|复制到|保存到|发送到|重命名为)"
)
_MISSING_PREDICATE_RE = re.compile(
    r"(?:把|将)(?:那个|这个|我的|这台|那台|当前|现在的|刚(?:下载|保存|创建|打开|收到)的|(?:这台|那台)?电脑的)?"
    r"(?:电脑|聊天软件|软件|程序|屏幕|音量|亮度|浏览器|记事本|文件|文件夹|窗口|目录|它|这个|那个)"
)


def _message_request_body(clean):
    """Commit to the first message verb; never absorb body text into a name.

    A delimited relay has an explicit recipient boundary. A bare ``告诉`` and
    arbitrary characters do not: they may already include a complete short
    message, so that ambiguous form cannot authorize extra listening.
    """
    match = _MESSAGE_REQUEST_RE.match(clean) or _DELIMITED_RELAY_REQUEST_RE.match(clean)
    return match.group("body") if match is not None else None


def _unclosed_request_quote(clean):
    """Only a quote starting a request's data field can extend the recording.

    Inspect the enclosing delimiter, without interpreting any action words in
    the data. ASCII apostrophes inside words and escaped quotes are literals.
    An unmatched quote in reported conversation is not an incomplete request.
    """
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'"}
    for index, char in enumerate(clean):
        if char not in pairs:
            continue
        if char == "'" and index and re.match(r"[A-Za-z0-9_]", clean[index - 1]):
            continue
        head = clean[:index].strip()
        if not (_REQUEST_QUOTE_HEAD_RE.fullmatch(head) or _message_request_body(head) == ""):
            # Later quotes in this field cannot change the enclosing boundary.
            return False
        closer = pairs[char]
        pos = index + 1
        while pos < len(clean):
            if char in ('"', "'") and clean[pos] == "\\":
                pos += 2
                continue
            if clean[pos] == closer:
                if char == "'" and pos + 1 < len(clean) and re.match(r"[A-Za-z0-9_]", clean[pos - 1]) and re.match(r"[A-Za-z0-9_]", clean[pos + 1]):
                    pos += 1
                    continue
                return False
            pos += 1
        return True
    return False


def needs_continuation(text):
    """Recognize unfilled request syntax, without generating or executing intent.

    This is a conservative listening hint, not a general sentence parser. A
    complete message can end with an action phrase; its body stays opaque.
    """
    from canaan_intent import _strip_prefix, is_noise_utterance, mask_quotes, negative_request
    request = _strip_prefix((text or "").strip()).strip("。.!！?？~～、…·- ")
    clean = request.strip("，,:： ")
    if not clean or clean.casefold() in {
        "好", "行", "嗯", "对", "是", "确认", "取消", "不", "停", "暂停", "继续", "你好", "嗨",
        "yes", "no", "ok", "okay", "hi", "hey", "hello", "stop", "cancel",
    }:
        return False
    if is_noise_utterance(clean):
        return True
    if _unclosed_request_quote(clean):
        return True
    if negative_request(clean):
        return False
    body = _message_request_body(request)
    if body is not None:
        return not body.strip("，,。.!！?？:：~～、…·- ")
    clean = mask_quotes(clean)
    if _MISSING_DESTINATION_RE.fullmatch(clean) or _MISSING_PREDICATE_RE.fullmatch(clean):
        return True
    if re.fullmatch(r"(?:把|将)(?:(?:那个|这个|我的|这台|那台)?(?:电脑|聊天软件|软件|程序|屏幕|音量|亮度|浏览器|记事本|文件|文件夹|它|这个|那个))?", clean):
        return True
    if re.fullmatch(r"(?:音量|亮度|屏幕|声音|那个|这个|然后)", clean):
        return True
    if re.fullmatch(r"(?:打开|启动|关闭|关掉|运行|搜索|查|搜|找|看|看看|查查|搜搜|找找|播放|放)(?:一下|下)?", clean):
        return True
    if re.fullmatch(r"(?:我)?(?:找|查|搜|看)(?:一下|下)?(?:那个|这个|什么来着)", clean):
        return True
    return False


class RecordingPrefetch:
    """One recording's provisional result, separate from its final recognition.

    At least 300 ms of counted silence makes get_audio(.3) stable. Confirmed
    speech thresholds stay unchanged; resumed speech invalidates the old work.
    Longer than 12 seconds is left to the ordinary path to limit wasted CPU.
    """

    def __init__(self, manager, model, min_speech_sec=.25, grace_sec=0):
        self.manager = manager
        self.model = model
        self.min_speech_sec = min_speech_sec
        self.grace_sec = min(3.0, max(0.0, float(grace_sec)))
        self.extended = False
        self.ticket = None
        self._speech_marker = None
        self._attempted_speech = None
        self._closed = False

    def observe(self, endpointer):
        if self._closed:
            return endpointer.state
        if endpointer.state in ("no_speech", "max_len"):
            self.discard()
            return endpointer.state
        if self.ticket is not None and endpointer.speech_samples != self._speech_marker:
            self.manager.discard(self.ticket)
            self.ticket = None
        if endpointer.state == "end":
            # The input thread only reads completed work. Never wait here.
            if self.grace_sec and endpointer.endpoint_wait_ms() < self.grace_sec * 1000 and self.ticket is not None and self.ticket.done.is_set():
                result = self.resolve(endpointer.get_audio(keep_tail_sec=.3), self.model)
                if result and needs_continuation(result[0]):
                    endpointer.state = "speaking"
                    self.extended = True
            return endpointer.state
        if endpointer.state != "speaking" or endpointer.silence_samples < round(.3 * endpointer.sr):
            return endpointer.state
        if not endpointer.is_valid_utterance(min_speech_sec=self.min_speech_sec):
            return endpointer.state
        if self._attempted_speech == endpointer.speech_samples:
            return endpointer.state
        audio = endpointer.get_audio(keep_tail_sec=.3)
        if len(audio) > 12 * endpointer.sr:
            self._attempted_speech = endpointer.speech_samples
            return endpointer.state
        self._speech_marker = endpointer.speech_samples
        self.ticket = self.manager.start(audio, self.model)
        if self.ticket is not None:
            # Occupied workers reject admission without inference or queuing.
            # Retry on the next silence tick; an admitted attempt stays unique.
            self._attempted_speech = endpointer.speech_samples
        return endpointer.state

    def resolve(self, final_audio, current_model, *, should_stop=None):
        if self._closed:
            return None
        return self.manager.resolve(self.ticket, final_audio, current_model, should_stop=should_stop)

    def discard(self):
        self._closed = True
        if self.ticket is not None:
            self.manager.discard(self.ticket)
        self.ticket = None
