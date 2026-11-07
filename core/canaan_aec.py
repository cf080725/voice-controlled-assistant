# -*- coding: utf-8 -*-
"""
回声消除（AEC）：团团说话时，把她自己的声音从麦克风信号里减掉，主人喊“团团”才听得见（打断）。

纯 NumPy 实现（不加载来源不明的二进制库）：分块频域自适应滤波（PBFDAF，与 Speex AEC 同类算法，overlap-save）。
- 参考信号：播放器每写一段声音就 push 进 RefTap（带写入时刻），麦克风每读一块就按时间取出对齐的参考段；
- 滤波器尾长 = 块长 × 分区数（默认 256 × 24 = 384 毫秒，覆盖声卡输出/输入延迟 + 房间混响）；
- 主人和团团同时说话（双讲）时降低步长，避免把主人的声音也学掉。
只在内存里处理，不存盘。
"""
import threading
import time
from collections import deque

import numpy as np

SR = 16000


class RefTap:
    """播放参考信号环形缓冲：push(样本, 采样率) 记录“这些样本从现在开始播”；take(结束时刻, 长度) 取对齐的一段。"""

    def __init__(self, keep_sec=8.0):
        self._lock = threading.Lock()
        self._chunks = deque()            # (开始时刻, 16k 样本)
        self.keep_sec = keep_sec
        self._play_end = 0.0              # 已排队播放的声音结束时刻

    def push(self, samples, sr=SR, t0=None):
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if sr != SR and len(x):
            n = int(round(len(x) * SR / sr))
            x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
        now = time.perf_counter() if t0 is None else t0
        with self._lock:
            start = max(now, self._play_end)     # 流式写入时紧接在上一段后面播
            self._chunks.append((start, x))
            self._play_end = start + len(x) / SR
            while self._chunks and self._chunks[0][0] + len(self._chunks[0][1]) / SR < now - self.keep_sec:
                self._chunks.popleft()

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._play_end = 0.0

    def truncate(self, now=None):
        """播放被中途停掉：丢弃 now 之后还没播出去的参考（已播出的部分留着，回声尾巴还要用）。"""
        now = time.perf_counter() if now is None else now
        with self._lock:
            kept = deque()
            for c0, x in self._chunks:
                if c0 >= now:
                    continue
                n = min(len(x), int(round((now - c0) * SR)))
                kept.append((c0, x[:n]))
            self._chunks = kept
            self._play_end = min(self._play_end, now)

    def take(self, t_end, n):
        """取 [t_end - n/SR, t_end) 这段时间里正在播放的参考信号（没在播的地方补 0）。"""
        out = np.zeros(n, np.float32)
        t_start = t_end - n / SR
        with self._lock:
            for c0, x in self._chunks:
                c1 = c0 + len(x) / SR
                if c1 <= t_start or c0 >= t_end:
                    continue
                a = max(c0, t_start)
                b = min(c1, t_end)
                i0 = int(round((a - c0) * SR))
                o0 = int(round((a - t_start) * SR))
                m = min(int(round((b - a) * SR)), len(x) - i0, n - o0)
                if m > 0:
                    out[o0:o0 + m] = x[i0:i0 + m]
        return out

    def active(self, now=None, margin=0.6):
        """现在（或 margin 秒前）还在播放吗。"""
        now = time.perf_counter() if now is None else now
        with self._lock:
            return now < self._play_end + margin


REF = RefTap()


class WebRTCEchoCanceller:
    """WebRTC 音频处理模块（AEC3 回声消除 + 降噪 + 高通），通过 LiveKit 官方 Python SDK（Apache-2.0）在本机运行，
    不联网。接口与 EchoCanceller 相同：process_stream / process（16 kHz float32）。"""

    FRAME = 160  # 10 毫秒

    def __init__(self, delay_ms=None, noise_suppression=True):
        from livekit import rtc
        self._rtc = rtc
        self.apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=noise_suppression,
                                             high_pass_filter=True, auto_gain_control=False)
        if delay_ms is not None:
            self.apm.set_stream_delay_ms(int(delay_ms))
        self._dbuf = np.zeros(0, np.float32)
        self._rbuf = np.zeros(0, np.float32)

    def _frame(self, x):
        f = self._rtc.AudioFrame.create(SR, 1, self.FRAME)
        a = np.frombuffer(f.data, dtype=np.int16)
        a[:] = np.clip(x * 32767.0, -32768, 32767).astype(np.int16)
        return f, a

    def process(self, mic, ref):
        mic = np.asarray(mic, np.float32).reshape(-1)
        ref = np.asarray(ref, np.float32).reshape(-1)
        out = np.empty_like(mic)
        F = self.FRAME
        n = len(mic) // F * F
        for i in range(0, n, F):
            rf, _ = self._frame(ref[i:i + F])
            self.apm.process_reverse_stream(rf)
            mf, a = self._frame(mic[i:i + F])
            self.apm.process_stream(mf)
            out[i:i + F] = np.frombuffer(mf.data, dtype=np.int16).astype(np.float32) / 32768.0
        out[n:] = mic[n:]
        return out

    def process_stream(self, mic, ref):
        self._dbuf = np.concatenate([self._dbuf, np.asarray(mic, np.float32).reshape(-1)])
        self._rbuf = np.concatenate([self._rbuf, np.asarray(ref, np.float32).reshape(-1)])
        n = len(self._dbuf) // self.FRAME * self.FRAME
        if n == 0:
            return np.zeros(0, np.float32)
        out = self.process(self._dbuf[:n], self._rbuf[:n])
        self._dbuf, self._rbuf = self._dbuf[n:], self._rbuf[n:]
        return out


def make_canceller(engine=None):
    """优先用 WebRTC（效果更好），装不上时退回纯 NumPy 实现。"""
    if engine in (None, "webrtc"):
        try:
            return WebRTCEchoCanceller()
        except Exception:
            if engine == "webrtc":
                raise
    return EchoCanceller()


class EchoCanceller:
    """PBFDAF 回声消除。process(麦克风块, 参考块) → 去掉回声后的块；块长固定为 block。"""

    def __init__(self, block=256, partitions=24, mu=1.0, eps=1e-6):
        self.B, self.P, self.mu, self.eps = block, partitions, mu, eps
        self.N = 2 * block
        self.W = np.zeros((partitions, block + 1), np.complex128)    # 各分区的频域滤波器
        self.X = np.zeros((partitions, block + 1), np.complex128)    # 参考信号频谱历史
        self.x_prev = np.zeros(block, np.float64)
        self.px = np.full(block + 1, 1e-3)                          # 参考功率谱（平滑）

    def reset(self):
        self.__init__(self.B, self.P, self.mu, self.eps)

    def _block(self, d, x):
        B = self.B
        xx = np.concatenate([self.x_prev, x])
        self.x_prev = x
        Xk = np.fft.rfft(xx)
        self.X = np.roll(self.X, 1, axis=0)
        self.X[0] = Xk
        Y = np.sum(self.W * self.X, axis=0)
        y = np.fft.irfft(Y, self.N)[B:]
        e = d - y
        # 双讲检测：误差能量远大于回声估计时，多半是主人在说话，放慢学习
        pe, py = float(np.dot(e, e)), float(np.dot(y, y)) + 1e-9
        mu = self.mu * (0.1 if pe > 4.0 * py and py > 1e-6 else 1.0)
        # 归一化：各频点在全部分区上的参考能量之和（平滑），语音这类不平稳信号上更稳
        self.px = 0.7 * self.px + 0.3 * np.sum(np.abs(self.X) ** 2, axis=0)
        E = np.fft.rfft(np.concatenate([np.zeros(B), e]))
        G = mu * E / (self.px + self.eps + 1e-3 * float(np.mean(self.px)))
        for p in range(self.P):
            grad = np.fft.irfft(np.conj(self.X[p]) * G, self.N)
            grad[B:] = 0.0                                          # 约束：只保留因果的前半段
            self.W[p] += np.fft.rfft(grad)
        return e

    def process_stream(self, mic, ref):
        """任意长度流式处理：不足一块的部分留到下次（输出比输入最多晚 block-1 个样本）。"""
        if not hasattr(self, "_dbuf"):
            self._dbuf, self._rbuf = np.zeros(0, np.float64), np.zeros(0, np.float64)
        self._dbuf = np.concatenate([self._dbuf, np.asarray(mic, np.float64).reshape(-1)])
        self._rbuf = np.concatenate([self._rbuf, np.asarray(ref, np.float64).reshape(-1)])
        n = len(self._dbuf) // self.B * self.B
        if n == 0:
            return np.zeros(0, np.float32)
        out = self.process(self._dbuf[:n], self._rbuf[:n])
        self._dbuf, self._rbuf = self._dbuf[n:], self._rbuf[n:]
        return out

    def process(self, mic, ref):
        mic = np.asarray(mic, np.float64).reshape(-1)
        ref = np.asarray(ref, np.float64).reshape(-1)
        out = np.empty_like(mic)
        B = self.B
        n = len(mic) // B * B
        for i in range(0, n, B):
            out[i:i + B] = self._block(mic[i:i + B], ref[i:i + B])
        if n < len(mic):                                            # 尾巴不足一块：原样输出
            out[n:] = mic[n:]
        return out.astype(np.float32)
