import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import os
import sys
import time
import sherpa_onnx
import numpy as np
import canaan_config

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KWS_DIR = os.path.join(BASE_DIR, "kws_models", "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")

class CanaanWakeWordDetector:
    """基于 sherpa-onnx Zipformer 3.3M 的超轻量级本地离线语音唤醒引擎"""

    def __init__(self, 
                 model_dir=None,
                 keywords_file=None,
                 keywords_score=None,
                 keywords_threshold=None):
        keywords_score = keywords_score if keywords_score is not None else getattr(canaan_config, "KWS_SCORE", 1.6)
        keywords_threshold = keywords_threshold if keywords_threshold is not None else \
            getattr(canaan_config, "KWS_THRESHOLD", 0.38)
        
        self.model_dir = model_dir or DEFAULT_KWS_DIR
        if keywords_file is None:
            keywords_file = getattr(canaan_config, "KWS_KEYWORDS_FILE", None) or \
                os.path.join(self.model_dir, "keywords_tuantuan.txt")
        self.keywords_file = keywords_file
        
        print("[*] 正在加载离线语音唤醒模型 (sherpa-onnx Zipformer KWS)...")
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(self.model_dir, "tokens.txt"),
            encoder=os.path.join(self.model_dir, "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            decoder=os.path.join(self.model_dir, "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            joiner=os.path.join(self.model_dir, "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
            keywords_file=self.keywords_file,
            keywords_score=keywords_score,
            keywords_threshold=keywords_threshold,
            # 1 个线程：每 100ms 解一小段，单线程绰绰有余（实测实时率不变）；
            # 2 个线程时 onnxruntime 的工作线程在两次调用之间空转，实测常年多占约 2.9 个核
            num_threads=1,
            provider="cpu"
        )
        self.stream = self.spotter.create_stream()
        phrases = getattr(canaan_config, "WAKE_PHRASES", ())
        print("[✓] 离线唤醒引擎已加载完毕！支持唤醒词：" + "、".join(f"‘{p}’" for p in phrases))

    def process_samples(self, samples_float32, sample_rate=16000):
        """传入音频片段（numpy float32 1D 数组），若检测到唤醒词则返回唤醒词字符串，否则返回 None"""
        self.stream.accept_waveform(sample_rate, samples_float32)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
            res = self.spotter.get_result(self.stream)
            if res:
                self.spotter.reset_stream(self.stream)
                return res
        return None

    def reset(self):
        self.spotter.reset_stream(self.stream)

if __name__ == "__main__":
    detector = CanaanWakeWordDetector()
    print("Wake word detector initialized successfully.")
