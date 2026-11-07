# -*- coding: utf-8 -*-
"""
桌面伴侣的语音与对口型（不依赖 Tk，可单独测试）。

- 复用 tuantuan_voice_engine：文本净化、磁盘缓存、第一段边收边播，参数与团团主程序完全相同（音质不降）。
- 合成与播放都在后台守护线程，界面线程每帧只读 is_speaking() / mouth_level()，不会卡住 60 帧主循环。
- 播放器在播放的同时计算音量包络（每 20 毫秒一格），mouth_level() 按当前播放位置给出 0/1/2（闭嘴/半张/张嘴）。
- 语音服务没开时不自己去拉起（避免和托盘程序抢端口），只显示气泡。
- 新的一句会打断正在说的一句。
"""
import os
import threading
import time

import numpy as np

# 托盘程序负责拉起 GPT-SoVITS；伴侣进程里不要重复启动
os.environ.setdefault("CANAAN_SUPERVISED", "1")

FRAME_SEC = 0.02            # 包络一格 20 毫秒
OPEN_RATIO, HALF_RATIO = 0.45, 0.18   # 相对本句最大音量：超过 0.45 张嘴，超过 0.18 半张

# 说话图（canaan_q_talk.png，152×230）里嘴巴的位置（原图坐标）
MOUTH_BOX = (81, 100, 100, 117)
SKIN_SAMPLE = (90, 97)
LIP_COLOR = (120, 52, 52, 255)


def envelope(samples, sr):
    """每 20 毫秒一格的音量（RMS）。"""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    n = max(1, int(sr * FRAME_SEC))
    m = len(x) // n
    if m == 0:
        return np.zeros(0, np.float32)
    return np.sqrt(np.mean(x[:m * n].reshape(m, n) ** 2, axis=1)).astype(np.float32)


class LipSync:
    """记录正在播放的音频包络和开始时刻；mouth_level() 由界面线程读取。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "_lock", threading.Lock()):
            self.env = np.zeros(0, np.float32)
            self.t0 = None
            self.peak = 1e-6

    def start(self, env, t0=None):
        with self._lock:
            self.env = np.asarray(env, np.float32)
            self.t0 = time.perf_counter() if t0 is None else t0
            self.peak = max(1e-6, float(self.env.max()) if len(self.env) else 1e-6)

    def append(self, env):
        with self._lock:
            if self.t0 is None:
                self.t0 = time.perf_counter()
            self.env = np.concatenate([self.env, np.asarray(env, np.float32)])
            if len(env):
                self.peak = max(self.peak, float(np.max(env)))

    def level(self, now=None):
        """0 闭嘴 / 1 半张 / 2 张嘴。"""
        with self._lock:
            if self.t0 is None or not len(self.env):
                return 0
            i = int(((time.perf_counter() if now is None else now) - self.t0) / FRAME_SEC)
            if i < 0 or i >= len(self.env):
                return 0
            v = self.env[i] / self.peak
        return 2 if v >= OPEN_RATIO else (1 if v >= HALF_RATIO else 0)


def _make_player(lipsync):
    """在团团主程序的声卡播放器上加一层：播放时同步记录音量包络。"""
    import tuantuan_voice_engine as jv

    class _LipSyncPlayer(jv._SoundDevicePlayer):
        def play(self, path, should_stop):
            try:
                import soundfile as sf
                data, sr = sf.read(path, dtype="float32")
                if data.ndim > 1:
                    data = data.mean(axis=1)
                lipsync.start(envelope(data, sr))
            except Exception:
                lipsync.reset()
            return super().play(path, should_stop)

        def play_stream(self, stream, should_stop):
            class _Tap:
                sr = stream.sr

                @staticmethod
                def chunks():
                    for c in stream.chunks():
                        lipsync.append(envelope(c, stream.sr))
                        yield c
            lipsync.reset()
            return super().play_stream(_Tap, should_stop)

    return _LipSyncPlayer()


class PetVoice:
    """伴侣说话：speak(text) 立即返回，后台合成并播放。"""

    def __init__(self, engine=None, player=None, enabled=True):
        self.enabled = enabled
        self.lipsync = LipSync()
        self._engine = engine
        self._player = player
        self._gen = 0
        self._speaking = False
        self._busy_gen = None   # 正在处理（合成或播放）的那一句的编号
        self.last_error = ""
        self.last_first_audio_sec = None

    def _get_engine(self):
        if self._engine is None:
            import tuantuan_voice_engine as jv
            self._engine = jv.TuantuanVoiceEngine()
        if self._player is None:
            self._player = _make_player(self.lipsync)
        return self._engine

    def warm(self):
        """启动时在后台先加载语音引擎模块（避免第一次说话时界面卡一下）。"""
        threading.Thread(target=lambda: self._get_engine() if self.enabled else None, daemon=True).start()

    def is_speaking(self):
        return self._speaking

    def is_busy(self):
        """当前这一句还在合成或播放。"""
        return self._busy_gen is not None and self._busy_gen == self._gen

    def mouth_level(self, now=None):
        return self.lipsync.level(now) if self._speaking else 0

    def stop(self):
        self._gen += 1
        self._speaking = False
        self.lipsync.reset()

    def speak(self, text, emotion="pamper"):
        """开始说一句（打断上一句）。返回是否已提交到后台（关闭或空文本返回 False）。"""
        if not self.enabled or not (text or "").strip():
            return False
        self._gen += 1
        gen = self._gen
        self._speaking = False
        self.lipsync.reset()
        self._busy_gen = gen
        threading.Thread(target=self._run, args=(text, emotion, gen), daemon=True).start()
        return True

    def start_stream(self, emotion="pamper"):
        """边生成边说：返回 sink(片段)，大模型每产出一段文字就调用一次，结束时调用 sink(None)。
        攒够一句就送去合成（与团团主程序陪聊相同的切句方式）。关闭语音时返回 None。"""
        if not self.enabled:
            return None
        import queue as _queue
        self._gen += 1
        gen = self._gen
        self._speaking = False   # 上一句立即停（它的线程看到编号变了会自己退出）
        self.lipsync.reset()
        self._busy_gen = gen
        q = _queue.Queue()
        threading.Thread(target=self._run_stream, args=(q, emotion, gen), daemon=True).start()
        return q.put

    def _run_stream(self, q, emotion, gen):
        t0 = time.perf_counter()
        try:
            eng = self._get_engine()
            if not eng.is_service_up(timeout=0.3):
                self.last_error = "语音服务未运行，只显示气泡"
                return
            import tuantuan_voice_engine as jv
            stop = lambda: gen != self._gen  # noqa: E731

            def _first():
                if gen == self._gen:
                    self.last_first_audio_sec = time.perf_counter() - t0
                    self._speaking = True

            emotion_fn = emotion if callable(emotion) else (lambda: emotion)
            jv.SpeechPipeline(eng, self._player).speak_stream(iter(q.get, None), emotion_fn,
                                                             should_stop=stop, on_first_audio=_first)
        except Exception as e:
            self.last_error = f"语音异常: {e}"
        finally:
            if gen == self._gen:
                self._speaking = False
                self.lipsync.reset()
                self._busy_gen = None

    def _run(self, text, emotion, gen):
        t0 = time.perf_counter()
        try:
            eng = self._get_engine()
            if not eng.is_service_up(timeout=0.3):
                self.last_error = "语音服务未运行，只显示气泡"
                return
            import tuantuan_voice_engine as jv
            stop = lambda: gen != self._gen  # noqa: E731

            def _first():
                if gen == self._gen:
                    self.last_first_audio_sec = time.perf_counter() - t0
                    self._speaking = True

            res = jv.SpeechPipeline(eng, self._player).speak(text, emotion, should_stop=stop, on_first_audio=_first)
            if res.get("failed"):
                self.last_error = "语音合成失败"
        except Exception as e:
            self.last_error = f"语音异常: {e}"
        finally:
            if gen == self._gen:
                self._speaking = False
                self.lipsync.reset()
                self._busy_gen = None


def make_mouth_frames(talk_img):
    """从说话图生成闭嘴、半张嘴两帧（原图分辨率，PIL RGBA），与原图一起组成三档口型。
    不改素材文件；图尺寸与预期不符时返回空字典（界面退回只用说话图）。"""
    from PIL import Image, ImageDraw
    if talk_img.size != (152, 230):
        return {}
    x0, y0, x1, y1 = MOUTH_BOX
    skin = talk_img.getpixel(SKIN_SAMPLE)
    mouth = talk_img.crop(MOUTH_BOX)

    closed = talk_img.copy()
    d = ImageDraw.Draw(closed)
    d.ellipse((x0, y0, x1, y1), fill=skin)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    d.arc((cx - 5, cy - 5, cx + 5, cy + 1), start=20, end=160, fill=LIP_COLOR, width=2)

    half = talk_img.copy()
    dh = ImageDraw.Draw(half)
    dh.ellipse((x0, y0, x1, y1), fill=skin)
    w, h = mouth.size
    small = mouth.resize((w, max(2, h // 2)), Image.LANCZOS)
    half.alpha_composite(small, (x0, y0 + (h - small.size[1]) // 2))
    return {"talk_closed": closed, "talk_half": half}
