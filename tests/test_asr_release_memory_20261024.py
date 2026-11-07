"""10-24：加载 SenseVoice 后提交内存多占约 2 GB，空跑几次 mimalloc 才还给系统。"""
import threading

import canaan_asr


class Model:
    def __init__(self):
        self.calls = 0

    def generate(self, **kw):
        self.calls += 1
        assert len(kw["input"]) == canaan_asr.SAMPLE_RATE and not kw["input"].any()  # 1 秒静音
        return [{"text": ""}]


def test_stops_as_soon_as_memory_is_returned():
    m = Model()
    readings = iter([3963, 3964, 3964, 3964, 1975, 1975])
    runs, before, after = canaan_asr.release_load_memory(m, private_mb=lambda: next(readings), sleep=lambda _: None)
    assert (runs, before, after) == (4, 3963, 1975) and m.calls == 4


def test_gives_up_after_max_runs():
    m = Model()
    runs, before, after = canaan_asr.release_load_memory(m, max_runs=3, private_mb=lambda: 3963, sleep=lambda _: None)
    assert runs == 3 and before == after == 3963


def test_skips_rounds_while_a_real_transcription_holds_the_lock():
    m = Model()
    held, done = threading.Event(), threading.Event()

    def busy():
        with canaan_asr.ASR_LOCK:
            held.set()
            done.wait(2)

    t = threading.Thread(target=busy)
    t.start()
    held.wait(2)
    try:
        runs, _, _ = canaan_asr.release_load_memory(m, max_runs=3, private_mb=lambda: None, sleep=lambda _: None)
    finally:
        done.set()
        t.join()
    assert runs == 0 and m.calls == 0   # 不和真话抢模型，也不排队等


def test_rounds_retry_after_a_wait_when_startup_load_masks_the_drop():
    """10-25：启动时别的预热同时在分配，一轮空跑没降下来（4857 → 4970），隔一会儿再来一轮。"""
    m = Model()
    waits = []
    readings = iter([4857, 4900, 4970, 4970, 2900])
    runs, before, after, rounds = canaan_asr.release_load_memory_rounds(
        m, max_runs=2, private_mb=lambda: next(readings), sleep=waits.append, gap=0.2, wait=20)
    assert (before, after, rounds) == (4857, 2900, 2) and runs == 3
    assert 20 in waits


def test_rounds_stop_after_first_success_and_give_up_after_limit():
    m = Model()
    readings = iter([3963, 1975])
    assert canaan_asr.release_load_memory_rounds(m, private_mb=lambda: next(readings), sleep=lambda _: None)[3] == 1
    runs, before, after, rounds = canaan_asr.release_load_memory_rounds(
        m, rounds=2, max_runs=2, private_mb=lambda: 4000, sleep=lambda _: None)
    assert (runs, before, after, rounds) == (4, 4000, 4000, 2)


def test_works_without_memory_readings():
    m = Model()
    runs, before, after = canaan_asr.release_load_memory(m, max_runs=2, private_mb=lambda: None, sleep=lambda _: None)
    assert runs == 2 and before is None and after is None
