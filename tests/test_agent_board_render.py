"""智能体任务看板的绘制：在线时画状态计数和任务行（进度条、当前步骤、等你处理 / 失败的说明），
离线时只给提示；最多画 MAX_ROWS 行；没有任何改动任务的按钮（只读）。不起后台线程、不联网。"""
import queue
import tkinter as tk
import types

import pytest

from tests._tk import shared_root

board = pytest.importorskip("canaan_agent_board")


def make_board(data, demo=False):
    root = shared_root()
    self = types.SimpleNamespace(demo=demo, data=data, q=queue.Queue())
    self.lbl_time = tk.Label(root)
    self.cols = {k: {"dot": tk.Label(root), "chips": tk.Frame(root), "rows": tk.Frame(root), "hint": f"{k} 提示"}
                 for k in ("muse", "dots")}
    self._row = lambda parent, t: board.AgentBoard._row(self, parent, t)
    self._render = lambda: board.AgentBoard._render(self)
    self.after = lambda ms, fn: None
    self._pump = lambda: board.AgentBoard._pump(self)
    return self


def texts(widget):
    out = []
    for w in widget.winfo_children():
        try:
            out.append(w.cget("text"))
        except tk.TclError:
            pass
        out += texts(w)
    return out


def test_render_demo_data():
    muse, dots = board.demo_data()
    b = make_board({"muse": muse, "dots": dots}, demo=True)
    b._render()
    rows = texts(b.cols["muse"]["rows"])
    assert "填报销单" in rows and "发票号码是多少？" in rows and any(t.startswith("步骤 2/4") for t in rows)
    chips = texts(b.cols["muse"]["chips"])
    assert len(chips) == 5 and any("1" in c for c in chips)
    assert "未运行" in texts(b.cols["dots"]["rows"])[0]
    assert b.lbl_time.cget("text").startswith("示例数据")
    assert b.cols["muse"]["dot"].cget("fg") == "#22c55e"
    assert not any(isinstance(w, tk.Button) for w in b.cols["muse"]["rows"].winfo_children())   # 只读


def test_render_empty_and_row_limit():
    many = [{"id": str(i), "title": f"任务{i}", "status": "running", "steps": None, "current": "", "updated": 0, "note": ""}
            for i in range(board.MAX_ROWS + 5)]
    b = make_board({"muse": {"online": True, "tasks": many}, "dots": {"online": True, "tasks": []}})
    b._render()
    assert len(b.cols["muse"]["rows"].winfo_children()) == board.MAX_ROWS
    assert "还没有任务" in texts(b.cols["dots"]["rows"])
    b._render()                                                       # 再画一次：旧的清掉，不叠加
    assert len(b.cols["muse"]["rows"].winfo_children()) == board.MAX_ROWS


def test_pump_drains_queue():
    muse, dots = board.demo_data()
    b = make_board({"muse": None, "dots": None})
    b.q.put((muse, dots))
    board.AgentBoard._pump(b)
    assert b.data["muse"] is muse and b.q.empty()
