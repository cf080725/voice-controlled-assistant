# -*- coding: utf-8 -*-
"""
唤醒后的语音端点检测（判断用户什么时候说完）。

两种实现，接口一致：
    SileroEndpointer  基于 sherpa_onnx.VoiceActivityDetector + silero_vad.onnx
    EnergyEndpointer  原来的 RMS 能量阈值法（模型或 API 不可用时的兜底）

用法：
    ep = create_endpointer(noise_floor=0.002)
    for chunk in stream:              # 每块 float32 一维数组，建议 100ms
        state = ep.feed(chunk)
        if state in ("end", "no_speech", "max_len"):
            break
    audio = ep.get_audio()            # 已裁掉多余的尾部静音

hang-over（说完后要等多久的静音）:
    已说时长 < long_speech_sec  → hangover_short（默认 0.8s，给“给谁谁发个……”这类思考停顿留余地）
    已说时长 >= long_speech_sec → hangover_long（默认 0.6s）
旧版能量法分别是 1.8s / 1.3s。
"""
import os
from collections import deque

import numpy as np

try:
    import canaan_config as _cfg
except Exception:  # 单独拷贝本文件时也能用
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


SAMPLE_RATE = 16000

STATE_WAITING = "waiting"      # 还没开口
STATE_SPEAKING = "speaking"    # 说话中或短暂停顿
STATE_END = "end"              # 正常说完
STATE_NO_SPEECH = "no_speech"  # 唤醒后一直没开口
STATE_MAX_LEN = "max_len"      # 超过最长录音时长


class _BaseEndpointer:
    kind = "base"

    def __init__(self, sample_rate=SAMPLE_RATE,
                 hangover_short=None, hangover_long=None, long_speech_sec=None,
                 no_speech_timeout=None, max_record_sec=None, preroll_sec=0.25):
        self.sr = sample_rate
        self.hangover_short = hangover_short if hangover_short is not None else _c("ENDPOINT_HANGOVER_SHORT_SEC", 0.8)
        self.hangover_long = hangover_long if hangover_long is not None else _c("ENDPOINT_HANGOVER_LONG_SEC", 0.6)
        self.long_speech_sec = long_speech_sec if long_speech_sec is not None else _c("ENDPOINT_LONG_SPEECH_SEC", 1.6)
        self.no_speech_timeout = no_speech_timeout if no_speech_timeout is not None else _c("ENDPOINT_NO_SPEECH_TIMEOUT_SEC", 4.0)
        self.max_record_sec = max_record_sec if max_record_sec is not None else _c("ENDPOINT_MAX_RECORD_SEC", 25.0)
        self.chunks = []
        self.total_samples = 0
        self.speech_samples = 0
        self.has_spoken = False
        self.silence_samples = 0   # 最近一次有声之后累计的静音
        self.state = STATE_WAITING
        # VAD needs a few frames before confirming speech. Retain a bounded
        # onset buffer, but never count it toward the valid-speech threshold.
        self._preroll = deque()
        self._preroll_samples = 0
        self._preroll_limit = max(0, int(round(preroll_sec * self.sr)))

    def _remember_onset(self, chunk):
        if not self._preroll_limit:
            return
        self._preroll.append(chunk.copy())
        self._preroll_samples += len(chunk)
        while self._preroll_samples > self._preroll_limit:
            overflow = self._preroll_samples - self._preroll_limit
            first = self._preroll.popleft()
            drop = min(overflow, len(first))
            self._preroll_samples -= drop
            if drop < len(first):
                self._preroll.appendleft(first[drop:].copy())

    # 子类实现：返回当前块是否判为有声
    def _is_speech(self, chunk):
        raise NotImplementedError

    def _extra_silence_lag(self):
        """VAD 内部判定静音的滞后（秒），计入总等待时长。"""
        return 0.0

    def required_silence_sec(self):
        spoken = self.speech_samples / self.sr
        target = self.hangover_short if spoken < self.long_speech_sec else self.hangover_long
        return max(0.0, target - self._extra_silence_lag())

    def feed(self, chunk):
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        remaining = max(0, int(round(self.max_record_sec * self.sr)) - self.total_samples)
        if not remaining:
            self.state = STATE_MAX_LEN
            return self.state
        chunk = chunk[:remaining]
        n = len(chunk)
        if not n:
            return self.state
        self.total_samples += n
        speech = self._is_speech(chunk)
        if speech:
            if not self.has_spoken:
                self.chunks.extend(self._preroll)
                self._preroll.clear()
                self._preroll_samples = 0
            self.has_spoken = True
            self.speech_samples += n
            self.silence_samples = 0
            self.chunks.append(chunk.copy())
            self.state = STATE_SPEAKING
        elif self.has_spoken:
            self.chunks.append(chunk.copy())
            self.silence_samples += n
            if self.silence_samples / self.sr >= self.required_silence_sec():
                self.state = STATE_END
        else:
            self._remember_onset(chunk)
            self.silence_samples += n
            if self.silence_samples >= int(round(self.no_speech_timeout * self.sr)):
                self.state = STATE_NO_SPEECH
        if self.state not in (STATE_END, STATE_NO_SPEECH) and self.total_samples >= int(round(self.max_record_sec * self.sr)):
            self.state = STATE_MAX_LEN
        return self.state

    def endpoint_wait_ms(self):
        """判定结束时实际等待的静音时长（含 VAD 内部滞后）。"""
        if not self.has_spoken:
            return 0
        return int(round((self.silence_samples / self.sr + self._extra_silence_lag()) * 1000))

    def speech_ms(self):
        return int(self.speech_samples * 1000 / self.sr)

    def record_ms(self):
        return int(self.total_samples * 1000 / self.sr)

    def get_audio(self, keep_tail_sec=0.3):
        """返回本句音频，尾部静音只保留 keep_tail_sec。"""
        if not self.chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self.chunks)
        drop = self.silence_samples - int(keep_tail_sec * self.sr)
        if self.has_spoken and drop > 0 and drop < len(audio):
            audio = audio[:-drop]
        return audio

    def is_valid_utterance(self, min_speech_sec=0.25):
        return self.has_spoken and self.speech_samples / self.sr >= min_speech_sec


class EnergyEndpointer(_BaseEndpointer):
    """RMS 能量阈值法：阈值随环境底噪自适应。"""
    kind = "energy"

    def __init__(self, noise_floor=0.002, **kw):
        super().__init__(**kw)
        self.thresh = max(0.0030, noise_floor * 1.8)

    def _is_speech(self, chunk):
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        return rms > self.thresh


class SileroEndpointer(_BaseEndpointer):
    """silero VAD：逐 32ms 窗口给出语音概率，对键盘声、风扇声比能量阈值稳得多。"""
    kind = "silero"

    # VAD 内部判定一段语音结束所需的最短静音，越小越灵敏；剩余的等待由本类计数
    VAD_MIN_SILENCE = 0.1

    # 模型只加载一次，之后每句话 reset 复用（同一时刻只会有一路录音）
    _vad_cache = {}

    def __init__(self, model_path=None, threshold=None, **kw):
        super().__init__(**kw)
        model_path = model_path or _c("VAD_MODEL_PATH", "")
        threshold = threshold if threshold is not None else _c("VAD_THRESHOLD", 0.5)
        key = (model_path, threshold, self.sr)
        vad = self._vad_cache.get(key)
        if vad is None:
            vad = self._build(model_path, threshold)
            self._vad_cache[key] = vad
        else:
            vad.reset()
        self.vad = vad

    def _build(self, model_path, threshold):
        import sherpa_onnx
        if not model_path or not os.path.exists(model_path):
            raise FileNotFoundError(f"silero VAD 模型不存在: {model_path}")
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = model_path
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = self.VAD_MIN_SILENCE
        cfg.silero_vad.min_speech_duration = 0.1
        cfg.silero_vad.window_size = 512
        cfg.sample_rate = self.sr
        cfg.num_threads = 1
        cfg.provider = "cpu"
        return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)

    def _extra_silence_lag(self):
        return self.VAD_MIN_SILENCE

    def _is_speech(self, chunk):
        self.vad.accept_waveform(chunk)
        detected = bool(self.vad.is_speech_detected())
        # 只用实时状态，不需要切好的语音段，及时丢弃避免内部缓冲增长
        while not self.vad.empty():
            self.vad.pop()
        return detected

    def reset(self):
        self.vad.reset()


_SILERO_DISABLED_REASON = None


def create_endpointer(noise_floor=0.002, prefer="silero", **kw):
    """优先 silero，失败时自动退回能量法（只打印一次原因）。"""
    global _SILERO_DISABLED_REASON
    if prefer == "silero" and _SILERO_DISABLED_REASON is None:
        if _c("ENDPOINT_MODE", "fixed") == "smart_turn":
            try:
                return SmartTurnEndpointer(**kw)
            except Exception as e:
                print(f"[!] Smart Turn 不可用，改用固定静音等待: {e}")
        try:
            return SileroEndpointer(**kw)
        except Exception as e:
            _SILERO_DISABLED_REASON = str(e)
            print(f"[!] silero VAD 不可用，端点检测退回能量阈值法: {e}")
    return EnergyEndpointer(noise_floor=noise_floor, **kw)


class SmartTurnEndpointer(SileroEndpointer):
    """Silero VAD + Smart Turn v3（判断“这句话说完了没有”）：
    静音满 vad_stop 秒时问一次 Smart Turn，把握够大（≥threshold）就结束；否则继续等，静音满 fallback 秒强制结束。
    本人 84 句带停顿录音实测（docs/EVALUATION.md 第 10 节）：截断 17%、等待 P50 0.8s（固定 0.8/0.6s 为 24%、1.1s）。
    canaan_config.ENDPOINT_MODE = "smart_turn" 时启用（默认 "fixed"）。"""

    _model = None

    def __init__(self, vad_stop=None, fallback=None, threshold=None, model_path=None, **kw):
        super().__init__(**kw)
        self.vad_stop = vad_stop if vad_stop is not None else _c("SMART_TURN_VAD_STOP_SEC", 0.3)
        self.fallback = fallback if fallback is not None else _c("SMART_TURN_FALLBACK_SEC", 1.5)
        self.st_threshold = threshold if threshold is not None else _c("SMART_TURN_THRESHOLD", 0.95)
        self._asked_at = None
        if SmartTurnEndpointer._model is None:
            import os
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = model_path or os.path.join(base, "models", "smart-turn", "smart-turn-v3.2-cpu.onnx")
            SmartTurnEndpointer._model = _SmartTurnModel(path)

    def required_silence_sec(self):
        return max(0.0, self.fallback - self._extra_silence_lag())

    def feed(self, chunk):
        state = super().feed(chunk)
        if state == STATE_SPEAKING and self.has_spoken and self.silence_samples > 0:
            sil = self.silence_samples / self.sr + self._extra_silence_lag()
            if sil >= self.vad_stop and self._asked_at != self.speech_samples:
                self._asked_at = self.speech_samples  # 每段静音只问一次
                if self._model.prob(np.concatenate(self.chunks)) >= self.st_threshold:
                    self.state = STATE_END
        return self.state


class _SmartTurnModel:
    def __init__(self, path):
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor
        opts = ort.SessionOptions()
        # 只在一句话结束时跑一次：工作线程跑完就睡，不在两次调用之间空转占 CPU
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.sess = ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.fe = WhisperFeatureExtractor(chunk_length=8)
        self.inp = self.sess.get_inputs()[0].name

    def prob(self, audio, sr=SAMPLE_RATE):
        import math
        a = np.asarray(audio, dtype=np.float32)[-sr * 8:]
        if len(a) < sr * 8:
            a = np.concatenate([np.zeros(sr * 8 - len(a), np.float32), a])
        f = self.fe(a, sampling_rate=sr, return_tensors="np", padding="max_length", max_length=sr * 8,
                    truncation=True, do_normalize=True).input_features.astype(np.float32)
        out = float(np.asarray(self.sess.run(None, {self.inp: f})[0]).reshape(-1)[0])
        return out if 0.0 <= out <= 1.0 else 1.0 / (1.0 + math.exp(-out))
