# -*- coding: utf-8 -*-
"""
团团 Desk OS · 单轮交互延迟埋点

一轮交互 = 用户说完一句话（端点判定完成）→ 团团开始出声 → 本轮结束。
所有阶段时间点都以「端点判定完成」为零点（毫秒），这样各阶段可以直接相减、
也直接对应用户体感延迟。录音时长与端点等待时长单独记录。

阶段约定（不是每轮都有全部阶段）：
    endpoint        端点检测从最后一帧有声到判定结束所等待的静音时长（单独字段 endpoint_ms）
    asr             语音识别完成
    llm_first_token 大模型返回第一个 token（快速规则命中时没有）
    llm_done        大模型输出完整决策
    action          动作执行完成
    tts_first_audio 第一段语音开始播放
    total           本轮结束（最后一段语音播完或被打断）

每轮结束后向 logs/latency.jsonl 追加一行 JSON，并打印一行摘要。
统计报表见 tools/latency_report.py。
"""
import os
import json
import time
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATENCY_LOG = os.path.join(BASE_DIR, "logs", "latency.jsonl")

STAGES = ["asr", "llm_first_token", "llm_done", "action", "tts_first_audio", "total"]

_write_lock = threading.Lock()


class TurnTracer:
    """单轮延迟追踪器。线程安全，同一阶段只记录第一次。"""

    def __init__(self, source="kws", t0=None, log_path=None):
        self.source = source
        self.t0 = t0 if t0 is not None else time.perf_counter()
        self.wall = time.strftime("%Y-%m-%d %H:%M:%S")
        self.marks = {}
        self.info = {}
        self.log_path = log_path or LATENCY_LOG
        self._lock = threading.Lock()
        self._finished = False
        try:  # 主人刚说过话：定时总结这类后台大模型任务往后推，不抢显卡
            import canaan_gpu_priority
            canaan_gpu_priority.mark_interaction()
        except Exception:
            pass

    def mark(self, stage):
        """记录某阶段完成时刻（相对零点的毫秒数），重复调用只保留第一次。"""
        ms = (time.perf_counter() - self.t0) * 1000.0
        with self._lock:
            if stage not in self.marks:
                self.marks[stage] = round(ms, 1)
        return ms

    def set(self, key, value):
        with self._lock:
            self.info[key] = value

    def elapsed_ms(self):
        return (time.perf_counter() - self.t0) * 1000.0

    def to_record(self):
        with self._lock:
            rec = {"time": self.wall, "source": self.source}
            rec.update(self.info)
            rec["stages_ms"] = dict(self.marks)
            return rec

    def summary_line(self):
        parts = []
        for s in STAGES:
            if s in self.marks:
                parts.append(f"{s}={self.marks[s]:.0f}")
        extra = []
        for k in ("endpoint_ms", "record_ms", "intent_source", "action"):
            if k in self.info:
                extra.append(f"{k}={self.info[k]}")
        return "[⏱ 延迟] " + " ".join(parts) + ("  | " + " ".join(extra) if extra else "")

    def finish(self, **extra):
        """结束本轮：写日志并打印摘要。只生效一次。"""
        with self._lock:
            if self._finished:
                return None
            self._finished = True
        for k, v in extra.items():
            self.set(k, v)
        self.mark("total")
        rec = self.to_record()
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with _write_lock:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[!] 延迟日志写入失败: {e}")
        print(self.summary_line())
        return rec


class _NullTracer:
    """未传入追踪器时的空实现，避免到处判空。"""

    def mark(self, stage):
        return 0.0

    def set(self, key, value):
        pass

    def finish(self, **extra):
        return None


NULL_TRACER = _NullTracer()


def percentile(values, p):
    """线性插值分位数，values 为空返回 None。"""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
