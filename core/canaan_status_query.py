"""Read-only, cooperatively bounded answers for explicit system-status queries.

Ports are supplied by the actor. No native library is imported or called here.
The budget observes a blocked provider only after that provider returns.
"""
from dataclasses import dataclass
import math
import re
import time


@dataclass(frozen=True)
class StatusAnswer:
    success: bool
    message: str
    speech: str
    detail: dict
    error_code: str = ""


def query_kind(user_text):
    """Select only an explicit, positive query clause in the trusted utterance."""
    prefix = r"(?:请|麻烦|帮我|先|再)*(?:查(?:询)?(?:一下|下)?|查看|看看|看一下|查查)?"
    for clause in re.split(r"[，,。；;]|然后|接着|之后", user_text or ""):
        clause = clause.strip().rstrip("？?！!啊呀吧呢吗呗嘛")
        memory = re.fullmatch(
            prefix + r"(?:告诉我)?(?:现在|目前|当前)?"
            r"(?:(?:这台)?电脑|本机|系统)?(?:的)?(?:现在|目前|当前)?"
            r"(?:(?:可用|剩余)(?:运行)?内存(?:有|是)?(?:多少|多大)?"
            r"|(?:运行)?内存(?:还剩|剩余|还有|剩下|可用|剩)(?:有|是)?(?:多少|多大)?(?:空间|容量)?"
            r"|(?:还剩|剩余|还有|剩下|可用|剩)(?:多少|多大)(?:可用|的)?(?:运行)?内存)",
            clause)
        if memory:
            return "memory_available", ()
        process = re.fullmatch(prefix + r"(?P<count>哪个|哪些)(?:程序|软件|进程)(?:现在|当前|目前)?(?:正在|在)?(?:最|比较)?占(?P<resource>内存|CPU|cpu)(?:最多|最高)?", clause)
        if not process:
            process = re.fullmatch(prefix + r"(?:最)?占(?P<resource>内存|CPU|cpu)的(?:程序|软件|进程)(?P<ending>是哪个|有哪些|是哪些)?", clause)
        if process:
            plural = process.groupdict().get("count") == "哪些" or process.groupdict().get("ending") in ("有哪些", "是哪些")
            return ("process_memory" if process.group("resource") == "内存" else "process_cpu", ("plural",) if plural else ())
        temperature = re.fullmatch(prefix + r"(显卡|GPU|gpu|CPU|cpu)(?:的)?温度(?:有?多少度?|高不高)?", clause)
        if temperature:
            return ("temperature_cpu" if temperature.group(1).lower() == "cpu" else "temperature_gpu", ())
        disk = re.fullmatch(prefix + r"([A-Za-z])\s*盘(?:的)?(?:还剩|还有|剩余|剩)(?:多少|多大)?(?:容量|容亮|空间)?", clause)
        if disk:
            return "disk", (disk.group(1).upper() + ":\\",)
    return None


class _Stopped(Exception):
    def __init__(self, code):
        self.code = code


class _Budget:
    def __init__(self, clock, should_stop, timeout):
        self.clock, self.should_stop = clock, should_stop
        self.started = self.last = clock()
        self.deadline = self.started + timeout

    def check(self):
        try:
            if self.should_stop() is not False:
                raise _Stopped("STATUS_CANCELLED")
        except _Stopped:
            raise
        except BaseException:
            raise _Stopped("STATUS_CANCELLED") from None
        now = self.clock()
        if not math.isfinite(self.started) or not math.isfinite(now) or now < self.last:
            raise _Stopped("STATUS_INCOMPLETE")
        self.last = now
        if now >= self.deadline:
            raise _Stopped("STATUS_TIMEOUT")
        return now


def _number(value, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Not a numeric reading")
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ValueError("Invalid reading")
    return value


def _name(value):
    # Process names are provider data: keep them to one line and out of markup.
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or "未知进程")).strip()[:128] or "未知进程"


def _failure(code, detail=None):
    speech = {
        "STATUS_CANCELLED": "这次状态查询已停止。",
        "STATUS_TIMEOUT": "这次状态查询超时，请稍后再查。",
        "STATUS_INCOMPLETE": "状态采样未完成，请稍后再查。",
        "STATUS_UNAVAILABLE": "未能读取所问的状态数据。",
    }[code]
    return StatusAnswer(False, speech, speech, detail or {}, code)


def answer_query(query, provider, *, should_stop=None, clock=None, sleep=None,
                 timeout=2.0, max_processes=512, sample_window=0.15):
    """Answer a selected query with fakeable, late-bound provider and time ports."""
    if type(max_processes) is not int or max_processes < 1:
        raise ValueError("max_processes must be positive")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if type(sample_window) not in (int, float) or not math.isfinite(sample_window) or sample_window <= 0 or sample_window >= timeout:
        raise ValueError("sample_window must fit the budget")
    clock = clock if clock is not None else time.monotonic
    sleep = sleep if sleep is not None else time.sleep
    should_stop = should_stop if should_stop is not None else lambda: False
    budget = _Budget(clock, should_stop, timeout)
    kind, targets = query
    detail = {"query": kind, "complete": False}
    transient = (OSError, ValueError, AttributeError, NotImplementedError)
    try:
        budget.check()
        transient += tuple(t for t in (getattr(provider, name, None) for name in
                                      ("AccessDenied", "NoSuchProcess", "ZombieProcess"))
                           if isinstance(t, type) and issubclass(t, Exception))
        if kind == "memory_available":
            if targets:
                return _failure("STATUS_UNAVAILABLE", detail)
            reading = provider.virtual_memory()
            budget.check()
            total, available = reading.total, reading.available
            # Native byte counters are unsigned integers, never percentages.
            if (type(total) is not int or type(available) is not int
                    or not 0 < total < 2 ** 64 or not 0 <= available <= total):
                return _failure("STATUS_UNAVAILABLE", detail)
            amount = f"{available}字节"
            for unit, scale in (("EiB", 2 ** 60), ("PiB", 2 ** 50), ("TiB", 2 ** 40),
                                ("GiB", 2 ** 30), ("MiB", 2 ** 20), ("KiB", 2 ** 10)):
                if available >= scale:
                    amount = f"{available / scale:.1f}{unit}"
                    break
            detail.update(complete=True, total_bytes=total, available_bytes=available)
            speech = f"可用内存{amount}。"
            message = f"当前可用内存 {available} 字节（{amount}），物理总内存 {total} 字节。"
            return StatusAnswer(True, message, speech, detail)
        if kind == "disk":
            drive = targets[0]
            if not re.fullmatch(r"[A-Z]:\\", drive):
                return _failure("STATUS_UNAVAILABLE", detail)
            free = _number(provider.disk_usage(drive).free) / 1024 ** 3
            budget.check()
            detail.update(complete=True, disks_free_gb={drive[0]: round(free, 1)})
            amount = f"{free / 1024:.1f}T" if free >= 1024 else f"{free:.1f}G"
            speech = f"{drive[0]}盘余{amount}。"
            return StatusAnswer(True, f"{drive[0]}盘剩余 {free:.1f} GB", speech, detail)
        if kind in ("temperature_cpu", "temperature_gpu"):
            source = getattr(provider, "sensors_temperatures", None)
            readings = source() if callable(source) else {}
            budget.check()
            resource = "CPU" if kind.endswith("cpu") else "GPU"
            pattern = (r"coretemp|k10temp|cpu[_ -]?thermal|cputemp|zenpower|\bcpu\b" if resource == "CPU"
                       else r"amdgpu|nvidia|\bgpu\b")
            values = []
            for group, entries in (readings or {}).items():
                for entry in entries:
                    budget.check()
                    label = str(getattr(entry, "label", ""))
                    if not re.search(pattern, str(group) + " " + label, re.I):
                        continue
                    try:
                        value = _number(entry.current, minimum=-100)
                    except (ValueError, AttributeError):
                        continue
                    if value <= 200:
                        values.append((value, str(group)[:80], label[:80]))
            if not values:
                speech = f"目前无法读取{resource}温度。"
                return StatusAnswer(False, speech, speech, detail, "STATUS_UNAVAILABLE")
            value, group, label = max(values)
            detail.update(complete=True, temperature_c=value, sensor_group=group, sensor_label=label,
                          selection="maximum matching sensor")
            speech = f"{resource}最高温{value:.1f}度。"
            return StatusAnswer(True, speech, speech, detail)
        if kind not in ("process_cpu", "process_memory"):
            return _failure("STATUS_UNAVAILABLE", detail)
        cpu = kind == "process_cpu"
        plural = "plural" in targets
        rows, seen = [], set()
        skipped = examined = 0
        complete = True
        iterator = iter(provider.process_iter())
        budget.check()
        for process in iterator:
            budget.check()
            if examined >= max_processes:
                complete = False
                break
            examined += 1
            try:
                pid = process.pid
                if type(pid) is not int or pid <= 0 or pid in seen:
                    continue
                seen.add(pid)
                if process.is_running() is not True:
                    raise ValueError("Process identity changed")
                budget.check()
                name = _name(process.name())
                budget.check()
                created = _number(process.create_time())
                budget.check()
                if cpu:
                    times = process.cpu_times()
                    value = _number(_number(times.user) + _number(times.system))
                    at = budget.check()
                    if process.is_running() is not True:
                        raise ValueError("Process identity changed")
                    budget.check()
                    rows.append((process, pid, name, created, value, at))
                else:
                    value = _number(process.memory_info().rss)
                    budget.check()
                    if process.is_running() is not True or _number(process.create_time()) != created:
                        raise ValueError("Process identity changed")
                    budget.check()
                    rows.append((pid, name, value))
            except transient:
                skipped += 1
        if cpu and rows:
            wait_until = budget.check() + sample_window
            while True:
                remaining = wait_until - budget.check()
                if remaining <= 0:
                    break
                sleep(min(0.02, remaining))
            sampled = []
            for process, pid, name, created, previous, at in rows:
                budget.check()
                try:
                    if process.is_running() is not True or _number(process.create_time()) != created:
                        raise ValueError("Process identity changed")
                    budget.check()
                    times = process.cpu_times()
                    current = _number(_number(times.user) + _number(times.system))
                    now = budget.check()
                    if process.is_running() is not True:
                        raise ValueError("Process identity changed")
                    budget.check()
                    elapsed = now - at
                    if current < previous or elapsed < sample_window * (1 - 1e-9):
                        raise ValueError("Invalid CPU sample")
                    sampled.append((pid, name, (current - previous) * 100 / elapsed))
                except transient:
                    skipped += 1
            rows = sampled
            detail.update(cpu_percent_basis="one logical CPU = 100%", sample_window_min_s=sample_window)
        budget.check()
        complete = complete and skipped == 0
        detail.update(complete=complete, examined=examined, skipped=skipped, readable=len(rows),
                      scope="enumerated readable processes at sampling time")
        if not rows:
            return _failure("STATUS_UNAVAILABLE", detail)
        pid, name, value = max(rows, key=lambda row: (row[2], -row[0]))
        if cpu and value == 0:
            speech = "采样期间可读进程CPU占用均为0。"
            detail["all_sampled_cpu_zero"] = True
            return StatusAnswer(True, speech, speech, detail)
        short_name = re.sub(r"[。！？!?~～，,；;]", "", name)[:8] or str(pid)
        scope = "" if complete else "可读中"
        if cpu:
            speech = f"{scope}{short_name}单核{value:.0f}%。"
            message = f"{('已枚举的可读进程中，' if not complete else '')}{name}（PID {pid}）CPU占用最高，{value:.1f}%（单个逻辑CPU=100%，间隔采样）。"
            metric = "cpu_percent"
        else:
            speech = f"{scope}{short_name}内存{value / 1024 ** 2:.0f}M。"
            message = f"{('已枚举的可读进程中，' if not complete else '')}{name}（PID {pid}）驻留内存最高，{value / 1024 ** 2:.1f} MB（RSS）。"
            metric = "rss_bytes"
        detail.update(top_process={"pid": pid, "name": name, metric: value})
        if plural:
            candidates = [row for row in rows if not cpu or row[2] > 0]
            ranked = sorted(candidates, key=lambda row: (-row[2], row[0]))[:3]
            detail.update(top_processes=[{"pid": p, "name": n, metric: v} for p, n, v in ranked],
                          returned_count=len(ranked), result_limit=3)
            if cpu:
                detail["positive_cpu_processes"] = len(candidates)
                items = "，".join(f"{n}（PID {p}）{v:.1f}%" for p, n, v in ranked)
                speech = f"{scope}{short_name}单核{value:.0f}%，列{len(ranked)}项。"
                message = f"本次可读进程的CPU占用前{len(ranked)}项：{items}（单个逻辑CPU=100%，间隔采样，仅列正值）；共读取{len(rows)}项，最多展示3项。"
            else:
                items = "，".join(f"{n}（PID {p}）{v / 1024 ** 2:.1f} MB" for p, n, v in ranked)
                speech = f"{scope}{short_name}内存{value / 1024 ** 2:.0f}M，列{len(ranked)}项。"
                message = f"本次可读进程的驻留内存前{len(ranked)}项：{items}（RSS）；共读取{len(rows)}项，最多展示3项。"
        return StatusAnswer(True, message, speech, detail)
    except _Stopped as stopped:
        return _failure(stopped.code, detail)
    except transient:
        return _failure("STATUS_UNAVAILABLE", detail)
