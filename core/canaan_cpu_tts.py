# -*- coding: utf-8 -*-
"""
CPU 语音合成（Windows 自带 SAPI 中文语音，如 Microsoft Huihui）：不占显存，零下载。
用于游戏模式，以及 GPT-SoVITS 服务没起来时的兜底。音色不是团团，只保证“能听到”。
"""
import os
import time
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, "tuantuan_voicepack", "cache", "cpu_tts")
_lock = threading.Lock()


def available():
    try:
        import comtypes.client  # noqa: F401
        return True
    except Exception:
        return False


def synthesize(text, rate=1):
    """合成到 16kHz wav，返回路径；失败返回 ""。"""
    if not text or not text.strip():
        return ""
    try:
        import comtypes
        import comtypes.client
    except Exception:
        return ""
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"cpu_{int(time.time() * 1000)}_{threading.get_ident() % 10000}.wav")
    with _lock:
        comtypes.CoInitialize()
        try:
            voice = comtypes.client.CreateObject("SAPI.SpVoice")
            from comtypes.gen import SpeechLib
            voices = voice.GetVoices()
            for i in range(voices.Count):
                desc = voices.Item(i).GetDescription()
                if "Chinese" in desc or "中文" in desc:
                    voice.Voice = voices.Item(i)
                    break
            voice.Rate = rate
            fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
            fmt.Type = SpeechLib.SAFT16kHz16BitMono
            stream = comtypes.client.CreateObject("SAPI.SpFileStream")
            stream.Format = fmt
            stream.Open(path, SpeechLib.SSFMCreateForWrite)
            voice.AudioOutputStream = stream
            voice.Speak(text)
            stream.Close()
        except Exception as e:
            print(f"[!] CPU 语音合成失败: {e}")
            return ""
        finally:
            comtypes.CoUninitialize()
    _rotate()
    return path if os.path.exists(path) and os.path.getsize(path) > 1000 else ""


def _rotate(keep=20):
    try:
        files = sorted((os.path.join(OUT_DIR, f) for f in os.listdir(OUT_DIR)), key=os.path.getmtime)
        for f in files[:-keep]:
            try:
                os.remove(f)
            except OSError:
                pass
    except OSError:
        pass
