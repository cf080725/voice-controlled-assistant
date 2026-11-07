# -*- coding: utf-8 -*-
"""把 hub 的界面消息、统一状态事件、提示中心的提示，翻译成灵动岛的状态（canaan_island_state）。

hub 只需要在三个地方接上：处理界面消息时 on_ui、订阅状态事件 on_status_event、每 80ms 调 tick。

规则：
- 对话优先：在听 / 在想 / 说话 / 等确认 / 失败时，新提示只加角标，排队；对话结束后按优先级补播到达动画。
- 到达的提示按形态停留一会儿（聊天 3 秒、重要消息 10 秒、完成 2.5 秒、失败 6 秒、等你处理 12 秒……），
  每个至少停 1.2 秒，然后收起；有正在进行的进度或等你处理的事，收起后保持紧凑显示（圆环 / 琥珀点）。
- 两个以上来源时，旁边分出一个小圆显示第二个来源。
- 全屏 / 演示 / 游戏时：提示只计数；岛只在待机时隐藏，有对话时照常显示（主人喊团团、团团在等确认都看得到）。
- 不把主人的原话放到岛上（投屏时别人也看得到）。
"""
import time

LISTEN = ("wake_word_triggered", "hotkey_trigger", "followup_listening")
BACK_TO_IDLE = ("playback_finished", "followup_ended")
CONVERSATION = ("listening", "thinking", "speaking", "confirm", "failure")
SPEAK_HOLD = 6.0      # 说完话后多久回到待机（没收到播放结束时的兜底）
FAIL_HOLD = 4.0
NOTICE_HOLD = 6.0
CONFIRM_HOLD = 30.0
MIN_SHOW = 1.2
HOLD = {"chat": 3.0, "p0": 10.0, "done": 2.5, "failed": 6.0, "waiting": 12.0, "progress": 3.0, "summary": 6.0, "info": 6.0}
PRIO = {"waiting": 0, "failed": 1, "p0": 2, "chat": 3, "summary": 3, "done": 4, "info": 5, "progress": 6}
REPLY_MAX = 60


class IslandBridge:
    def __init__(self, state, center=None, clock=time.monotonic, quiet_probe=None):
        self.state, self.center, self.clock = state, center, clock
        self.quiet_probe = quiet_probe or (lambda: False)
        self.mode, self.until = "idle", 0.0
        self._last_quiet_check, self._hidden, self._quiet = 0.0, False, False
        self.queue = []               # 等着播到达动画的提示
        self.present = None           # 正在展开的那条
        self.present_since = 0.0
        self.notices = []
        self.confirm_seq = 0
        self.hidden_by_user = False

    # ---- 输入：对话 ----
    def on_ui(self, msg_type, data):
        if msg_type in LISTEN:
            self._set("listening", "在听…", hold=20)
        elif msg_type == "user_speech":
            self._set("thinking", "在想…", hold=20)       # 不把主人的原话放到岛上（投屏时别人也看得到）
        elif msg_type == "task_step":
            _, step, total, _desc = data
            self._set("thinking", f"第 {step}/{total} 步", hold=30)
        elif msg_type == "canaan_reply":
            reply = data[0] if isinstance(data, (tuple, list)) else str(data)
            self._set("speaking", safe_reply(reply), hold=SPEAK_HOLD + min(12.0, len(str(reply)) * 0.18))
        elif msg_type == "task_finished":
            _, success, message = data
            if not success:
                self._set("failure", "没办成：" + str(message)[:40], hold=FAIL_HOLD)
        elif msg_type in BACK_TO_IDLE and self.mode in ("speaking", "listening"):
            self._to_idle()
        elif msg_type == "status":
            st, text = data
            if st == "processing":
                self._set("thinking", str(text)[:30], hold=30)
            elif st == "error":
                self._set("failure", str(text)[:40], hold=FAIL_HOLD)

    def on_status_event(self, event):
        kind = event.get("kind")
        if kind == "awaiting_confirm":
            try:
                import canaan_events
                label = canaan_events.action_label(event.get("action", ""))
            except Exception:
                label = ""
            self.confirm_seq += 1
            self._set("confirm", label or "有一个操作", hold=CONFIRM_HOLD, confirm_id=f"c{self.confirm_seq}")
        elif kind == "failure" and self.mode != "failure":
            self._set("failure", "被拦下了" if event.get("result") == "blocked" else "没办成", hold=FAIL_HOLD)
        elif kind in ("success",) and self.mode == "confirm":
            self._to_idle()

    def confirm_matches(self, confirm_id):
        """岛上点的确认 / 取消，是不是现在这一次确认（防止点到过期的卡片）。"""
        return self.mode == "confirm" and confirm_id == f"c{self.confirm_seq}"

    # ---- 输入：提示中心 ----
    def on_notices(self, event, notice, ordered):
        """提示中心的回调。show = 到达（排队播动画）；其余只更新角标和紧凑显示。"""
        self.notices = ordered
        if event == "show" and notice:
            self.queue = [q for q in self.queue if q["id"] != notice["id"]] + [notice]
            self.queue.sort(key=lambda n: PRIO.get(n.get("variant"), 9))
            if not self._quiet:
                self._try_present(self.clock())
        elif self.present and not any(n["id"] == self.present["id"] for n in ordered):
            self.present = None                      # 正在展开的那条被处理掉了：收起
            if self.mode == "notice":
                self.mode = "idle"
        elif self.present:
            fresh = next((n for n in ordered if n["id"] == self.present["id"]), None)
            if fresh:
                self.present = {**fresh, "arrive": self.present.get("arrive")}
        self._publish()

    # ---- 定时 ----
    def tick(self):
        now = self.clock()
        if now - self._last_quiet_check >= 1.0:
            self._last_quiet_check = now
            quiet = bool(self.quiet_probe())
            if self.center is not None:
                self.center.set_quiet(quiet)
                self.center.tick()
            self._quiet = quiet
        if self.until and now >= self.until and self.mode != "idle":
            if self.mode == "notice":
                self.present = None
            self._to_idle()
        self._try_present(now)
        self._publish()

    def _try_present(self, now):
        """队列里有提示、岛也空着（或正在展开的已经停够 1.2 秒且新的更重要），就播下一条。"""
        if self.mode not in ("idle", "notice") or not self.queue:
            return
        if self.mode == "notice" and now - self.present_since < MIN_SHOW:
            return
        nxt = self.queue[0]
        if self.mode == "notice" and PRIO.get(nxt.get("variant"), 9) > PRIO.get((self.present or {}).get("variant"), 9) \
                and now < self.until:
            return
        self.queue.pop(0)
        if any(n["id"] == nxt["id"] for n in self.notices):     # 已经被处理掉的不播
            self.present, self.present_since = nxt, now
            self.mode = "notice"
            self.until = now + HOLD.get(nxt.get("variant"), NOTICE_HOLD)

    # ---- 内部 ----
    def _set(self, mode, text, hold=0.0, confirm_id=None):
        self.mode = mode
        self.until = self.clock() + hold if hold else 0.0
        self._text = text
        self._confirm_id = confirm_id
        self._publish()

    def _to_idle(self):
        self.mode, self.until = "idle", 0.0
        self._text = ""
        self._publish()

    def resting(self):
        """收起后保持的紧凑显示：等你处理 > 进度。"""
        for v in ("waiting", "progress"):
            n = next((n for n in self.notices if n.get("variant") == v), None)
            if n:
                return n
        return None

    def secondary(self, main_source):
        srcs = []
        for n in self.notices:
            if n.get("source") and n["source"] != main_source and n["source"] not in srcs:
                srcs.append(n["source"])
        return {"source": srcs[0]} if srcs else None

    def _publish(self):
        mode = self.mode
        hidden = self.hidden_by_user or (self._quiet and mode not in CONVERSATION)
        present = self.present if mode == "notice" else (self.resting() if mode == "idle" else None)
        main_src = (present or (self.notices[0] if self.notices else {})).get("source")
        sec = self.secondary(main_src) if mode in ("idle", "notice") and len(self.notices) > 1 else None
        self.state.update(mode="hidden" if hidden else mode, text=getattr(self, "_text", ""), detail="",
                          notices=self.notices, present=present, secondary=sec,
                          confirm_id=getattr(self, "_confirm_id", None) if mode == "confirm" else None)


def safe_reply(reply):
    """岛上显示的团团回答：截短；像是在念消息内容的（消息总结、某某说……）只说“已整理好，详情在看板”。"""
    text = str(reply or "")
    if any(w in text for w in ("消息总结", "聊天记录", "群里", "聊天软件里", "聊天软件里", "说：", "说:")):
        return "已整理好，详情在看板"
    return text[:REPLY_MAX]
