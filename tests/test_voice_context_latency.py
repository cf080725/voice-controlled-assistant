"""Optional agent status must not block a spoken response."""
import threading
import time

import pytest

from canaan_agent_coordinator import OpenMuseProvider, OpenDotsProvider
from canaan_companion import build_context


@pytest.fixture
def providers(monkeypatch):
    for provider in (OpenMuseProvider, OpenDotsProvider):
        monkeypatch.setattr(provider, "_cached_context", None)
        monkeypatch.setattr(provider, "_cached_time", 0)
        monkeypatch.setattr(provider, "_refreshing", False, raising=False)
    return OpenMuseProvider, OpenDotsProvider


def test_slow_agent_status_does_not_delay_voice_context(providers, monkeypatch):
    release, finished = threading.Event(), threading.Event()
    entered = [threading.Event(), threading.Event()]
    completed = [threading.Event(), threading.Event()]
    result = []

    def slow(index):
        def fetch(cls, force=False):
            entered[index].set()
            try:
                release.wait(2)
                cls._cached_context = {"online": True, "running_tasks": ["fresh task"]}
                cls._cached_time = time.time()
                return cls._cached_context
            finally:
                completed[index].set()
        return classmethod(fetch)

    for i, provider in enumerate(providers):
        monkeypatch.setattr(provider, "get_context", slow(i))

    def run():
        result.append(build_context(fg_app="code"))
        finished.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert finished.wait(.3), "Voice context synchronously waited for optional agents"
        assert all(event.wait(1) for event in entered)
        assert "fresh task" not in result[0]
    finally:
        release.set()
        worker.join(3)
        for event in completed:
            event.wait(1)
    assert all(event.is_set() for event in completed)
    assert "fresh task" in build_context(fg_app="code"), "The next turn lost the refreshed agent status"


def test_fresh_agent_snapshot_is_used_without_network(providers, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Fresh snapshot triggered a network request")

    for i, provider in enumerate(providers):
        monkeypatch.setattr(provider, "get_context", unexpected)
        provider._cached_context = {"online": True, "running_tasks": [f"synthetic-task-{i}"]}
        provider._cached_time = time.time()
    context = build_context(fg_app="code")
    assert "synthetic-task-0" in context and "synthetic-task-1" in context


def test_stale_snapshot_is_omitted_and_only_one_refresh_runs(providers, monkeypatch):
    provider = providers[0]
    release, started, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def fetch(cls, force=False):
        calls.append(1)
        started.set()
        try:
            release.wait(2)
            return {}
        finally:
            finished.set()

    monkeypatch.setattr(provider, "get_context", classmethod(fetch))
    provider._cached_context = {"online": True, "running_tasks": ["outdated task"]}
    provider._cached_time = time.time() - provider._ttl - 1
    try:
        assert provider.get_context_cached() == {}
        assert started.wait(1)
        for _ in range(10):
            assert provider.get_context_cached() == {}
        assert calls == [1]
    finally:
        release.set()
        finished.wait(1)
