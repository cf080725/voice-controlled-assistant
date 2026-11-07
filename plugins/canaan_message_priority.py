"""Explainable local triage. Priority is a reading order, never execution authority."""
import re

SOURCES = {"chatapp": "聊天软件", "imapp": "IMApp", "feishu": "飞书", "claw": "Claw"}
# 公开版只接飞书（官方开放平台）和 Claw / MCP 推送；聊天软件、聊天软件 来源只在自用版里
PERSONAL_ONLY_SOURCES = ("chatapp", "imapp")
try:
    import canaan_config as _cfg
    if getattr(_cfg, "EDITION", "personal") == "public":
        SOURCES = {k: v for k, v in SOURCES.items() if k not in PERSONAL_ONLY_SOURCES}
except ImportError:
    pass
PRIORITIES = {"P0": "尽快核对", "P1": "需要关注", "P2": "普通消息"}
DEFAULT_IMPORTANCE = {"vip_contacts": [], "self_names": [], "muted_conversations": []}
RULE_VERSION = "triage-3"


def validate_importance(values):
    if not isinstance(values, dict) or set(values) - DEFAULT_IMPORTANCE.keys():
        raise ValueError("未知重要程度设置")
    settings = {**DEFAULT_IMPORTANCE, **values}
    for key, items in settings.items():
        if not isinstance(items, list) or len(items) > 100 or any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 200 for s in items):
            raise ValueError(f"{key} 须为最多 100 个非空名称")
        settings[key] = list(dict.fromkeys(s.strip() for s in items))
    return settings


def classify_message(message, settings=None):
    settings = settings or DEFAULT_IMPORTANCE
    text = message.get("text") or ""
    reasons = []
    # shortcut: 中文文本规则不理解所有语境；用人工标注和独立评测集再升级模型。
    if message.get("conversation") in settings["muted_conversations"]:
        return _result(5, "P2", ["你已降低这个会话的优先级"])
    if re.search(r"可能是你的同事.{0,12}请.{0,4}确认", text):
        return _result(10, "P2", ["客户端联系人推荐，不是业务行动通知"])
    if re.search(r"优惠|返现|领券|秒杀|促销|免费领取|加群推广", text):
        return _result(10, "P2", ["疑似推广消息，请核对原文"])
    if re.search(r"(?:引用|转述|举例|诈骗话术)[：:]|小心诈骗|这是诈骗", text):
        return _result(25, "P2", ["引用或风险提醒，不能据此推断本人待办"])
    clauses = re.split(r"[。！？!?\n；;，,]|但是|但|不过", text)
    active = "。".join(c for c in clauses if not re.search(
        r"(?:不用|无需|不必|不要|不需要).{0,12}(?:处理|回复|提交|交(?:周报|日报|报告|作业|材料|表格|方案)|转账|确认|开会)|"
        r"(?:已|已经|已完成|已成功).{0,6}(?:修复|恢复|解决|处理|提交|交(?:周报|日报|报告|作业|材料|表格|方案)|取消)|(?:会议|任务|截止).{0,6}取消|不紧急|不着急", c))
    score = 10 if text else 0
    vip = message.get("sender") in settings["vip_contacts"]
    if vip:
        score += 25
        reasons.append("你设置的重要联系人（名称精确匹配）")
    direct = any(re.search(r"@" + re.escape(name) + r"(?![\w\u4e00-\u9fff])", active) for name in settings["self_names"])
    if direct:
        score += 20
        reasons.append("提到了你设置的本人名称")
    action = bool(re.search(r"请.{0,16}(?:提交|交(?:周报|日报|报告|作业|材料|表格|方案)|回复|确认|排查|修复|处理|参加|审核|过目)|"
                            r"(?:今天|明天|后天|周[一二三四五六日天]|上午|下午|今晚|(?:\d{1,2}|[一二三四五六七八九十两]{1,3})点).{0,16}交(?:周报|日报|报告|作业|材料|表格|方案)|"
                            r"(?<!我)记得.{0,12}(?:提交|回复|确认|参加|处理|打印|带来|收藏|缴|签|报名|上课|开会)|"
                            r"需要你|麻烦|待审批|进展如何|(?<!通)过一下", active))
    deadline = bool(re.search(r"截止|截至|到期|DDL|deadline|今天|明天|后天|周[一二三四五六日天]|(?:\d{1,2}|[一二三四五六七八九十两]{1,3})[点:：]|上午|下午|今晚", active, re.I))
    meeting = bool(re.search(r"开会|会议|评审|答辩|面试|预约", active))
    incident_text = re.sub(r"(?:没有|并未|不存在|无)(?:发生|出现|任何)?(?:故障|崩溃|宕机|告警|数据丢失|泄漏|火灾)(?!原因|根因)", "", active)
    incident = bool(re.search(r"(?:线上|生产|服务).{0,12}(?:故障|崩溃|宕机|告警|不可用)|数据丢失|泄漏|火灾|急救|救命", incident_text))
    urgent = bool(re.search(r"紧急|立即|马上|尽快|严重|立刻", active))
    if action:
        score += 25
        reasons.append("包含具体行动或回复请求，需核对适用角色")
    if deadline and (action or meeting):
        score += 20
        reasons.append("包含时间要求，日期以原文为准")
    if meeting:
        score += 20
        reasons.append("会议或预约通知")
    if incident:
        score = max(score, 85)
        reasons.append("疑似正在发生的故障或人身安全事件")
    if urgent and action:
        score += 20
        reasons.append("明确要求及时处理")
    priority = "P0" if incident or (urgent and action and (deadline or direct)) else "P1" if score >= 35 else "P2"
    if priority != "P0":
        score = min(score, 69)
    return _result(max(75, min(score, 100)) if priority == "P0" else score, priority, reasons or ["未发现明确的行动、时间或重要联系人线索"])


def _result(score, priority, reasons):
    return {"priority": priority, "label": PRIORITIES[priority], "score": score,
            "reasons": reasons, "reviewed": False, "rule_version": RULE_VERSION}


def apply_review(importance, review):
    if not review or not review.get("priority"):
        return importance
    return {**importance, "priority": review["priority"], "label": PRIORITIES[review["priority"]],
            "score": {"P0": 100, "P1": 60, "P2": 0}[review["priority"]], "reviewed": True,
            "reasons": ["你已标注：" + review["reason"]], "suggested_priority": importance["priority"]}
