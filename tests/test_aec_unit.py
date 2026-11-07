"""回声消除：参考信号按时间对齐取出（重采样、补零、过期丢弃）；NumPy 版能把纯回声压下去；
流式处理不丢样本；WebRTC 版装不上时退回 NumPy 版。只用合成信号，不碰麦克风。"""
import numpy as np
import pytest

import canaan_aec as aec

SR = aec.SR


def test_reftap_alignment_and_gaps():
    tap = aec.RefTap()
    x = np.arange(1600, dtype=np.float32)
    tap.push(x, t0=10.0)                                     # 10.0 ~ 10.1 秒在播
    seg = tap.take(10.05, 800)                               # 前半段
    assert np.array_equal(seg, x[:800])
    seg = tap.take(10.15, 1600)                              # 一半在播、一半已播完补 0
    assert np.array_equal(seg[:800], x[800:]) and not seg[800:].any()
    assert tap.active(now=10.2) and not tap.active(now=11.0)


def test_reftap_streaming_and_resample_and_clear():
    tap = aec.RefTap(keep_sec=1.0)
    tap.push(np.ones(1600, np.float32), t0=0.0)
    tap.push(np.full(1600, 2, np.float32), t0=0.0)          # 流式：接在上一段后面播
    seg = tap.take(0.2, 3200)
    assert seg[:1600].mean() == 1 and seg[1600:].mean() == 2
    tap.push(np.ones(4800, np.float32), sr=48000, t0=5.0)    # 48k → 16k
    assert np.count_nonzero(tap.take(5.1, 1600)) == 1600
    assert len(tap._chunks) == 1                              # 过期的旧段丢掉
    tap.clear()
    assert not tap.take(5.1, 1600).any() and not tap.active(now=5.0, margin=0)


def test_numpy_canceller_removes_pure_echo():
    rng = np.random.default_rng(0)
    ref = rng.standard_normal(SR * 3).astype(np.float32) * 0.3
    echo = np.concatenate([np.zeros(40), ref[:-40]]) * 0.6   # 2.5 毫秒延迟、衰减的回声
    out = aec.EchoCanceller(block=256, partitions=4).process(echo, ref)
    tail = slice(SR * 2, SR * 3)                              # 收敛之后
    assert np.mean(out[tail] ** 2) < 0.05 * np.mean(echo[tail] ** 2)


def test_numpy_canceller_stream_keeps_all_samples_and_reset():
    c = aec.EchoCanceller(block=256, partitions=2)
    got = sum(len(c.process_stream(np.zeros(n, np.float32), np.zeros(n, np.float32))) for n in (100, 300, 400, 30))
    assert got == 768                                         # 830 个样本里整块的部分
    assert len(c.process(np.zeros(300), np.zeros(300))) == 300   # 不足一块的尾巴原样输出
    c.W[:] = 1
    c.reset()
    assert not c.W.any()


def test_make_canceller_fallback(monkeypatch):
    assert isinstance(aec.make_canceller("numpy"), aec.EchoCanceller)

    def broken(*a, **k):
        raise ImportError("没装 livekit")
    monkeypatch.setattr(aec, "WebRTCEchoCanceller", broken)
    assert isinstance(aec.make_canceller(), aec.EchoCanceller)
    with pytest.raises(ImportError):
        aec.make_canceller("webrtc")


def test_webrtc_canceller_shapes():
    pytest.importorskip("livekit")
    c = aec.WebRTCEchoCanceller()
    out = c.process(np.zeros(1650, np.float32), np.zeros(1650, np.float32))
    assert out.shape == (1650,)
    assert len(c.process_stream(np.zeros(100, np.float32), np.zeros(100, np.float32))) == 0
    assert len(c.process_stream(np.zeros(100, np.float32), np.zeros(100, np.float32))) == 160
