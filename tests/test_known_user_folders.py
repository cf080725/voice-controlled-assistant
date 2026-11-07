"""Open the user's redirected system folder, not an assumed home subfolder."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from canaan_action_framework.actions import app_actions as apps


@pytest.mark.parametrize("name,kind", [("下载", "downloads"), ("downloads", "downloads"),
                                      ("文档", "documents"), ("桌面", "desktop")])
def test_shortcut_uses_system_known_folder(name, kind, monkeypatch, tmp_path):
    redirected = tmp_path / "Redirected" / kind
    redirected.mkdir(parents=True)
    resolve = Mock(return_value=str(redirected))
    monkeypatch.setattr(apps, "_user_folder_path", resolve, raising=False)
    assert apps.OpenPathAction.resolve(name) == str(redirected)
    resolve.assert_called_once_with(kind)


def test_execution_opens_resolved_existing_folder(monkeypatch, tmp_path):
    redirected = tmp_path / "RedirectedDownloads"
    redirected.mkdir()
    monkeypatch.setattr(apps, "_user_folder_path", lambda kind: str(redirected), raising=False)
    launch = Mock()
    monkeypatch.setattr(apps.os, "startfile", launch)
    result = apps.OpenPathAction().execute(SimpleNamespace(param_value=lambda: "downloads"))
    assert result.success
    launch.assert_called_once_with(str(redirected))


def test_literal_paths_are_not_replaced_by_a_known_folder(tmp_path):
    custom = str(tmp_path / "Some Folder")
    assert apps.OpenPathAction.resolve(custom) == custom
    assert apps.OpenPathAction.resolve("工作区") == apps.BASE_DIR


def test_unavailable_shell_api_falls_back_to_home(monkeypatch):
    monkeypatch.setattr(apps, "_windows_known_folder_path", Mock(side_effect=OSError("unavailable")))
    assert apps._user_folder_path("downloads") == str(apps.Path.home() / "Downloads")


@pytest.mark.parametrize("com_result,uninitialize", [(0, True), (1, True), (-2147417850, False)])
@pytest.mark.parametrize("shell_result", [0, -2147024894])
def test_native_buffer_and_com_are_released_on_success_and_failure(
        monkeypatch, com_result, uninitialize, shell_result):
    import ctypes

    allocation = ctypes.create_unicode_buffer(r"D:\Redirected\Downloads")

    def lookup(folder, flags, token, output):
        assert flags == 0 and token is None
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(allocation)
        return shell_result

    shell = SimpleNamespace(SHGetKnownFolderPath=Mock(side_effect=lookup))
    ole = SimpleNamespace(CoInitializeEx=Mock(return_value=com_result),
                          CoTaskMemFree=Mock(), CoUninitialize=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: {"shell32": shell, "ole32": ole}[name])
    if shell_result:
        with pytest.raises(OSError):
            apps._windows_known_folder_path(apps._USER_FOLDERS["downloads"][0])
    else:
        assert apps._windows_known_folder_path(apps._USER_FOLDERS["downloads"][0]) == allocation.value
    ole.CoTaskMemFree.assert_called_once()
    assert ole.CoTaskMemFree.call_args.args[0].value == ctypes.addressof(allocation)
    assert ole.CoUninitialize.call_count == int(uninitialize)
