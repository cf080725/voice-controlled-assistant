"""Canonical status actor through the real registry; all hardware ports fake."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "core") not in sys.path:
    sys.path.insert(0, str(ROOT / "core"))

import canaan_status_query as helper
import canaan_turn_pipeline as turns
import canaan_execution_guard as execution_guard_module
from canaan_action_framework import models
from canaan_action_framework import action_registry as registry_module
from canaan_action_framework.actions import system_actions

class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Denied(Exception):
    pass


class Gone(Exception):
    pass


class Process:
    def __init__(self, clock, pid=11, name="editor.exe", rss=500 * 1024 ** 2, rate=0.3, deny=False, reuse=False):
        self.clock, self.pid, self.label, self.rss, self.rate = clock, pid, name, rss, rate
        self.deny, self.reuse = deny, reuse
        self.creates = 0
        self.running_checks = 0
    def name(self):
        if self.deny:
            raise Denied()
        return self.label
    def create_time(self):
        self.creates += 1
        return 10.0  # psutil caches this, so repeated calls alone cannot detect reuse.
    def is_running(self):
        self.running_checks += 1
        return not (self.reuse and self.running_checks > 1)
    def memory_info(self):
        return NS(rss=self.rss)
    def cpu_times(self):
        return NS(user=1.0 + self.clock.now * self.rate, system=0.0)


def provider(clock):
    calls = []
    processes = [Process(clock), Process(clock, pid=22, name="browser.exe", rss=1024 ** 3, rate=0.8)]
    source = NS(cpu_percent=lambda **kw: 12.3, virtual_memory=lambda: NS(percent=45.6),
                sensors_battery=lambda: NS(percent=78), AccessDenied=Denied, NoSuchProcess=Gone,
                process_iter=lambda: iter(processes),
                sensors_temperatures=lambda: {"coretemp": [NS(current=62.5, label="Package id 0")],
                                              "nvidia": [NS(current=53.0, label="GPU")]})
    def disk_usage(drive):
        calls.append(drive)
        return NS(free={"C:\\": 128.5, "D:\\": 256.8, "E:\\": 64.0}[drive] * 1024 ** 3)
    source.disk_usage = disk_usage
    return source, processes, calls


@pytest.fixture
def actor(monkeypatch):
    clock = Clock()
    port, processes, calls = provider(clock)
    registry = registry_module.ActionRegistry
    status_action = system_actions.SystemStatusAction()
    audits = []
    monkeypatch.setattr(system_actions, "psutil", port)
    monkeypatch.setattr(registry, "_actions", {"system_status": status_action})
    monkeypatch.setattr(registry, "_alias_map", {name: "system_status" for name in
                        ["system_status", *status_action.aliases]})
    monkeypatch.setattr(registry, "_middlewares", [])
    monkeypatch.setattr(registry, "_audit_log", classmethod(
                        lambda cls, context, result: audits.append((context, result))))
    monkeypatch.setattr(registry_module, "dry_run_enabled", lambda: False)
    monkeypatch.setattr(registry_module, "execution_allowed", lambda: True)
    monkeypatch.setattr(execution_guard_module, "execution_allowed", lambda: True)
    real_answer = helper.answer_query
    monkeypatch.setattr(helper, "answer_query", lambda query, provider, **kw:
                        real_answer(query, provider, clock=clock, sleep=clock.sleep, **kw))
    def invoke(text):
        before = len(audits)
        result = registry.dispatch("system_status", "", source="rule", user_text=text)
        assert len(audits) == before + 1
        context, logged = audits[-1]
        assert context.user_text == text and context.source == "rule" and logged is result
        assert isinstance(context, models.ActionContext)
        if result.success:
            assert result.verification.gate_name == "NO_POSTCONDITION"
            assert result.verification.verified is False
        speech = turns.spoken_result("system_status", {"reply": "正在检测整机负载。"}, result.to_dict(), "rule")
        return result, speech
    return NS(invoke=invoke, port=port, processes=processes, calls=calls, clock=clock, audits=audits)


@pytest.mark.parametrize("text,expected", [("CPU温度多少度", "62.5"), ("显卡温度高不高", "53.0"),
    ("哪个进程最占内存", "1024"), ("哪个程序最占CPU", "80"), ("E盘还剩多少空间", "64.0")])
def test_explicit_request_reaches_actual_short_speech(actor, text, expected):
    result, speech = actor.invoke(text)
    assert result.success and expected in speech
    if "盘" in text:
        assert actor.calls == ["E:\\"] and result.detail["disks_free_gb"] == {"E": 64.0}
    elif "温度" in text:
        assert "最高温" in speech and "内存" not in speech
    else:
        assert result.detail["top_process"]["pid"] == 22 and "browser" in speech


@pytest.mark.parametrize("text", ["查一下硬盘还剩多少容亮", "硬盘还剩多少空间"])
def test_normal_disk_contract_stays_intact(actor, text):
    result, speech = actor.invoke(text)
    assert result.success and "C盘余128.5G" in speech and "D盘余256.8G" in speech
    assert actor.calls == ["C:\\", "D:\\"]


@pytest.mark.parametrize("text", ["看看CPU占用", "查询电脑状态", "看看CPU占用，别查E盘剩余空间"])
def test_normal_summary_not_hijacked_by_negative_clause(actor, text):
    result, speech = actor.invoke(text)
    assert result.success and "12.3" in speech and "45.6" in speech
    assert actor.calls == ["C:\\", "D:\\"]


@pytest.mark.parametrize("query", ["CPU温度多少度", "哪个进程最占内存", "E盘剩余空间"])
def test_cancelled_before_provider_read(query):
    class NoReads:
        def __getattr__(self, key):
            pytest.fail("provider touched after cancellation: " + key)
    answer = helper.answer_query(helper.query_kind(query), NoReads(), should_stop=lambda: True)
    assert not answer.success and answer.error_code == "STATUS_CANCELLED"


def ask(text, port=None, clock=None, **kw):
    clock = clock or Clock()
    if port is None:
        port = provider(clock)[0]
    return helper.answer_query(helper.query_kind(text), port, clock=clock, sleep=clock.sleep, **kw)


def test_missing_windows_temperature_source_is_explicit_unavailable():
    answer = ask("CPU温度", NS())
    assert not answer.success and answer.error_code == "STATUS_UNAVAILABLE"
    assert "CPU温度" in answer.speech and "无法读取" in answer.speech


def test_unrelated_temperature_does_not_answer_cpu_or_gpu():
    port = NS(sensors_temperatures=lambda: {"nvme": [NS(current=35.0, label="Composite")],
                                          "acpitz": [NS(current=42.0, label="") ]})
    assert not ask("CPU温度", port).success
    assert not ask("显卡温度", port).success


def test_invalid_temperature_ignored_and_gpu_is_not_cpu():
    port = NS(sensors_temperatures=lambda: {"coretemp": [NS(current=float("nan"), label="Core 0")],
                                          "nvidia": [NS(current=54.0, label="GPU")]})
    assert not ask("CPU温度", port).success
    assert ask("显卡温度", port).detail["temperature_c"] == 54.0


def test_memory_permission_denied_is_partial_not_global_first():
    clock = Clock()
    port, processes, _ = provider(clock)
    processes[0].deny = True
    answer = ask("哪个进程最占内存", port, clock)
    assert answer.success and answer.detail["skipped"] == 1 and not answer.detail["complete"]
    assert "可读中" in answer.speech and "可读进程中" in answer.message


def test_cpu_has_real_sampling_interval_and_per_cpu_basis():
    clock = Clock()
    answer = ask("哪个进程最占CPU", clock=clock)
    assert answer.success and clock.now >= 0.15
    assert answer.detail["top_process"]["cpu_percent"] == pytest.approx(80)
    assert "单核" in answer.speech


def test_cpu_all_zero_does_not_arbitrarily_name_top():
    clock = Clock()
    port, processes, _ = provider(clock)
    for process in processes:
        process.rate = 0
    answer = ask("哪个进程最占CPU", port, clock)
    assert answer.success and answer.detail["all_sampled_cpu_zero"]
    assert "top_process" not in answer.detail and "browser" not in answer.speech


@pytest.mark.parametrize("text", ["哪个进程最占CPU", "哪个进程最占内存"])
def test_pid_reuse_never_attributes_new_process_reading(text):
    clock = Clock()
    port, processes, _ = provider(clock)
    processes[1].reuse = True
    answer = ask(text, port, clock)
    assert answer.success and answer.detail["top_process"]["pid"] == 11
    assert not answer.detail["complete"] and answer.detail["skipped"] == 1


def test_process_budget_labels_limited_scope():
    answer = ask("哪个进程最占内存", max_processes=1)
    assert answer.success and not answer.detail["complete"] and answer.detail["examined"] == 1
    assert answer.detail["top_process"]["pid"] == 11 and "可读中" in answer.speech


def test_cancellation_during_sampling_never_produces_rank():
    clock = Clock()
    answer = ask("哪个进程最占CPU", clock=clock, should_stop=lambda: clock.now >= 0.04)
    assert not answer.success and answer.error_code == "STATUS_CANCELLED" and clock.now < 0.15
    assert "top_process" not in answer.detail


def test_blocked_provider_return_observes_deadline():
    clock = Clock()
    port, _, _ = provider(clock)
    def late(_drive):
        clock.now = 3.0
        return NS(free=123)
    port.disk_usage = late
    answer = ask("E盘剩余空间", port, clock)
    assert not answer.success and answer.error_code == "STATUS_TIMEOUT"
    assert "disks_free_gb" not in answer.detail


def test_process_exits_between_cpu_samples_is_partial():
    clock = Clock()
    port, processes, _ = provider(clock)
    def times():
        if clock.now:
            raise Gone()
        return NS(user=1.0, system=0.0)
    processes[1].cpu_times = times
    answer = ask("哪个进程最占CPU", port, clock)
    assert answer.success and answer.detail["top_process"]["pid"] == 11 and answer.detail["skipped"] == 1


def test_process_names_do_not_push_measurement_out_of_short_speech():
    clock = Clock()
    port, processes, _ = provider(clock)
    processes[1].label = "非常非常非常非常非常非常长的进程名字.exe\n下一行"
    answer = ask("哪个进程最占CPU", port, clock)
    speech = turns.spoken_result("system_status", {}, NS(success=True, to_dict=None).__dict__ | {"speech_override": answer.speech}, "rule")
    assert "80%" in speech and "单核" in speech and "\n" not in answer.detail["top_process"]["name"]


def test_process_name_punctuation_cannot_end_speech_before_reading():
    clock = Clock()
    port, processes, _ = provider(clock)
    processes[1].label = "browser!foo.exe"
    answer = ask("哪个进程最占CPU", port, clock)
    speech = turns.spoken_result("system_status", {}, {"success": True, "speech_override": answer.speech}, "rule")
    assert "80%" in speech and "单核" in speech


def test_nonfinite_initial_clock_fails_before_provider_read():
    port = NS(disk_usage=lambda drive: pytest.fail("invalid clock must reject before IO"))
    answer = helper.answer_query(helper.query_kind("E盘剩余空间"), port, clock=lambda: float("nan"))
    assert not answer.success and answer.error_code == "STATUS_INCOMPLETE"


def test_temperature_access_denied_is_unavailable():
    def unavailable():
        raise Denied()
    answer = ask("CPU温度", NS(sensors_temperatures=unavailable, AccessDenied=Denied))
    assert not answer.success and answer.error_code == "STATUS_UNAVAILABLE"


@pytest.mark.parametrize("text", ["别查CPU温度", "他说哪个进程最占CPU", "如果E盘还剩多少空间", "看看CPU占用，别查E盘剩余空间"])
def test_query_selector_does_not_promote_nondirect_clause(text):
    assert helper.query_kind(text) is None


@pytest.mark.parametrize("text", [
    base + tail
    for base in ("哪个程序最占内存", "哪个进程现在最占CPU", "CPU温度多少度")
    for tail in ("呢", "啊", "呀", "呗", "嘛")
] + ["哪些程序比较占CPU", "哪个程序目前最占内存", "哪个进程正在占CPU",
     "帮我查看哪个程序最占内存呢", "最占内存的程序是哪个呢", "哪个进程占CPU最多"])
def test_routed_positive_suffix_and_modifier_query_is_answered(actor, text):
    import canaan  # The canonical path bootstrap; no main/service launch.
    from canaan_intent import fast_match_intent, apply_command_guard
    decision = fast_match_intent(text)
    assert decision and decision["action"] == "system_status", decision
    decision = apply_command_guard(text, decision)
    assert decision and decision["action"] == "system_status", decision
    result, speech = actor.invoke(text)
    assert result.success
    if "温度" in text:
        assert result.detail.get("temperature_c") == 62.5 and "62.5" in speech
    else:
        assert result.detail.get("top_process", {}).get("pid") == 22 and "browser" in speech


@pytest.mark.parametrize("text", ["他说哪个程序最占内存呢", "如果哪个进程现在最占CPU呢",
    "别查哪个程序最占内存呗", "看看CPU占用，别查哪个程序最占内存呢"])
def test_suffix_handling_keeps_complete_positive_clause_boundary(text):
    assert helper.query_kind(text) is None


@pytest.mark.parametrize("resource,expected", [("CPU", [22, 33, 44]), ("内存", [33, 22, 44])])
def test_plural_process_query_returns_actual_top_three_and_spoken_count(actor, resource, expected):
    import canaan
    from canaan_intent import fast_match_intent, apply_command_guard
    actor.processes.extend([
        Process(actor.clock, pid=33, name="worker.exe", rss=2048 * 1024 ** 2, rate=0.6),
        Process(actor.clock, pid=44, name="service.exe", rss=750 * 1024 ** 2, rate=0.4),
    ])
    text = "哪些进程最占" + resource
    decision = fast_match_intent(text)
    assert decision and decision["action"] == "system_status"
    assert apply_command_guard(text, decision)["action"] == "system_status"
    selected = helper.query_kind(text)
    assert selected[1] == ("plural",)
    result, speech = actor.invoke(text)
    assert result.success and [row["pid"] for row in result.detail["top_processes"]] == expected
    assert result.detail["returned_count"] == 3 and result.detail["readable"] == 4
    assert result.detail["top_process"]["pid"] == expected[0]
    assert "列3项" in speech and "前3项" in result.message and "最多展示3项" in result.message
    for name in ("browser.exe", "worker.exe", "service.exe"):
        assert name in result.message


def test_plural_cpu_does_not_fill_heavy_process_list_with_zero_samples(actor):
    actor.processes[0].rate = 0
    result, speech = actor.invoke("哪些进程最占CPU")
    assert result.success and [row["pid"] for row in result.detail["top_processes"]] == [22]
    assert result.detail["returned_count"] == 1 and result.detail["positive_cpu_processes"] == 1
    assert "列1项" in speech and "editor.exe" not in result.message and "仅列正值" in result.message
