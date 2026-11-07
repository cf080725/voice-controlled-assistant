# -*- coding: utf-8 -*-
"""统一提示中心：电脑里各处来的提示（系统通知、已采集的消息、Muse / Dots、VS Code 和编程助手……）
统一成一种“提示”，去重、合并、限频，再交给灵动岛（主）、桌宠（配合）、Windows 系统通知（重要的）。

隐私（主人定：聊天类默认只显示来源和条数）：
- 聊天和邮件类来源（聊天软件、聊天软件、飞书、钉钉、Outlook、邮件……）只给“聊天软件：3 条新消息”，不带正文、默认不带发件人；
  主人在设置里把某个联系人 / 群加进“可以预览”名单后，才显示一句脱敏过的预览。
- 其他来源只用标题那一行（脱敏、截短），不读正文。
- 全屏 / 演示 / 游戏时只计数不弹，结束后汇总一次。
- 提示本身只在内存里，不写日志，最多保留 24 小时；灵动岛显示用的那一份见 canaan_island_state（只有脱敏后的短标题）。

每条提示有一个“形态”（variant）：chat 聊天、p0 重要消息、progress 进度、waiting 等你处理、done 完成、
failed 失败、info / summary 一般和汇总。灵动岛和桌宠按形态做不同的动作。
“到达”（发 show 事件、带新的 arrive 序号）才做到达动画；同一个聊天来源 60 秒内只到达一次，之后只更新条数。
"""
import json
import os
import threading
import time
import uuid

KINDS = ("message", "progress", "waiting", "done", "failed", "info")
CHAT_SOURCES = {"聊天软件", "ChatApp", "IMApp", "TIM", "飞书", "Lark", "钉钉", "DingTalk", "企业聊天软件", "WeCom", "Telegram",
                "WhatsApp", "Discord", "Slack", "Teams", "Microsoft Teams", "Line", "Signal",
                # 邮件也按聊天处理：只显示来源和条数（发件人、主题都可能是隐私）
                "Outlook", "Outlook (new)", "Mail", "邮件", "Gmail", "Foxmail", "网易邮箱大师", "邮箱", "Thunderbird"}
PRIORITY = {"waiting": 0, "failed": 1, "message": 2, "done": 3, "progress": 4, "info": 5}
VARIANT_PRIORITY = {"waiting": 0, "failed": 1, "p0": 2, "chat": 3, "summary": 3, "done": 4, "info": 5, "progress": 6}
TTL = 24 * 3600
STALE = 2 * 3600            # 进度 / 等你处理两小时没有更新就算过期（比如插件崩了没报结束）
RATE = 60                   # 同一个聊天来源 60 秒内只做一次到达动画
DEDUPE = 30                 # 同一条提示 30 秒内重复报，只更新不再展开
MAX_NOTICES = 30
TITLE_MAX = 40


def _mask(text):
    if not text:
        return ""
    try:
        from canaan_privacy_shield import PrivacyShield
        return PrivacyShield.mask_text(text)[0]
    except Exception:
        return ""                                   # 脱敏出错就不显示内容


def is_chat_source(source):
    return any(s.lower() == (source or "").lower() for s in CHAT_SOURCES)


def variant_of(n):
    if n.get("key") == "quiet-summary":
        return "summary"
    if n["kind"] == "message":
        return "p0" if n.get("important") else "chat"
    return n["kind"]


_QUERY_RE = None


def is_notice_query(text):
    """“刚才谁找我”“有什么新提示”“有人给我发消息吗”这类问句（问提示本身，不是让做事）。"""
    global _QUERY_RE
    if _QUERY_RE is None:
        import re
        _QUERY_RE = re.compile(r"^(?:团团|加南)?[，,\s]*(?:刚才|刚刚|这会儿|现在|今天)?(?:有)?(?:谁|有人|什么人|哪些人)(?:找过?我|给我发(?:了)?(?:消息|信息)?|发消息给我)"
                               r"|^(?:团团|加南)?[，,\s]*(?:刚才|刚刚|现在)?有(?:什么|哪些|没有)(?:新)?(?:提示|通知)(?:吗|呀|啊|呢)?[？?]?$"
                               r"|^(?:团团|加南)?[，,\s]*(?:提示|通知)(?:都)?有(?:什么|哪些)")
    return bool(_QUERY_RE.search(str(text or "").strip()))


def default_actions(kind, openable=True):
    acts = []
    if openable:
        acts.append({"id": "open", "label": "打开"})
    if kind in ("message", "info", "done"):
        acts.append({"id": "later", "label": "稍后"})
    acts.append({"id": "dismiss", "label": "忽略"})
    return acts


class NoticeCenter:
    def __init__(self, clock=time.time, preview_allow=None, show_sender=False):
        self.clock = clock
        self.lock = threading.RLock()
        self.notices = []                          # 显示顺序由 ordered() 决定
        self.subscribers = []
        self.quiet = False
        self.quiet_counts = {}
        self.preview_allow = set(preview_allow or ())   # 主人允许预览的联系人 / 群
        self.show_sender = show_sender
        self.arrive_seq = 0
        self._last_arrive = {}                     # 来源 → 上次到达的时间（聊天限频）
        self._snoozed = []

    # ---- 订阅 ----
    def subscribe(self, fn):
        self.subscribers.append(fn)

    def _emit(self, event, notice=None):
        public = self.public(notice) if notice else None
        for fn in list(self.subscribers):
            try:
                fn(event, public, self.ordered())
            except Exception:
                pass

    # ---- 写入 ----
    def post(self, source, kind, title="", detail="", progress=None, key=None, sender=None, conversation=None,
             openable=True, actions=None, important=False):
        """新提示或更新已有提示（同一个 key）。返回提示 id；被丢弃时返回 None。
        important=True 只对聊天消息有意义：重要消息（P0），一定展开，不受限频。"""
        if kind not in KINDS or not source:
            return None
        source = str(source)[:24]
        with self.lock:
            self._expire_locked()
            if is_chat_source(source) and kind == "message":
                return self._post_chat_locked(source, sender, conversation, title, important)
            title = _mask(str(title))[:TITLE_MAX]
            detail = _mask(str(detail))[:TITLE_MAX] if detail else ""
            key = key or f"{source}:{kind}:{title}"
            existing = next((n for n in self.notices if n["key"] == key), None)
            now = self.clock()
            # 同一件事的进度往前走（第 2/5 步 → 第 3/5 步）只更新角标和进度条，不再每步展开打扰
            if existing and kind == "progress" and existing["kind"] == "progress":
                existing.update(title=title or existing["title"], ts=now,
                                progress=progress if progress is not None else existing.get("progress"))
                self._emit("update", existing)
                return existing["id"]
            if existing:
                same = existing["kind"] == kind
                existing.update(kind=kind, title=title or existing["title"], detail=detail or existing["detail"],
                                progress=progress if progress is not None else existing.get("progress"), ts=now,
                                actions=actions or default_actions(kind, openable))
                notice = existing
                repeat = same and now - notice.get("arrived_at", -1e9) < DEDUPE
            else:
                notice = {"id": uuid.uuid4().hex[:10], "key": key, "source": source, "kind": kind, "title": title,
                          "detail": detail, "progress": progress, "ts": now, "count": 1,
                          "actions": actions or default_actions(kind, openable)}
                self.notices.append(notice)
                repeat = False
            if kind in ("done", "failed") and notice.get("progress") is not None:
                notice["progress"] = 100 if kind == "done" else notice["progress"]
            self._trim_locked()
            return self._deliver_locked(notice, arrive=not repeat)

    def _post_chat_locked(self, source, sender, conversation, preview, important=False):
        """聊天类：按来源合并计数；默认不带正文和发件人。"""
        key = f"chat:{source}"
        now = self.clock()
        notice = next((n for n in self.notices if n["key"] == key), None)
        if notice is None:
            notice = {"id": uuid.uuid4().hex[:10], "key": key, "source": source, "kind": "message", "title": "",
                      "detail": "", "progress": None, "ts": now, "count": 0, "senders": [],
                      "actions": default_actions("message")}
            self.notices.append(notice)
        notice["count"] += 1
        notice["ts"] = now
        notice["important"] = bool(important or notice.get("important"))
        who = (sender or "").strip()[:12]
        if who and who not in notice["senders"]:
            notice["senders"] = (notice["senders"] + [who])[-3:]
        if self.show_sender and notice["senders"]:
            names = "、".join(notice["senders"][:2])
            notice["title"] = f"{names}{'等' if len(notice['senders']) > 1 or notice['count'] > 1 else ''} {notice['count']} 条新消息"
        else:
            notice["title"] = f"{notice['count']} 条新消息"
        allowed = (conversation and conversation in self.preview_allow) or (sender and sender in self.preview_allow)
        notice["detail"] = _mask(str(preview or ""))[:TITLE_MAX] if allowed and preview else ""
        self._trim_locked()
        limited = not important and now - self._last_arrive.get(source, -1e9) < RATE
        return self._deliver_locked(notice, arrive=not limited)

    def _deliver_locked(self, notice, arrive=True):
        if self.quiet:
            self.quiet_counts[notice["source"]] = self.quiet_counts.get(notice["source"], 0) + 1
            self._emit("count", notice)
        elif arrive:
            now = self.clock()
            self.arrive_seq += 1
            notice["arrive"] = self.arrive_seq
            notice["arrived_at"] = now
            self._last_arrive[notice["source"]] = now
            self._emit("show", notice)
        else:
            self._emit("update", notice)
        return notice["id"]

    # ---- 安静时段 ----
    def set_quiet(self, quiet):
        with self.lock:
            if quiet == self.quiet:
                return
            self.quiet = quiet
            if not quiet and self.quiet_counts:
                total = sum(self.quiet_counts.values())
                srcs = "、".join(list(self.quiet_counts)[:3])
                self.quiet_counts = {}
                summary = {"id": uuid.uuid4().hex[:10], "key": "quiet-summary", "source": "提示", "kind": "info",
                           "title": f"刚才来了 {total} 条提示", "detail": srcs, "progress": None, "ts": self.clock(),
                           "count": total, "actions": [{"id": "list", "label": "查看"}, {"id": "dismiss", "label": "忽略"}]}
                self.notices = [n for n in self.notices if n["key"] != "quiet-summary"] + [summary]
                self._deliver_locked(summary)

    # ---- 定时 ----
    def tick(self):
        """定时调用（灵动岛那边每秒一次）：推迟的提示到点回来（重新展开一次），过期的、卡住的进度清掉。"""
        with self.lock:
            before = len(self.notices)
            back = self._expire_locked()
            for n in back:
                self._deliver_locked(n)
            if not back and len(self.notices) != before:
                self._emit("update")

    # ---- 读 / 处理 ----
    def ordered(self):
        with self.lock:
            return [self.public(n) for n in sorted(self.notices, key=lambda n: (PRIORITY.get(n["kind"], 9), -n["ts"]))]

    @staticmethod
    def public(n):
        """给界面的字段（不含内部的发件人列表等）。"""
        out = {k: n.get(k) for k in ("id", "source", "kind", "title", "detail", "progress", "count", "actions", "arrive")}
        out["variant"] = variant_of(n)
        return out

    def find(self, notice_id):
        with self.lock:
            n = next((n for n in self.notices if n["id"] == notice_id), None)
            return self.public(n) if n else None

    def act(self, notice_id, action):
        """岛上点了某条提示的按钮。返回 (动作, 提示) 交给 hub 做；dismiss / later 在这里处理。"""
        with self.lock:
            notice = next((n for n in self.notices if n["id"] == notice_id), None)
            if notice is None:
                return None, None
            if action in ("dismiss", "open", "list"):
                self.notices.remove(notice)
            elif action == "later":
                notice["ts"] = self.clock() + 30 * 60      # 半小时后再排到前面
                self.notices.remove(notice)
                self._snoozed.append(notice)
            self._emit("update")
            return action, self.public(notice)

    def dismiss_source(self, source, chat_only=False):
        """清掉某个来源的提示。chat_only=True 时只清聊天 / 邮件那条（主人切到这个应用、看过了）。"""
        with self.lock:
            keep = [n for n in self.notices if n["source"] != source or (chat_only and n["kind"] != "message")]
            if len(keep) != len(self.notices):
                self.notices = keep
                self._emit("update")
                return True
            return False

    def summary_text(self):
        """“刚才谁找我”的回答：只说来源、条数和标题类别，不说正文。"""
        lst = self.ordered()
        if not lst:
            return "这会儿没有新提示。"
        parts = []
        for n in lst[:5]:
            if n["kind"] == "message":
                parts.append(f"{n['source']} {n['count']} 条消息" + ("（有重要的）" if n["variant"] == "p0" else ""))
            elif n["kind"] == "progress":
                parts.append(f"{n['source']} 在做「{n['title']}」" + (f"，{n['progress']}%" if n.get("progress") is not None else ""))
            else:
                word = {"waiting": "在等你处理", "failed": "没办成", "done": "办好了", "info": ""}.get(n["kind"], "")
                parts.append(f"{n['source']}{word}「{n['title']}」" if n["title"] else f"{n['source']}{word}")
        more = f"，还有 {len(lst) - 5} 条" if len(lst) > 5 else ""
        return "刚才：" + "；".join(parts) + more + "。"

    def _expire_locked(self):
        """返回到点回来的推迟提示（调用方决定要不要重新展开）。"""
        now = self.clock()
        self.notices = [n for n in self.notices
                        if (now - n["ts"] < TTL and n["kind"] not in ("waiting", "progress")) or
                        (n["kind"] in ("waiting", "progress") and now - n["ts"] < STALE)]
        back = []
        for n in list(self._snoozed):
            if now >= n["ts"]:
                self._snoozed.remove(n)
                self.notices.append(n)
                back.append(n)
        return back

    def _trim_locked(self):
        if len(self.notices) > MAX_NOTICES:
            keep = sorted(self.notices, key=lambda n: (PRIORITY.get(n["kind"], 9), -n["ts"]))[:MAX_NOTICES]
            self.notices = [n for n in self.notices if n in keep]


SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "notice_settings.json")
PREVIEW_MAX = 50
FLAGS = {"sound": False, "speak_important": False, "blur": True}     # 提示音、重要的事说出来、灵动岛背景模糊


def load_settings(path=SETTINGS_PATH):
    out = {"show_sender": False, "preview_allow": [], **FLAGS}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        out["show_sender"] = bool(data.get("show_sender", False))
        out["preview_allow"] = [str(x)[:40] for x in data.get("preview_allow", [])][:PREVIEW_MAX]
        for k, v in FLAGS.items():
            out[k] = bool(data.get(k, v))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return out


def validated_settings(body):
    show = body.get("show_sender", False)
    allow = body.get("preview_allow", [])
    if not isinstance(show, bool):
        raise ValueError("show_sender 须为 true / false")
    if not isinstance(allow, list) or len(allow) > PREVIEW_MAX or \
            not all(isinstance(x, str) and 1 <= len(x.strip()) <= 40 for x in allow):
        raise ValueError(f"预览名单最多 {PREVIEW_MAX} 个，每个 1–40 字")
    out = {"show_sender": show, "preview_allow": sorted({x.strip() for x in allow})}
    for k, v in FLAGS.items():
        val = body.get(k, v)
        if not isinstance(val, bool):
            raise ValueError(f"{k} 须为 true / false")
        out[k] = val
    return out


def save_settings(settings, path=SETTINGS_PATH, center=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)
    os.replace(tmp, path)
    center = center or default()
    with center.lock:
        center.show_sender = settings["show_sender"]
        center.preview_allow = set(settings["preview_allow"])
    for fn in list(_settings_listeners):
        try:
            fn(settings)
        except Exception:
            pass
    return settings


_settings_listeners = []


def on_settings(fn):
    """设置改了以后通知（灵动岛的提示音、背景模糊；桌宠的“重要的事说出来”）。"""
    _settings_listeners.append(fn)


_default = None


def default():
    global _default
    if _default is None:
        s = load_settings()
        _default = NoticeCenter(preview_allow=s["preview_allow"], show_sender=s["show_sender"])
    return _default
