# -*- coding: utf-8 -*-
"""
语音里的联系人名常被听错同音字（王小明 → 王晓明 / 黄小明），聊天软件里搜不到就发不出去。
这里拿本机已知的联系人名（别名表 + 收到过消息的会话名）按拼音找最像的一个；
只有唯一、足够像的才改，改完的名字会在确认时念给主人听，主人确认后才发。只读本机文件，不联网。
"""
import json
import os
import re
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALIAS_FILE = os.path.join(ROOT, "chatapp_contacts_alias.json")
EVENTS_FILE = os.path.join(ROOT, "data", "chatapp_events.jsonl")

_lock = threading.Lock()
_cache = {"key": None, "names": set()}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _usable(name):
    name = str(name or "").strip()
    return 2 <= len(name) <= 10 and "群" not in name and not re.search(r"[@\n<>]", name)


def known_contacts(alias_file=ALIAS_FILE, events_file=EVENTS_FILE):
    """别名表里的名字 + 聊天软件消息记录里出现过的会话名（去掉群聊）。按文件修改时间缓存。"""
    key = (alias_file, _mtime(alias_file), events_file, _mtime(events_file))
    with _lock:
        if _cache["key"] == key:
            return set(_cache["names"])
    names = set()
    try:
        with open(alias_file, encoding="utf-8") as f:
            data = json.load(f)
        for table in ("aliases", "name_to_remark"):
            for k, v in (data.get(table) or {}).items():
                names.update(x for x in (k, *(v if isinstance(v, list) else [v])) if isinstance(x, str))
    except Exception:
        pass
    try:
        with open(events_file, encoding="utf-8") as f:
            for line in f:
                try:
                    names.add(json.loads(line).get("conversation") or "")
                except Exception:
                    continue
    except Exception:
        pass
    names = {n.strip() for n in names if _usable(n)}
    with _lock:
        _cache.update(key=key, names=names)
    return set(names)


_INITIALS = ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w")


def _split(syllable):
    for ini in _INITIALS:
        if syllable.startswith(ini) and len(syllable) > len(ini):
            return ini, syllable[len(ini):]
    return "", syllable


def _syllable_score(a, b):
    if a == b:
        return 1.0
    (ia, fa), (ib, fb) = _split(a), _split(b)
    if fa == fb or fa.lstrip("iu") == fb.lstrip("iu"):
        return .7   # 韵母相同：xiang / qiang、wu / hu
    if ia and ia == ib:
        return .4
    return 0.0


def _pinyin(name):
    from pypinyin import lazy_pinyin
    return lazy_pinyin(name)


def similarity(a, b):
    """两个名字的拼音相似度（0~1），长度不同为 0。"""
    if len(a) != len(b) or not a:
        return 0.0
    pa, pb = _pinyin(a), _pinyin(b)
    if len(pa) != len(pb):
        return 0.0
    return sum(_syllable_score(x, y) for x, y in zip(pa, pb)) / len(pa)


def correct_contact(name, known=None, threshold=.75, margin=.15):
    """返回 (结果名, 状态)：状态 exact / corrected / ambiguous（结果为候选列表）/ unknown（原样返回）。"""
    name = str(name or "").strip()
    known = known_contacts() if known is None else set(known)
    if not name or name in known:
        return name, "exact"
    scored = sorted(((similarity(name, k), k) for k in known), reverse=True)
    scored = [(s, k) for s, k in scored if s >= threshold]
    # 只说了名字后两个字（“国强”）：唯一一个以它结尾的已知联系人
    if not scored and len(name) >= 2:
        tails = [k for k in known if len(k) == len(name) + 1 and k.endswith(name)]
        if len(tails) == 1:
            return tails[0], "corrected"
    if not scored:
        return name, "unknown"
    if len(scored) > 1 and scored[0][0] - scored[1][0] < margin:
        return [k for _, k in scored[:3]], "ambiguous"
    return scored[0][1], "corrected"


def correct_decision(decision, known=None):
    """发聊天软件的决策（直接的 send_chatapp，或等确认的 _pending）里联系人搜不到时按拼音改正。
    改正后的名字会出现在确认播报里；有几个都像就改成反问，正文记进草稿（_draft），主人说名字就能接上。"""
    if not decision:
        return decision
    send = decision if decision.get("action") == "send_chatapp" else decision.get("_pending")
    if not send or send.get("action") != "send_chatapp":
        return decision
    try:
        p = json.loads(send.get("param") or "{}")
    except Exception:
        return decision
    old = str(p.get("contact") or "").strip()
    if not old:
        return decision
    try:
        new, status = correct_contact(old, known)
    except Exception:
        return decision  # 拼音库不可用时原样走，聊天软件里搜不到照样会拦下
    if status == "corrected":
        p["contact"] = new
        fixed = dict(send, param=json.dumps(p, ensure_ascii=False))
        print(f"[🔤 联系人纠正] 听到的名字通讯录里没有，按读音改成已知联系人（确认时会念出来）")
        if send is decision:
            return fixed
        return dict(decision, _pending=fixed, reply=str(decision.get("reply") or "").replace(old, new))
    if status == "ambiguous":
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "contact_ambiguous",
                "reply": f"你是要发给{'还是'.join(new)}？", "_draft": {"message": p.get("message", "")}}
    return decision
