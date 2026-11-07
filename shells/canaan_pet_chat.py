# -*- coding: utf-8 -*-
"""
桌面伴侣的大模型对话（不依赖 Tk，可单独测试）。

- 默认用本地陪聊模型（复用团团主程序的 CompanionChat：同一份人设、人设守卫、流式输出）。
- DeepSeek 只是可选开关，默认关闭：data/canaan_pet_llm.json 里 provider 设为 "deepseek" 且有 API Key 才会用。
- 模型在回复里用【害羞】/[shy] 这样的动作标签表达情绪：TagFilter 把标签从文字里去掉，换成姿态事件。
- 后台线程请求模型，事件放进线程安全队列（("pose", 姿态) / ("text", 新增文字) / ("done", 全文) / ("error", 原因)），
  界面线程每帧取出来更新气泡；去掉标签后的文字同时送进语音（边生成边说）。
"""
import os
import re
import json
import time
import queue
import threading
import collections
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(ROOT, "data", "canaan_pet_llm.json")

# 中文动作标签 → 72 态里的姿态
TAG_TO_POSE = {
    "害羞": "shy", "比心": "cheer", "开心": "cheer", "加油": "cheer", "庆祝": "cheer", "嘟嘴": "pout", "生气": "pout",
    "傲娇": "tsundere", "花痴": "heart_eyes", "心动": "heart_eyes", "委屈": "crying", "哭": "crying",
    "惊讶": "shock", "震惊": "shock", "跳舞": "dance", "拔刀": "blade", "警惕": "action", "警戒": "action",
    "打哈欠": "yawn", "困": "yawn", "喝咖啡": "coffee", "喝茶": "tea", "吃东西": "eating", "思考": "reading",
    "看书": "reading", "工作": "work", "敲键盘": "typing", "眩晕": "dizzy", "膜拜": "worship", "听歌": "music_headset",
}
TAG_RE = re.compile(r"[【\[]\s*([一-龥A-Za-z_]{1,12})\s*[】\]]")
_OPEN = "【["

PET_CONTEXT = ("（你现在是主人电脑桌面上的 Q 版小挂件团团，回复要短，一两句话，不超过 40 个字。）\n"
               "回复开头先用一个中文方括号动作标签表示你的动作，只能从这些里选："
               "【害羞】【比心】【嘟嘴】【傲娇】【花痴】【委屈】【惊讶】【跳舞】【拔刀】【打哈欠】【喝咖啡】【吃东西】【思考】【加油】")


def tag_pose(word, valid_poses=None):
    """标签文字 → 姿态键；不认识返回 None。英文标签直接当姿态键（在 valid_poses 里才算）。"""
    w = (word or "").strip()
    pose = TAG_TO_POSE.get(w)
    if pose is None and re.fullmatch(r"[A-Za-z_]+", w):
        pose = w.lower()
    if pose and (valid_poses is None or pose in valid_poses):
        return pose
    return None


_BARE_HEAD_RE = re.compile(r"^\s*([一-龥]{1,4})[\s，,：:]+")


class TagFilter:
    """流式去掉动作标签。feed(片段) 返回 (可见文字, [姿态...])；标签可能被拆在两段里，没闭合前先扣着。
    模型偶尔把开头的标签写成不带括号的“害羞 我……”，开头这种认识的标签词也当标签处理。"""

    def __init__(self, valid_poses=None):
        self.valid = valid_poses
        self.buf = ""
        self.head_checked = False

    def feed(self, piece):
        self.buf += piece or ""
        out, poses = "", []
        if not self.head_checked:
            s = self.buf.lstrip()
            if not s or (len(s) < 6 and not re.search(r"[\s，,：:]", s)):
                return "", []  # 开头还太短，判断不了是不是标签词，先等
            self.head_checked = True
            m = _BARE_HEAD_RE.match(self.buf)
            if m and m.group(1) in TAG_TO_POSE:
                p = tag_pose(m.group(1), self.valid)
                if p:
                    poses.append(p)
                self.buf = self.buf[m.end():]
        while True:
            m = TAG_RE.search(self.buf)
            if m:
                out += self.buf[:m.start()]
                p = tag_pose(m.group(1), self.valid)
                if p:
                    poses.append(p)
                self.buf = self.buf[m.end():]
                continue
            # 没有完整标签：最后一个未闭合的括号之后先扣着（最多 14 个字，超过说明不是标签）
            idx = max(self.buf.rfind(c) for c in _OPEN)
            if idx >= 0 and len(self.buf) - idx <= 14 and not any(c in self.buf[idx:] for c in "】]"):
                out += self.buf[:idx]
                self.buf = self.buf[idx:]
            else:
                out += self.buf
                self.buf = ""
            break
        return out, poses

    def flush(self):
        rest, self.buf = self.buf, ""
        return rest


def load_settings(path=SETTINGS_FILE):
    s = {"provider": "local", "deepseek_api_key": "", "deepseek_model": "deepseek-chat"}
    try:
        with open(path, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    if not s.get("deepseek_api_key"):
        s["deepseek_api_key"] = os.environ.get("DEEPSEEK_API_KEY", "")
    return s


def _deepseek_chunks(messages, key, model, timeout=30):
    """DeepSeek（OpenAI 兼容接口）流式输出。只有用户在设置里打开才会调用。"""
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps({"model": model, "messages": messages, "stream": True, "max_tokens": 120}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            line = line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            piece = (json.loads(data)["choices"][0].get("delta") or {}).get("content") or ""
            if piece:
                yield piece


class PetChat:
    """ask(text) 立即返回；结果从 events 队列取。"""

    def __init__(self, valid_poses=None, companion=None, settings=None, history_turns=6):
        self.valid = valid_poses
        self._companion = companion
        self.settings = settings if settings is not None else load_settings()
        self.history = collections.deque(maxlen=history_turns * 2)
        self.events = queue.Queue()
        self._gen = 0
        self.busy = False
        self.last_first_text_sec = None

    @property
    def provider(self):
        p = self.settings.get("provider", "local")
        return "deepseek" if p == "deepseek" and self.settings.get("deepseek_api_key") else "local"

    def _get_companion(self):
        if self._companion is None:
            import canaan_companion
            self._companion = canaan_companion.CompanionChat()
        return self._companion

    def warm(self):
        """启动时在后台先加载陪聊模块（第一次 import 会短暂占住全局锁，放在启动时而不是第一次对话时）。"""
        def _w():
            try:
                if self.provider == "local":
                    self._get_companion()
            except Exception:
                pass
        threading.Thread(target=_w, daemon=True).start()

    def cancel(self):
        self._gen += 1
        self.busy = False

    def ask(self, user_text, speech_sink=None):
        """speech_sink(片段) 收到去掉标签的文字（送给语音边生成边说），结束时收到 None。"""
        text = (user_text or "").strip()
        if not text:
            return False
        from canaan_learning import capture_feedback
        try:
            feedback = capture_feedback(text)
        except ValueError as ex:
            self.events.put(("error", str(ex)))
            return False
        if feedback:
            self.cancel()
            self.events.put(("done", feedback))
            return True
        self._gen += 1
        self.busy = True
        threading.Thread(target=self._run, args=(text, self._gen, speech_sink), daemon=True).start()
        return True

    def _pieces(self, text):
        if self.provider == "deepseek":
            import canaan_companion
            msgs = [{"role": "system", "content": canaan_companion.load_persona_prompt() + "\n" + PET_CONTEXT}]
            msgs += list(self.history) + [{"role": "user", "content": text}]
            return _deepseek_chunks(msgs, self.settings["deepseek_api_key"], self.settings.get("deepseek_model", "deepseek-chat")), None
        cs = self._get_companion().stream(text, history=list(self.history), context=PET_CONTEXT)
        return cs.iter_text(), cs

    def _run(self, text, gen, sink):
        t0 = time.perf_counter()
        full = ""
        raw = ""  # 带标签的原文，存进对话历史，模型下一轮才会继续用标签
        tf = TagFilter(self.valid)
        try:
            pieces, cs = self._pieces(text)
            for piece in pieces:
                if gen != self._gen:
                    return
                raw += piece
                vis, poses = tf.feed(piece)
                for p in poses:
                    self.events.put(("pose", p))
                if vis:
                    if not full:
                        self.last_first_text_sec = time.perf_counter() - t0
                    full += vis
                    self.events.put(("text", vis))
                    if sink:
                        sink(vis)
            rest = tf.flush()
            if rest and gen == self._gen:
                full += rest
                self.events.put(("text", rest))
                if sink:
                    sink(rest)
            if cs is not None and cs.error and not full:
                raise RuntimeError(cs.error)
            full = full.strip()
            if gen == self._gen:
                if full:
                    self.history.append({"role": "user", "content": text})
                    self.history.append({"role": "assistant", "content": raw.strip() or full})
                    self.events.put(("done", full))
                else:
                    self.events.put(("error", "模型没有回复"))
        except Exception as e:
            if gen == self._gen:
                self.events.put(("error", f"对话出错：{e}"))
        finally:
            if sink:
                sink(None)
            if gen == self._gen:
                self.busy = False
