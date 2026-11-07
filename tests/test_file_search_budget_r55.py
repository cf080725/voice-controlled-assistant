"""Candidate SearchFileAction through real Registry, temporary trees only."""
from pathlib import Path
from types import SimpleNamespace
import os
import threading

import pytest
import canaan_os_executor as executor
from canaan_action_framework import ActionRegistry
from canaan_action_framework.actions import app_actions as apps
from canaan_execution_guard import execution_guard


@pytest.fixture
def env(tmp_path, monkeypatch):
    opened, walked = [], []
    original_exists, original_isfile, original_isdir = os.path.exists, os.path.isfile, os.path.isdir
    original_walk = os.walk
    def allowed(path):
        return Path(path).resolve().is_relative_to(tmp_path)
    monkeypatch.setattr(os.path, "exists", lambda path: allowed(path) and original_exists(path))
    monkeypatch.setattr(os.path, "isfile", lambda path: allowed(path) and original_isfile(path))
    monkeypatch.setattr(os.path, "isdir", lambda path: allowed(path) and original_isdir(path))
    def walk(path, *args, **kwargs):
        assert allowed(path), "Real disk traversal forbidden"
        walked.append(str(path))
        return original_walk(path, *args, **kwargs)
    monkeypatch.setattr(os, "walk", walk)
    strict_root_exists = None
    try:
        import canaan_file_search
    except ImportError:
        pass  # The first red run intentionally uses the unmodified candidate.
    else:
        strict_root_exists = canaan_file_search._root_exists
        monkeypatch.setattr(canaan_file_search, "_root_exists", lambda path: allowed(path) and original_exists(path))
    monkeypatch.setattr(os, "startfile", opened.append)
    monkeypatch.setattr(executor, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(executor, "FISHSIM_DIR", str(tmp_path))
    monkeypatch.setattr(apps, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(apps, "FISHSIM_DIR", str(tmp_path))
    monkeypatch.setattr(apps, "_user_folder_path", lambda _: str(tmp_path))
    action = apps.SearchFileAction()
    monkeypatch.setattr(ActionRegistry, "_actions", {"search_file": action})
    monkeypatch.setattr(ActionRegistry, "_alias_map", {"search_file": "search_file", "open_file": "search_file"})
    monkeypatch.setattr(ActionRegistry, "_audit_log", lambda *args: None)
    monkeypatch.delenv("CANAAN_DRY_RUN", raising=False)
    return SimpleNamespace(root=tmp_path, opened=opened, walked=walked, strict_root_exists=strict_root_exists)


def dispatch(query, **kwargs):
    return ActionRegistry.dispatch("search_file", query, source="rule", **kwargs)


def test_unscoped_multiple_candidates_do_not_open_walk_first(env):
    (env.root / "report-one.txt").touch()
    (env.root / "report-two.txt").touch()
    result = dispatch("report-")
    assert not result.success and result.error_code == "AMBIGUOUS_TARGET"
    assert env.opened == []


def test_revocation_during_scan_opens_nothing_even_when_registry_revokes(env, monkeypatch):
    stopped = threading.Event()
    target = env.root / "report.txt"
    target.touch()
    def walk(path, *args, **kwargs):
        yield str(env.root), ["next"], ["unrelated.txt"]
        stopped.set()
        yield str(env.root), [], [target.name]
    monkeypatch.setattr(os, "walk", walk)
    with execution_guard(lambda: not stopped.is_set()):
        result = dispatch("report")
    assert result.error_code == "EXECUTION_REVOKED"
    assert env.opened == []


def test_step_callback_revocation_before_open_does_not_open_direct_file(env):
    stopped = threading.Event()
    target = env.root / "direct.txt"
    target.touch()
    def step(number, total, description):
        if number == 2:
            stopped.set()
    with execution_guard(lambda: not stopped.is_set()):
        result = dispatch(str(target), on_step=step)
    assert result.error_code == "EXECUTION_REVOKED"
    assert env.opened == []


def test_late_walk_yield_is_timeout_and_partial_match_never_opens(env, monkeypatch):
    now = [0.0]
    target = env.root / "report.txt"
    target.touch()
    def walk(path, *args, **kwargs):
        now[0] = 3.0
        yield str(env.root), [], [target.name]
    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(executor.time, "monotonic", lambda: now[0])
    result = dispatch("report")
    assert not result.success and result.error_code == "SEARCH_TIMEOUT"
    assert env.opened == []


def test_complete_unique_candidate_opens_once_with_existing_unverified_receipt(env):
    target = env.root / "report.txt"
    target.touch()
    result = dispatch("report")
    assert result.success and env.opened == [str(target)]
    assert result.verification.gate_name == "NO_POSTCONDITION" and not result.verification.verified


def test_complete_empty_search_keeps_file_not_found(env):
    result = dispatch("no-such-file")
    assert not result.success and result.error_code == "FILE_NOT_FOUND"
    assert env.opened == []


def test_partial_candidate_before_timeout_is_not_opened(env, monkeypatch):
    now = [0.0]
    def walk(path, *args, **kwargs):
        yield str(env.root), [], ["report.txt"]
        now[0] = 3.0
        yield str(env.root), [], []
    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(executor.time, "monotonic", lambda: now[0])
    result = dispatch("report")
    assert not result.success and result.error_code == "SEARCH_TIMEOUT" and env.opened == []


def test_visible_walk_error_with_partial_candidate_is_incomplete(env, monkeypatch):
    def walk(path, *, onerror):
        yield str(env.root), [], ["report.txt"]
        onerror(PermissionError("fixture only"))
    monkeypatch.setattr(os, "walk", walk)
    result = dispatch("report")
    assert not result.success and result.error_code == "SEARCH_INCOMPLETE" and env.opened == []


def test_inaccessible_root_is_not_silently_treated_as_complete(env, monkeypatch):
    from canaan_file_search import search_files
    denied = env.root / "denied"
    denied.mkdir()
    actual_stat = os.stat
    def stat(path, *args, **kwargs):
        if str(path) == str(denied):
            raise PermissionError("fixture metadata denial")
        return actual_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", stat)
    result = search_files("report", [str(denied)], exists=env.strict_root_exists)
    assert result.status == "incomplete" and result.reason == "io_error" and not result.paths


def test_stop_check_exception_fails_closed_before_metadata(env):
    from canaan_file_search import search_files
    def failure():
        raise RuntimeError("fixture stop failure")
    result = search_files("report", [str(env.root)], should_stop=failure,
                          exists=lambda _: pytest.fail("Stopped search must not probe metadata"))
    assert result.status == "cancelled" and result.reason == "stop_check_failed" and not result.paths


@pytest.mark.parametrize("budget,reason", [({"max_directories": 1}, "directory_budget"),
                                          ({"max_files": 1}, "file_budget")])
def test_node_budget_returns_partial_state(env, budget, reason):
    from canaan_file_search import search_files
    def walk(*args, **kwargs):
        yield str(env.root), [], ["report-one.txt", "other.txt"]
        yield str(env.root / "child"), [], ["report-two.txt"]
    result = search_files("report", [str(env.root)], limit=3, walk=walk, **budget)
    assert result.status == "incomplete" and result.reason == reason
    assert result.paths == (str(env.root / "report-one.txt"),)


def test_match_limit_is_not_claimed_complete(env):
    from canaan_file_search import search_files
    def walk(*args, **kwargs):
        yield str(env.root), [], ["report-one.txt", "report-two.txt", "report-three.txt"]
    result = search_files("report", [str(env.root)], limit=2, walk=walk)
    assert result.status == "incomplete" and result.reason == "match_limit" and len(result.paths) == 2


def test_pre_cancelled_helper_avoids_metadata(env):
    from canaan_file_search import search_files
    result = search_files("report", [str(env.root)], should_stop=lambda: True,
                          exists=lambda _: pytest.fail("Cancelled search must not probe metadata"))
    assert result.status == "cancelled" and result.paths == ()


def test_existing_relative_file_direct_path_is_preserved(env, monkeypatch):
    monkeypatch.chdir(env.root)
    (env.root / "direct.txt").touch()
    monkeypatch.setattr(apps, "_executor", lambda: pytest.fail("Existing relative file must not recurse"))
    result = dispatch("direct.txt")
    assert result.success and env.opened == ["direct.txt"]


def test_directory_pruning_policy_and_legacy_list_interface_are_preserved(env):
    for name in (".hidden", "node_modules", "kept"):
        folder = env.root / name
        folder.mkdir()
        (folder / "report.txt").touch()
    found = executor.CanaanOSExecutor.search_local_files("report", roots=[str(env.root)])
    assert isinstance(found, list) and found == [str(env.root / "kept" / "report.txt")]


def test_default_root_order_is_unchanged_and_budget_not_from_request(env, monkeypatch):
    import canaan_file_search
    from canaan_file_search import FileSearchResult
    captured = []
    monkeypatch.setattr(executor, "FISHSIM_DIR", str(env.root / "Fish"))
    monkeypatch.setattr(executor, "BASE_DIR", str(env.root / "Project"))
    monkeypatch.setattr(apps, "_user_folder_path", lambda kind: str(env.root / kind))
    def search(keyword, roots, **kwargs):
        captured.append((keyword, roots, kwargs))
        return FileSearchResult((), "complete", "exhausted")
    monkeypatch.setattr(canaan_file_search, "search_files", search)
    executor.CanaanOSExecutor.search_local_files_result("report")
    assert captured[0][1] == [str(env.root / "Fish"), str(env.root / "Project"),
        str(env.root / "desktop"), str(env.root / "documents"), str(env.root / "downloads"), "D:\\\\"]
    assert callable(captured[0][2]["should_stop"])


@pytest.mark.parametrize("budget", [{"timeout": 0}, {"limit": 0}, {"max_files": False}])
def test_invalid_trusted_budgets_are_rejected_before_walk(env, budget):
    from canaan_file_search import search_files
    with pytest.raises(ValueError):
        search_files("report", [str(env.root)], walk=lambda *_args, **_kwargs: pytest.fail("Invalid budget"), **budget)
