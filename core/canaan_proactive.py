# -*- coding: utf-8 -*-
"""
主动开口（克制版）。只在这些情况说话，先响一声提示音：
    1. 明确的提醒：“20 分钟后提醒我关火”“明天早上 8 点提醒我开会”“下午三点提醒我交报告”——到点必提醒
    2. 每天一次问候：早上 7~11 点，电脑闲置 4 小时以上后第一次有人操作
    3. 任务间隙：连续操作电脑 2 小时提醒休息；退出游戏模式时问一句
限制（提醒除外）：
    - 安静时段（默认 23:00~8:00）不说
    - 全屏/演示/游戏/勿扰时不说（SHQueryUserNotificationState ≠ 可通知）
    - 每天最多 3 次，两次之间至少 60 分钟
    - 用户打断或说“知道了/别说了”算一次拒绝：该类间隔翻倍；同类被拒 2 次当天不再说
    - 说“别打扰/今天别打扰我”：当天不再主动开口（提醒照常）
"""
import os
import re
import json
import time
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass

try:
    import canaan_config as _cfg
except Exception:
    _cfg = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE_DIR, "data", "proactive.json")

QUIET = tuple(getattr(_cfg, "PROACTIVE_QUIET_HOURS", None) or (23, 8))
MAX_PER_DAY = int(getattr(_cfg, "PROACTIVE_MAX_PER_DAY", 3))
MIN_GAP_MIN = float(getattr(_cfg, "PROACTIVE_MIN_GAP_MIN", 60))
BREAK_AFTER_MIN = float(getattr(_cfg, "PROACTIVE_BREAK_AFTER_MIN", 120))

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}


def _num(s):
    """阿拉伯数字或中文数字（到九百九十九）：十二、九十、一百二十、零五 → 12、90、120、5。"""
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    if s == "半":
        return 0.5
    if not s or any(ch not in _CN_NUM and ch != "百" for ch in s):
        return None
    if s.startswith("零") and len(s) > 1:            # “三点零五”的“零五”
        s = s.lstrip("零")
    total, cur = 0, None
    for ch in s:
        if ch == "百":
            total += (cur if cur is not None else 1) * 100
            cur = None
        elif ch == "十":
            total += (cur if cur is not None else 1) * 10
            cur = None
        elif ch == "零":
            continue
        elif ch == "半":
            return None
        else:
            if cur is not None:                        # “一二”这种连写不是数
                return None
            cur = _CN_NUM[ch]
    return total + (cur or 0)


# “提醒我 / 叫我 / 喊我 / 通知我”都算；必须同时有明确的时间（时长或钟点），“叫我小明”这类不会被当成提醒
_VERB = r"(?:提醒|叫|喊|通知)(?:一下|一声)?我"
_WD = "一二三四五六日天"
_DAY = rf"(今天|明天|后天|大后天|今晚|今早|明早|明晚|下周[{_WD}]|下星期[{_WD}]|下礼拜[{_WD}]|周[{_WD}]|星期[{_WD}]|礼拜[{_WD}])?"
_PART = r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?"
_CLOCK = r"([\d一二两三四五六七八九十]+)\s*(?:[点:：]钟?|时)\s*(?:(?![一三]刻)([\d零一二三四五六七八九十]+)\s*分?|(半)|(一刻)|(三刻))?\s*整?"
_DUR = r"([\d一二两三四五六七八九十百零]+(?:\.\d+)?)?\s*(?:个)?\s*(半)?\s*(分钟|小时|钟头)"
_REL_RE = re.compile(rf"^(?:请)?(?:过|再)?\s*{_DUR}(?:以?后|之后)?\s*(?:再)?{_VERB}\s*(.*)$")
_REL2_RE = re.compile(rf"^{_VERB}\s*{_DUR}(?:以?后|之后)\s*(.*)$")
_ABS_RE = re.compile(rf"^(?:在)?{_DAY}\s*{_PART}\s*{_CLOCK}\s*(?:的时候|左右)?\s*(?:再)?{_VERB}\s*(.*)$")
_ABS2_RE = re.compile(rf"^{_VERB}\s*{_DAY}\s*{_PART}\s*{_CLOCK}\s*(?:的时候|左右)?\s*(.*)$")
_SET_HEAD = r"^(?:帮我|给我|请)?(?:设|定|加|订)(?:一?个|一下)?\s*"
_SET_RE = re.compile(_SET_HEAD + r"(.+?)的\s*([^\s的，,]{1,8})\s*提醒\s*(.*)$")       # 设个十五分钟后的喝水提醒
_SET_PLAIN_RE = re.compile(_SET_HEAD + r"(.+?)的?\s*提醒\s*[，,]?\s*(.*)$")              # 设个明早八点的提醒，开会
_WEEKDAY = {c: i for i, c in enumerate("一二三四五六")} | {"日": 6, "天": 6}
_DAY_PART = {"今晚": "晚上", "明晚": "晚上", "今早": "早上", "明早": "早上"}
DEFAULT_WHAT = "时间到了"


def _day_offset(day, now):
    """日子说法 → 距今天的天数；None 表示没说（由钟点自己决定今天还是明天）。"""
    if not day:
        return None
    if day in ("今天", "今晚", "今早"):
        return 0
    if day in ("明天", "明早", "明晚"):
        return 1
    if day == "后天":
        return 2
    if day == "大后天":
        return 3
    target = _WEEKDAY[day[-1]]
    if day.startswith("下"):
        return (7 - now.weekday()) + target          # 下周一 = 下一个自然周的周一
    return (target - now.weekday()) % 7               # 周三：今天或接下来最近的周三
_MUTE_RE = re.compile(r"^(?:今天)?(?:先)?(?:别|不要)(?:再)?(?:打扰|烦)我?(?:了)?(?:啊|吧)?$|^勿扰(?:模式)?$")
_DISMISS_RE = re.compile(r"^(?:知道了|别说了|好了好了|行了|闭嘴|不用你管|烦死了)$")


_REMINDER_TYPOS = (("提行我", "提醒我"), ("提醒下我", "提醒我"), ("提醒一哈我", "提醒我"),
                   # 10-26：“七十五分钟后喊我一下，看看……”“三小时后别忘了提醒我……”“六十分钟以后提醒俺……”
                   # “二十五分钟过后提醒我……”“八分钟后给个提醒，让我……”
                   ("喊我一下，", "提醒我"), ("喊我一下", "提醒我"), ("别忘了提醒我", "提醒我"), ("提醒俺", "提醒我"),
                   ("过后提醒我", "后提醒我"), ("给个提醒，让我", "提醒我"), ("给个提醒让我", "提醒我"))
# 不带“提醒我”但意思一样的说法：“给我提个醒”“帮我记着”“帮我记个提醒，……”
_IMPLIED_VERB_RE = re.compile(r"(?:帮我|给我|替我|请)?(?:记(?:一?个|个|下)?提醒|设(?:一?个)?提醒|提(?:一?个|个)醒)(?:哈|啊|呀|吧)?"
                              r"|(?:帮我|给我|替我|请)(?:记着|记一下|记住)(?:哈|啊|呀|吧)?")
# 明说不要提醒（“别给我创建提醒”“我自己记着”）：不设
_NO_REMINDER_RE = re.compile(r"(?:别|不要|不用|不必|无需|甭)(?:再)?(?:给我|帮我|替我)?(?:创建|设|定|加|弄|建)?(?:一?个)?提醒|自己记")
_TIME_HEAD_RE = re.compile(rf"^(?:{_DUR}(?:以?后|之后)|{_DAY}\s*{_PART}\s*{_CLOCK})")
# “下午四点四十，今天的，提醒我……”：中间插进来的“今天的”挪到最前面
_FLOAT_DAY_RE = re.compile(r"[，,]\s*(今天|明天|后天)的?\s*[，,]\s*")


def parse_reminder(text, now=None):
    """解析明确的提醒，返回 (到点时间 datetime, 内容) 或 None。
    先按原话解析；认不出时依次试：把“提个醒 / 记着”换成“提醒我”，去掉句首一句交代（“我先歇会儿，……”）。"""
    now = now or datetime.now()
    t = re.sub(r"[。.!！~～\s]+$", "", (text or "").strip())
    for wrong, right in _REMINDER_TYPOS:
        t = t.replace(wrong, right)
    if _NO_REMINDER_RE.search(t):
        return None
    m = _FLOAT_DAY_RE.search(t)
    if m and not t.startswith(m.group(1)):
        t = m.group(1) + t[:m.start()] + t[m.end():]
    candidates = [t]
    if not re.search(_VERB, t) and _IMPLIED_VERB_RE.search(t):
        rest = _IMPLIED_VERB_RE.sub("", t, count=1)
        rest = re.sub(r"^[，,\s]+", "", re.sub(r"[，,]{2,}", "，", rest))
        head = _TIME_HEAD_RE.match(rest)
        if head:
            candidates.append(rest[:head.end()] + "提醒我" + rest[head.end():])
    lead = re.match(r"^([^，,。]{1,15})[，,]\s*(.+)$", t)
    if lead and not re.search(r"别|不要|不用|不必|取消|算了|如果|假如|要是|说|问", lead.group(1)):
        candidates.append(lead.group(2))
    for cand in candidates:
        got = _parse_reminder_core(cand, now)
        if got:
            due, what = got
            # 去掉内容末尾“别让我忘了哈”“莫忘了”这类叮嘱，提醒时只念要做的事
            what = re.sub(r"(?:^|[，,])\s*(?:别让我忘了|别忘了|莫忘了|不要忘了|千万别忘|这个我总会漏|我总会忘)[哈啊呀了哦]*$", "", what)
            # 10-26：后面跟的是交代 / 语气（“，已经贴得乱七八糟了”“，定上吧”“，我那时还没睡”“哈”“，别让我到时候忘事”），提醒时只念要做的事
            what = re.sub(r"[，,]\s*(?:已经|定上|我那时|那时|反正|最近|这段时间|省得|免得|别让我|别又|坐太久)[^，,]*$", "", what)
            what = re.sub(r"[哈啊呀哦嘛]+$", "", what.strip("，, "))
            # “提个醒，我要摆好活动用的展品”：内容在“我要”后面
            what = re.sub(r"^我(?:要|得|需要)\s*", "", what)
            if re.fullmatch(r"别让我[^，,]*忘[^，,]*|省得忘了?|免得忘了?", what):
                what = ""
            return due, (what.strip("，, ") or DEFAULT_WHAT)
    return None


def _parse_reminder_core(t, now):
    # “设个十五分钟后的喝水提醒”“定一个明早八点的提醒，开会” → 换成“十五分钟后提醒我喝水”再解析
    m = _SET_RE.match(t) or _SET_PLAIN_RE.match(t)
    if m:
        when, before, after = m.group(1), (m.group(2) if m.re is _SET_RE else ""), m.group(m.re.groups)
        what = (before or after.strip("，, ")).strip()
        t = f"{when.strip()}提醒我{what}"
    m = _REL_RE.match(t) or _REL2_RE.match(t)
    if m:
        raw, half, unit, what = m.groups()
        if raw is None and not half:
            return None
        n = (_num(raw) if raw else 0) or 0
        if raw and _num(raw) is None:
            return None
        n += 0.5 if half else 0                       # “一个半小时”“半小时”
        if n <= 0:
            return None
        delta = timedelta(minutes=n) if unit == "分钟" else timedelta(hours=n)
        return now + delta, (what.strip("，, ") or DEFAULT_WHAT)
    m = _ABS_RE.match(t) or _ABS2_RE.match(t)
    if m:
        day, part, hh, mm, half, quarter, three_q, what = m.groups()
        h = _num(hh)
        if h is None or h > 24:
            return None
        minute = 30 if half else 15 if quarter else 45 if three_q else ((_num(mm) or 0) if mm else 0)
        if minute >= 60:
            return None
        part = part or _DAY_PART.get(day or "")
        if part in ("下午", "傍晚", "晚上", "夜里") and h < 12:
            h += 12
        if part == "中午" and h < 11:
            h += 12
        if h == 24:
            h = 0
        due = now.replace(hour=int(h), minute=int(minute), second=0, microsecond=0)
        offset = _day_offset(day, now)
        if offset is not None:
            due += timedelta(days=offset)
            if day and day[0] in "周星礼" and due <= now:   # 说“周三”而今天周三已过点：下周三
                due += timedelta(days=7)
        elif due <= now:
            due += timedelta(days=1)
        return due, (what.strip("，, ") or DEFAULT_WHAT)
    return None


_default_policy = None
_person_policies = {}
_default_policy_lock = threading.Lock()


def personal_state_path(profile_key, *, recall=False, directory=None):
    import hashlib
    name = hashlib.sha256(str(profile_key).encode('utf-8')).hexdigest()
    return os.path.join(directory or os.path.dirname(STATE_PATH), 'proactive_people', name + ('.recall.json' if recall else '.json'))


def policy_directory(binding, *, context=None):
    people = getattr(getattr(context, 'hub', None), 'people', None)
    store = getattr(binding, 'store', None)
    people = people if people is not None else getattr(store, 'people', None)
    path = getattr(people, 'path', None) or getattr(store, 'path', None)
    return os.path.dirname(os.path.abspath(path)) if path else None


def default_policy(profile_key=None, *, directory=None):
    """Share one policy per person across speech, schedulers and action execution."""
    global _default_policy
    if profile_key is None:
        from canaan_user_context import execution_cancel_event
        execution = execution_cancel_event()
        if execution is not None:
            if execution.is_set():
                raise RuntimeError('Reminder context has changed')
            profile_key = execution.binding.profile_key
            if directory is None:
                directory = policy_directory(execution.binding, context=execution.context)
    profile_key = profile_key or 'owner'
    root = os.path.realpath(directory or os.path.dirname(STATE_PATH))
    with _default_policy_lock:
        if profile_key == 'owner' and os.path.normcase(root) == os.path.normcase(os.path.realpath(os.path.dirname(STATE_PATH))):
            if _default_policy is None:
                _default_policy = ProactivePolicy(state_path=STATE_PATH)
            policy = _default_policy
        else:
            path = (os.path.join(root, os.path.basename(STATE_PATH)) if profile_key == 'owner' else
                    personal_state_path(profile_key, directory=root))
            cache_key = os.path.normcase(os.path.realpath(path))
            policy = _person_policies.get(cache_key)
            if policy is None:
                policy = _person_policies[cache_key] = ProactivePolicy(state_path=path)
        policy.profile_key = profile_key
        return policy


def forget_profile(profile_key, *, directory=None):
    """Remove only this person's state in the people database's data directory."""
    from pathlib import Path
    if not re.fullmatch(r'person:[1-9][0-9]*', str(profile_key)):
        raise ValueError('Only a personal reminder profile can be deleted')
    root = Path(directory or os.path.dirname(STATE_PATH)).resolve()
    paths = [Path(personal_state_path(profile_key, recall=recall, directory=str(root))) for recall in (False, True)]
    if not all(path.resolve().is_relative_to(root) for path in paths):
        raise ValueError('Personal reminder state is outside its data directory')
    with _default_policy_lock:
        policy = _person_policies.pop(os.path.normcase(os.path.realpath(paths[0])), None)
        if policy is None:
            for path in paths:
                path.unlink(missing_ok=True)
        else:
            with policy._reminder_lock:
                policy._deleted = True
                policy.state['reminders'] = []
                for path in paths:
                    path.unlink(missing_ok=True)


def notification_ok():
    """Windows 当前是否适合打扰（非全屏、非演示、非勿扰）。

    系统说“有全屏程序”（QUNS_BUSY）时再核对一下前台窗口：透明的全屏覆盖层（录屏 / 标注工具、游戏覆盖层、
    远程控制的提示层）也会让系统这么报，但主人其实没在看全屏内容。只有前台窗口真的铺满它所在的显示器才算。"""
    try:
        import ctypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            if state.value == 2:                       # QUNS_BUSY：全屏程序（可能是透明覆盖层）
                return not foreground_fullscreen()
            return state.value == 5                    # QUNS_ACCEPTS_NOTIFICATIONS
    except Exception:
        pass
    return True


def foreground_fullscreen():
    """前台窗口是否铺满它所在的显示器（不算桌面本身）。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = wintypes.HWND
        u.MonitorFromWindow.restype = wintypes.HMONITOR
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
        u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        h = u.GetForegroundWindow()
        if not h:
            return False
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        u.GetMonitorInfoW(u.MonitorFromWindow(h, 2), ctypes.byref(mi))
        r = wintypes.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        m = mi.rcMonitor
        return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom
    except Exception:
        return False


def idle_seconds():
    """距离用户上次键鼠操作的秒数。"""
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
        return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0
    except Exception:
        return 0.0


class ProactivePolicy:
    """纯逻辑（时钟可注入，便于测试）：决定某类主动发言现在能不能说。"""

    def __init__(self, state_path=STATE_PATH, clock=datetime.now):
        self.state_path = state_path
        self.clock = clock
        self.state = {"day": "", "count": 0, "last": None, "muted_day": "", "dismiss": {}, "reminders": []}
        self._reminder_lock = threading.RLock()   # 提醒列表会被调度线程和“设提醒”动作同时改
        self._load()

    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state.update(json.load(f))
        except Exception:
            pass
        if not isinstance(self.state["reminders"], list):
            self.state["reminders"] = []

    def save(self):
        import tempfile
        with self._reminder_lock:
            if getattr(self, '_deleted', False):
                return False
            temporary = None
            try:
                directory = os.path.dirname(self.state_path) or '.'
                os.makedirs(directory, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', errors='backslashreplace', dir=directory,
                        prefix=os.path.basename(self.state_path) + '.', suffix='.tmp', delete=False) as file:
                    temporary = file.name
                    json.dump(self.state, file, ensure_ascii=False, indent=1)
                os.replace(temporary, self.state_path)
                temporary = None
                return True
            except Exception:
                return False
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

    def _roll_day(self):
        today = self.clock().strftime("%Y-%m-%d")
        if self.state["day"] != today:
            self.state.update(day=today, count=0, dismiss={})

    def in_quiet_hours(self):
        h = self.clock().hour
        start, end = QUIET
        return h >= start or h < end if start > end else start <= h < end

    def allowed(self, kind):
        """非提醒类：安静时段、勿扰、每日上限、最小间隔（被拒后翻倍）、同类被拒 2 次。返回 (bool, 原因)。"""
        self._roll_day()
        now = self.clock()
        if self.state["muted_day"] == self.state["day"]:
            return False, "今天勿扰"
        if self.in_quiet_hours():
            return False, "安静时段"
        if self.state["count"] >= MAX_PER_DAY:
            return False, "今天次数已满"
        d = self.state["dismiss"].get(kind, 0)
        if d >= 2:
            return False, "这类今天被拒绝过两次"
        if self.state["last"]:
            gap = (now - datetime.fromisoformat(self.state["last"])).total_seconds() / 60
            if gap < MIN_GAP_MIN * (2 ** d):
                return False, "间隔太短"
        return True, ""

    def record_spoken(self, kind):
        self._roll_day()
        self.state["count"] += 1
        self.state["last"] = self.clock().isoformat(timespec="seconds")
        self.state["last_kind"] = kind
        self.save()

    def record_dismissed(self, kind=None):
        self._roll_day()
        kind = kind or self.state.get("last_kind")
        if kind:
            self.state["dismiss"][kind] = self.state["dismiss"].get(kind, 0) + 1
            self.save()

    def mute_today(self):
        with self._reminder_lock:
            self._roll_day()
            previous = self.state["muted_day"]
            self.state["muted_day"] = self.state["day"]
            if not self.save():
                self.state["muted_day"] = previous
                raise OSError("Proactive settings could not be saved")

    # --- 提醒 ---
    def add_reminder(self, due, what):
        from contextlib import nullcontext
        from canaan_user_context import execution_cancel_event
        execution = execution_cancel_event()
        with execution.context.lock if execution is not None else nullcontext():
            if execution is not None and (execution.is_set() or execution.binding.profile_key != getattr(self, 'profile_key', execution.binding.profile_key)):
                raise RuntimeError('Reminder context has changed')
            with self._reminder_lock:
                if getattr(self, '_deleted', False):
                    raise RuntimeError('Reminder profile was deleted')
                self.state["reminders"].append({"due": due.isoformat(timespec="seconds"), "what": what})
                if not self.save():
                    self.state["reminders"].pop()
                    raise OSError('Reminder state could not be saved')

    def requeue_reminder(self, reminder):
        with self._reminder_lock:
            if getattr(self, '_deleted', False):
                return
            self.state["reminders"].append(reminder)
            self.save()

    def due_reminders(self):
        """Peek due work; remove it only after successful delivery."""
        with self._reminder_lock:
            now = self.clock()
            due = []
            for reminder in self.state["reminders"]:
                try:
                    text = reminder["what"]
                    if isinstance(text, str) and text.encode("utf-8") and datetime.fromisoformat(reminder["due"]) <= now:
                        due.append(reminder)
                except (KeyError, TypeError, ValueError):
                    continue
            return due

    def finish_reminder(self, reminder):
        with self._reminder_lock:
            if getattr(self, '_deleted', False):
                return False
            for index, current in enumerate(self.state['reminders']):
                if current is reminder:
                    self.state['reminders'].pop(index)
                    if not self.save():
                        self.state['reminders'].insert(index, current)
                        return False
                    return True
            return False


# 主人自己说拿不定主意 / 说混了：不替他选，先问（提醒、执行动作都一样）
HESITATE_RE = re.compile(r"拿不定|拿不准|纠结|犹豫|选不出|说混|讲混|搞混|弄混|绕一块|没想好|没想妥|还没定|糊涂|"
                         r"两条都算|两句我都要算数|都算我的要求|(?:时间|几点)[^，,。]{0,4}想不起来")


def _other_steps(text, now=None):
    """句子里除了提醒还有别的要做的分句。"""
    try:
        from canaan_intent import command_clauses
    except Exception:
        return False
    # “帮我记个提醒，十二分钟后……”里的“记个提醒”只是说要设提醒，不是另一步
    clauses = [c for c in command_clauses(text) if not _IMPLIED_VERB_RE.fullmatch(c.strip("，, "))]
    if len(clauses) >= 2:
        now = now or datetime.now()
        parsed = [parse_reminder(clause, now) for clause in clauses]
        # 带同一时间的“记个提醒”也可以只是引导，后句补正文；不能凭此新建一条空提醒。
        def timed_header(index, clause):
            implied = _IMPLIED_VERB_RE.search(clause)
            item = parsed[index]
            return (implied and not clause[implied.end():].strip("，, 。！!吧哈呀")
                    and item and item[1] == DEFAULT_WHAT
                    and any(later and later[0] == item[0] and later[1] != DEFAULT_WHAT
                            for later in parsed[index + 1:]))
        clauses = [clause for index, clause in enumerate(clauses) if not timed_header(index, clause)]
    # 两个独立提醒也必须交给复合执行器，不能把第二个时间和任务塞进第一个正文。
    return len(clauses) >= 2


@dataclass(frozen=True)
class ProactiveCommandEffect:
    operation: str
    due: object = None
    what: str = ""

    def __post_init__(self):
        if self.operation not in {"mute_today", "record_dismissed", "add_reminder"}:
            raise ValueError("Unknown deferred proactive operation")
        if self.operation == "add_reminder" and (not isinstance(self.due, datetime) or not isinstance(self.what, str)):
            raise ValueError("A reminder needs a bound datetime and text")


@dataclass(frozen=True)
class PreparedProactiveCommand:
    decision: dict
    effect: ProactiveCommandEffect | None = None


def prepare_command(text, policy):
    """“X 分钟后提醒我…”“别打扰我”“知道了”等，命中返回决策 dict，否则 None。"""
    t = re.sub(r"[。.!！~～\s]+$", "", (text or "").strip())
    if _MUTE_RE.match(t):
        return PreparedProactiveCommand({"action": "chat", "param": "", "emotion": "pamper", "reply": "好，今天我不主动打扰你了，有事喊我。"}, ProactiveCommandEffect("mute_today"))
    if _DISMISS_RE.match(t) and policy.state.get("last") and \
            (policy.clock() - datetime.fromisoformat(policy.state["last"])).total_seconds() < 120:
        return PreparedProactiveCommand({"action": "chat", "param": "", "emotion": "tease", "reply": "好，不啰嗦了。"}, ProactiveCommandEffect("record_dismissed"))
    for wrong, right in _REMINDER_TYPOS:   # “八分钟后给个提醒，让我……”先归一，免得被当成两步
        t = t.replace(wrong, right)
    if _other_steps(t, policy.clock()):
        return None   # “明天十点提醒我……，然后把亮度调到 50%”：交给多步拆分，别把后半句塞进提醒内容
    r = parse_reminder(t, policy.clock())
    if r and re.search(r"……|…|\.\.\.", t) and re.search(r"卡|想不起|记不清|嘴边|哎", t):
        # “今天下午四点提醒我准备……哎事情名字卡嘴边了”：要做的事没说完，先问
        return PreparedProactiveCommand({"action": "chat", "param": "", "emotion": "confident", "reply": "到时候提醒你什么？后面那部分没说完。"})
    if r and r[1] == DEFAULT_WHAT and re.search(r"设(?:一?个)?提醒|定(?:一?个)?提醒", t):
        # “今天11点设个提醒吧，别让我到时候忘事”：只说了时间，没说提醒什么
        return PreparedProactiveCommand({"action": "chat", "param": "", "emotion": "confident", "reply": "到时候提醒你做什么呀？"})
    if r and HESITATE_RE.search(t):
        # 要是一个问句：原来是陈述句，主人听了不知道该接什么（“早八晚八讲混了”→ 问是早上还是晚上）
        both = re.search(r"早[上晨]?\s*([零一二两三四五六七八九十\d]+)[^，,。]{0,3}晚[上]?\s*\1", t)
        ask = f"是早上{both.group(1)}点还是晚上{both.group(1)}点？你定好了我再设。" if both else "要定在什么时间？你定好了我再设。"
        return PreparedProactiveCommand({"action": "chat", "param": "", "emotion": "confident", "reply": ask})
    if r:
        due, what = r
        return PreparedProactiveCommand({"action": "chat", "param": "", "emotion": "confident", "reply": f"准备在{due.strftime('%m月%d日 %H:%M')}提醒你{what}。"}, ProactiveCommandEffect("add_reminder", due, what))
    return None


def apply_command(prepared, policy):
    if not isinstance(prepared, PreparedProactiveCommand):
        raise TypeError("A prepared proactive command is required")
    effect = prepared.effect
    result = dict(prepared.decision)
    if effect is not None:
        if not isinstance(effect, ProactiveCommandEffect):
            raise TypeError("A typed proactive effect is required")
        if effect.operation == "mute_today":
            policy.mute_today()
        elif effect.operation == "record_dismissed":
            policy.record_dismissed()
        elif effect.operation == "add_reminder":
            policy.add_reminder(effect.due, effect.what)
            when = effect.due.strftime("%H:%M") if effect.due.date() == policy.clock().date() else effect.due.strftime("%m月%d日 %H:%M")
            result["reply"] = f"好，{when}提醒你{effect.what}。"
        else:
            raise ValueError("Unknown deferred proactive operation")
    return result


def handle_command(text, policy):
    """Compatibility entry: commit the same interpreted local operation."""
    prepared = prepare_command(text, policy)
    return apply_command(prepared, policy) if prepared is not None else None


class ProactiveScheduler(threading.Thread):
    """每 30 秒检查一次；deliver(text, kind) 由 hub 提供（先响提示音再说话，正在对话时返回 False 表示稍后再试）。"""

    def __init__(self, policy, deliver, is_game=lambda: False, poll=30.0, recall=None, recall_state=None,
                 recall_warmup=60.0, valid=lambda: True, notify=None):
        super().__init__(daemon=True, name="ProactiveScheduler")
        self.policy, self.deliver, self.is_game, self.poll = policy, deliver, is_game, poll
        self.valid, self.notify = valid, notify
        self.recall, self.recall_state = recall, recall_state   # 每天第一次见面提起重点（canaan_daily_recall）
        self._recall_pending = False
        self._delivered_reminders = {}
        # 刚启动时智能体监听、接口都还没起来，等一会儿再拼“今天要记得”，免得漏掉待处理的任务
        # 不能叫 _started：会盖掉 threading.Thread 自己的 _started 事件，start() 直接报错
        self._created_at, self._recall_warmup = time.monotonic(), recall_warmup
        self._halt = threading.Event()
        self._active_since = None
        self._was_idle_long = True
        self._was_game = False

    def step(self, idle=None, notif_ok=None):
        if not self.valid():
            return
        now = self.policy.clock()
        idle = idle_seconds() if idle is None else idle
        notif_ok = notification_ok() if notif_ok is None else notif_ok
        # 1. 提醒：到点必说
        for r in self.policy.due_reminders():
            if not self.valid():
                continue
            reminder_key = id(r)
            if reminder_key in self._delivered_reminders:
                if self.policy.finish_reminder(r):
                    del self._delivered_reminders[reminder_key]
                continue
            try:
                import canaan_notify  # 系统通知：同一条提醒只弹一次（正忙时稍后再说也不重复弹）
                post = self.notify if self.notify is not None else canaan_notify.post
                post("reminder", "提醒", r["what"], key=f"reminder:{r['due']}:{r['what']}")
            except Exception:
                pass
            if self.deliver(f"提醒你：{r['what']}。", "reminder"):
                if not self.policy.finish_reminder(r):
                    self._delivered_reminders[reminder_key] = r
        # 2. 每日问候：闲置 4 小时以上后第一次操作，早上 7~11 点
        if idle > 4 * 3600:
            self._was_idle_long = True
        elif self._was_idle_long and idle < 60:
            self._was_idle_long = False
            self._recall_pending = self.recall is not None and self.recall_state is not None
            if self._recall_pending:
                self._step_recall(notif_ok)
            # 还有“今天要记得”没说时不单独问早安（说重点时就算打过招呼了；没重点可说时再补问早安）
            if not self._recall_pending:
                self._greet_if_morning(notif_ok)
        elif self._recall_pending and idle < 60:
            self._step_recall(notif_ok)   # 刚才正在对话没说成，人还在就再试
        # 3. 连续操作 2 小时提醒休息
        if idle < 300:
            self._active_since = self._active_since or now
            if (now - self._active_since).total_seconds() >= BREAK_AFTER_MIN * 60:
                if self._try("已经连续忙了两个小时了，起来活动一下、喝口水吧。", "break", notif_ok):
                    self._active_since = now
        else:
            self._active_since = None
        # 4. 刚退出游戏
        game = self.is_game()
        if self._was_game and not game:
            self._try("打完啦？战绩怎么样？", "after_game", notif_ok)
        self._was_game = game

    def _step_recall(self, notif_ok):
        if time.monotonic() - self._created_at < self._recall_warmup:
            return                        # 还在预热，保持待提起，下一轮再试
        if not self.recall_state.due():
            self._recall_pending = False
            return
        text = self.recall()
        if not self.valid():
            return
        if not text:                      # 今天没有要提的，不凑数，照常问早安
            self.recall_state.mark()
            self._recall_pending = False
            self._greet_if_morning(notif_ok)
            return
        if notif_ok and not self.is_game() and self.deliver(text, "recall"):
            self.recall_state.mark()
            self.policy.state["greet_day"] = self.policy.clock().strftime("%Y-%m-%d")  # 提过重点就不再单独问早安
            self.policy.save()
            self._recall_pending = False

    def _greet_if_morning(self, notif_ok):
        now = self.policy.clock()
        if 7 <= now.hour < 11 and self.policy.state.get("greet_day") != now.strftime("%Y-%m-%d"):
            if self._try("早上好，今天也要元气满满哦。", "greeting", notif_ok):
                self.policy.state["greet_day"] = now.strftime("%Y-%m-%d")
                self.policy.save()


    def _try(self, text, kind, notif_ok):
        ok, _why = self.policy.allowed(kind)
        if not self.valid() or not ok or not notif_ok or self.is_game():
            return False
        if self.deliver(text, kind):
            self.policy.record_spoken(kind)
            return True
        return False

    def run(self):
        while not self._halt.is_set():
            try:
                self.step()
            except Exception as e:
                print(f"[!] 主动发言检查失败: {e}")
            self._halt.wait(self.poll)

    def stop(self):
        self._halt.set()


class PersonalProactiveScheduler(ProactiveScheduler):
    """Bind each scheduler cycle to one identity before reading any private data."""

    def __init__(self, hub, deliver, is_game=lambda: False, poll=30.0, recall_warmup=60.0):
        super().__init__(hub.proactive_policy, deliver, is_game=is_game, poll=poll, recall_warmup=recall_warmup)
        self.hub = hub
        self._scope = object()
        self._runner = None

    def step(self, idle=None, notif_ok=None):
        from canaan_voice_capture import VoiceCaptureContext, VoiceUIEvent
        import canaan_daily_recall
        capture = VoiceCaptureContext(self.hub)
        if not capture.current(final=True):
            return
        if self._scope != capture.token:
            key = capture.binding.profile_key if capture.binding is not None else 'owner'
            directory = policy_directory(capture.binding, context=getattr(self.hub, 'user_context', None))
            policy = default_policy(key, directory=directory)
            state = (self.hub.recall_state if key == 'owner' else
                     canaan_daily_recall.RecallState(personal_state_path(key, recall=True, directory=directory)))
            def notify(kind, title, body, key=None):
                if not capture.current(final=True):
                    return False
                self.hub.ui_queue.put(VoiceUIEvent('proactive_personal_notice', (kind, title, body, key),
                                                  binding=capture.binding))
                return True
            self._runner = ProactiveScheduler(policy,
                lambda text, kind: self.deliver(text, kind, voice_context=capture),
                is_game=self.is_game, poll=self.poll,
                recall=lambda: self.hub.daily_recall_text(voice_context=capture), recall_state=state,
                recall_warmup=self._recall_warmup, valid=lambda: capture.current(final=True), notify=notify)
            self._scope = capture.token
        return self._runner.step(idle=idle, notif_ok=notif_ok)
