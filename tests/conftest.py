# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import builtins

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import canaan  # noqa: E402,F401  目录拆分后把 core/shells/plugins 放到搜索路径最前

# 单元测试的审计日志写到临时目录，不污染 logs/canaan_audit.jsonl
os.environ.setdefault("CANAAN_AUDIT_LOG", os.path.join(tempfile.gettempdir(), "canaan_test_audit.jsonl"))
os.environ.setdefault("CANAAN_NOTIFY_QUEUE", os.path.join(tempfile.gettempdir(), "canaan_test_notify.jsonl"))

collect_ignore = ["legacy"]

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_gpu_priority(monkeypatch, tmp_path):
    """隔离真实演示开关与交互计时；测试不读取或关闭正在演示的程序设置。"""
    try:
        import canaan_gpu_priority
    except ImportError:
        yield
        return
    monkeypatch.setattr(canaan_gpu_priority, "DEMO_FLAG", str(tmp_path / "demo_mode.flag"))
    canaan_gpu_priority._reset_for_tests()
    yield
    canaan_gpu_priority._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_pet_desktop_watchers(monkeypatch):
    """GUI defaults use memory data; board/provider tests keep their real code."""
    watchers = []
    patched_modules = set()

    def memory_agent_snapshot(pet):
        # Keep the queue and HUD consumer, but do not create the production
        # endless poller: restoring HTTP mocks cannot stop a sleeping daemon.
        muse = {"online": False, "tasks": []}
        dots = {"online": False, "tasks": []}
        pet._ui_q.put(lambda: pet._on_agents(muse, dots))

    def isolate_loaded_pet():
        for name, pet_module in tuple(sys.modules.items()):
            if name.rsplit(".", 1)[-1] != "canaan_companion_pet" or pet_module is None:
                continue
            if id(pet_module) in patched_modules:
                continue
            core = getattr(pet_module, "CanaanPetCore", None)
            if core is None:  # The module may still be importing its dependencies.
                continue
            patched_modules.add(id(pet_module))
            monkeypatch.setattr(core, "_start_agent_watch", memory_agent_snapshot)
            original = getattr(pet_module, "SenseWatcher", None)
            if original is None:
                continue

            def create(interval=1.0, foreground=None, idle_sec=None, cursor=None, procs=None, bg_music=None,
                       _original=original):
                watcher = _original(interval=interval,
                                    foreground=foreground or (lambda: ("", "")),
                                    idle_sec=idle_sec or (lambda: 999.0),
                                    cursor=cursor or (lambda: (0, 0)),
                                    procs=procs or (lambda: set()),
                                    bg_music=bg_music or (lambda names: False))
                watchers.append(watcher)
                return watcher

            monkeypatch.setattr(pet_module, "SenseWatcher", create)

    isolate_loaded_pet()
    original_import = builtins.__import__

    def import_with_pet_isolation(name, globals=None, locals=None, fromlist=(), level=0):
        result = original_import(name, globals, locals, fromlist, level)
        if name.rsplit(".", 1)[-1] == "canaan_companion_pet" or "canaan_companion_pet" in (fromlist or ()):
            isolate_loaded_pet()
        return result

    # Some GUI helpers import the pet only inside the test body.  Observe that
    # import instead of eagerly loading a desktop module for unrelated tests.
    monkeypatch.setattr(builtins, "__import__", import_with_pet_isolation)
    try:
        yield
    finally:
        for watcher in watchers:
            watcher.stop()
        for watcher in watchers:
            if watcher.is_alive():
                watcher.join(timeout=1.0)
            assert not watcher.is_alive(), "GUI test left a desktop sensor thread running"
