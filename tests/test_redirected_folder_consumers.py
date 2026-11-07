"""File lookup, app discovery and screenshot targets follow redirected folders."""

from types import SimpleNamespace
from unittest.mock import Mock

import canaan_os_executor as executor
import pytest
from canaan_action_framework.actions import app_actions as apps
from canaan_action_framework.actions import system_actions as system
from PIL import Image, ImageGrab


def test_application_scan_includes_redirected_desktop(monkeypatch, tmp_path):
    desktop = tmp_path / "Redirected Desktop"
    desktop.mkdir()
    (desktop / "Demo Editor.lnk").touch()
    (desktop / "Uninstall Demo.lnk").touch()
    resolve = Mock(return_value=str(desktop))
    monkeypatch.setattr(apps, "_user_folder_path", resolve)
    monkeypatch.setattr(executor.os.path, "exists", lambda path: str(path) == str(desktop))
    monkeypatch.setattr(executor.winreg, "OpenKey", Mock(side_effect=OSError("isolated registry")))
    monkeypatch.setattr(executor.CanaanOSExecutor, "_cached_app_index", None)
    monkeypatch.setattr(executor.CanaanOSExecutor, "_last_index_time", 0)

    found = executor.CanaanOSExecutor.scan_system_applications(force_refresh=True)

    assert found == {"demo editor": str(desktop / "Demo Editor.lnk")}
    resolve.assert_called_once_with("desktop")


@pytest.mark.parametrize("kind", ["desktop", "documents", "downloads"])
def test_file_search_includes_each_redirected_folder(kind, monkeypatch, tmp_path):
    folder = tmp_path / ("Redirected " + kind)
    folder.mkdir()
    target = folder / "demo-notes.txt"
    target.touch()
    folders = {key: str(tmp_path / ("Redirected " + key))
               for key in ("desktop", "documents", "downloads")}
    monkeypatch.setattr(apps, "_user_folder_path", folders.__getitem__)
    monkeypatch.setattr(executor.os.path, "exists", lambda path: str(path) == str(folder))

    assert executor.CanaanOSExecutor.search_local_files("demo-notes") == [str(target)]


def test_screenshot_uses_redirected_desktop_without_capturing(monkeypatch, tmp_path):
    desktop = tmp_path / "Redirected Desktop"
    desktop.mkdir()
    resolve = Mock(return_value=str(desktop))
    monkeypatch.setattr(apps, "_user_folder_path", resolve)
    image = Mock(wraps=Image.new("RGB", (2, 2), (20, 40, 60)))
    capture = Mock(return_value=image)
    monkeypatch.setattr(ImageGrab, "grab", capture)
    monkeypatch.setattr(system.time, "strftime", lambda fmt: "20261023_120000")

    result = system.ScreenshotAction().execute(SimpleNamespace())

    expected = str(desktop / "Screenshot_20261023_120000.png")
    assert result.success and result.detail == expected
    image.save.assert_called_once_with(expected)
    capture.assert_called_once_with(all_screens=True)
    resolve.assert_called_once_with("desktop")
    with Image.open(expected) as saved:
        assert saved.format == "PNG" and saved.getpixel((0, 0)) == (20, 40, 60)
