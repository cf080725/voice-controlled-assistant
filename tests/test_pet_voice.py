# -*- coding: utf-8 -*-
import time
import threading

import numpy as np
from PIL import Image

import canaan_pet_voice as pv


def test_envelope_frames_and_levels():
    sr = 16000
    loud = np.sin(np.arange(sr // 5) / 3).astype(np.float32)          # 0.2 秒响
    quiet = np.zeros(sr // 5, np.float32)                               # 0.2 秒静音
    env = pv.envelope(np.concatenate([loud, quiet]), sr)
    assert len(env) == 20                                               # 20 毫秒一格
    ls = pv.LipSync()
    ls.start(env, t0=100.0)
    assert ls.level(100.05) == 2                                        # 响的部分张嘴
    assert ls.level(100.30) == 0                                        # 静音部分闭嘴
    assert ls.level(99.0) == 0 and ls.level(200.0) == 0                 # 播放前/后闭嘴


def test_lipsync_append_streaming():
    ls = pv.LipSync()
    ls.append(np.array([0.5, 0.5], np.float32))
    ls.append(np.array([0.05], np.float32))
    t0 = ls.t0
    assert ls.level(t0 + 0.01) == 2 and ls.level(t0 + 0.045) == 0


class _Engine:
    def __init__(self, up=True):
        self.up, self.calls = up, []

    def is_service_up(self, timeout=0.05):
        return self.up

    def synthesize_clean(self, seg, emotion):
        self.calls.append((seg, emotion))
        return f"/fake/{len(self.calls)}.wav"


class _Player:
    def __init__(self, sec=0.15):
        self.sec, self.played, self.speaking_seen = sec, [], []

    def play(self, path, should_stop):
        self.played.append(path)
        end = time.time() + self.sec
        while time.time() < end:
            if should_stop():
                return False
            time.sleep(0.01)
        return True


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_speak_runs_in_background_and_ends_idle():
    synthesizing, release_synthesis, playing, release_play, returned = [threading.Event() for _ in range(5)]
    errors = []
    class BlockingEngine(_Engine):
        def synthesize_clean(self, seg, emotion):
            synthesizing.set()
            assert release_synthesis.wait(5)
            return super().synthesize_clean(seg, emotion)
    class BlockingPlayer(_Player):
        def play(self, path, should_stop):
            self.played.append(path)
            playing.set()
            assert release_play.wait(5)
            return not should_stop()
    eng, player = BlockingEngine(), BlockingPlayer()
    v = pv.PetVoice(engine=eng, player=player)
    def submit():
        try:
            assert v.speak("你好呀，亲爱的。今天也一起加油吧！")
            returned.set()
        except BaseException as exc:
            errors.append(exc)
    caller = threading.Thread(target=submit)
    caller.start()
    try:
        assert returned.wait(3), 'speak waited for synthesis on its caller'
        assert synthesizing.wait(3)
        assert v.is_busy() and not v.is_speaking()
        release_synthesis.set()
        assert playing.wait(3)
        assert v.is_speaking()
    finally:
        release_synthesis.set()
        release_play.set()
        caller.join(3)
        assert _wait(lambda: not v.is_busy())
    assert not errors and not caller.is_alive() and not v.is_speaking()
    assert len(player.played) == len(eng.calls) >= 1
    assert v.last_first_audio_sec is not None and v.last_error == ""


def test_new_sentence_interrupts_old():
    eng, player = _Engine(), _Player(sec=0.5)
    v = pv.PetVoice(engine=eng, player=player)
    v.speak("第一句话说得比较长，还没说完。后面还有一句。")
    assert _wait(v.is_speaking)
    v.speak("第二句。")
    assert _wait(lambda: any(s == "第二句。" for s, _ in eng.calls))
    assert _wait(lambda: not v.is_speaking(), timeout=4)


def test_service_down_no_audio_no_spawn():
    eng, player = _Engine(up=False), _Player()
    v = pv.PetVoice(engine=eng, player=player)
    v.speak("测试")
    assert _wait(lambda: v.last_error != "")
    assert eng.calls == [] and player.played == [] and not v.is_speaking()


def test_disabled_or_empty_does_nothing():
    v = pv.PetVoice(engine=_Engine(), player=_Player(), enabled=False)
    assert v.speak("你好") is False
    v2 = pv.PetVoice(engine=_Engine(), player=_Player())
    assert v2.speak("   ") is False


def test_mouth_frames_shape():
    img = Image.new("RGBA", (152, 230), (240, 200, 170, 255))
    frames = pv.make_mouth_frames(img)
    assert set(frames) == {"talk_closed", "talk_half"}
    assert all(f.size == (152, 230) for f in frames.values())
    assert pv.make_mouth_frames(Image.new("RGBA", (10, 10))) == {}


def test_supervised_flag_set():
    import os
    assert os.environ.get("CANAAN_SUPERVISED") == "1"                  # 伴侣进程不会自己拉起语音服务
