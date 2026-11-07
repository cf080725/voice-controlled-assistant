import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import os
import sys
import time
import json
import re
import subprocess
import webbrowser
import urllib.parse
import ctypes
import threading
import winreg
import psutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FISHSIM_DIR = os.environ.get("FISHSIM_DIR", r"D:\FishSim")

# Process-level Per-Monitor DPI Awareness (V2) & Interactive Desktop Binding
user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    hw_init = user32.OpenWindowStationW('WinSta0', False, 0x10000000)
    if hw_init: user32.SetProcessWindowStation(hw_init)
    hd_init = user32.OpenDesktopW('Default', 0, False, 0x10000000)
    if hd_init: user32.SetThreadDesktop(hd_init)
except Exception:
    pass

# 音量/多媒体键等系统动作已移至 canaan_action_framework/actions/system_actions.py

class CanaanOSExecutor:
    """动作执行入口 + 底层驱动库（应用索引、聊天软件窗口激活/OCR 校验/发送/撤回、文件检索）。
    各动作的具体实现已迁移到 canaan_action_framework/actions/*.py 并通过 ActionRegistry 注册。"""

    # 常见核心基础应用映射表
    BUILTIN_APP_MAP = {
        "calc": "calc.exe",
        "calculator": "calc.exe",
        "计算器": "calc.exe",
        "notepad": "notepad.exe",
        "记事本": "notepad.exe",
        "taskmgr": "taskmgr.exe",
        "任务管理器": "taskmgr.exe",
        "explorer": "explorer.exe",
        "资源管理器": "explorer.exe",
        "cmd": "cmd.exe",
        "terminal": "wt.exe",
        "终端": "powershell.exe",
        "powershell": "powershell.exe",
        "edge": "msedge.exe",
        "chrome": "chrome.exe",
        "settings": "ms-settings:",
        "设置": "ms-settings:",
        "control": "control.exe",
        "控制面板": "control.exe",
        "浏览器": "msedge.exe",
        "browser": "msedge.exe",
        "默认浏览器": "msedge.exe",
        "微软浏览器": "msedge.exe",
    }

    # 别名智能词库
    APP_ALIASES = {
        "vscode": ["visual studio code", "vscode", "code"],
        "代码": ["visual studio code", "cursor", "trae", "code"],
        "编辑器": ["cursor", "visual studio code", "trae", "notepad"],
        "cursor": ["cursor"],
        "trae": ["trae", "trae cn"],
        "obsidian": ["obsidian", "黑曜石"],
        "笔记": ["obsidian"],
        "wps": ["wps office", "wps"],
        "word": ["wps office", "word"],
        "excel": ["wps office", "excel"],
        "ppt": ["wps office", "powerpoint", "演示"],
        "文档": ["wps office"],
        "表格": ["wps office"],
        "幻灯片": ["wps office"],
        "blender": ["blender", "blender 5.2"],
        "建模": ["blender 5.2"],
        "聊天软件": ["chatapp", "chatapp"],
        "chatapp": ["chatapp", "chatapp"],
        "chatapp": ["chatapp", "chatapp"],
        "mqtt": ["mqttx"],
        "mqttx": ["mqttx"],
        "uu": ["uu远程", "uu"],
        "远程": ["uu远程"],
        "deepseek": ["deepseek harness"],
        "fishsim": ["fishsim", "机器鱼"],
        "机器鱼": ["fishsim"],
        "游戏": ["examplegame", "游戏"],
        "网易云音乐": ["cloudmusic", "网易云音乐", "网易云"],
        "imapp": ["imapp", "imapp.exe"],
        "steam": ["steam", "steam.exe"],
    }

    _cached_chatapp_path = None
    _cached_app_index = None
    _last_index_time = 0

    @classmethod
    def scan_system_applications(cls, force_refresh: bool = False) -> dict:
        """动态实时扫描电脑全局安装的所有软件、桌面快捷方式与常用工具库"""
        now = time.time()
        if not force_refresh and cls._cached_app_index and (now - cls._last_index_time < 300):
            return cls._cached_app_index

        apps = {}

        # 1. 扫描所有用户的开始菜单与桌面快捷方式
        from canaan_action_framework.actions.app_actions import _user_folder_path
        scan_dirs = [
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
            os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs"),
            _user_folder_path("desktop"),
            r"C:\Users\Public\Desktop"
        ]

        for base_dir in scan_dirs:
            if os.path.exists(base_dir):
                for root, _, files in os.walk(base_dir):
                    for f in files:
                        if f.lower().endswith(".lnk"):
                            raw_name = f[:-4].strip()
                            # 过滤卸载程序
                            if "卸载" in raw_name or "uninstall" in raw_name.lower():
                                continue
                            full_path = os.path.join(root, f)
                            apps[raw_name.lower()] = full_path

        # 2. 检查自定义高频核心目录
        custom_executables = [
            (r"D:\Program Files\Tencent\ChatApp\ChatApp.exe", ["聊天软件", "chatapp", "chatapp"]),
            (os.path.join(FISHSIM_DIR, r"FishSim-V1\WindowsNoEditor\FishSim.exe"), ["fishsim", "机器鱼", "仿真器"]),
            (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"), ["ollama", "大模型引擎"]),
            (r"C:\Program Files\Microsoft VS Code\Code.exe", ["vscode", "code"]),
            (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"), ["vscode", "code"]),
            (os.path.expandvars(r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe"), ["cursor"]),
            (os.path.expandvars(r"%LOCALAPPDATA%\Programs\trae\Trae.exe"), ["trae"]),
            (r"C:\Program Files\Google\Chrome\Application\chrome.exe", ["chrome", "谷歌浏览器"]),
            (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", ["edge", "微软浏览器"]),
        ]
        for exe_path, names in custom_executables:
            if os.path.exists(exe_path):
                for n in names:
                    apps[n.lower()] = exe_path

        # 3. 扫描注册表 App Paths
        reg_app_paths = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for root_key in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            try:
                k = winreg.OpenKey(root_key, reg_app_paths)
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        sub_name = winreg.EnumKey(k, i)
                        sub_k = winreg.OpenKey(k, sub_name)
                        exe_path, _ = winreg.QueryValueEx(sub_k, "")
                        if exe_path and os.path.exists(exe_path):
                            clean_k = sub_name.lower().replace(".exe", "")
                            if clean_k not in apps:
                                apps[clean_k] = exe_path
                    except Exception:
                        pass
            except Exception:
                pass

        cls._cached_app_index = apps
        cls._last_index_time = now
        return apps

    @classmethod
    def find_application(cls, query: str) -> str:
        """万能多模态模糊寻径：根据用户口语智能定位本地任何软件或游戏"""
        if not query:
            return ""

        q = query.lower().strip()

        # 1. 内置命令直查
        if q in cls.BUILTIN_APP_MAP:
            return cls.BUILTIN_APP_MAP[q]

        # 2. 检查聊天软件专用探测
        if q in ["聊天软件", "chatapp", "chatapp", "聊天软件"] and hasattr(cls, "find_chatapp_path"):
            wx_path = cls.find_chatapp_path()
            if wx_path:
                return wx_path

        # 3. 扫描已安装软件索引
        apps = cls.scan_system_applications()

        # 3.1 别名库直查
        aliases = cls.APP_ALIASES.get(q, [q])
        for a in aliases:
            if a in apps:
                return apps[a]

        # 3.2 完全匹配
        if q in apps:
            return apps[q]

        # 3.3 包含匹配 (优先匹配首部包含或包含关键词的软件)
        for name, path in apps.items():
            if q == name or any(a in name for a in aliases):
                return path

        # 子串模糊匹配只对 2~12 字的查询生效：单字（如“别”）或整句话会误中大量无关应用
        if 2 <= len(q) <= 12:
            for name, path in apps.items():
                if re.search(r'(nativemessag|messagehost|update|helper|crash|uninstall|setup|installer)', name):
                    continue
                if q in name or (len(name) >= 2 and name in q):
                    return path

        return ""


    _chatapp_hwnd = None

    @classmethod
    def force_foreground_window(cls, hwnd: int) -> bool:
        """穿透 Windows 系统焦点锁定（包括 3D 全屏游戏、DirectX 覆盖层）强行置顶目标窗口"""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            h_winsta = user32.OpenWindowStationW('WinSta0', False, 0x10000000)
            if h_winsta:
                user32.SetProcessWindowStation(h_winsta)
            h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
            if h_desk:
                user32.SetThreadDesktop(h_desk)

            fg_hwnd = user32.GetForegroundWindow()
            if fg_hwnd == hwnd:
                cls._chatapp_hwnd = hwnd
                return True

            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
            cur_thread = kernel32.GetCurrentThreadId()
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)

            if fg_thread and cur_thread != fg_thread:
                user32.AttachThreadInput(cur_thread, fg_thread, True)
            if target_thread and cur_thread != target_thread:
                user32.AttachThreadInput(cur_thread, target_thread, True)

            # 模拟 Alt 键绕过 Windows SetForegroundWindow 权限门禁
            VK_MENU = 0x12
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

            user32.ShowWindow(hwnd, 9) # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)

            if fg_thread and cur_thread != fg_thread:
                user32.AttachThreadInput(cur_thread, fg_thread, False)
            if target_thread and cur_thread != target_thread:
                user32.AttachThreadInput(cur_thread, target_thread, False)

            time.sleep(0.2)
            cls._chatapp_hwnd = hwnd
            return True
        except Exception:
            return False


    # 聊天软件小区域专用实例；一般全屏 OCR 使用 canaan_ufo_agent._GLOBAL_OCR。
    _rapid_ocr = None
    _chatapp_ocr_init_lock = threading.Lock()

    @classmethod
    def _get_chatapp_ocr_engine(cls):
        """后台预备与前台调用只构造一次；构造失败不缓存，后续仍可重试。"""
        if cls._rapid_ocr is not None:
            return cls._rapid_ocr
        with cls._chatapp_ocr_init_lock:
            if cls._rapid_ocr is None:
                from rapidocr_onnxruntime import RapidOCR
                # 小裁剪保留原生尺度，避免 min736 把标题放大到数百万像素。
                cls._rapid_ocr = RapidOCR(det_limit_type="max", det_limit_side_len=736)
            return cls._rapid_ocr

    @classmethod
    def _warmup_ocr_engine(cls):
        """后台预备聊天软件 OCR；失败留可见诊断，前台工厂保留原始异常。"""
        try:
            cls._get_chatapp_ocr_engine()
            return True
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("聊天软件 OCR 预备失败（%s），下次使用将重试", type(exc).__name__)
            return False

    @classmethod
    def _grab_screen_safe(cls, bbox):
        """线程安全、抗钩子锁死、桌面会话隔离的屏幕截图捕获器"""
        import threading
        user32 = ctypes.windll.user32
        res = [None]
        err = [None]

        def _grab_worker():
            try:
                hw = user32.OpenWindowStationW('WinSta0', False, 0x10000000)
                if hw: user32.SetProcessWindowStation(hw)
                hd = user32.OpenDesktopW('Default', 0, False, 0x10000000)
                if hd: user32.SetThreadDesktop(hd)
                from PIL import ImageGrab
                res[0] = ImageGrab.grab(bbox=bbox)
            except Exception as e:
                err[0] = e

        t = threading.Thread(target=_grab_worker)
        t.start()
        t.join(timeout=3.0)
        if err[0]:
            raise err[0]
        return res[0]




    @classmethod
    def is_contact_match(cls, contact_query: str, title_on_screen: str, alias_data: dict) -> bool:
        """只接受整名或显式别名，不能从相似姓名推断联系人相同。"""
        if not isinstance(contact_query, str) or not isinstance(title_on_screen, str):
            return False
        punctuation = str.maketrans({"（": "(", "）": ")", "【": "[", "】": "]"})
        suffix = re.compile(r"\s*(?:\([^()\[\]]+\)|\[[^()\[\]]+\])\s*$")

        def normalize(value):
            if not isinstance(value, str):
                return ""
            value = value.translate(punctuation).strip().casefold()
            return re.sub(r"\s+(?=[(\[])", "", value)

        query, title = normalize(contact_query), normalize(title_on_screen)
        if not query or not title:
            return False
        # 群人数是安全边界，不能作为普通备注清洗掉。
        if "群" not in query and ("群" in title or re.search(r"[(\[]\s*\d+\s*[)\]]", title)):
            return False

        def whole_name_matches(expected):
            if title == expected:
                return True
            # 原话已明确部门/备注时，不能丢掉这部分去匹配另一个同名的人。
            if not expected or suffix.search(expected):
                return False
            bare_title = title
            while True:
                annotation = suffix.search(bare_title)
                if not annotation or not bare_title[:annotation.start()].strip():
                    break
                bare_title = bare_title[:annotation.start()].strip()
            return bare_title == expected

        if whole_name_matches(query):
            return True

        config = alias_data if isinstance(alias_data, dict) else {}
        aliases = config.get("aliases", {})
        if isinstance(aliases, dict):
            for key, values in aliases.items():
                if not isinstance(values, (list, tuple, set)):
                    continue
                group = {normalize(value) for value in [key, *values] if isinstance(value, str)}
                if query in group and any(whole_name_matches(entry) for entry in group):
                    return True

        name_to_remark = config.get("name_to_remark", {})
        if isinstance(name_to_remark, dict):
            for name, remark in name_to_remark.items():
                if not isinstance(name, str) or not isinstance(remark, str):
                    continue
                name, remark = normalize(name), normalize(remark)
                if ((query == name and whole_name_matches(remark))
                        or (query == remark and whole_name_matches(name))):
                    return True
        return False




    @classmethod
    def search_local_files(cls, keyword: str, limit: int = 5, roots=None) -> list:
        """兼容旧列表调用；自动打开必须使用带完整状态的检索入口。"""
        return list(cls.search_local_files_result(keyword, limit=limit, roots=roots, exists=os.path.exists).paths)

    @classmethod
    def search_local_files_result(cls, keyword: str, limit: int = 2, roots=None, *, should_stop=None, exists=None):
        """有预算的协作式检索；不完整结果不能作为唯一文件自动打开。"""
        from canaan_file_search import FileSearchResult, search_files
        from canaan_execution_guard import execution_allowed
        should_stop = should_stop if should_stop is not None else lambda: not execution_allowed()
        try:
            if should_stop() is not False:
                return FileSearchResult((), "cancelled", "stopped")
        except BaseException:
            return FileSearchResult((), "cancelled", "stop_check_failed")
        if not keyword:
            return FileSearchResult((), "complete", "exhausted")

        from canaan_action_framework.actions.app_actions import _user_folder_path
        search_roots = list(roots) if roots is not None else [
            FISHSIM_DIR,
            BASE_DIR,
            _user_folder_path("desktop"),
            _user_folder_path("documents"),
            _user_folder_path("downloads"),
            r"D:\\"
        ]

        return search_files(keyword, search_roots, limit=limit, should_stop=should_stop, exists=exists)

    @classmethod
    def execute(cls, action: str, param="", on_step=None, source: str = "llm",
                confirmed: bool = False, user_text: str = "") -> dict:
        """统一入口（保持旧接口，hub 与旧脚本无需改动）。
        具体动作全部在 canaan_action_framework/actions/*.py 中注册，这里只做：
          - multi_action 复合动作：按顺序逐个调度，遇到需要确认的子动作就停下并交给上层
          - 其他动作：ActionRegistry.dispatch（权限闸门 + 执行 + 校验 + 审计）
        source: "rule"（确定性规则）/ "llm"（大模型，默认，最严格）；confirmed: 用户是否已口头确认
        """
        action = (action or "").lower().strip()
        if action == "chat":  # 闲聊不算任务，不发状态事件
            return cls._execute(action, param, on_step, source, confirmed, user_text)
        import canaan_events

        def step_event(step, total, desc=""):
            canaan_events.publish("step", action, step, total)  # 只发步骤号，不发描述（描述里可能有原话）
            if on_step is not None:
                on_step(step, total, desc)
        res = cls._execute(action, param, step_event, source, confirmed, user_text)
        code = res.get("error_code")
        if code == "CONFIRM_REQUIRED":
            canaan_events.publish("awaiting_confirm", action, result="confirm")
        elif res.get("success"):
            canaan_events.publish("success", action, result="ok")
        else:
            canaan_events.publish("failure", action, result="blocked" if code == "SECURITY_BLOCKED" else "failed")
        return res

    @classmethod
    def _execute(cls, action, param, on_step, source, confirmed, user_text):
        from canaan_action_framework import ActionRegistry
        from canaan_query_answers import AnswerCollector, QueryExecutionResult, IncompleteAnswers
        from canaan_query_context import trusted_query_bindings, step_query_scope
        from canaan_command_reply import _family

        def is_query(step):
            actor = ActionRegistry.get_action(step["action"])
            canonical = actor.name if actor is not None else step["action"].strip().lower()
            return canonical == "system_status" or (canonical == "run_cmd" and bool(_family(step.get("param", ""))[0]))

        def dispatch_step(step, index, collector, bindings):
            actor = ActionRegistry.get_action(step["action"])
            canonical = actor.name if actor is not None else step["action"].strip().lower()
            query = is_query(step)
            with collector.step(index + 1, canonical) as capture:
                if query:
                    binding = bindings[index] if len(bindings) == collector.segment_steps else None
                    with step_query_scope(binding, step_index=index, required=bool(user_text)):
                        result = ActionRegistry.dispatch(step["action"], step.get("param", ""),
                            on_step=on_step, source=source, confirmed=bool(confirmed and index == 0),
                            user_text=user_text).to_dict()
                else:
                    result = ActionRegistry.dispatch(step["action"], step.get("param", ""),
                        on_step=on_step, source=source, confirmed=bool(confirmed and index == 0),
                        user_text=user_text).to_dict()
                if result.get("success", False):
                    try:
                        capture.commit()
                    except IncompleteAnswers:
                        # A plain provider result cannot manufacture a typed answer.
                        result = {**result, "success": False, "error_code": "QUERY_ANSWER_UNAVAILABLE",
                                  "message": "查询结果未完整保存，后续步骤已停止。",
                                  "speech_override": "查询结果未完整保存，后续已停止。"}
            return result

        if action != "multi_action":
            steps = [{"action": action, "param": param}]
            collector = AnswerCollector(segment_steps=1, expected_query_steps=(1,) if is_query(steps[0]) else ())
            result = dispatch_step(steps[0], 0, collector, trusted_query_bindings(user_text, steps) if is_query(steps[0]) else ())
            stop = ("completed" if result.get("success", False) else "confirmation_required"
                    if result.get("error_code") == "CONFIRM_REQUIRED" else "failed")
            return QueryExecutionResult(result, collector.finish(stop))

        sub_list = param
        if isinstance(param, str):
            try:
                sub_list = json.loads(param)
                if isinstance(sub_list, str):
                    sub_list = json.loads(sub_list)
            except Exception:
                sub_list = []
        if (not isinstance(sub_list, list) or not 1 <= len(sub_list) <= 32
                or any(not isinstance(s, dict) or not isinstance(s.get("action"), str)
                       or not s["action"].strip() or s["action"].strip().lower() == "multi_action"
                       for s in sub_list)):
            return {"success": False, "action": "multi_action", "message": "复合动作参数为空或格式错误",
                    "error_code": "INVALID_PARAM", "speech_override": "这串指令我没看懂。"}

        collector = AnswerCollector(segment_steps=len(sub_list),
                                    expected_query_steps=tuple(i + 1 for i, step in enumerate(sub_list) if is_query(step)))
        bindings = trusted_query_bindings(user_text, sub_list) if collector.expected else ()
        messages = []
        for i, sub in enumerate(sub_list):
            s_act, s_param = sub.get("action", ""), sub.get("param", "")
            res = dispatch_step(sub, i, collector, bindings)
            if res.get("error_code") == "CONFIRM_REQUIRED":
                # 只保留尚未执行的步骤；一次确认只授权恢复队列的第一步。
                remaining = sub_list[i:]
                res["pending_decision"] = ({"action": s_act, "param": s_param} if len(remaining) == 1
                                           else {"action": "multi_action", "param": json.dumps(remaining, ensure_ascii=False)})
                skipped = len(sub_list) - i - 1
                if messages:
                    res["message"] = "；".join(messages) + "；" + res.get("message", "")
                if skipped:
                    res["message"] += f"（其后 {skipped} 步未执行）"
                return QueryExecutionResult(res, collector.finish("confirmation_required"))
            if res.get("message"):
                messages.append(res["message"])
            if not res.get("success", False):
                # 保留失败步骤的错误、解释与追踪信息，未执行的尾部不能报成完成。
                skipped = len(sub_list) - i - 1
                message = "；".join(messages)
                if skipped:
                    message += f"（其后 {skipped} 步未执行）"
                return QueryExecutionResult({**res, "action": "multi_action", "error_code": res.get("error_code") or "STEP_FAILED",
                        "failed_action": s_act, "failed_step": i + 1, "completed_steps": i,
                        "skipped_steps": skipped, "message": message,
                        "speech_override": res.get("speech_override") or "有一步没完成，后续步骤已停止。"}, collector.finish("failed"))
            time.sleep(0.3)
        return QueryExecutionResult({
            "success": True,
            "action": "multi_action",
            "message": "；".join(messages),
            "speech_override": "都办好了。",
        }, collector.finish("completed"))

def cancel_reply(decision):
    """主人说“取消”：挂起的是复合动作时，说清楚后面几步也一起不做了。"""
    steps = []
    if isinstance(decision, dict) and decision.get("action") == "multi_action":
        try:
            steps = json.loads(decision.get("param") or "[]")
        except (TypeError, ValueError):
            steps = []
    rest = len(steps) - 1 if isinstance(steps, list) else 0
    return f"好，已取消，后面 {rest} 步也不做了。" if rest > 0 else "好，已取消。"


# 模块导入时自动后台静默预热 OCR 引擎
try:
    threading.Thread(target=CanaanOSExecutor._warmup_ocr_engine, daemon=True).start()
except Exception:
    pass

if __name__ == "__main__":
    print("=== 测试 Universal Canaan OS Executor ===")
    print("1. 动态应用发现扫描:", len(CanaanOSExecutor.scan_system_applications()), "款应用已索引")
    for app in ["cursor", "obsidian", "wps", "聊天软件", "blender", "trae", "vscode"]:
        p = CanaanOSExecutor.find_application(app)
        print(f"   [{app}] -> {p}")
    print("2. 全盘文件检索测试:", CanaanOSExecutor.search_local_files("指南", limit=2))
    print("3. 系统状态检测:", CanaanOSExecutor.execute("system_status")["message"])

# 聊天软件桌面操作只在自用版里（canaan_chatapp_executor.py）；公开版没有这个文件，CanaanOSExecutor 就不带这些方法
import importlib.util as _importlib_util
if _importlib_util.find_spec("canaan_chatapp_executor") is not None:
    import canaan_chatapp_executor as _chatapp_executor
    _chatapp_executor.install(CanaanOSExecutor)
