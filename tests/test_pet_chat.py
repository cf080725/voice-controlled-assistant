# -*- coding: utf-8 -*-
import time

import canaan_pet_chat as pc

POSES = {"shy", "cheer", "pout", "blade", "dance", "reading", "tsundere"}


def _feed_all(pieces, valid=POSES):
    tf = pc.TagFilter(valid)
    out, poses = "", []
    for p in pieces:
        v, ps = tf.feed(p)
        out += v
        poses += ps
    return out + tf.flush(), poses


def test_tag_filter_chinese_and_english_tags():
    assert _feed_all(["【害羞】谢谢夸奖。"]) == ("谢谢夸奖。", ["shy"])
    assert _feed_all(["[跳舞] 来跳个舞！"]) == (" 来跳个舞！", ["dance"])
    assert _feed_all(["好呀[cheer]，太棒了"]) == ("好呀，太棒了", ["cheer"])


def test_tag_split_across_pieces():
    assert _feed_all(["【害", "羞】你", "好"]) == ("你好", ["shy"])
    assert _feed_all(["[bl", "ade] 看招"]) == (" 看招", ["blade"])


def test_unknown_tag_removed_without_pose_and_plain_brackets_kept_if_long():
    assert _feed_all(["【不认识】嗯"]) == ("嗯", [])
    long = "【这不是标签而是一段很长很长的括号内容没有闭合"
    out, poses = _feed_all([long])
    assert out == long and poses == []


def test_tag_pose_validation():
    assert pc.tag_pose("害羞", POSES) == "shy"
    assert pc.tag_pose("blade", POSES) == "blade"
    assert pc.tag_pose("motorcycle", POSES) is None      # 不在可用姿态里
    assert pc.tag_pose("乱写", POSES) is None


class _CS:
    def __init__(self, pieces, error=None):
        self.pieces, self.error = pieces, error

    def iter_text(self):
        for p in self.pieces:
            time.sleep(0.01)
            yield p


class _Companion:
    def __init__(self, pieces, error=None):
        self.pieces, self.error, self.calls = pieces, error, []

    def stream(self, text, history=None, context=""):
        self.calls.append((text, list(history or []), context))
        return _CS(self.pieces, self.error)


def _collect(chat, timeout=3.0):
    evs, end = [], time.time() + timeout
    while time.time() < end:
        try:
            e = chat.events.get(timeout=0.05)
        except Exception:
            continue
        evs.append(e)
        if e[0] in ("done", "error"):
            break
    return evs


def test_chat_events_and_speech_sink_without_tags():
    comp = _Companion(["【害", "羞】被你夸", "得不好意思了。", "[cheer]谢谢！"])
    chat = pc.PetChat(valid_poses=POSES, companion=comp, settings={"provider": "local"})
    spoken = []
    t = time.perf_counter()
    assert chat.ask("你好可爱", speech_sink=spoken.append)
    assert time.perf_counter() - t < 0.05                                 # 立即返回
    evs = _collect(chat)
    assert evs[0] == ("pose", "shy")
    assert ("pose", "cheer") in evs
    assert evs[-1] == ("done", "被你夸得不好意思了。谢谢！")
    assert "".join(x for x in spoken if x) == "被你夸得不好意思了。谢谢！"  # 送去语音的文字不含标签
    assert spoken[-1] is None                                             # 结束信号
    assert PET_CONTEXT_SENT(comp)
    # 第二轮带上历史
    chat.ask("再说一句")
    _collect(chat)
    # 历史里保留带标签的原文，模型下一轮才会继续用标签
    assert comp.calls[1][1][-1] == {"role": "assistant", "content": "【害羞】被你夸得不好意思了。[cheer]谢谢！"}


def test_bare_tag_word_at_start():
    assert _feed_all(["害羞 我只会一点", "点小舞步。"]) == ("我只会一点点小舞步。", ["shy"])
    assert _feed_all(["跳舞，来跳个舞！"]) == ("来跳个舞！", ["dance"])
    assert _feed_all(["好呀。"]) == ("好呀。", [])                   # 很短的回复照常输出
    assert _feed_all(["晚安 明日见"]) == ("晚安 明日见", [])          # 不认识的开头词不当标签


def PET_CONTEXT_SENT(comp):
    return "动作标签" in comp.calls[0][2]


def test_chat_error_reports_and_closes_sink():
    comp = _Companion([], error=RuntimeError("连接失败"))
    chat = pc.PetChat(valid_poses=POSES, companion=comp, settings={"provider": "local"})
    spoken = []
    chat.ask("在吗", speech_sink=spoken.append)
    evs = _collect(chat)
    assert evs[-1][0] == "error" and spoken == [None] and not chat.busy


def test_cancel_drops_old_reply():
    comp = _Companion(["第一句很长"] * 20)
    chat = pc.PetChat(valid_poses=POSES, companion=comp, settings={"provider": "local"})
    chat.ask("第一个问题")
    time.sleep(0.05)
    chat.cancel()
    time.sleep(0.4)
    evs = []
    while not chat.events.empty():
        evs.append(chat.events.get())
    assert not any(e[0] == "done" for e in evs)


def test_deepseek_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    s = pc.load_settings(str(tmp_path / "none.json"))
    assert s["provider"] == "local"
    assert pc.PetChat(settings=s).provider == "local"
    # 打开开关但没有 Key：仍用本地模型
    assert pc.PetChat(settings={"provider": "deepseek", "deepseek_api_key": ""}).provider == "local"
    assert pc.PetChat(settings={"provider": "deepseek", "deepseek_api_key": "k"}).provider == "deepseek"
