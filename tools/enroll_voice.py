# -*- coding: utf-8 -*-
"""
录入主人声纹（声纹锁定用）。两种方式：
    1. 现场录 5 句（每句约 3 秒），默认
    2. --from-recordings：用 tools/record_eval_sets.py 录好的唤醒词片段（D:/canaan_eval_audio/user_recordings/wake）
录入后声纹保存在 data/speaker_profile.npy（只存本机）。录音时自动暂停团团聆听。
用法: py -3.12 tools/enroll_voice.py [--from-recordings]
"""
import os
import sys
import glob
import time
import argparse
import threading

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import canaan  # noqa: E402,F401  目录拆分后补全 core/shells/plugins 搜索路径
from canaan_speaker import SpeakerVerifier, SR  # noqa: E402

PROMPTS = ["今天天气真不错，适合出去走走。", "帮我打开记事本，我要写点东西。", "团团，把音量调小一点。",
           "晚上七点提醒我给妈妈打电话。", "这首歌挺好听的，再放一遍吧。"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-recordings", action="store_true")
    ap.add_argument("--dir", default=r"D:\canaan_eval_audio\user_recordings\turns",
                    help="录音目录（默认用带停顿的整句录音，比两个字的唤醒词更能代表声纹）")
    args = ap.parse_args()
    v = SpeakerVerifier()
    clips = []
    if args.from_recordings:
        import soundfile as sf
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from record_eval_sets import has_speech
        for p in sorted(glob.glob(os.path.join(args.dir, "*.wav")))[:30]:
            d, sr = sf.read(p, dtype="float32")
            if sr == SR and has_speech(d):  # 录音工具录的是 16kHz；跳过没录到说话声的
                clips.append(d)
            if len(clips) >= 10:
                break
        if not clips:
            print(f"没找到录音：{args.dir}")
            return 1
    else:
        import sounddevice as sd
        pause = os.path.join(ROOT, "data", "listen_paused.flag")
        created = not os.path.exists(pause)
        if created:
            os.makedirs(os.path.dirname(pause), exist_ok=True)
            open(pause, "w").close()
        try:
            print("录入声纹：请用平时说话的音量读下面 5 句，每句录 4 秒。")
            for i, t in enumerate(PROMPTS, 1):
                input(f"\n[{i}/5] 回车后读：{t}")
                a = sd.rec(int(SR * 4), samplerate=SR, channels=1, dtype="float32")
                sd.wait()
                clips.append(a.reshape(-1))
                print("    录好了")
        finally:
            if created:
                os.remove(pause)
    n = v.enroll(clips)
    print(f"[OK] 已用 {n} 段录音生成声纹，保存在 {v.profile_path}。托盘菜单打开“声纹锁定”后生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
