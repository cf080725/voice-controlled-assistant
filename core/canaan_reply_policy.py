# -*- coding: utf-8 -*-
"""
命令执行后的播报规则：先干活再说话，最多一句（主人反馈“讲了半天不干事”）。

- 成功：只念第一句，超过 max_chars 字在逗号处截短；完整结果写在字幕框（HUD）里，不念。
- 失败：说清卡在哪一步、为什么（“没办成，卡在第 2 步（搜索联系人）：聊天软件未登录”）。
"""
import re

_SENT_RE = re.compile(r"^(.+?[。！？!?~～])")
_FILLER_RE = re.compile(r"^(?:好的|好嘞|好呀|好)?(?:亲爱的|宝贝|主人)[，,、\s]*")


def one_sentence(text, max_chars=24):
    """取第一句；去掉开头的“好的亲爱的，”；还太长就在最后一个逗号处截断。"""
    t = " ".join(str(text or "").split())
    t = _FILLER_RE.sub("", t) or t
    m = _SENT_RE.match(t)
    s = m.group(1) if m else t
    if len(s) > max_chars:
        cut = max((s.rfind(c, 0, max_chars) for c in "，,；;"), default=-1)
        s = (s[:cut] if cut >= 6 else s[:max_chars]).rstrip("，,；; ") + "。"
    return s


def failure_speech(message, last_step=None, max_reason=20):
    """失败播报：卡在第几步（做什么）+ 简短原因。last_step=(第几步, 共几步, 描述)。"""
    reason = one_sentence(re.sub(r"^[^:：]{0,12}[:：]\s*", "", str(message or "没能执行成功")), max_reason).rstrip("。")
    if last_step and last_step[0]:
        step, total, desc = last_step
        desc = re.sub(r"^正在|\.\.\.$|…+$", "", str(desc or "")).strip()[:12]
        return f"没办成，卡在第 {step} 步（{desc}）：{reason}。" if desc else f"没办成，卡在第 {step} 步：{reason}。"
    return f"没办成：{reason}。"


def failure_detail(message, last_step=None):
    """字幕框里的失败说明（完整原因，不截短）。"""
    head = f"卡在第 {last_step[0]}/{last_step[1]} 步：{last_step[2]}\n" if last_step and last_step[0] else ""
    return head + "原因：" + str(message or "没能执行成功")
