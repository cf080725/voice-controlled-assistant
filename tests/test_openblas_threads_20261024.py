"""10-24：numpy 的 OpenBLAS 按 CPU 线程数（本机 18）预留缓冲，每个进程光 import 就占约 595 MB 提交内存。
主程序、语音服务限 4 线程，灵动岛限 1 线程。"""
import ast
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def method(name, **namespace):
    path = ROOT / "core/canaan_desk_os_hub.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CanaanDeskOSHub")
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def test_hub_limits_openblas_before_any_numpy_import():
    src = (ROOT / "core/canaan_desk_os_hub.py").read_text(encoding="utf-8")
    limit = src.index('os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")')
    first_numpy = min(i for i in (src.find("import numpy"), src.find("from numpy")) if i >= 0)
    assert limit < first_numpy


def test_island_is_started_with_one_blas_thread(monkeypatch):
    seen = {}

    def popen(cmd, **kw):
        seen.update(kw["env"])
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "4")
    import os
    launch = method("_launch_island", os=os, sys=sys, threading=threading, BASE_DIR=str(ROOT),
                    subprocess=SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL),
                    time=SimpleNamespace(sleep=lambda *_: None))
    hub = SimpleNamespace(ui_queue=SimpleNamespace(put=lambda *_: None))
    launch(hub)
    assert seen["OPENBLAS_NUM_THREADS"] == "1" and seen["CANAAN_PARENT_PID"]


def test_tray_children_get_the_limit_but_keep_user_override():
    src = (ROOT / "shells/canaan_tray.py").read_text(encoding="utf-8")
    assert 'env.setdefault("OPENBLAS_NUM_THREADS", "4")' in src
    assert 'tts_env.setdefault("OPENBLAS_NUM_THREADS", "4")' in src
