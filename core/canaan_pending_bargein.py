"""Short pending replies over playback, with reference rejection before full ASR.

Playback alone is never evidence of consent. The listener requires near-end
speech after echo cancellation and rejects audio matching the playback tap.
All audio stays in bounded memory and long, unfinished input is discarded.
"""
import time
from collections import deque

import numpy as np

SR = 16000
MIN_REPLY_SAMPLES = int(.2 * SR)


class AmbientNoiseFloor:
    """Bounded lower-envelope estimate; nearby conversation is not room silence."""

    def __init__(self):
        self._levels = deque([.002] * 10, maxlen=100)
        self.value = .002

    def update(self, rms):
        if not np.isfinite(rms) or rms < 0:
            return self.value
        self._levels.append(float(rms))
        # Ten seconds of idle samples, using the quieter fifth. Even sustained
        # nearby speech cannot make a later short reply require shouting.
        self.value = min(.008, max(.0002, float(np.quantile(self._levels, .2))))
        return self.value


def _similarity(samples, history):
    """Largest normalized reference match, allowing device/room delay."""
    x = np.asarray(samples, np.float32).reshape(-1)
    h = np.asarray(history, np.float32).reshape(-1)
    if not len(x) or len(h) < len(x):
        return 0.0
    x = x - x.mean()
    h = h - h.mean()
    energy = float(np.dot(x, x))
    if energy < 1e-9:
        return 0.0
    sums = np.concatenate(([0.0], np.cumsum(h.astype(np.float64) ** 2)))
    windows = sums[len(x):] - sums[:-len(x)]
    denom = np.sqrt(np.maximum(windows * energy, 1e-18))
    scores = np.abs(np.correlate(h, x, mode="valid")) / denom
    return min(1.0, float(scores.max(initial=0.0)))


def _cubic_echo_similarity(samples, history):
    """Strong evidence of cubic playback residue after its linear part was cancelled.

    This bounded reference model covers a specific nonlinear echo case; it is
    not a general room/voice classifier. Only the strong-echo fallback uses it.
    """
    reference = np.asarray(history, np.float32).reshape(-1)
    if not len(reference):
        return 0.0
    reference = reference - reference.mean()
    scale = float(np.max(np.abs(reference), initial=0.0))
    if scale < 1e-6:
        return 0.0
    reference = reference / scale
    cubic = reference * reference * reference
    energy = float(np.dot(reference, reference))
    cubic -= reference * (float(np.dot(cubic, reference)) / max(energy, 1e-9))
    return _similarity(samples, cubic)


class PendingSpeechListener:
    """A per-pending, full-utterance listener; output still needs ASR and consent parsing."""

    def __init__(self, noise_floor=.002, *, canceller=None, reference=None, clock=time.perf_counter,
                 max_record_sec=3.5):
        import canaan_aec
        self.canceller = canceller if canceller is not None else canaan_aec.make_canceller()
        self.reference = reference if reference is not None else canaan_aec.REF
        self.clock = clock
        # 10-24 live log reached .109 after idle speech polluted the old EMA.
        # This is an onset gate only: reference rejection and full ASR still
        # decide whether a complete, task-bound confirmation was actually said.
        floor = float(noise_floor)
        self.threshold = max(.006, min(.020, floor * 2.5 if np.isfinite(floor) else .006))
        self.max_samples = int(min(25.0, max(.3, float(max_record_sec))) * SR)
        self._preroll = deque(maxlen=3)
        self._audio = []
        self._voiced = self._quiet = self._samples = 0
        self._blocked = False
        # 原声几乎就是播报时，回声消除后还剩多少能量（近 2 秒的残留回声水平）
        self._residual = deque(maxlen=20)
        self.bargein_audio = np.zeros(0, np.float32)
        self.dropped = None  # 最近一次丢弃整句的原因，只给日志用
        # 一句话里累计 0.2 秒近端人声：先停提示，同一门限也用于提交完整短答。
        self.onset = False
        self._run = 0
        self._onset_sent = False
        self._stats = {"blocks": 0, "loud": 0, "echo": 0, "near": 0, "raw_max": 0.0, "clean_max": 0.0}
        self._last_stats = None
        self._last_near_end_at = None
        self.last_timing = None

    def _reset(self):
        self._last_near_end_at = None
        self.last_timing = None
        self._audio = []
        self._voiced = self._quiet = self._samples = 0
        self._run = 0
        self._onset_sent = False
        self._stats = {"blocks": 0, "loud": 0, "echo": 0, "near": 0, "raw_max": 0.0, "clean_max": 0.0}

    def stats_text(self):
        """上一句（或正在收的这句）的声学统计，一行文字，给日志用。"""
        s = getattr(self, "_last_stats", None) or self._stats
        floor = float(np.median(self._residual)) if self._residual else 0.0
        return (f"块数 {s['blocks']}，够响 {s['loud']}，像播报 {s['echo']}，算人声 {s['near']}，"
                f"原声峰值 {s['raw_max']:.3f}，消除后峰值 {s['clean_max']:.3f}，残留回声 {floor:.4f}，"
                f"基线 {len(self._residual)} 块，门限 {self.threshold:.3f}")

    @property
    def capturing(self):
        return bool(self._audio)

    def discontinuity(self):
        """Input overflow or profile change invalidates the whole partially heard answer."""
        self._reset()
        self._preroll.clear()
        self.bargein_audio = np.zeros(0, np.float32)
        self.onset = False
        self._blocked = True
        self.dropped = "input_gap"

    def feed(self, microphone, *, playback_silent=False):
        self.last_timing = None
        self.bargein_audio = np.zeros(0, np.float32)
        raw = np.asarray(microphone, np.float32).reshape(-1)
        if not len(raw):
            return None
        now = self.clock()
        ref = self.reference.take(now - .03, len(raw))
        history = self.reference.take(now + .1, len(raw) + SR)
        active = self.reference.active(now=now)
        valid = (len(ref) == len(raw) and len(history) >= len(raw)
                 and np.isfinite(ref).all() and np.isfinite(history).all())
        energy = valid and float(np.dot(history, history)) >= 1e-8
        if not valid or (not energy and (active or not playback_silent)) or (not active and not playback_silent):
            self._reset()
            self._preroll.clear()
            self._blocked = True  # Missing playback evidence must never enable a raw-audio consent path.
            return None
        # Silence requires an explicit current playback lifecycle proof from the hub.
        # An absent/broken tap during playback still cannot open this path.
        clean = (raw.copy() if not active and not energy and playback_silent else
                 np.asarray(self.canceller.process_stream(raw, ref), np.float32).reshape(-1))
        if not len(clean):
            return None
        rms = float(np.sqrt(np.mean(clean ** 2)))
        raw_rms = float(np.sqrt(np.mean(raw ** 2)))
        raw_echo = _similarity(raw, history) >= .86
        clean_similarity = _similarity(clean, history)
        floor = float(np.median(self._residual)) if self._residual else None
        near_end = rms >= self.threshold and not raw_echo and clean_similarity < .60
        if not near_end and raw_echo and rms >= self.threshold and floor is not None:
            # 双讲：主人和播报同时出声时原声当然像播报，改看消除后的能量是否远高于残留回声
            near_end = rms >= max(self.threshold, 6 * floor) and clean_similarity < .85
            if near_end:
                # 只拒绝 >=.97 的强非线性参考匹配；不提高人声门限、不增加等待，
                # 也不把未知的非线性环境宣称为已解决。普通近端路径不计算这一项。
                near_end = _cubic_echo_similarity(clean, history) < .97
        # 当前块不能先抬高用于判定它自身的门槛。原声像播报并不足以证明只有回声：
        # 消除后仍匹配播报，或低于人声门限，才登记为下一块可用的 echo-only 基线。
        if raw_echo and not near_end and (rms < self.threshold or clean_similarity >= .85):
            self._residual.append(rms)
        if near_end:
            self.bargein_audio = clean
        if self._audio or near_end:
            # 只记数字（不存声音）：这句里各块的原声/消除后能量、是否像播报、是否算人声，给真机调参用
            s = self._stats
            s["blocks"] += 1
            s["loud"] += rms >= self.threshold
            s["echo"] += raw_echo
            s["near"] += near_end
            s["raw_max"] = max(s["raw_max"], raw_rms)
            s["clean_max"] = max(s["clean_max"], rms)
        if self._blocked:
            self._quiet = 0 if near_end else self._quiet + len(clean)
            if self._quiet >= int(.3 * SR):
                self._blocked = False
                self._reset()
                self._preroll.clear()
            return None
        if not self._audio:
            if not near_end:
                self._preroll.append(clean.copy())
                return None
            self._audio = list(self._preroll)
            self._preroll.clear()
        self._audio.append(clean.copy())
        self._samples += len(clean)
        if near_end:
            self._last_near_end_at = now
            self._voiced += len(clean)
            self._quiet = 0
            self._run += len(clean)
            # 10-23 真机两次都只收到 0.20 秒有效人声（双讲时近端帧断断续续），连续 0.3 秒从没触发过；
            # 改成这句里累计 0.2 秒就停提示。离线模拟误停会多一些，主人选了宁可误停（字幕还在、可直接再说）。
            if self._voiced >= MIN_REPLY_SAMPLES and not self._onset_sent:
                self.onset = self._onset_sent = True
        else:
            self._quiet += len(clean)
            self._run = 0
        if self._samples >= self.max_samples:
            self._last_stats = dict(self._stats)
            self._reset()
            self._blocked = True  # Never classify a tail cut from a long utterance.
            self.dropped = "too_long"
            return None
        if self._quiet < int(.3 * SR):
            return None
        audio = np.concatenate(self._audio) if self._voiced >= MIN_REPLY_SAMPLES else None
        self.dropped = None if audio is not None else f"voiced_{self._voiced / SR:.2f}s"
        self._last_stats = dict(self._stats)
        timing = None
        if audio is not None and self._last_near_end_at is not None:
            wait = now - self._last_near_end_at
            if 0 <= wait <= 30:
                timing = {"endpoint_wait_sec": wait, "frame_sec": len(raw) / SR}
        self._reset()
        self.last_timing = timing
        return audio
