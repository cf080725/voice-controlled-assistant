# -*- coding: utf-8 -*-
"""
应用、文件与网页类动作：打开/关闭应用、搜索并打开文件、打开文件夹、打开网址、网页搜索。
底层的应用索引与文件检索复用 canaan_os_executor.CanaanOSExecutor 里的驱动函数。
"""
import os
import re
import time
import webbrowser
import urllib.parse
from pathlib import Path
import psutil

from canaan_action_framework.models import (
    ActionContext, ActionResult, ActionCategory, PermissionLevel, VerificationResult
)
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry
from canaan_execution_guard import execution_allowed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
FISHSIM_DIR = os.environ.get("FISHSIM_DIR", r"D:\FishSim")

CHATAPP_NAMES = {"聊天软件", "chatapp", "chatapp", "聊天软件"}
# 打开后会直接执行代码的文件类型：打开前需要用户确认
EXECUTABLE_EXTS = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi",
                   ".com", ".scr", ".lnk", ".py", ".pyw", ".reg", ".hta", ".jar"}

_USER_FOLDERS = {
    "desktop": ("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Desktop"),
    "downloads": ("374DE290-123F-4565-9164-39C4925E467B", "Downloads"),
    "documents": ("FDD39AD0-238F-46AF-ADB4-6C85480369C7", "Documents"),
    "pictures": ("33E28130-4E1E-4676-835A-98395C3BC3BB", "Pictures"),
}


def _windows_known_folder_path(folder_id):
    """Ask the shell for the current user's redirected folder and free its buffer."""
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    folder = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    shell, ole = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32,
                                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    initialized = ole.CoInitializeEx(None, 2)
    # RPC_E_CHANGED_MODE means this thread already initialized another COM mode.
    if initialized not in (0, 1, -2147417850):
        raise OSError(f"COM initialization failed: {initialized}")
    result = ctypes.c_wchar_p()
    try:
        status = shell.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(result))
        if status != 0 or not result.value:
            raise OSError(f"Known folder lookup failed: {status}")
        return result.value
    finally:
        if result:
            ole.CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))
        if initialized in (0, 1):
            ole.CoUninitialize()


def _user_folder_path(kind):
    folder_id, default = _USER_FOLDERS[kind]
    if os.name == "nt":
        try:
            return _windows_known_folder_path(folder_id)
        except (OSError, ValueError, AttributeError):
            pass
    return str(Path.home() / default)


def _executor():
    from canaan_os_executor import CanaanOSExecutor
    return CanaanOSExecutor


@ActionRegistry.register
class OpenAppAction(BaseAction):
    """打开应用：聊天软件走专用唤醒流程；ms-settings: 等系统 URI 直接交给 Windows；其余按应用索引查找。
    找不到就如实说找不到，不再把模型给的文本丢给 shell 执行。"""
    name = "open_app"
    aliases = ["launch_app", "open", "start_app"]
    description = "打开电脑中的应用程序（名称模糊匹配开始菜单、桌面快捷方式与注册表 App Paths）"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        app_name = context.param_value().strip('"').strip()
        if not app_name:
            return ActionResult(success=False, action=self.name, error_code="INVALID_TARGET",
                                message="未指定要打开的应用", speech_override="要打开哪个软件呀？")
        if app_name.lower() in CHATAPP_NAMES:
            return ActionRegistry.get_action("open_chatapp").execute(context)

        context.report_step(1, 2, f"正在检索应用: {app_name}")
        target = _executor().find_application(app_name)
        if not target:
            return ActionResult(success=False, action=self.name, error_code="APP_NOT_FOUND",
                                message=f"未能在系统中找到应用: {app_name}",
                                speech_override=f"没找着{app_name}，是不是叫别的名字？")
        label = target if target.endswith(":") else os.path.splitext(os.path.basename(target))[0]
        context.report_step(2, 2, f"正在启动: {label}")
        try:
            # ms-settings: 等 URI 与 exe/lnk 都可以直接交给 ShellExecute，不经过命令行解析
            os.startfile(target)
        except Exception as e:
            return ActionResult(success=False, action=self.name, error_code="LAUNCH_FAILED",
                                message=f"启动 {label} 失败: {e}", speech_override="启动出了点问题。")
        return ActionResult(success=True, action=self.name, message=f"已启动应用程序: {label}",
                            detail=target, speech_override="打开了。")


def _focus_process_names(target):
    """Application identities, not the launcher's interchangeable fallback candidates."""
    name = str(target or "").strip().strip('"').casefold()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}\.exe", name):
        return frozenset((name,))
    groups = {
        ("计算器", "calc", "calculator", "windows calculator"): ("calc.exe", "calculator.exe", "calculatorapp.exe"),
        ("记事本", "notepad"): ("notepad.exe",),
        ("chrome", "google chrome", "谷歌", "谷歌浏览器"): ("chrome.exe",),
        ("edge", "msedge", "microsoft edge", "微软浏览器"): ("msedge.exe",),
        ("firefox", "火狐", "火狐浏览器"): ("firefox.exe",),
        ("浏览器", "browser"): ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"),
        ("聊天软件", "chatapp", "chatapp", "聊天软件"): ("chatapp.exe", "chatapp.exe"),
        ("word", "微软word"): ("winword.exe",),
        ("excel", "微软excel"): ("excel.exe",),
        ("powerpoint", "ppt", "微软powerpoint"): ("powerpnt.exe",),
        ("wps", "wps office"): ("wps.exe", "et.exe", "wpp.exe"),
        ("wps文字",): ("wps.exe",),
        ("wps表格",): ("et.exe",),
        ("wps演示",): ("wpp.exe",),
        ("vscode", "vs code", "visual studio code"): ("code.exe",),
        ("资源管理器", "文件资源管理器", "explorer"): ("explorer.exe",),
    }
    for aliases, executables in groups.items():
        if name in aliases:
            return frozenset(executables)
    # Exact process basenames also support installed apps outside the stable map.
    if re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", name):
        return frozenset((name + ".exe",))
    return frozenset()


def _same_app_window(first, second):
    fields = ("hwnd", "pid", "created", "processes")
    return bool(first and second and all(first.get(key) == second.get(key) for key in fields))


class _AppWindowBackend:
    """Small Win32 port; construction and every operation can use a fake API."""
    def __init__(self, user32=None, process_factory=None):
        import ctypes
        from ctypes import wintypes
        if user32 is None:
            if os.name != "nt":
                raise OSError("Window focus requires Windows")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.api = user32
        self.process_factory = process_factory or psutil.Process
        self._callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        style_name = "GetWindowLongPtrW" if hasattr(self.api, "GetWindowLongPtrW") else "GetWindowLongW"
        self._style = getattr(self.api, style_name)
        signatures = {
            "GetForegroundWindow": ([], wintypes.HWND),
            "GetShellWindow": ([], wintypes.HWND),
            "GetDesktopWindow": ([], wintypes.HWND),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
            "EnumWindows": ([self._callback, wintypes.LPARAM], wintypes.BOOL),
            "EnumChildWindows": ([wintypes.HWND, self._callback, wintypes.LPARAM], wintypes.BOOL),
            style_name: ([wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t if style_name == "GetWindowLongPtrW" else wintypes.LONG),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    def _pid(self, hwnd):
        import ctypes
        from ctypes import wintypes
        pid = wintypes.DWORD()
        thread = self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) if thread and pid.value else 0

    def _process(self, pid):
        process = self.process_factory(pid)
        return pid, process.name().casefold(), float(process.create_time())

    def describe(self, hwnd):
        import ctypes
        hwnd = int(hwnd or 0)
        try:
            if not hwnd or not self.api.IsWindow(hwnd) or not self.api.IsWindowVisible(hwnd):
                return None
            if hwnd in (int(self.api.GetShellWindow() or 0), int(self.api.GetDesktopWindow() or 0)):
                return None
            if int(self.api.GetAncestor(hwnd, 3) or 0) != hwnd:
                return None
            class_name = ctypes.create_unicode_buffer(256)
            if not self.api.GetClassNameW(hwnd, class_name, len(class_name)):
                return None
            if class_name.value.casefold() in {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"}:
                return None
            if self._style(hwnd, -20) & (0x80 | 0x08000000):  # WS_EX_TOOLWINDOW / WS_EX_NOACTIVATE
                return None
            pid = self._pid(hwnd)
            if not pid:
                return None
            owner = self._process(pid)
            processes = {owner}
            if owner[1] == "applicationframehost.exe":
                def child_process(child, unused):
                    try:
                        if (self.api.IsWindow(child) and self.api.IsWindowVisible(child)
                                and int(self.api.GetAncestor(child, 2) or 0) == hwnd):
                            child_pid = self._pid(child)
                            if child_pid:
                                identity = self._process(child_pid)
                                if (self.api.IsWindow(child) and self.api.IsWindowVisible(child)
                                        and int(self.api.GetAncestor(child, 2) or 0) == hwnd
                                        and self._pid(child) == child_pid and self._process(child_pid) == identity):
                                    processes.add(identity)
                    except (OSError, ValueError, psutil.Error):
                        pass
                    return True
                # EnumChildWindows' return value is unused by its Win32 contract.
                self.api.EnumChildWindows(hwnd, self._callback(child_process), 0)
            if self._pid(hwnd) != pid or self._process(pid) != owner:
                return None
            return {"hwnd": hwnd, "pid": pid, "created": owner[2], "processes": tuple(sorted(processes))}
        except (OSError, ValueError, psutil.Error):
            return None

    def list_windows(self):
        records = []
        def collect(hwnd, unused):
            record = self.describe(hwnd)
            if record:
                records.append(record)
            return True
        callback = self._callback(collect)
        if not self.api.EnumWindows(callback, 0):
            raise OSError("Cannot enumerate application windows")
        return records

    def foreground_root(self):
        try:
            foreground = int(self.api.GetForegroundWindow() or 0)
            if not foreground:
                return 0
            root = int(self.api.GetAncestor(foreground, 3) or 0)
            owner = self.describe(root)
            process = self._process(self._pid(foreground))
            if (owner and not self.api.IsIconic(root) and process in owner["processes"]
                    and int(self.api.GetForegroundWindow() or 0) == foreground):
                return root
        except (OSError, ValueError, psutil.Error):
            pass
        return 0

    def activate(self, record):
        hwnd = record["hwnd"]
        if not _same_app_window(record, self.describe(hwnd)):
            return False
        if self.api.IsIconic(hwnd):
            self.api.ShowWindow(hwnd, 9)  # SW_RESTORE only for a minimized window.
        if not _same_app_window(record, self.describe(hwnd)):
            return False
        return bool(self.api.SetForegroundWindow(hwnd))


def _app_window_backend():
    return _AppWindowBackend()


@ActionRegistry.register
class FocusAppAction(BaseAction):
    """Focus an existing app window and prove the current foreground identity."""
    name = "focus_app"
    aliases = ["focus_application", "bring_app_forward", "activate_app"]
    description = "将已打开的指定软件窗口置前；存在多个窗口时先核对目标，不启动软件代替聚焦"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION
    input_schema = {"type": "object", "properties": {"value": {"type": "string", "description": "已打开的软件名称"}}, "required": ["value"]}

    def _failure(self, code, message):
        return ActionResult(success=False, action=self.name, error_code=code, message=message, speech_override=message)

    def execute(self, context):
        names = _focus_process_names(context.param_value())
        if not names:
            return self._failure("INVALID_TARGET", "要把哪个软件窗口置前？请说软件名称。")
        try:
            backend = _app_window_backend()
            windows = [record for record in backend.list_windows()
                       if names.intersection(identity[1] for identity in record["processes"])]
            if not windows:
                return self._failure("WINDOW_NOT_FOUND", "没找到可确认的目标软件窗口。请先打开它，或说清软件名称。")
            foreground = backend.foreground_root()
            current = [record for record in windows if record["hwnd"] == foreground]
            if len(windows) > 1 and not current:
                return self._failure("AMBIGUOUS_WINDOW", "找到多个匹配窗口，请先选定要置前的窗口。")
            target = current[0] if current else windows[0]
            if not _same_app_window(target, backend.describe(target["hwnd"])):
                return self._failure("WINDOW_CHANGED", "目标窗口已变化，这次没有置前，请重新指定。")
            context.metadata.update(focus_backend=backend, focus_window=target)
            if target["hwnd"] != foreground:
                backend.activate(target)
            for attempt in range(3):
                if not _same_app_window(target, backend.describe(target["hwnd"])):
                    return self._failure("WINDOW_CHANGED", "目标窗口已变化，这次没有确认置前。")
                if backend.foreground_root() == target["hwnd"]:
                    return ActionResult(success=True, action=self.name, message="目标软件窗口已置前。", speech_override="窗口已置前。",
                                        detail={"hwnd": target["hwnd"]})
                if attempt < 2:
                    time.sleep(0.02)
            return self._failure("FOCUS_NOT_VERIFIED", "没有确认目标窗口已到前台，后续步骤已停止。")
        except Exception:
            return self._failure("WINDOW_QUERY_FAILED", "暂时无法核对软件窗口，这次没有继续操作。")

    def verify(self, context):
        backend, target = context.metadata.get("focus_backend"), context.metadata.get("focus_window")
        try:
            passed = bool(backend and target and _same_app_window(target, backend.describe(target["hwnd"]))
                          and backend.foreground_root() == target["hwnd"])
        except Exception:
            passed = False
        return VerificationResult(passed=passed, verified=passed, gate_name="APP_FOREGROUND",
                                  error_code=None if passed else "FOCUS_NOT_VERIFIED",
                                  reason="" if passed else "目标窗口的前台状态或进程身份已变化")


def _scoped_roots(scope):
    if scope not in ("desktop", "downloads", "documents", "project"):
        return None
    roots = [FISHSIM_DIR, BASE_DIR] if scope == "project" else [_user_folder_path(scope)]
    seen, unique = set(), []
    for root in roots:
        if root and os.path.isdir(root):
            key = os.path.normcase(os.path.abspath(root))
            if key not in seen:
                seen.add(key)
                unique.append(str(root))
    return unique


@ActionRegistry.register
class SearchFileAction(BaseAction):
    name = "search_file"
    aliases = ["open_file", "find_file"]
    description = "在工程目录与常用目录中按文件名关键词查找并打开文件"
    category = ActionCategory.FILE_OPERATIONS
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        query = context.param_value().strip('"').strip()
        def stopped_result():
            return ActionResult(success=False, action=self.name, error_code="SEARCH_CANCELLED",
                                message="文件检索已停止", speech_override="这次查找已停止，没有继续打开文件。")
        if not execution_allowed():
            return stopped_result()
        if not query:
            return ActionResult(success=False, action=self.name, error_code="INVALID_TARGET",
                                message="未指定要找的文件", speech_override="要找哪个文件呀？")
        context.report_step(1, 2, f"正在检索: {query}")
        if not execution_allowed():
            return stopped_result()
        scope = (getattr(context, "params", None) or {}).get("root")
        matches, target, search = None, None, None
        if scope is not None:
            if scope not in ("desktop", "downloads", "documents", "project") or re.search(r"[\\/:]", query):
                return ActionResult(success=False, action=self.name, error_code="INVALID_TARGET",
                                    message="指定目录检索需要已知目录和文件名", speech_override="请说清目录和文件名。")
            roots = _scoped_roots(scope)
            if not execution_allowed():
                return stopped_result()
            if not roots:
                return ActionResult(success=False, action=self.name, error_code="PATH_NOT_FOUND",
                                    message="指定的检索目录不存在", speech_override="这个目录不存在。")
            matches = []
            for root in roots:
                if not execution_allowed():
                    return stopped_result()
                path = os.path.join(root, query)
                if os.path.isfile(path):
                    matches.append(path)
            if not matches:
                if not execution_allowed():
                    return stopped_result()
                search = _executor().search_local_files_result(query, limit=2, roots=roots)
        elif os.path.isfile(query):
            target = query
        else:
            if not execution_allowed():
                return stopped_result()
            search = _executor().search_local_files_result(query, limit=2)
        if not execution_allowed():
            return stopped_result()
        if search is not None:
            if search.status == "cancelled":
                return stopped_result()
            if search.status == "timeout":
                return ActionResult(success=False, action=self.name, error_code="SEARCH_TIMEOUT",
                                    message="文件检索未完成，请缩小目录或补充完整路径",
                                    speech_override="这次查找没有完成，请说清目录或完整路径。")
            if not search.complete and not (search.reason == "match_limit" and len(search.paths) >= 2):
                return ActionResult(success=False, action=self.name, error_code="SEARCH_INCOMPLETE",
                                    message="文件检索未完成，请缩小目录或补充完整路径",
                                    speech_override="这次查找没有完成，请说清目录或完整路径。")
            matches = list(search.paths)
        if matches is not None:
            if len(matches) > 1:
                return ActionResult(success=False, action=self.name, error_code="AMBIGUOUS_TARGET",
                                    message="指定目录中存在多个匹配文件，请补充完整文件名或路径",
                                    speech_override="找到了多个文件，要打开哪一个？请说完整文件名或路径。")
            target = matches[0] if matches else None
        if not target:
            return ActionResult(success=False, action=self.name, error_code="FILE_NOT_FOUND",
                                message=f"未找到与“{query}”相关的文件", speech_override=f"没找着{query}呢。")
        if Path(target).suffix.lower() in EXECUTABLE_EXTS and not context.confirmed:
            return self.confirm_required(context, f"打开可执行文件 {target}", "打开该类型文件会直接运行程序")
        context.report_step(2, 2, f"正在打开: {os.path.basename(target)}")
        if not execution_allowed():
            return stopped_result()
        os.startfile(target)
        return ActionResult(success=True, action=self.name, message=f"已打开文件: {target}",
                            detail=target, speech_override="找到了，已经打开。")


@ActionRegistry.register
class OpenPathAction(BaseAction):
    name = "open_path"
    aliases = ["open_folder", "open_dir"]
    description = "在资源管理器中打开文件夹（桌面/下载/文档/工程目录或具体路径）"
    category = ActionCategory.FILE_OPERATIONS
    permission_level = PermissionLevel.NORMAL_MUTATION

    SHORTCUTS = {
        ("desktop", "桌面"): lambda: _user_folder_path("desktop"),
        ("downloads", "下载", "下载文件夹"): lambda: _user_folder_path("downloads"),
        ("documents", "文档", "我的文档"): lambda: _user_folder_path("documents"),
        ("pictures", "图片", "照片", "我的图片"): lambda: _user_folder_path("pictures"),
        ("fishsim", "机器鱼", "仿真目录"): lambda: FISHSIM_DIR,
        ("aaa", "管家", "工作区"): lambda: BASE_DIR,
        ("d盘", "d:", "d:\\"): lambda: "D:\\",
        ("c盘", "c:", "c:\\"): lambda: "C:\\",
    }

    @classmethod
    def resolve(cls, raw: str) -> str:
        key = raw.strip().strip('"').lower()
        for names, fn in cls.SHORTCUTS.items():
            if key in names:
                return fn()
        return os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))

    def execute(self, context: ActionContext) -> ActionResult:
        raw = context.param_value()
        params = getattr(context, "params", None) or {}
        scope = params.get("root")
        if scope is not None:
            roots = _scoped_roots(scope)
            if roots is None or not raw or raw in (".", "..") or re.search(r"[\\/:]", raw):
                return ActionResult(success=False, action=self.name, error_code="INVALID_TARGET",
                                    message="指定目录操作需要已知目录和子文件夹名", speech_override="请说清目录和文件夹名称。")
            names = [raw]
            if not params.get("literal"):
                short = re.sub(r"(?:文件夹|目录)$", "", raw)
                if short and short != raw:
                    names.append(short)
            matches = list(dict.fromkeys(os.path.join(root, name) for root in roots for name in names
                                         if os.path.isdir(os.path.join(root, name))))
            if len(matches) > 1:
                return ActionResult(success=False, action=self.name, error_code="AMBIGUOUS_TARGET",
                                    message="存在多个匹配文件夹，请指定完整路径", speech_override="找到了多个文件夹，要打开哪一个？请说完整路径。")
            path = matches[0] if matches else ""
        else:
            path = self.resolve(raw)
        if not path or not os.path.isdir(path):
            # 只打开文件夹；文件交给 search_file（会对可执行文件做确认）
            return ActionResult(success=False, action=self.name, error_code="PATH_NOT_FOUND",
                                message=f"找不到文件夹: {raw}", speech_override="这个文件夹好像不存在。")
        os.startfile(path)
        return ActionResult(success=True, action=self.name, message=f"已打开文件夹: {path}",
                            detail=path, speech_override="打开了。")


def _browser_request(action, context, target, request_kind):
    """The browser port acknowledges a launch request, never page readiness."""
    context.metadata.pop("_browser_request_observation", None)
    detail = {"target": target, "request_accepted": None, "page_verified": False}
    try:
        accepted = webbrowser.open(target)
    except Exception:
        message = f"浏览器{request_kind}请求发生异常，是否已接受尚未核实，后续步骤已停止。"
        return ActionResult(success=False, action=action, error_code="BROWSER_REQUEST_ERROR",
                            message=message, detail=detail, speech_override=message)
    if accepted is not True:
        rejected = accepted is False
        detail["request_accepted"] = False if rejected else None
        message = (f"浏览器未接受{request_kind}请求，页面状态未核实。" if rejected else
                   f"浏览器是否接受{request_kind}请求尚未核实，后续步骤已停止。")
        return ActionResult(success=False, action=action,
                            error_code="BROWSER_REQUEST_REJECTED" if rejected else "BROWSER_REQUEST_UNCONFIRMED",
                            message=message, detail=detail, speech_override=message)
    context.metadata["_browser_request_observation"] = {
        "trace_id": context.trace_id, "action": action, "context_action": context.action_name,
        "target": target, "accepted": True,
    }
    detail["request_accepted"] = True
    return ActionResult(success=True, action=action,
                        message=f"已向浏览器发送{request_kind}请求: {target}（页面状态未核实）",
                        detail=detail, speech_override=f"已向浏览器发送{request_kind}请求。")


def _browser_verification(context, action, target):
    observation = context.metadata.get("_browser_request_observation")
    registered = ActionRegistry.get_action(context.action_name)
    passed = bool(target and isinstance(observation, dict) and registered and registered.name == action
                  and observation.get("accepted") is True
                  and observation.get("trace_id") == context.trace_id
                  and observation.get("action") == action
                  and observation.get("context_action") == context.action_name
                  and observation.get("target") == target)
    return VerificationResult(passed=passed, verified=False, gate_name="BROWSER_REQUEST_ACCEPTED",
                              error_code=None if passed else "BROWSER_REQUEST_UNCONFIRMED",
                              reason="仅确认本次浏览器请求已接受，页面加载和内容未核实。" if passed else
                                     "本次浏览器请求的接受状态未核实。")


@ActionRegistry.register
class OpenUrlAction(BaseAction):
    name = "open_url"
    aliases = ["browse", "open_website"]
    description = "用默认浏览器打开网址（仅 http/https）"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    @staticmethod
    def normalize(raw: str):
        """补全协议并校验，只接受 http/https + 合法主机名；不合法返回 None。"""
        url = (raw or "").strip().strip('"').strip()
        if not url:
            return None
        # urlparse silently removes embedded tab/newline and leading C0 controls.
        if re.search(r"[\x00-\x1f]", url):
            return None
        if "://" not in url:
            if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I) and not re.match(r"^[\w.-]+:\d+", url):
                return None  # javascript:、file:、ms-settings: 之类的非网页协议
            url = "https://" + url
        try:
            p = urllib.parse.urlparse(url)
            p.port
            host = p.hostname or ""
        except ValueError:
            return None
        if p.scheme.lower() not in ("http", "https") or not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$", host, re.I):
            return None
        return url

    def execute(self, context: ActionContext) -> ActionResult:
        context.metadata.pop("_browser_request_observation", None)
        raw = context.param_value()
        url = self.normalize(raw)
        if not url:
            return ActionResult(success=False, action=self.name, error_code="INVALID_URL",
                                message=f"只支持打开 http/https 网址: {raw!r}", speech_override="这个网址打不开。")
        return _browser_request(self.name, context, url, "打开")

    def verify(self, context: ActionContext) -> VerificationResult:
        return _browser_verification(context, self.name, self.normalize(context.param_value()))


@ActionRegistry.register
class WebSearchAction(BaseAction):
    name = "web_search"
    aliases = ["search_web", "baidu"]
    description = "用百度搜索关键词"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        context.metadata.pop("_browser_request_observation", None)
        query = context.param_value()
        if not query:
            return ActionResult(success=False, action=self.name, error_code="INVALID_TARGET",
                                message="未指定搜索内容", speech_override="要搜什么呀？")
        return _browser_request(self.name, context, f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}", "搜索")

    def verify(self, context: ActionContext) -> VerificationResult:
        query = context.param_value()
        target = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}" if query else None
        return _browser_verification(context, self.name, target)


@ActionRegistry.register
class CloseAppAction(BaseAction):
    """按进程名精确关闭应用。不用 shell、不做子串匹配、系统关键进程与本程序自身一律不碰。"""
    name = "close_app"
    aliases = ["kill_app", "close", "quit_app"]
    description = "关闭指定应用（进程名或常用中文名精确匹配）"
    category = ActionCategory.APPLICATION_LAUNCHER
    permission_level = PermissionLevel.SENSITIVE
    confirm_even_from_rule = True  # 关程序可能丢未保存的内容：规则识别的也先确认

    APP_PROCESS_ALIASES = {
        "记事本": ["notepad.exe"], "notepad": ["notepad.exe"],
        "计算器": ["calculatorapp.exe", "calc.exe"], "calc": ["calculatorapp.exe", "calc.exe"],
        "聊天软件": ["chatapp.exe", "chatapp.exe"], "chatapp": ["chatapp.exe", "chatapp.exe"], "chatapp": ["chatapp.exe", "chatapp.exe"],
        "网易云": ["cloudmusic.exe"], "网易云音乐": ["cloudmusic.exe"], "音乐": ["cloudmusic.exe"], "cloudmusic": ["cloudmusic.exe"],
        "浏览器": ["msedge.exe"], "edge": ["msedge.exe"], "chrome": ["chrome.exe"], "谷歌浏览器": ["chrome.exe"],
        "vscode": ["code.exe"], "code": ["code.exe"], "cursor": ["cursor.exe"], "trae": ["trae.exe"],
        "imapp": ["imapp.exe"], "steam": ["steam.exe"], "obsidian": ["obsidian.exe"], "blender": ["blender.exe"],
        "wps": ["wps.exe", "et.exe", "wpp.exe"], "word": ["winword.exe"], "excel": ["excel.exe"],
        "任务管理器": ["taskmgr.exe"], "taskmgr": ["taskmgr.exe"], "画图": ["mspaint.exe"],
        "fishsim": ["fishsim.exe"], "机器鱼": ["fishsim.exe"], "mqttx": ["mqttx.exe"],
    }
    PROTECTED = {
        "system", "system idle process", "registry", "memory compression", "secure system",
        "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe",
        "svchost.exe", "explorer.exe", "dwm.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
        "conhost.exe", "runtimebroker.exe", "searchhost.exe", "startmenuexperiencehost.exe",
        "shellexperiencehost.exe", "taskhostw.exe", "spoolsv.exe", "audiodg.exe", "msmpeng.exe",
        "securityhealthservice.exe", "wudfhost.exe", "dllhost.exe", "userinit.exe", "logonui.exe",
        # 团团自身依赖：Python 解释器（hub、GPT-SoVITS 语音服务都跑在 python.exe 里）与本地大模型服务
        "python.exe", "pythonw.exe", "py.exe", "ollama.exe", "ollama app.exe",
    }

    @classmethod
    def target_process_names(cls, target: str):
        t = (target or "").strip().strip('"').lower()
        if not t:
            return []
        names = list(cls.APP_PROCESS_ALIASES.get(t, []))
        if not names:
            names = [t if t.endswith(".exe") else t + ".exe"]
        return [n for n in names if n not in cls.PROTECTED]

    @staticmethod
    def _self_pids():
        pids = {os.getpid()}
        try:
            for p in psutil.Process(os.getpid()).parents():
                pids.add(p.pid)
        except Exception:
            pass
        return pids

    @staticmethod
    def _is_hub_process(proc):
        try:
            if (proc.info.get("name") or "").lower() not in ("python.exe", "pythonw.exe", "py.exe"):
                return False
            return any("canaan_desk_os_hub" in (a or "") for a in proc.cmdline())
        except Exception:
            return False

    def execute(self, context: ActionContext) -> ActionResult:
        target = context.param_value()
        names = self.target_process_names(target)
        if not names:
            return ActionResult(success=False, action=self.name, error_code="INVALID_TARGET",
                                message=f"关闭目标为空或属于系统关键进程: {target!r}",
                                speech_override="要关哪个程序呀？" if not target.strip() else "这个是系统进程，不能关。")
        context.report_step(1, 2, f"正在查找进程: {', '.join(names)}")
        skip = self._self_pids()
        victims = []
        for proc in psutil.process_iter(["pid", "name"]):
            pname = (proc.info.get("name") or "").lower()
            if pname in names and proc.info["pid"] not in skip and not self._is_hub_process(proc):
                victims.append(proc)
        if not victims:
            return ActionResult(success=False, action=self.name, error_code="PROCESS_NOT_FOUND",
                                message=f"没有找到正在运行的 {target}（{', '.join(names)}）",
                                speech_override=f"{target} 好像没在运行。")
        context.report_step(2, 2, f"正在关闭 {len(victims)} 个进程")
        for p in victims:
            try:
                p.terminate()
            except Exception:
                pass
        gone, alive = psutil.wait_procs(victims, timeout=3)
        if alive:
            return ActionResult(success=False, action=self.name, error_code="TERMINATE_PARTIAL",
                                message=f"已关闭 {len(gone)} 个，{len(alive)} 个进程未响应（可能需要管理员权限或有未保存内容）",
                                speech_override=f"{target} 没完全关掉，可能有没保存的内容。")
        return ActionResult(success=True, action=self.name, message=f"已关闭 {target}（{len(gone)} 个进程）",
                            detail=[p.pid for p in gone], speech_override="关掉了。")
