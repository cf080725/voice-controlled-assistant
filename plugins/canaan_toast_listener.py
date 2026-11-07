# -*- coding: utf-8 -*-
"""读 Windows 系统通知（WinRT UserNotificationListener），转成统一提示中心的提示。

- 普通桌面程序订阅“通知变化”事件可能报错（网上有 0x80070490 的报告），这里用轮询 GetNotificationsAsync（默认 2 秒）。
  本机实测：已授权、轮询能拿到 聊天软件 / Claude / ChatGPT 等应用的通知。
- 只处理启动之后新出现的通知；团团自己发的不再转回来。
- 聊天类应用：只算条数（发件人只在主人开了“显示发件人”时用）；其他应用：只取标题那一行，脱敏截短交给提示中心。
- 没授权时不弹窗，只报告状态；授权要主人在看板 / 托盘里点一次（request_access 会弹出系统的授权框）。
"""
import asyncio
import re
import threading

OWN_APPS = ("团团", "团团", "Python", "pythonw", "Canaan")
DONE_RE = re.compile(r"完成|已完成|成功|done|finished|completed|succeeded|ready", re.I)
FAIL_RE = re.compile(r"失败|出错|错误|error|failed|exception", re.I)
WAIT_RE = re.compile(r"等待|需要你|需要您|请确认|待确认|approval|permission|waiting|needs your|input required", re.I)


def classify(app, title):
    """应用名和标题 → (提示类别, 是否聊天类)。"""
    from canaan_notice_center import is_chat_source
    if is_chat_source(app):
        return "message", True
    t = title or ""
    if WAIT_RE.search(t):
        return "waiting", False
    if FAIL_RE.search(t):
        return "failed", False
    if DONE_RE.search(t):
        return "done", False
    return "info", False


def texts_of(notification):
    """通知里的文字行（只在内存里用，不落盘）。"""
    try:
        binding = notification.notification.visual.get_binding("ToastGeneric")
        return [t.text for t in binding.get_text_elements() if t.text]
    except Exception:
        return []


class ToastListener(threading.Thread):
    def __init__(self, center, poll=2.0):
        super().__init__(daemon=True, name="toast-listener")
        self.center, self.poll = center, poll
        self.seen = set()
        self.status = "未启动"
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    async def _snapshot(self):
        import winrt.windows.ui.notifications as n
        import winrt.windows.ui.notifications.management as m
        listener = m.UserNotificationListener.current
        if int(listener.get_access_status()) != 1:
            self.status = "未授权：请在看板“设置 → 提示中心”里点“允许读取系统通知”"
            return None
        return list(await listener.get_notifications_async(n.NotificationKinds.TOAST))

    def handle(self, items, first=False):
        """处理一批通知。first=True 时只记下已有的，不提示。"""
        for it in items:
            try:
                nid = it.id
                app = it.app_info.display_info.display_name
            except Exception:
                continue
            if nid in self.seen:
                continue
            self.seen.add(nid)
            if first or any(o.lower() in (app or "").lower() for o in OWN_APPS):
                continue
            lines = texts_of(it)
            title = lines[0] if lines else ""
            kind, chat = classify(app, title)
            if chat:
                # 聊天类：第一行一般是发件人，第二行是正文；正文只在主人允许预览的对象上用
                self.center.post(app, "message", title=lines[1] if len(lines) > 1 else "", sender=title)
            else:
                self.center.post(app, kind, title=title, key=f"toast:{app}:{nid}")
        if len(self.seen) > 2000:
            self.seen = set(list(self.seen)[-1000:])

    def run(self):
        global _current
        _current = self
        try:
            import winrt.windows.ui.notifications.management  # noqa: F401
        except ImportError:
            self.status = "没装 winrt 组件，系统通知不可用"
            return
        loop = asyncio.new_event_loop()
        first = True
        while not self._halt.is_set():
            try:
                items = loop.run_until_complete(self._snapshot())
                if items is not None:
                    self.handle(items, first=first)
                    first = False
                    self.status = "正常"
            except Exception as e:
                self.status = f"读取系统通知出错：{type(e).__name__}"
            self._halt.wait(self.poll)
        loop.close()


_current = None


def status():
    """看板显示用：读取系统通知现在是什么状态（只有文字）。"""
    return _current.status if _current is not None else "未启动（团团启动后自动开始）"


def request_access():
    """弹出系统授权框（只在主人点按钮时调用）。返回授权状态文字。"""
    import winrt.windows.ui.notifications.management as m

    async def go():
        return int(await m.UserNotificationListener.current.request_access_async())
    status = asyncio.run(go())
    return {1: "已允许", 2: "已拒绝（要去 Windows 设置 → 隐私 → 通知 里打开）"}.get(status, "未决定")
