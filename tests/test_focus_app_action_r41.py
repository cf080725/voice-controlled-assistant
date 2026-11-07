"""Real focus action and native adapter with entirely synthetic window metadata."""
import copy
import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest

import canaan_os_executor as executor
from canaan_action_framework import ActionRegistry
from canaan_action_framework.actions import app_actions
from canaan_action_framework.models import ActionContext, ActionResult


def window(hwnd=101, pid=11, exe="chrome.exe", created=1.0, children=()):
    return {"hwnd": hwnd, "pid": pid, "created": created,
            "processes": tuple(sorted(((pid, exe, created), *children)))}


class FakeBackend:
    def __init__(self, windows=(), foreground=0):
        self.windows = {w["hwnd"]: copy.deepcopy(w) for w in windows}
        self.current = foreground
        self.calls = []
        self.on_activate = None
        self.on_describe = None
        self.foregrounds = []

    def list_windows(self):
        return copy.deepcopy(list(self.windows.values()))

    def foreground_root(self):
        return self.foregrounds.pop(0) if self.foregrounds else self.current

    def describe(self, hwnd):
        if self.on_describe:
            self.on_describe(self, hwnd)
        return copy.deepcopy(self.windows.get(hwnd))

    def activate(self, target):
        self.calls.append(target["hwnd"])
        if self.on_activate:
            return self.on_activate(self, target)
        self.current = target["hwnd"]
        return True


@pytest.fixture
def focus_fixture(monkeypatch):
    backend = FakeBackend([window()])
    monkeypatch.setattr(app_actions, "_app_window_backend", lambda: backend, raising=False)
    monkeypatch.setattr(app_actions, "time", SimpleNamespace(sleep=lambda _: None), raising=False)
    monkeypatch.delenv("CANAAN_DRY_RUN", raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("Real native/window/launch access is forbidden")
    monkeypatch.setattr(ctypes, "WinDLL", forbidden)
    monkeypatch.setattr(app_actions.os, "startfile", forbidden, raising=False)
    monkeypatch.setattr(app_actions.webbrowser, "open", forbidden)
    monkeypatch.setattr(executor.CanaanOSExecutor, "force_foreground_window", forbidden)
    return backend


def focus(param="chrome", name="focus_app"):
    return ActionRegistry.dispatch(name, param, source="rule", user_text=f"把{param}窗口置前")


@pytest.mark.parametrize("alias", ["focus_app", " FOCUS_APPLICATION ", "bring_app_forward", "activate_app"])
def test_focus_dispatch_verifies_actual_foreground_for_registered_alias(focus_fixture, alias):
    result = focus(name=alias)
    assert result.success and result.verification.verified
    assert result.verification.gate_name == "APP_FOREGROUND"
    assert focus_fixture.calls == [101] and focus_fixture.current == 101


def test_same_words_in_another_process_title_are_not_an_app_identity(focus_fixture):
    focus_fixture.windows = {202: {**window(202, 22, "notepad.exe"), "title": "chrome.exe instructions.txt"}}
    result = focus()
    assert not result.success and result.error_code == "WINDOW_NOT_FOUND"
    assert not focus_fixture.calls


def test_missing_existing_app_is_not_launched(focus_fixture):
    focus_fixture.windows.clear()
    result = focus()
    assert not result.success and result.error_code == "WINDOW_NOT_FOUND"
    assert not focus_fixture.calls


@pytest.mark.parametrize("target", ["", "那个窗口", "编辑器", "C:\\Programs\\chrome.exe", "chrome;notepad"])
def test_unresolved_or_path_like_targets_do_not_query_or_focus(focus_fixture, monkeypatch, target):
    monkeypatch.setattr(app_actions, "_app_window_backend", lambda: pytest.fail("Invalid target reached native backend"))
    result = focus(target)
    assert not result.success and result.error_code == "INVALID_TARGET"


@pytest.mark.parametrize("reported", [False, True])
def test_api_return_value_cannot_replace_actual_foreground_proof(focus_fixture, reported):
    focus_fixture.on_activate = lambda backend, target: reported
    result = focus()
    assert not result.success and result.error_code == "FOCUS_NOT_VERIFIED"
    assert not result.speech_override or "已置前" not in result.speech_override


def test_actual_foreground_can_confirm_success_despite_zero_api_return(focus_fixture):
    def changed(backend, target):
        backend.current = target["hwnd"]
        return False
    focus_fixture.on_activate = changed
    result = focus()
    assert result.success and result.verification.verified


def test_multiple_existing_windows_require_a_choice(focus_fixture):
    focus_fixture.windows[202] = window(202, 22)
    result = focus()
    assert not result.success and result.error_code == "AMBIGUOUS_WINDOW"
    assert not focus_fixture.calls


def test_already_foreground_matching_window_is_selected_without_activation(focus_fixture):
    focus_fixture.windows[202] = window(202, 22)
    focus_fixture.current = 202
    result = focus()
    assert result.success and result.verification.verified and not focus_fixture.calls


def test_generic_browser_does_not_choose_between_chrome_and_edge(focus_fixture):
    focus_fixture.windows[202] = window(202, 22, "msedge.exe")
    result = focus("浏览器")
    assert not result.success and result.error_code == "AMBIGUOUS_WINDOW"
    assert not focus_fixture.calls


@pytest.mark.parametrize("target,exe", [("Word", "wps.exe"), ("Excel", "et.exe"), ("PowerPoint", "wpp.exe")])
def test_office_launch_fallbacks_are_not_focus_identity_aliases(focus_fixture, target, exe):
    focus_fixture.windows = {101: window(exe=exe)}
    result = focus(target)
    assert not result.success and result.error_code == "WINDOW_NOT_FOUND"
    assert not focus_fixture.calls


@pytest.mark.parametrize("target,other", [("wps.exe", "et.exe"), ("et.exe", "wps.exe"), ("wpp.exe", "wps.exe")])
def test_explicit_process_basename_does_not_expand_to_a_brand_group(focus_fixture, target, other):
    focus_fixture.windows = {101: window(exe=other)}
    result = focus(target)
    assert not result.success and result.error_code == "WINDOW_NOT_FOUND"
    assert not focus_fixture.calls


def test_hosted_calculator_uses_actual_child_process_identity(focus_fixture):
    focus_fixture.windows = {101: window(exe="applicationframehost.exe", children=((21, "calculatorapp.exe", 2.0),))}
    result = focus("计算器")
    assert result.success and result.verification.verified


def test_reused_handle_before_activation_never_focuses_the_replacement(focus_fixture):
    def replaced(backend, hwnd):
        backend.windows[hwnd] = window(hwnd, 99, created=9.0)
    focus_fixture.on_describe = replaced
    result = focus()
    assert not result.success and result.error_code == "WINDOW_CHANGED"
    assert not focus_fixture.calls


def test_reused_pid_with_new_creation_time_is_not_the_original_window(focus_fixture):
    def replaced(backend, hwnd):
        backend.windows[hwnd] = window(hwnd, 11, created=9.0)
    focus_fixture.on_describe = replaced
    result = focus()
    assert not result.success and result.error_code == "WINDOW_CHANGED"
    assert not focus_fixture.calls


def test_post_execution_verification_rechecks_foreground_and_stops_tail(focus_fixture, monkeypatch):
    focus_fixture.foregrounds = [0, 101, 0]
    shots = []
    screenshot = ActionRegistry.get_action("screenshot")
    monkeypatch.setattr(screenshot, "execute", lambda context: shots.append(True) or ActionResult(success=True, action="screenshot"))
    result = executor.CanaanOSExecutor.execute("multi_action", [
        {"action": "focus_app", "param": "chrome"}, {"action": "screenshot", "param": ""}], source="rule")
    assert not result["success"] and result["error_code"] == "FOCUS_NOT_VERIFIED"
    assert result["failed_action"] == "focus_app" and result["skipped_steps"] == 1
    assert not shots


def test_process_identity_is_rechecked_after_a_reported_focus(focus_fixture):
    def wrong_process(backend, target):
        backend.current = target["hwnd"]
        backend.windows[target["hwnd"]] = window(target["hwnd"], 99, "notepad.exe", 9.0)
        return True
    focus_fixture.on_activate = wrong_process
    result = focus()
    assert not result.success and result.error_code in ("WINDOW_CHANGED", "FOCUS_NOT_VERIFIED")


class FakeUser32:
    def __init__(self, *, minimized=False, maximized=False, frame=False):
        self.hwnd = 2 ** 40 + 17
        self.child = self.hwnd + 1
        self.popup = self.hwnd + 2
        self.current = self.hwnd
        self.minimized, self.maximized = minimized, maximized
        self.calls = []
        self.frame = frame
        self.popup_pid = 11
        self.pids = {self.hwnd: 11, self.child: 22, self.popup: 11}
        self.names = {11: "applicationframehost.exe" if frame else "chrome.exe", 22: "calculatorapp.exe", 33: "notepad.exe"}
        self.failed_pid = False
        self.denied = False
        self.shell_hwnd = 0
        self.desktop_hwnd = 0
        self.class_name = "Chrome_WidgetWin_1"
        self.ex_style = 0
        self.hidden = set()
        self.child_root = self.hwnd
        self.times = {pid: float(pid) for pid in self.names}
        self.IsWindow = lambda h: h in self.pids
        self.IsWindowVisible = lambda h: h in self.pids and h not in self.hidden
        self.GetAncestor = lambda h, mode: (self.child_root if h == self.child else self.hwnd) if h in self.pids else 0
        self.GetForegroundWindow = lambda: self.current
        self.GetShellWindow = lambda: self.shell_hwnd
        self.GetDesktopWindow = lambda: self.desktop_hwnd
        def class_name(hwnd, output, size):
            output.value = self.class_name[:size - 1]
            return len(output.value)
        self.GetClassNameW = class_name
        self.GetWindowLongPtrW = lambda hwnd, index: self.ex_style
        self.GetWindowLongW = lambda hwnd, index: self.ex_style
        self.IsIconic = lambda h: self.minimized
        def restore(hwnd, flag):
            self.calls.append(("restore", hwnd, flag))
            self.minimized = False
            return False
        self.ShowWindow = restore
        self.SetForegroundWindow = lambda h: self.calls.append(("focus", h)) or True
        self.GetWindowThreadProcessId = self._pid_function()
        self.EnumWindows = lambda callback, unused: callback(self.hwnd, 0)
        self.EnumChildWindows = lambda root, callback, unused: (callback(self.child, 0) if self.frame else 0) and 0

    def _pid_function(self):
        def get_pid(hwnd, output):
            if self.failed_pid or hwnd not in self.pids:
                return 0
            ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = self.pids[hwnd]
            return 7
        return get_pid

    def process(self, pid):
        import psutil
        if self.denied:
            raise psutil.AccessDenied(pid)
        return SimpleNamespace(name=lambda: self.names[pid], create_time=lambda: self.times[pid])


def native(fake):
    backend_class = getattr(app_actions, "_AppWindowBackend", None)
    assert backend_class is not None, "Native focus adapter is not implemented"
    return backend_class(user32=fake, process_factory=fake.process)


def test_native_signatures_preserve_64_bit_handles_and_fresh_pid_output(focus_fixture):
    fake = FakeUser32()
    backend = native(fake)
    record = backend.list_windows()[0]
    assert record["hwnd"] == fake.hwnd and record["pid"] == 11
    assert fake.GetForegroundWindow.restype == wintypes.HWND
    assert fake.SetForegroundWindow.argtypes == [wintypes.HWND]
    assert fake.GetWindowThreadProcessId.argtypes == [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    fake.failed_pid = True
    assert backend.describe(fake.hwnd) is None


@pytest.mark.parametrize("minimized,maximized", [(True, False), (False, True), (False, False)])
def test_native_restores_only_minimized_windows_and_keeps_maximized_state(focus_fixture, minimized, maximized):
    fake = FakeUser32(minimized=minimized, maximized=maximized)
    backend = native(fake)
    assert backend.activate(backend.describe(fake.hwnd))
    assert ("restore", fake.hwnd, 9) in fake.calls if minimized else not any(c[0] == "restore" for c in fake.calls)
    assert ("focus", fake.hwnd) in fake.calls and fake.maximized == maximized


def test_native_framehost_child_identity_does_not_depend_on_enum_child_return_value(focus_fixture):
    fake = FakeUser32(frame=True)
    record = native(fake).describe(fake.hwnd)
    assert (22, "calculatorapp.exe", 22.0) in record["processes"]


def test_hidden_hosted_app_is_not_a_visible_app_target(focus_fixture, monkeypatch):
    fake = FakeUser32(frame=True)
    fake.hidden.add(fake.child)
    backend = native(fake)
    monkeypatch.setattr(app_actions, "_app_window_backend", lambda: backend)
    result = focus("计算器")
    assert not result.success and result.error_code == "WINDOW_NOT_FOUND"
    assert not fake.calls


@pytest.mark.parametrize("change", ["hidden", "new_pid", "new_birth", "reparented"])
def test_hosted_app_identity_is_reproved_after_execution(focus_fixture, monkeypatch, change):
    fake = FakeUser32(frame=True)
    backend = native(fake)
    monkeypatch.setattr(app_actions, "_app_window_backend", lambda: backend)
    action = ActionRegistry.get_action("focus_app")
    context = ActionContext(action_name="focus_app", params={"value": "计算器"}, source="rule")
    assert action.execute(context).success
    if change == "hidden":
        fake.hidden.add(fake.child)
    elif change == "new_pid":
        fake.pids[fake.child] = 33
    elif change == "new_birth":
        fake.times[22] = 99.0
    else:
        fake.child_root = fake.hwnd + 999
    verification = action.verify(context)
    assert not verification.passed and not verification.verified
    assert verification.error_code == "FOCUS_NOT_VERIFIED"


def test_native_owned_dialog_matches_only_same_application_process(focus_fixture):
    fake = FakeUser32()
    backend = native(fake)
    fake.current = fake.popup
    assert backend.foreground_root() == fake.hwnd
    fake.pids[fake.popup] = 33
    assert backend.foreground_root() == 0


def test_native_access_denied_never_falls_back_to_a_title_match(focus_fixture):
    fake = FakeUser32()
    fake.denied = True
    assert native(fake).list_windows() == []


@pytest.mark.parametrize("class_name", ["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"])
def test_native_explorer_shell_classes_are_not_application_windows(focus_fixture, class_name):
    fake = FakeUser32()
    fake.names[11] = "explorer.exe"
    fake.class_name = class_name
    assert native(fake).list_windows() == []


@pytest.mark.parametrize("kind", ["shell_hwnd", "desktop_hwnd"])
def test_native_shell_and_desktop_handles_are_not_focus_candidates(focus_fixture, kind):
    fake = FakeUser32()
    setattr(fake, kind, fake.hwnd)
    assert native(fake).list_windows() == []


@pytest.mark.parametrize("style", [0x80, 0x08000000])
def test_native_tool_or_noactivate_windows_are_not_focus_candidates(focus_fixture, style):
    fake = FakeUser32()
    fake.ex_style = style
    assert native(fake).list_windows() == []


def test_a_still_minimized_window_is_not_a_verified_foreground(focus_fixture):
    fake = FakeUser32(minimized=True)
    backend = native(fake)
    assert backend.foreground_root() == 0


def test_native_32_bit_style_function_is_a_supported_fallback(focus_fixture):
    fake = FakeUser32()
    del fake.GetWindowLongPtrW
    assert native(fake).list_windows()[0]["hwnd"] == fake.hwnd
