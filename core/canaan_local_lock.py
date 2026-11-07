"""A process-scoped advisory lock, released by closing the returned file."""
import os
from pathlib import Path


def acquire_file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lease = path.open("a+b")
    if lease.tell() == 0:
        lease.write(b"0")
        lease.flush()
    lease.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lease.close()
        raise RuntimeError("该数据目录的服务已在运行，请勿重复启动") from None
    return lease
