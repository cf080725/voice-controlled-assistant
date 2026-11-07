"""Exercise actual dispatch and PNG persistence with synthetic pixels only."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab

import canaan_intent as ci
import canaan_os_executor as executor
from canaan_action_framework import ActionRegistry
from canaan_action_framework.actions import app_actions, system_actions
from canaan_action_framework.models import ActionResult, VerificationResult


@pytest.fixture
def screen_fixture(monkeypatch, tmp_path):
    desktop = tmp_path / "redirected-desktop"
    desktop.mkdir()
    events = []
    def capture(**kwargs):
        assert kwargs == {"all_screens": True}
        events.append("screenshot")
        return Image.new("RGB", (3, 2), (12, 34, 56))
    monkeypatch.setattr(ImageGrab, "grab", capture)
    monkeypatch.setattr(app_actions, "_user_folder_path", lambda folder: str(desktop))
    monkeypatch.setattr(system_actions, "time", SimpleNamespace(strftime=lambda fmt: "20990101_120000"))
    opening = ActionRegistry.get_action("open_app")
    monkeypatch.setattr(opening, "execute", lambda context: events.append("open_app") or ActionResult(
        success=True, action="open_app", message="Synthetic launch only"))
    monkeypatch.setattr(opening, "verify", lambda context: VerificationResult(
        passed=True, verified=False, gate_name="SYNTHETIC_LAUNCH"))
    searching = ActionRegistry.get_action("web_search")
    monkeypatch.setattr(searching, "execute", lambda context: events.append("web_search") or ActionResult(
        success=True, action="web_search", message="Synthetic search only"))
    monkeypatch.setattr(app_actions.webbrowser, "open", lambda *a, **kw: pytest.fail("Real browser access is forbidden"))
    # Keep the real multi-action scheduler; avoid its deliberate pacing in these isolated tests.
    import time
    monkeypatch.setattr(executor, "time", SimpleNamespace(sleep=lambda seconds: None, time=time.time))
    return desktop, events


def dispatch_capture():
    return ActionRegistry.dispatch("screenshot", "", source="rule", user_text="截个图")


def resolve(text, proposed=None):
    decision = proposed if proposed is not None else ci.fast_match_intent(text)
    return ci.apply_command_guard(text, decision) if decision else None


def drive(text, proposed=None):
    decision = resolve(text, proposed)
    assert decision is not None, text
    if decision["action"] == "chat":
        return decision, None
    result = executor.CanaanOSExecutor.execute(decision["action"], decision.get("param", ""),
        source="rule" if proposed is None else "llm", user_text=text)
    return decision, result


def test_storage_same_second_preserves_existing_file_and_both_new_captures(screen_fixture):
    desktop, _ = screen_fixture
    existing = desktop / "Screenshot_20990101_120000.png"
    existing.write_bytes(b"Existing file must survive")
    first, second = dispatch_capture(), dispatch_capture()
    assert first.success and second.success
    assert Path(first.detail) != Path(second.detail)
    assert Path(first.detail) != existing and Path(second.detail) != existing
    assert existing.read_bytes() == b"Existing file must survive"
    for result in (first, second):
        path = Path(result.detail)
        assert path.parent == desktop
        with Image.open(path) as image:
            assert image.format == "PNG" and image.size == (3, 2)
            assert image.getpixel((0, 0)) == (12, 34, 56)
        assert result.verification.verified and result.verification.gate_name == "SCREENSHOT_SAVED"


def test_storage_concurrent_requests_reserve_different_paths(screen_fixture):
    desktop, _ = screen_fixture
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: dispatch_capture(), range(6)))
    assert all(result.success for result in results), results
    assert len({result.detail for result in results}) == len(list(desktop.glob("*.png"))) == 6


def test_storage_capture_failure_does_not_claim_success_or_create_a_file(screen_fixture, monkeypatch):
    desktop, _ = screen_fixture
    def unavailable(**kwargs):
        raise OSError("fixture capture unavailable")
    monkeypatch.setattr(ImageGrab, "grab", unavailable)
    result = dispatch_capture()
    assert not result.success and result.error_code == "SCREENSHOT_CAPTURE_FAILED"
    assert "截好了" not in (result.speech_override or "")
    assert not list(desktop.iterdir())


def test_storage_partial_save_failure_removes_only_its_new_file(screen_fixture, monkeypatch):
    desktop, _ = screen_fixture
    original = desktop / "Screenshot_20990101_120000.png"
    original.write_bytes(b"Original screenshot")
    class BrokenImage:
        def save(self, path):
            Path(path).write_bytes(b"Partial PNG")
            raise OSError("fixture disk write failed")
        def close(self):
            pass
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: BrokenImage())
    result = dispatch_capture()
    assert not result.success and result.error_code == "SCREENSHOT_SAVE_FAILED"
    assert "截好了" not in (result.speech_override or "")
    assert list(desktop.iterdir()) == [original]
    assert original.read_bytes() == b"Original screenshot"


@pytest.mark.parametrize("saved", [None, b"Not a PNG"])
def test_storage_verifies_file_contents_before_announcing_success(screen_fixture, monkeypatch, saved):
    desktop, _ = screen_fixture
    class NonImage:
        def save(self, path):
            if saved is not None:
                Path(path).write_bytes(saved)
        def close(self):
            pass
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: NonImage())
    result = dispatch_capture()
    assert not result.success and result.error_code == "SCREENSHOT_VERIFY_FAILED"
    assert not result.speech_override and not list(desktop.iterdir())


@pytest.mark.parametrize("text", ["照张屏", "照一张屏", "照一下屏幕"])
def test_spoken_capture_reaches_png_driver(screen_fixture, text):
    desktop, events = screen_fixture
    decision, result = drive(text)
    assert decision["action"] == "screenshot" and result["success"]
    assert events == ["screenshot"] and len(list(desktop.glob("*.png"))) == 1


@pytest.mark.parametrize("text", [
    "能帮我截个图吗", "把当前屏幕整张拍下来存好", "屏幕画面存一张",
    "帮我take个screenshot", "截个频幕给我", "截个途看看", "截个屏存桌面",
])
def test_model_semantic_capture_keeps_its_local_delivery_path(screen_fixture, text):
    desktop, events = screen_fixture
    decision, result = drive(text, {"action": "screenshot", "param": "", "_conf": 0.99})
    assert decision["action"] == "screenshot" and result["success"]
    assert result["verification"]["verified"] and events == ["screenshot"]
    assert len(list(desktop.glob("*.png"))) == 1


@pytest.mark.parametrize("text", ["打开计算器 再照张屏", "打开计算器，再照张屏", "打开计算器再照张屏", "打开计算器并照张屏"])
def test_spoken_multi_step_keeps_the_launch_before_capture(screen_fixture, text):
    desktop, events = screen_fixture
    decision, result = drive(text)
    assert decision["action"] == "multi_action" and result["success"]
    assert events == ["open_app", "screenshot"]
    assert len(list(desktop.glob("*.png"))) == 1


@pytest.mark.parametrize("text", ["照一张屏，再照一张屏", "截个图，再截个图"])
def test_explicit_two_captures_persist_two_images(screen_fixture, text):
    desktop, events = screen_fixture
    decision, result = drive(text)
    assert decision["action"] == "multi_action" and result["success"]
    assert events == ["screenshot", "screenshot"]
    assert len(list(desktop.glob("*.png"))) == 2


@pytest.mark.parametrize("text", ["照一张屏就算了", "打开计算器，别照一张屏"])
def test_model_capture_of_cancelled_step_never_reaches_the_driver(screen_fixture, text):
    desktop, events = screen_fixture
    decision, result = drive(text, {"action": "screenshot", "param": "", "_conf": 0.99})
    assert decision["action"] == "chat" and not decision.get("_pending")
    assert result is None and not events and not list(desktop.iterdir())


@pytest.mark.parametrize("text,first", [("打开计算器", "open_app"), ("百度搜截图样例", "web_search")])
@pytest.mark.parametrize("encoding", [0, 1, 2])
@pytest.mark.parametrize("shot_name", ["screenshot", " SCREEN_SHOT ", "capture_screen"])
def test_model_cannot_append_an_unrequested_capture_to_a_multi_action(screen_fixture, text, first, encoding, shot_name):
    desktop, events = screen_fixture
    plan = [
        {"action": first, "param": "计算器" if first == "open_app" else "截图样例"},
        {"action": shot_name, "param": ""}]
    for _ in range(encoding):
        plan = json.dumps(plan, ensure_ascii=False)
    proposal = {"action": "multi_action", "param": plan, "_conf": 0.99}
    decision, result = drive(text, proposal)
    assert decision["action"] == "chat" and not decision.get("_pending")
    assert result is None and not events and not list(desktop.iterdir())


@pytest.mark.parametrize("text", [
    "截图前先把隐私窗口关掉", "截个图但不要包括聊天窗口", "截个图，存到剪贴板别保存文件",
])
def test_model_cannot_ignore_capture_preconditions_or_scope(screen_fixture, text):
    desktop, events = screen_fixture
    proposal = {"action": "screenshot", "param": "", "_conf": 0.99}
    decision, result = drive(text, proposal)
    assert decision["action"] == "chat" and not decision.get("_pending")
    assert result is None and not events and not list(desktop.iterdir())


def test_failed_capture_stops_the_remaining_launch(screen_fixture, monkeypatch):
    desktop, events = screen_fixture
    def unavailable(**kwargs):
        events.append("screenshot-failed")
        raise OSError("Synthetic capture failure")
    monkeypatch.setattr(ImageGrab, "grab", unavailable)
    result = executor.CanaanOSExecutor.execute("multi_action", json.dumps([
        {"action": "screenshot", "param": ""}, {"action": "open_app", "param": "计算器"}], ensure_ascii=False),
        source="rule", user_text="截图，然后打开计算器")
    assert not result["success"] and result["error_code"] == "SCREENSHOT_CAPTURE_FAILED"
    assert events == ["screenshot-failed"] and not list(desktop.iterdir())


def test_multi_step_preserves_failure_after_a_completed_prefix(screen_fixture, monkeypatch):
    desktop, events = screen_fixture
    def unavailable(**kwargs):
        events.append("screenshot-failed")
        raise OSError("Synthetic capture failure")
    monkeypatch.setattr(ImageGrab, "grab", unavailable)
    result = executor.CanaanOSExecutor.execute("multi_action", [
        {"action": "open_app", "param": "计算器"}, {"action": "screenshot", "param": ""},
        {"action": "open_app", "param": "记事本"}], source="rule", user_text="打开计算器，截图，然后打开记事本")
    assert not result["success"] and result["error_code"] == "SCREENSHOT_CAPTURE_FAILED"
    assert result["action"] == "multi_action" and result["failed_action"] == "screenshot"
    assert result["failed_step"] == 2 and result["completed_steps"] == result["skipped_steps"] == 1
    assert result["trace_id"] and "没能获取屏幕图像" in result["speech_override"]
    assert "Synthetic launch only" in result["message"]
    assert events == ["open_app", "screenshot-failed"] and not list(desktop.iterdir())
