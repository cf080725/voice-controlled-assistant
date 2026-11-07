"""全部界面测试共用一个隐藏的 Tk 根窗口。
同一个进程里反复创建 Tk 解释器，偶尔会初始化失败（“tcl_findLibrary”“找不到 tk.tcl / spinbox.tcl”），
这是之前桌宠测试时好时坏的原因；共用一个就不会再出现。"""
import tkinter as tk

_ROOT = None


def shared_root():
    global _ROOT
    if _ROOT is not None:
        try:
            _ROOT.winfo_exists()
            return _ROOT
        except tk.TclError:
            _ROOT = None
    default = getattr(tk, "_default_root", None)
    if default is not None:
        try:
            default.winfo_exists()
            _ROOT = default
            return _ROOT
        except tk.TclError:
            pass
    _ROOT = tk.Tk()
    _ROOT.withdraw()
    return _ROOT
