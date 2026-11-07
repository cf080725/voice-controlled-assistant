"""Actual close actor and registry; every process operation uses a fake psutil port."""
from types import SimpleNamespace

import pytest

from canaan_action_framework import ActionRegistry
from canaan_action_framework.actions import app_actions
import canaan_action_framework.action_registry as registry_module


@pytest.fixture
def process_port(monkeypatch, tmp_path):
    state = SimpleNamespace(processes=[], terminated=[], listed=[], waited=[],
                            own_pid=9000, parent_pid=9001)

    class Process:
        def __init__(self, name, pid, *, resistant=False, command=()):
            self.pid = pid
            self.info = {"name": name, "pid": pid}
            self.resistant = resistant
            self.command = list(command)
            self.finished = False

        def terminate(self):
            state.terminated.append((self.pid, self.info["name"]))
            self.finished = not self.resistant

        def cmdline(self):
            return list(self.command)

    def add(name, pid=100, **kwargs):
        process = Process(name, pid, **kwargs)
        state.processes.append(process)
        return process

    def process_iter(fields):
        state.listed.append(tuple(fields))
        return iter(state.processes)

    def process(pid):
        assert pid == state.own_pid
        return SimpleNamespace(parents=lambda: [SimpleNamespace(pid=state.parent_pid)])

    def wait_procs(processes, timeout):
        state.waited.append(([process.pid for process in processes], timeout))
        return ([process for process in processes if process.finished],
                [process for process in processes if not process.finished])

    monkeypatch.setattr(app_actions, "psutil", SimpleNamespace(
        Process=process, process_iter=process_iter, wait_procs=wait_procs))
    monkeypatch.setattr(app_actions, "os", SimpleNamespace(getpid=lambda: state.own_pid))
    monkeypatch.setattr(registry_module, "AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("CANAAN_DRY_RUN", raising=False)
    state.add = add
    return state


def close(target, *, confirmed=True, source="rule", action="close_app"):
    return ActionRegistry.dispatch(action, target, source=source, confirmed=confirmed,
                                   user_text=f"关闭{target}")


@pytest.mark.parametrize("target,other_application", [("Word", "wps.exe"), ("Excel", "et.exe")])
def test_missing_microsoft_app_never_closes_running_wps_in_its_place(process_port, target, other_application):
    f = process_port
    f.add(other_application)

    result = close(target)

    assert f.terminated == [], result.to_dict()
    assert not result.success and result.error_code == "PROCESS_NOT_FOUND"


@pytest.mark.parametrize("target,requested_exe,other_application", [
    ("Word", "winword.exe", "wps.exe"),
    ("Excel", "excel.exe", "et.exe"),
    ("winword.exe", "winword.exe", "wps.exe"),
    ("excel.exe", "excel.exe", "et.exe"),
])
def test_microsoft_name_or_exact_process_closes_only_its_own_application(
        process_port, target, requested_exe, other_application):
    f = process_port
    f.add(requested_exe.upper(), 100)
    f.add(other_application, 101)
    f.add("notepad.exe", 102)
    f.add(requested_exe + ".bak", 103)
    f.add("prefix-" + requested_exe, 104)

    result = close(target)

    assert result.success and result.detail == [100]
    assert f.terminated == [(100, requested_exe.upper())]


def test_explicit_wps_request_keeps_the_existing_suite_scope(process_port):
    f = process_port
    for name, pid in [("wps.exe", 100), ("et.exe", 101), ("wpp.exe", 102),
                      ("winword.exe", 103), ("excel.exe", 104), ("mywps.exe", 105)]:
        f.add(name, pid)

    result = close("WPS")

    assert result.success and result.detail == [100, 101, 102]
    assert f.terminated == [(100, "wps.exe"), (101, "et.exe"), (102, "wpp.exe")]


@pytest.mark.parametrize("source", ["rule", "llm"])
def test_closing_still_waits_for_confirmation_before_querying_or_terminating(process_port, source):
    f = process_port
    f.add("winword.exe", 100)
    f.add("wps.exe", 101)

    initial = close("Word", source=source, confirmed=False)

    assert not initial.success and initial.error_code == "CONFIRM_REQUIRED"
    assert not f.listed and not f.terminated and not f.waited

    confirmed = close("Word", source=source, confirmed=True)

    assert confirmed.success and confirmed.detail == [100]
    assert f.terminated == [(100, "winword.exe")]


def test_exact_application_request_keeps_self_and_parent_pids_protected(process_port):
    f = process_port
    f.add("winword.exe", f.own_pid)
    f.add("winword.exe", f.parent_pid)
    f.add("winword.exe", 100)
    f.add("wps.exe", 101)

    result = close("Word")

    assert result.success and result.detail == [100]
    assert f.terminated == [(100, "winword.exe")]


@pytest.mark.parametrize("target", ["python.exe", "explorer.exe"])
def test_protected_process_names_remain_invalid_before_process_query(process_port, target):
    f = process_port
    f.add(target)

    result = close(target)

    assert not result.success and result.error_code == "INVALID_TARGET"
    assert not f.listed and not f.terminated and not f.waited


def test_unresponsive_microsoft_process_fails_without_touching_wps(process_port):
    f = process_port
    f.add("winword.exe", 100, resistant=True)
    f.add("wps.exe", 101)

    result = close("Word")

    assert not result.success and result.error_code == "TERMINATE_PARTIAL"
    assert f.terminated == [(100, "winword.exe")]
    assert result.speech_override != "关掉了。"

