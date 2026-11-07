# -*- coding: utf-8 -*-
"""跨进程文件锁：Windows 用 msvcrt，POSIX 用 fcntl。

用途：日志追加与保留清理共用同一把锁，避免清理重写时把刚追加的行覆盖掉。
锁文件是 `<目标文件>.lock`，不会删除；它只是同步点。
"""
import os
import time
from contextlib import contextmanager


class FileLockTimeout(TimeoutError):
    """等待文件锁超时。"""


@contextmanager
def file_lock(path, timeout=5.0):
    lock_path = str(path) + ".lock"
    parent = os.path.dirname(os.path.abspath(lock_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    start = time.monotonic()
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() - start >= timeout:
                        raise FileLockTimeout(f"等待文件锁超时: {lock_path}")
                    time.sleep(0.05)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() - start >= timeout:
                        raise FileLockTimeout(f"等待文件锁超时: {lock_path}")
                    time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)