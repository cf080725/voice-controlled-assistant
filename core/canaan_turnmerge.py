# -*- coding: utf-8 -*-
"""
说话中途停顿被误判为“说完了”的补救（canaan_config.TURN_MERGE_ENABLED，默认关闭）：

1. 拼接：端点检测判定结束后，再听 TURN_MERGE_WINDOW_SEC（默认 1 秒）；这段时间里又开口，就把后半句接到前半句后面，
   整句重新识别、重新理解，而不是当成新的一句。
2. 先理解、后执行：判定结束时立刻对前半句做识别和意图判断（不白等）。
   - 能撤回的动作（打开软件、调音量、截图……）不等，马上执行；
   - 撤不回的动作（发聊天软件、撤回、关程序、执行命令/脚本、界面自动操作、删记忆）必须等这 1 秒确认没有接着说才执行；
     接着说了就丢掉前半句的决定，用整句重新判断。
3. 内容缺失继续等：前半句是“发聊天软件说…/告诉某某…/跟某某说…”这类后面必然跟内容、却没说内容的，等待窗口延长到
   TURN_MERGE_INCOMPLETE_WINDOW_SEC（默认 2.5 秒）。
"""
import re

import numpy as np

try:
    import canaan_config as _cfg
except Exception:
    _cfg = None

# 撤不回的动作：必须确认说完才执行
IRREVERSIBLE = {"send_chatapp", "recall_chatapp", "close_app", "run_cmd", "run_script", "ufo_task", "forget_memory", "forget_person_memory",
                "quit_assistant"}

_INCOMPLETE_RE = re.compile(
    r"(?:发|聊天软件|消息|短信)\S{0,6}(?:说|：|:)$|(?:告诉|通知|提醒|转告|问问|问一下)\S{1,6}$|(?:跟|和|对|给)\S{1,6}说$"
    r"|(?:帮我|给我)?(?:搜|查|找|打开|关掉|关闭|算|放)(?:一下)?$|(?:那个|这个|就是|然后|还有|嗯|呃)$")


def window_sec():
    return float(getattr(_cfg, "TURN_MERGE_WINDOW_SEC", 1.0))


def incomplete_window_sec():
    return float(getattr(_cfg, "TURN_MERGE_INCOMPLETE_WINDOW_SEC", 2.5))


def content_missing(text, decision=None):
    """前半句明显后面还有内容（“给老妈发聊天软件说”“告诉王小明”“帮我查一下”）。"""
    t = re.sub(r"[。！？!?.，,\s]+$", "", (text or "").strip())
    if _INCOMPLETE_RE.search(t):
        return True
    if decision and decision.get("action") == "send_chatapp":
        import json
        try:
            p = json.loads(decision.get("param") or "{}")
            if not (p.get("message") or "").strip():
                return True
        except Exception:
            pass
    if decision and decision.get("_guard") in ("empty_message", "no_contact"):
        return True
    return False


def adaptive_window(text, decision, turn_prob, short=None, long=None, threshold=None, incomplete=None):
    """判定结束时选等待窗口：Smart Turn 认为已说完（概率 ≥ threshold）且内容不缺 → 短窗口；
    内容明显缺 → 内容缺失窗口；其余 → 长窗口。"""
    short = short if short is not None else float(getattr(_cfg, "TURN_MERGE_SHORT_WINDOW_SEC", 0.3))
    long = long if long is not None else window_sec()
    threshold = threshold if threshold is not None else float(getattr(_cfg, "TURN_MERGE_COMPLETE_PROB", 0.9))
    if content_missing(text, decision):
        return incomplete if incomplete is not None else incomplete_window_sec()
    if must_wait(decision):
        return long  # 撤不回的动作：不管模型多有把握，都按长窗口等，宁可慢一点
    if turn_prob is not None and turn_prob >= threshold:
        return short
    return long


def must_wait(decision):
    """这个决定能不能在“可能还没说完”时就执行。"""
    return (decision or {}).get("action") in IRREVERSIBLE


class MergingEndpointer:
    """包一层端点检测器：第一次判定结束时返回 "provisional"（可开始识别/理解），
    此后继续听；窗口内重新开口则回到 "speaking"（之后再次判定结束会再返回 provisional），
    窗口内没开口则返回 "final"。get_audio() 返回到目前为止的全部音频（含中间停顿）。"""

    def __init__(self, factory, window=None, sr=16000):
        self.factory = factory
        self.sr = sr
        self.window = window if window is not None else window_sec()
        self.ep = factory()
        self.audio = []
        self.state = "waiting"
        self._end_at = None       # 判定结束时已有的样本数
        self._after = 0           # 判定结束后又收到的样本数
        self._resumed = 0         # 判定结束后重新开口的次数（拼接次数）

    def extend_window(self, sec):
        self.window = max(self.window, sec)

    def set_window(self, sec):
        """本次判定结束后的等待窗口（自适应窗口用：判断说完了就短等，没把握就长等）。"""
        self.window = sec

    def feed(self, chunk):
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        self.audio.append(chunk)
        if self._end_at is None:
            st = self.ep.feed(chunk)
            if st == "end":
                self._end_at = sum(len(a) for a in self.audio)
                self._after = 0
                self.state = "provisional"
                return self.state
            self.state = st
            return st
        # 已判定结束：看窗口内是否重新开口
        self._after += len(chunk)
        if self.ep._is_speech(chunk):
            self._resumed += 1
            # 重新开口：继续用同一个检测器累计（之前的话算已说部分）
            self.ep.state = "speaking"
            self.ep.silence_samples = 0
            self._end_at = None
            self.state = "speaking"
            return self.state
        if self._after / self.sr >= self.window:
            self.state = "final"
        else:
            self.state = "waiting_confirm"
        return self.state

    @property
    def merged(self):
        return self._resumed

    def get_audio(self):
        return np.concatenate(self.audio) if self.audio else np.zeros(0, np.float32)
