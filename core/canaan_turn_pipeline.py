# -*- coding: utf-8 -*-
"""一句话的决策链（从 canaan_desk_os_hub 里抽出来的纯函数，方便单独测试；hub 照旧调用，行为不变）。

decide：上一轮挂起的确认 / 取消 → 规则通道（提示查询、主动提醒、记忆管理、快速规则）→ 本地大模型
        → 指代消解（“刚才那个人”）→ 命令守卫（只是提到动作的句子先问一句）。
spoken_result：播报内容只来自执行结果（大模型的 reply 不用来报“完成”），失败说清卡在哪一步。
result_note：写进短期记忆的那一句结果。
"""
from canaan_reply_policy import failure_speech, one_sentence
from canaan_query_answers import (QueryFragment, get_query_document, merge_prefix,
                                  clone_execution_result, utf16_units)


def decide(clean_text, pending_kind, pending, rule_channels, llm, resolve_refs, guard, cancel_reply=None, *, planner=None):
    """返回 dict：decision、exec_source（rule / llm / 挂起时的来源）、confirmed、exec_user_text、intent_source。
    rule_channels：按顺序试的规则函数（文字 → 决策或 None）；llm(文字) → 决策；
    resolve_refs(决策) → 决策；guard(文字, 决策, require_confidence) → 决策。"""
    out = {"exec_source": "llm", "confirmed": False, "exec_user_text": clean_text}
    if pending_kind == "confirm":
        out.update(decision=dict(pending["decision"]), exec_source=pending["source"], confirmed=True,
                   exec_user_text=pending["user_text"], intent_source="confirm")
        return out
    if pending_kind == "cancel":
        reply = cancel_reply((pending or {}).get("decision")) if cancel_reply else "好，已取消。"
        out.update(decision={"action": "chat", "param": "", "emotion": "confident", "reply": reply},
                   exec_source="rule", intent_source="cancel")
        return out
    decision = None
    for fn in rule_channels:
        decision = fn(clean_text)
        if decision:
            break
    if decision:
        out.update(exec_source="rule", intent_source="rule")
    else:
        decision = llm(clean_text)
        out["intent_source"] = "llm"
    decision = resolve_refs(decision)
    decision = guard(clean_text, decision, out["exec_source"] == "llm")
    if planner is not None:
        from canaan_intent_planner import planning_trigger
        if planning_trigger(clean_text, decision):
            candidate = planner(clean_text, decision)
            if candidate is not None:
                decision = guard(clean_text, resolve_refs(candidate), True)
                out.update(exec_source="llm", intent_source="planner")
    out["decision"] = decision
    return out


def spoken_result(action, decision, exec_res, exec_source, last_step=None, describe=None):
    """先干活再说话、最多一句：成功只念第一句；失败说清卡在哪一步；等确认时说要确认什么。"""
    ok = exec_res.get("success", True)
    reply = decision.get("reply", "我在呢。")
    if not ok and exec_res.get("error_code") == "CONFIRM_REQUIRED":
        speech = exec_res.get("speech_override") or (describe(decision) if describe else reply)
    elif not ok:
        speech = exec_res.get("speech_override") or failure_speech(exec_res.get("message", ""), last_step)
    elif action == "chat":
        speech = reply or exec_res.get("speech_override")
    else:
        speech = one_sentence(exec_res.get("speech_override") or (reply if exec_source == "rule" else None)
                              or exec_res.get("message") or reply)
    typed = get_query_document(exec_res)
    document = merge_prefix(None, typed) if type(typed) is QueryFragment else typed
    if document is not None and document.entries:
        summary = document.summary_reply()
        if summary:
            if ok:
                speech = document.compact_reply(24) or ("查询摘要：" + summary)
            else:
                speech = "已查询：" + summary + str(speech or "")
            if utf16_units(speech) > 4096:
                raise ValueError("Complete query summary exceeds reply budget")
    if not speech or not str(speech).strip():
        speech = "我在呢。" if action == "chat" else "处理完了，结果在屏幕上。"
    return speech


def result_note(exec_res):
    ok = exec_res.get("success", True)
    head = "成功" if ok else "未执行" if exec_res.get("error_code") == "CONFIRM_REQUIRED" else "失败"
    return head + "：" + str(exec_res.get("message", ""))
