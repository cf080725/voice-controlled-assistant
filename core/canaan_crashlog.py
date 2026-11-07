# -*- coding: utf-8 -*-
"""崩溃原因记录：未捕获的异常、界面回调里的异常、原生崩溃（faulthandler）写到 logs/hub_crash.log。

只记时间、在哪个线程、异常类型和调用栈（文件名:行号:函数名），不记异常消息——消息里可能带着主人说的原话。
"""
import faulthandler
import os
import sys
import threading
import time
import traceback

_state = {"path": None, "fault_file": None}
# 每次启动在原生崩溃日志里写一行说明：faulthandler 在 Windows 上会把一些已处理的系统异常也记成
# “Windows fatal exception”，看着像崩溃其实不是。真正有没有崩溃，看托盘写的 logs/hub_exits.jsonl。
BENIGN_CODES = ("0x80010108", "0x8001010d", "0x406d1388", "0xe06d7363", "0x000006ba", "0x80010012")
NATIVE_LOG_NOTE = ("\n===== {time} 启动 =====\n"
                   "# 说明：下面如果出现 Windows fatal exception: code " + " / ".join(BENIGN_CODES) +
                   "，多半是正常关闭或系统已处理的异常（Python faulthandler 在 Windows 上的已知误报），不是崩溃；"
                   "是否真的崩溃看 logs/hub_exits.jsonl 里托盘记的分类。\n")


def is_benign_native(line):
    """原生崩溃日志里这一行是不是已知误报。"""
    return "Windows fatal exception" in line and any(c in line.lower() for c in BENIGN_CODES)


def _frames(tb):
    return [f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in traceback.extract_tb(tb)][-12:]


def record(exc_type, tb, where):
    """写一条崩溃记录；本身出错也不抛。"""
    path = _state["path"]
    if not path:
        return
    try:
        line =f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{where}] {getattr(exc_type, '__name__', str(exc_type))}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            for fr in _frames(tb):
                f.write(f"    {fr}\n")
    except Exception:
        pass


def install(log_dir):
    """在 hub 启动时调用一次。"""
    os.makedirs(log_dir, exist_ok=True)
    _state["path"] = os.path.join(log_dir, "hub_crash.log")

    def excepthook(exc_type, exc, tb):
        record(exc_type, tb, "主线程")
    sys.excepthook = excepthook

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        record(args.exc_type, args.exc_traceback, f"线程 {getattr(args.thread, 'name', '?')}")
    threading.excepthook = thread_hook

    try:  # 原生崩溃（访问违例等）只会输出各线程的调用栈，不含任何数据
        _state["fault_file"] = open(os.path.join(log_dir, "hub_crash_native.log"), "a", encoding="utf-8")
        _state["fault_file"].write(NATIVE_LOG_NOTE.format(time=time.strftime("%Y-%m-%d %H:%M:%S")))
        _state["fault_file"].flush()
        faulthandler.enable(file=_state["fault_file"], all_threads=True)
    except Exception:
        pass


def tk_callback_hook(exc_type, exc, tb):
    """赋给 tk.Tk.report_callback_exception：界面回调里的异常只记类型和调用栈。"""
    record(exc_type, tb, "界面回调")
