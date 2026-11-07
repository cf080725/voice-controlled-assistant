# -*- coding: utf-8 -*-
"""
智能体任务看板：把 OpenMuse 和 OpenDots 的任务画出来（只读、只访问本机、3 秒刷新一次）。

    py -3.12 canaan_agent_board.py           打开看板
    py -3.12 canaan_agent_board.py --demo    用示例数据看效果（服务没开时）

每个智能体一栏：在线状态、各状态数量、最近的任务（状态颜色、标题、步骤进度条、当前步骤、更新时间、结果/问题/错误摘要）。
网络请求在后台线程，界面不卡；看板不提供暂停、取消等改动任务的按钮，要操作请点“打开网页”。
"""
import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import sys
import time
import queue
import threading
import webbrowser
import tkinter as tk

import canaan_agent_board_data as ab

BG, CARD, LINE, FG, MUTED, ACCENT = "#13141f", "#1c1d2b", "#2a2b3d", "#e8e8f0", "#8b8ca3", "#6366f1"
STATUS_COLOR = {"running": "#38bdf8", "queued": "#a78bfa", "waiting": "#f59e0b", "scheduled": "#818cf8",
                "paused": "#94a3b8", "done": "#22c55e", "failed": "#ef4444", "cancelled": "#64748b"}
FONT = "Microsoft YaHei UI"
MAX_ROWS = 10
REFRESH_SEC = 3.0


def demo_data():
    now = time.time()
    muse = {"online": True, "error": "", "tasks": [
        {"id": "1", "title": "填报销单", "status": "waiting", "steps": (2, 4), "current": "等你确认发票号",
         "updated": now - 40, "note": "发票号码是多少？"},
        {"id": "2", "title": "监控京东显卡价格", "status": "running", "steps": (1, 3), "current": "比价中",
         "updated": now - 5, "note": ""},
        {"id": "3", "title": "订下周去杭州的高铁", "status": "failed", "steps": (2, 5), "current": "",
         "updated": now - 1800, "note": "12306 登录超时"},
        {"id": "4", "title": "整理本周会议纪要", "status": "done", "steps": (4, 4), "current": "",
         "updated": now - 7200, "note": "已生成 3 页纪要"}]}
    dots = {"online": False, "error": "连接被拒绝", "tasks": []}
    return muse, dots


class AgentBoard(tk.Tk):
    def __init__(self, demo=False):
        super().__init__()
        self.title("智能体任务看板 · Muse / Dots")
        self.configure(bg=BG)
        self.geometry("880x560")
        self.minsize(640, 400)
        self.demo = demo
        self.q = queue.Queue()
        self.data = {"muse": None, "dots": None}
        head = tk.Frame(self, bg=BG)
        head.pack(fill=tk.X, padx=16, pady=(14, 6))
        tk.Label(head, text="智能体任务看板", bg=BG, fg=FG, font=(FONT, 15, "bold")).pack(side=tk.LEFT)
        self.lbl_time = tk.Label(head, text="正在读取……", bg=BG, fg=MUTED, font=(FONT, 9))
        self.lbl_time.pack(side=tk.RIGHT)
        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 14))
        body.columnconfigure(0, weight=1, uniform="c")
        body.columnconfigure(1, weight=1, uniform="c")
        body.rowconfigure(0, weight=1)
        self.cols = {}
        for i, (key, name, web, hint) in enumerate((("muse", "OpenMuse 自主智能体", ab.MUSE_WEB, "在 D:\\muse 里启动后显示"),
                                                    ("dots", "OpenDots 虚拟同事", ab.DOTS_WEB, "在 D:\\dots 里启动后显示"))):
            card = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=LINE)
            card.grid(row=0, column=i, sticky="nsew", padx=(0, 8) if i == 0 else (8, 0))
            top = tk.Frame(card, bg=CARD)
            top.pack(fill=tk.X, padx=12, pady=(10, 4))
            dot = tk.Label(top, text="●", bg=CARD, fg=MUTED, font=(FONT, 11))
            dot.pack(side=tk.LEFT)
            tk.Label(top, text=name, bg=CARD, fg=FG, font=(FONT, 11, "bold")).pack(side=tk.LEFT, padx=(4, 0))
            tk.Button(top, text="打开网页", command=lambda u=web: webbrowser.open(u), bg=LINE, fg=FG, relief=tk.FLAT,
                      activebackground=ACCENT, activeforeground=FG, font=(FONT, 9), padx=8).pack(side=tk.RIGHT)
            chips = tk.Frame(card, bg=CARD)
            chips.pack(fill=tk.X, padx=12, pady=(0, 6))
            rows = tk.Frame(card, bg=CARD)
            rows.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))
            self.cols[key] = {"dot": dot, "chips": chips, "rows": rows, "hint": hint}
        threading.Thread(target=self._fetch_loop, daemon=True).start()
        self.after(200, self._pump)

    # 后台线程取数据，界面线程画
    def _fetch_loop(self):
        while True:
            if self.demo:
                m, d = demo_data()
            else:
                m, d = ab.fetch_muse(), ab.fetch_dots()
            self.q.put((m, d))
            time.sleep(REFRESH_SEC)

    def _pump(self):
        try:
            while True:
                m, d = self.q.get_nowait()
                self.data = {"muse": m, "dots": d}
                self._render()
        except queue.Empty:
            pass
        self.after(300, self._pump)

    def _render(self):
        self.lbl_time.config(text=("示例数据 · " if self.demo else "") + "更新于 " + time.strftime("%H:%M:%S"))
        for key, col in self.cols.items():
            info = self.data[key]
            col["dot"].config(fg="#22c55e" if info["online"] else MUTED)
            for w in col["chips"].winfo_children() + col["rows"].winfo_children():
                w.destroy()
            if not info["online"]:
                tk.Label(col["rows"], text="未运行\n" + col["hint"], bg=CARD, fg=MUTED, font=(FONT, 10),
                         justify=tk.LEFT).pack(anchor="w", pady=10)
                continue
            c = ab.counts(info["tasks"])
            for st in ("running", "waiting", "queued", "done", "failed"):
                tk.Label(col["chips"], text=f"{ab.STATUS_LABEL[st]} {c.get(st, 0)}", bg=LINE,
                         fg=STATUS_COLOR[st] if c.get(st) else MUTED, font=(FONT, 9), padx=6, pady=1
                         ).pack(side=tk.LEFT, padx=(0, 6))
            if not info["tasks"]:
                tk.Label(col["rows"], text="还没有任务", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=10)
            for t in info["tasks"][:MAX_ROWS]:
                self._row(col["rows"], t)

    def _row(self, parent, t):
        color = STATUS_COLOR.get(t["status"], MUTED)
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill=tk.X, pady=(0, 8))
        tk.Frame(row, bg=color, width=3).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        box = tk.Frame(row, bg=CARD)
        box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        line1 = tk.Frame(box, bg=CARD)
        line1.pack(fill=tk.X)
        tk.Label(line1, text=ab.STATUS_LABEL.get(t["status"], t["status"]), bg=CARD, fg=color,
                 font=(FONT, 9, "bold")).pack(side=tk.LEFT)
        tk.Label(line1, text=t["title"], bg=CARD, fg=FG, font=(FONT, 10), anchor="w").pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(line1, text=ab.ago(t["updated"]), bg=CARD, fg=MUTED, font=(FONT, 8)).pack(side=tk.RIGHT)
        if t.get("steps") and t["steps"][1]:
            done, total = t["steps"]
            bar = tk.Canvas(box, height=6, bg=LINE, highlightthickness=0)
            bar.pack(fill=tk.X, pady=(3, 0))
            bar.bind("<Configure>", lambda e, b=bar, f=done / total, c=color: (
                b.delete("all"), b.create_rectangle(0, 0, int(e.width * f), 6, fill=c, width=0)))
            tk.Label(box, text=f"步骤 {done}/{total}" + (f" · {t['current']}" if t["current"] else ""), bg=CARD,
                     fg=MUTED, font=(FONT, 8), anchor="w").pack(fill=tk.X)
        if t.get("note"):
            tk.Label(box, text=t["note"], bg=CARD, fg=color if t["status"] in ("waiting", "failed") else MUTED,
                     font=(FONT, 9), anchor="w", justify=tk.LEFT, wraplength=360).pack(fill=tk.X)


if __name__ == "__main__":
    AgentBoard(demo="--demo" in sys.argv).mainloop()
