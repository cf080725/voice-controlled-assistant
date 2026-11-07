"""Ground relative volume key counts in their own source clause.

This module has no device, model, configuration, or execution dependencies.
The caller supplies the existing number parser and masks quoted payloads.
"""
import re


_ACTIONS = {"volume_up", "volume_down"}
_OBJECT = r"(?:(?:系统|电脑|扬声器|外放|耳机)的?\s*)?(?:音量|声音)"
_MODIFIERS = r"(?:(?:给我|帮我|先|再|稍微|稍稍)\s*)*"
_UP = r"往上(?:调|加|升)?|向上(?:调|加)?|调高|调大|提高|升高|上调|增大|加大|增加"
_DOWN = r"往下(?:调|降|减)?|向下(?:调|减)?|调低|调小|降低|下调|减小|减少|关小"
_VERB = rf"(?P<verb>{_UP}|{_DOWN})"
_FRAMES = (
    re.compile(rf"^(?:把|将)?\s*{_OBJECT}\s*{_MODIFIERS}{_VERB}\s*(?P<tail>[^，,；;。！!?？\n]*)$"),
    re.compile(rf"^{_MODIFIERS}{_VERB}\s*{_OBJECT}\s*(?P<tail>[^，,；;。！!?？\n]*)$"),
)
_COUNT = re.compile(r"(?P<number>[0-9]{1,2}|[一二两三四五六七八九]|十[一二三四五六七八九]?|[二三四]十[一二三四五六七八九]?|五十)\s*(?:档|格|次)")
_VAGUE = re.compile(r"(?:一?点(?:点|儿)?|一些|些|一下|一小点|一丢丢)?")
_POLITE_END = re.compile(r"(?:吧|呗|呀|啊|啦|了|哈|就行|即可)+$")
_STEP_UNIT = re.compile(r"[0-9零〇一二两三四五六七八九十百半几若干点.负-]\s*(?:档|格|次)|%|％|百分之")


def volume_adverb_continues(before, after):
    """Keep 再 between an unfinished volume object and its relative verb.

    A completed predicate such as 调低音量 cannot match the object-only head,
    so its following 再 still starts another operation.
    """
    return bool(re.fullmatch(rf"(?:把|将)?\s*{_OBJECT}\s*{_MODIFIERS}", before.strip())
                and re.match(rf"(?:{_UP}|{_DOWN})", after.lstrip()))


def _parameter(decision, value):
    if decision.get("param") == value:
        return decision
    updated = {**decision, "param": value}
    # A source-derived value has no observed model token probability.
    if "_pconf" in updated:
        updated["_pconf"] = None
    return updated


def _clarify(decision):
    return {"action": "chat", "param": "", "_guard": "volume_steps_mismatch", "_pending": None,
            "reply": "请说明音量调整方向和1到50的整数档数。", "_model": (decision or {}).get("_model", "")}


def normalize_volume_steps(source, decision, parse_number, *, rules=False):
    """Recover explicit counts, retaining implicit defaults and action ownership.

    ``source`` is one local operation with request prefixes/quotes handled by
    the intent layer. This function does not authorize the operation: existing
    negation, mention, plan, permission, and confidence checks still apply.
    """
    if decision is not None and decision.get("action") not in _ACTIONS:
        return decision
    source = source.strip(" 。！!？?\r\n")
    match = next((m for frame in _FRAMES if (m := frame.fullmatch(source))), None)
    if match is None:
        # A count without a volume target must not become a default operation.
        if decision and (not source or '“Q”' in source or _STEP_UNIT.search(source)):
            return None if rules else _clarify(decision)
        return decision if rules or decision is None else _parameter(decision, "")
    tail = _POLITE_END.sub("", match["tail"].strip()).strip()
    # The existing target-inheritance path can produce “音量调大音量”.
    # The repeated object still names the same domain, never a numeric value.
    tail = re.sub(rf"^{_OBJECT}\s*", "", tail, count=1)
    if _VAGUE.fullmatch(tail):
        return decision if rules or decision is None else _parameter(decision, "")
    count = _COUNT.fullmatch(tail)
    if count is None:
        return _clarify(decision)
    number = parse_number(count["number"])
    if number is None or not 1 <= number <= 50 or int(number) != number:
        return _clarify(decision)
    action = "volume_up" if re.fullmatch(_UP, match["verb"]) else "volume_down"
    if decision is not None and decision["action"] != action:
        return _clarify(decision)
    if decision is None:
        decision = {"action": action, "emotion": "confident", "reply": "音量调大了。" if action == "volume_up" else "音量调小了。"}
    return _parameter(decision, str(int(number)))
