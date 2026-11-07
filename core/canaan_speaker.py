# -*- coding: utf-8 -*-
"""
声纹锁定：只响应录入过的那个人的声音，旁边其他人说话（包括喊“团团”）一律忽略。

模型：3D-Speaker CAM++（中文，16kHz，sherpa-onnx 运行，CPU，约 28MB，不占显存）
录入：`py -3.12 tools/enroll_voice.py`（录 5 句），或用 tools/record_eval_sets.py 录好的唤醒词片段（--from-recordings）
      声纹保存在 data/speaker_profile.npy（只存本机，不入库）
判定：唤醒词片段与指令语音分别和录入声纹算余弦相似度，低于阈值就忽略（写日志）
配置：canaan_config.SPEAKER_LOCK_ENABLED（默认关）、SPEAKER_LOCK_THRESHOLD
"""
import os
import time
import threading

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(BASE_DIR, "models", "speaker", "campplus_zh.onnx")
PROFILE_PATH = os.path.join(BASE_DIR, "data", "speaker_profile.npy")
SR = 16000


class SpeakerVerifier:
    _lock = threading.Lock()

    def __init__(self, model_path=MODEL_PATH, profile_path=PROFILE_PATH, num_threads=2):
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=num_threads)
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.profile_path = profile_path
        self.profile = self._load()

    def _load(self):
        try:
            return np.load(self.profile_path)
        except Exception:
            return None

    @property
    def enrolled(self):
        return self.profile is not None

    def embed(self, samples, sr=SR):
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        with self._lock:
            s = self.extractor.create_stream()
            s.accept_waveform(sr, samples)
            s.input_finished()
            if not self.extractor.is_ready(s):
                return None
            v = np.array(self.extractor.compute(s), dtype=np.float32)
        n = np.linalg.norm(v)
        return v / n if n > 0 else None

    def enroll(self, clips, sr=SR):
        """clips：若干段本人说话的音频（float32）。返回参与平均的段数。"""
        embs = [e for e in (self.embed(c, sr) for c in clips if len(c) > sr * 0.5) if e is not None]
        if not embs:
            raise ValueError("没有可用的录音（每段至少 0.5 秒）")
        m = np.mean(embs, axis=0)
        self.profile = m / np.linalg.norm(m)
        os.makedirs(os.path.dirname(self.profile_path), exist_ok=True)
        np.save(self.profile_path, self.profile)
        return len(embs)

    def score(self, samples, sr=SR):
        """与录入声纹的余弦相似度；未录入或音频太短返回 None。"""
        if self.profile is None or len(samples) < sr * 0.3:
            return None
        e = self.embed(samples, sr)
        return float(np.dot(e, self.profile)) if e is not None else None


FLAG_PATH = os.path.join(BASE_DIR, "data", "speaker_lock.flag")  # 托盘开关：存在即开启


class SpeakerGate:
    """hub 用：enabled 且已录入时才判定；返回 (是否放行, 分数, 耗时毫秒)。出错时放行（不因声纹故障让助手失灵）。
    enabled = 配置开关 或 托盘标志文件；最多取前 4 秒音频计算（控制耗时，约 30ms）。"""

    def __init__(self, enabled=False, threshold=0.5, flag_path=FLAG_PATH):
        self._config_enabled = enabled
        self.threshold = threshold
        self.flag_path = flag_path
        self._v = None

    @property
    def enabled(self):
        return self._config_enabled or os.path.exists(self.flag_path)

    @enabled.setter
    def enabled(self, value):
        self._config_enabled = value

    def verifier(self):
        if self._v is None:
            self._v = SpeakerVerifier()
        return self._v

    def check(self, samples, sr=SR, threshold=None):
        """threshold 不填用 self.threshold（整句指令）；唤醒词片段太短，调用方传更低的阈值。"""
        th = self.threshold if threshold is None else threshold
        if not self.enabled:
            return True, None, 0.0
        t0 = time.perf_counter()
        try:
            v = self.verifier()
            if not v.enrolled:
                return True, None, 0.0
            s = v.score(np.asarray(samples, dtype=np.float32).reshape(-1)[:int(sr * 4)], sr)
        except Exception as e:
            print(f"[!] 声纹判定出错，放行: {e}")
            return True, None, 0.0
        ms = (time.perf_counter() - t0) * 1000
        if s is None:
            return True, None, ms
        return s >= th, s, ms
