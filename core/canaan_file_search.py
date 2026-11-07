"""Cooperative, read-only filename traversal with explicit completeness."""
from dataclasses import dataclass
import math
import os
import time


@dataclass(frozen=True)
class FileSearchResult:
    paths: tuple
    status: str
    reason: str = ""
    directories: int = 0
    files: int = 0

    @property
    def complete(self):
        return self.status == "complete"


def _root_exists(path):
    # os.path.exists suppresses PermissionError/OSError. Stateful callers must
    # distinguish a missing root from an inaccessible portion of their scope.
    try:
        os.stat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def search_files(keyword, roots, *, limit=2, timeout=2.0,
                 max_directories=4096, max_files=32768,
                 should_stop=None, clock=None, walk=None, exists=None):
    """Return partial candidates with a reason; never open a file.

    Callbacks and budgets are trusted server ports. Checks cannot interrupt a
    blocked walk/metadata call: a deadline is observed only after it returns.
    Dependencies bind at call time so temporary-tree adapters remain usable.
    """
    for value in (limit, max_directories, max_files):
        if type(value) is not int or value < 1:
            raise ValueError("Search counts must be positive integers")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Search timeout must be positive and finite")
    clock, walk, exists = clock or time.monotonic, walk or os.walk, exists or _root_exists
    should_stop = should_stop if should_stop is not None else lambda: False
    paths, seen = [], set()
    directories = files_seen = 0
    io_error = False
    started = clock()
    deadline = started + timeout

    def result(status, reason):
        return FileSearchResult(tuple(paths), status, reason, directories, files_seen)

    def checkpoint():
        try:
            if should_stop() is not False:
                return result("cancelled", "stopped")
        except BaseException:
            return result("cancelled", "stop_check_failed")
        now = clock()
        if not math.isfinite(now):
            return result("incomplete", "clock_invalid")
        if now >= deadline:
            return result("timeout", "deadline")
        return None

    def onerror(_error):
        nonlocal io_error
        io_error = True

    keyword = keyword.lower().strip() if keyword else ""
    stopped = checkpoint()
    if stopped is not None:
        return stopped
    for base in roots:
        stopped = checkpoint()
        if stopped is not None:
            return stopped
        try:
            present = exists(base)
            stopped = checkpoint()
            if stopped is not None:
                return stopped
            if not present:
                continue
            iterator = iter(walk(base, onerror=onerror))
            while True:
                stopped = checkpoint()
                if stopped is not None:
                    return stopped
                if directories >= max_directories:
                    return result("incomplete", "directory_budget")
                try:
                    root, dirs, files = next(iterator)
                except StopIteration:
                    break
                stopped = checkpoint()
                if stopped is not None:
                    return stopped
                directories += 1
                dirs[:] = [name for name in dirs if not name.startswith(".") and
                           name not in ("__pycache__", "node_modules", "dist", "$RECYCLE.BIN")]
                for name in files:
                    stopped = checkpoint()
                    if stopped is not None:
                        return stopped
                    if files_seen >= max_files:
                        return result("incomplete", "file_budget")
                    files_seen += 1
                    if keyword in name.lower():
                        path = os.path.join(root, name)
                        if path not in seen:
                            seen.add(path)
                            paths.append(path)
                            if len(paths) >= limit:
                                return result("incomplete", "match_limit")
        except OSError:
            io_error = True
    stopped = checkpoint()
    if stopped is not None:
        return stopped
    return result("incomplete", "io_error") if io_error else result("complete", "exhausted")
