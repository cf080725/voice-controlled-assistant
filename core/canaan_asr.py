# -*- coding: utf-8 -*-
"""
语音识别封装：直接把内存中的 float32 / 16kHz 单声道数组交给模型，不再落盘成 wav 再读回。

SenseVoice (funasr) 优先，失败或结果为空时退回 faster-whisper。
"""
import re
import threading
import numpy as np

SAMPLE_RATE = 16000
WHISPER_PROMPT = "以下是安徽亳州涡阳方言中原官话与普通话指令：团团，机器鱼，拾掇，可照，中不中，弄啥哩，给俺整小点。"
ASR_LOCK = threading.RLock()


def to_mono_f32(audio):
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim > 1:
        a = a.mean(axis=1) if a.shape[1] > 1 else a.reshape(-1)
    return np.ascontiguousarray(a)


def sensevoice_language():
    """canaan_config.ASR_LANGUAGE（默认 "zh"，可设 "auto"）；配置读不到时用 "zh"。"""
    try:
        import canaan_config
        return getattr(canaan_config, "ASR_LANGUAGE", "zh") or "zh"
    except Exception:
        return "zh"


def transcribe_sensevoice(model, audio):
    res = model.generate(input=to_mono_f32(audio), fs=SAMPLE_RATE, language=sensevoice_language(), use_itn=True)
    if res and len(res) > 0 and "text" in res[0]:
        return re.sub(r"<\|.*?\|>", "", res[0]["text"]).strip()
    return ""


def transcribe_whisper(model, audio, command=True):
    segments, _ = model.transcribe(to_mono_f32(audio), language="zh", initial_prompt=WHISPER_PROMPT if command else None)
    return "".join(s.text for s in segments).strip()


def release_load_memory(model, max_runs=8, gap=0.2, private_mb=None, sleep=None):
    """加载 SenseVoice 后用 1 秒静音空跑几次，让 torch 的 mimalloc 把加载时留下的内存还给系统。
    10-24 实测：加载后提交内存 3963 MB（参数只有 892 MB），第 4 次空跑后降到约 1970 MB，间隔 0.2~1 秒都一样；
    不空跑的话主程序要等约 12 分钟才还。MIMALLOC_PURGE_DELAY=0 也能马上还，但每句识别从约 230 毫秒变成约 360 毫秒，不用。
    正在识别真话时（ASR_LOCK 被占）这一轮跳过，不排队。返回 (空跑次数, 开始 MB, 结束 MB)，量不到内存时 MB 为 None。"""
    import time
    if private_mb is None:
        try:
            import psutil
            proc = psutil.Process()
            private_mb = lambda: proc.memory_info().private >> 20  # noqa: E731
        except Exception:
            private_mb = lambda: None  # noqa: E731
    sleep = sleep or time.sleep
    silence = np.zeros(SAMPLE_RATE, np.float32)
    start = now = private_mb()
    runs = 0
    for _ in range(max_runs):
        if ASR_LOCK.acquire(blocking=False):
            try:
                transcribe_sensevoice(model, silence)
                runs += 1
            finally:
                ASR_LOCK.release()
        now = private_mb()
        if start is not None and now is not None and now < start - 500:
            break
        sleep(gap)
    return runs, start, now


def release_load_memory_rounds(model, rounds=3, wait=20.0, private_mb=None, sleep=None, **kw):
    """启动时语音识别和唤醒监听、两个大模型预热、语音合成预热同时在抢内存，一轮空跑可能降不下来
    （10-25 启动：4857 → 4970 MB，十几分钟后才自己还）。没降下来就隔 wait 秒再来一轮，最多 rounds 轮。
    返回 (总空跑次数, 第一轮开始 MB, 最后 MB, 轮数)。"""
    import time
    sleep = sleep or time.sleep
    total, first, last = 0, None, None
    for i in range(1, rounds + 1):
        runs, start, last = release_load_memory(model, private_mb=private_mb, sleep=sleep, **kw)
        total += runs
        first = start if first is None else first
        if start is None or last is None or last < min(start, first) - 500:
            return total, first, last, i
        if i < rounds:
            sleep(wait)
    return total, first, last, rounds


def transcribe(audio, sense_voice=None, whisper=None, command=True, *, should_stop=None):
    """返回 (文本, 引擎名)；已取消的请求返回空文本与 cancelled。"""
    acquired = False
    stopped = should_stop if should_stop is not None else lambda: False
    try:
        if stopped():
            return "", "cancelled"
        if should_stop is None:
            ASR_LOCK.acquire()
        else:
            while not ASR_LOCK.acquire(timeout=.05):
                if stopped():
                    return "", "cancelled"
        acquired = True
        if stopped():
            return "", "cancelled"
        if sense_voice is not None:
            text = ""
            try:
                text = transcribe_sensevoice(sense_voice, audio)
            except Exception as e:
                print(f"[!] SenseVoice 转写失败: {type(e).__name__}，切换 Whisper 识别")
            if stopped():
                return "", "cancelled"
            if text:
                return text, "sensevoice"
        if whisper is not None:
            if stopped():
                return "", "cancelled"
            text = transcribe_whisper(whisper, audio, command)
            return ("", "cancelled") if stopped() else (text, "whisper")
        return "", "none"
    finally:
        if acquired:
            ASR_LOCK.release()
