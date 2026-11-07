# -*- coding: utf-8 -*-
"""
================================================================================
   👑 团团 (Tuantuan) · Desk OS 视觉多模态通用智能体 (Microsoft UFO³ Production)
================================================================================
   摒弃一切幼稚的硬编码规则与玩具脚本分支，基于微软开源 Microsoft UFO³ 工业级架构：
   1. HostAgent (全局视窗调度与多阶段规划中枢):
      - 动态前台视窗聚焦与任意进程拓扑追踪 (Universal Window Lifecycle)
      - 无目标自适应：自动继承当前活跃视窗，或精准调配跨软件视窗
      - 真实高精度事实遥测流 (Real-time True Hardware & OS Facts to HUD)
   2. AppAgent (视窗双模态感知与 Set-of-Mark 控件对齐专精智能体):
      - Windows UI Automation (UIA) 深度控件树提取 (0MB 额外显存，毫秒级)
      - RapidOCR 视觉感知补全 (针对自绘界面/游戏/Qt/Electron 文本 Grounding)
      - Set-of-Mark (SoM) 动态编号与语义对齐 (精准计算坐标与原生 Pattern)
      - 动作闭环验证 (Act-Verify Loop: 点击、输入、滚动、快捷键)
   3. 人性化成熟口语输出引擎 (Anti-AI Humanized Speech):
      - 彻底消除机械播报腔、人机味与生硬技术类名拼读
      - 呈现成熟、从容、利落、微带魅惑的真正团团风范
================================================================================
"""

import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import os
import sys
import time
import re
import json
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# DPI-Awareness & Desktop Binding
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

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

import pyperclip
import uiautomation as uia
from PIL import Image, ImageGrab

# 尝试导入 RapidOCR 作为视觉文本感知后备
try:
    from rapidocr_onnxruntime import RapidOCR
    _GLOBAL_OCR = RapidOCR()
except Exception:
    _GLOBAL_OCR = None

from canaan_task_manager import GLOBAL_TASK_MANAGER


# ==============================================================================
#  1. 数据模型与 Set-of-Mark 标记实体
# ==============================================================================

@dataclass
class UIElement:
    """Set-of-Mark (SoM) 视窗可交互标记实体"""
    som_id: int                      # 全局唯一动态编号 [1..N]
    name: str                        # 控件语义名称
    clean_label: str                 # 清洗后的口语化标签 (去除技术后缀与快捷键提示)
    control_type: str                # UIA 控件类型 (Button, Edit, Tab, etc.)
    auto_id: str                     # AutomationId
    class_name: str                  # Win32 ClassName
    rect: Tuple[int, int, int, int]  # (left, top, right, bottom)
    center: Tuple[int, int]          # (center_x, center_y)
    source: str                      # 'uia' | 'visual_ocr'
    confidence: float = 1.0
    raw_control: Any = None

    def to_summary(self) -> str:
        return f"[#{self.som_id}] {self.control_type}: '{self.clean_label}' @ ({self.center[0]},{self.center[1]})"


def clean_control_name(raw_name: str) -> str:
    """清洗控件名称：剔除快捷键提示、技术类名、过长路径，保留核心自然语言标签"""
    if not raw_name:
        return ""
    # 剥离诸如 (F5), (Alt + 向上键), (Ctrl+C) 等后缀
    s = re.sub(r'\(.*?\)|（.*?）', '', raw_name).strip()
    # 剥离引号包围
    s = re.sub(r'[“”"\'「」]', '', s).strip()
    # 如果包含长文件名，仅提取主干
    if len(s) > 20 and ("_" in s or "\\" in s or "/" in s):
        parts = re.split(r'[_\\/\s]', s)
        s = parts[0] if parts else s[:15]
    return s.strip() or raw_name[:15]


# ==============================================================================
#  2. AppAgent: 视窗级双模态感知与智能对齐专精智能体
# ==============================================================================

class CanaanAppAgent:
    """
    通用应用专精智能体：基于 Set-of-Mark 动态索引与 UIA+OCR 双模态闭环执行
    """

    INTERACTIVE_CONTROL_TYPES = {
        "ButtonControl", "EditControl", "MenuItemControl", "TabItemControl",
        "ListItemControl", "CheckBoxControl", "RadioButtonControl", "HyperlinkControl",
        "ComboBoxControl", "SplitButtonControl", "TreeItemControl", "DocumentControl"
    }

    # 深度语义同义词知识库
    SYNONYM_DICT = {
        "刷新": ["refresh", "reload", "更新", "重新加载", "f5"],
        "后退": ["back", "backward", "上一页", "上移", "返回上一级"],
        "前进": ["forward", "下一页", "向前"],
        "搜索": ["search", "find", "查找", "检索", "query", "搜索框"],
        "地址栏": ["address", "url", "location", "网址", "textbox"],
        "确定": ["ok", "confirm", "yes", "提交", "submit", "好的", "保存并确定"],
        "取消": ["cancel", "关闭", "close", "退出", "abort"],
        "保存": ["save", "另存为", "save as", "导出", "写入"],
        "新建": ["new", "create", "添加", "add", "新标签页"],
        "关闭": ["close", "关闭标签页", "退出", "cancel"],
        "全屏": ["fullscreen", "最大化", "maximize"],
        "最小化": ["minimize", "隐藏"],
        "设置": ["settings", "配置", "首选项", "options"],
        "下载": ["download", "下载中心", "保存文件"],
        "蓝牙": ["bluetooth", "无线连接", "设备"]
    }

    def __init__(self, hwnd: int, app_name: str = ""):
        self.hwnd = hwnd
        self.app_name = app_name
        self.root_ctrl = None
        if hwnd and user32.IsWindow(hwnd):
            try:
                self.root_ctrl = uia.ControlFromHandle(hwnd)
            except Exception:
                self.root_ctrl = None

    def get_window_rect(self) -> Optional[Tuple[int, int, int, int]]:
        if not self.hwnd or not user32.IsWindow(self.hwnd):
            return None
        rect = wintypes.RECT()
        if user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
        return None

    def capture_screenshot(self) -> Optional[Image.Image]:
        w_rect = self.get_window_rect()
        if not w_rect:
            return None
        l, t, r, b = w_rect
        if r <= l or b <= t:
            return None
        try:
            return ImageGrab.grab(bbox=(l, t, r, b))
        except Exception as e:
            print(f"[AppAgent] 截图感知警告: {e}")
            return None

    def perceive(self, max_depth: int = 6) -> List[UIElement]:
        """
        Set-of-Mark 全维度感知：
        1. 遍历 UIA 控件树提取所有真实交互节点
        2. 若控件较少，自动启动 RapidOCR 补充自绘文本节点
        3. 分配连续且唯一的动态编号 [1..N]
        """
        discovered: List[UIElement] = []
        som_counter = 1

        # 1. 结构化 UIA 嗅探
        if self.root_ctrl:
            def _traverse(node, depth):
                nonlocal som_counter
                if depth > max_depth or len(discovered) >= 150:
                    return
                try:
                    c_type = node.ControlTypeName
                    name = (node.Name or "").strip()
                    auto_id = (node.AutomationId or "").strip()
                    rect_obj = node.BoundingRectangle

                    if rect_obj:
                        l, t, r, b = rect_obj.left, rect_obj.top, rect_obj.right, rect_obj.bottom
                        w, h = r - l, b - t
                        if w >= 4 and h >= 4 and l > -10000 and t > -10000:
                            if c_type in self.INTERACTIVE_CONTROL_TYPES or name or auto_id:
                                cx = l + w // 2
                                cy = t + h // 2
                                clean = clean_control_name(name)
                                elem = UIElement(
                                    som_id=som_counter,
                                    name=name,
                                    clean_label=clean or auto_id or c_type,
                                    control_type=c_type,
                                    auto_id=auto_id,
                                    class_name=node.ClassName or "",
                                    rect=(l, t, r, b),
                                    center=(cx, cy),
                                    source="uia",
                                    confidence=1.0,
                                    raw_control=node
                                )
                                discovered.append(elem)
                                som_counter += 1

                    for child in node.GetChildren():
                        _traverse(child, depth + 1)
                except Exception:
                    pass

            try:
                _traverse(self.root_ctrl, 0)
            except Exception as e:
                print(f"[AppAgent] UIA 遍历微小异常: {e}")

        # 2. 视觉 OCR 补充 (自绘界面保护)
        if len(discovered) < 8 and _GLOBAL_OCR:
            w_rect = self.get_window_rect()
            if w_rect:
                win_img = self.capture_screenshot()
                if win_img:
                    try:
                        import tempfile
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                            tmp_path = tmp.name
                        win_img.save(tmp_path)
                        ocr_res, _ = _GLOBAL_OCR(tmp_path)
                        try: os.remove(tmp_path)
                        except Exception: pass

                        if ocr_res:
                            wl, wt, _, _ = w_rect
                            for box, text, score in ocr_res:
                                text_clean = text.strip()
                                if not text_clean or score < 0.65:
                                    continue
                                xs = [p[0] for p in box]
                                ys = [p[1] for p in box]
                                el = int(wl + min(xs))
                                et = int(wt + min(ys))
                                er = int(wl + max(xs))
                                eb = int(wt + max(ys))
                                cx = (el + er) // 2
                                cy = (et + eb) // 2
                                clean = clean_control_name(text_clean)

                                elem = UIElement(
                                    som_id=som_counter,
                                    name=text_clean,
                                    clean_label=clean,
                                    control_type="VisualText",
                                    auto_id="",
                                    class_name="OCR_Grounding",
                                    rect=(el, et, er, eb),
                                    center=(cx, cy),
                                    source="visual_ocr",
                                    confidence=float(score)
                                )
                                discovered.append(elem)
                                som_counter += 1
                    except Exception as e:
                        print(f"[AppAgent] OCR 视觉感知后备异常: {e}")

        return discovered

    def ground_intent(self, user_intent: str, elements: List[UIElement]) -> Tuple[Optional[UIElement], str, Optional[str]]:
        """
        自主目标与动作对齐：
        返回: (命中控件, 动作类型, 待输入参数)
        动作类型包括: 'click' | 'input' | 'double_click' | 'hotkey'
        """
        clean_intent = user_intent.strip()
        intent_lower = clean_intent.lower()

        # 1. 判定动作动词与输入文本
        action_type = "click"
        input_value = None

        m_input = re.search(r'(?:在|往)?\s*(.+?)\s*(?:里|中|内)?\s*(?:输入|写|填入|打入)\s*(?:一句|一段|点)?(?::|：)?(.*)', clean_intent)
        if m_input:
            action_type = "input"
            target_desc = m_input.group(1).strip()
            from canaan_gui_task_contract import literal_input_value
            input_value = literal_input_value(m_input.group(2))
            if not input_value and not target_desc:
                input_value = clean_intent
        else:
            # 提取点击目标名词
            m_click = re.search(r'(?:点击|点一下|按一下|打开|选择|切换到|点)\s*(?:那个|这个)?\s*(.+?)(?:按钮|图标|选项|tab|标签页|标签)?$', clean_intent)
            target_desc = m_click.group(1).strip() if m_click else clean_intent

        target_lower = target_desc.lower().strip()
        editable_types = {"EditControl", "DocumentControl"}
        generic_input = target_lower in {"输入框", "编辑框", "文本框", "正文", "文档"}
        if action_type == "input" and generic_input:
            if sum(elem.control_type in editable_types for elem in elements) != 1:
                return None, action_type, input_value

        # 2. 扩充同义词搜索池
        search_terms = [target_lower]
        for key, syns in self.SYNONYM_DICT.items():
            if key in target_lower:
                search_terms.extend(syns)
            for s in syns:
                if s in target_lower and key not in search_terms:
                    search_terms.append(key)

        # 3. 对所有已标记的 SoM 控件进行多维相关性打分
        best_elem = None
        best_score = -1.0

        for elem in elements:
            if action_type == "input" and elem.control_type not in editable_types:
                continue
            score = 0.0
            elem_clean = elem.clean_label.lower()
            elem_raw = elem.name.lower()
            elem_id = elem.auto_id.lower()

            # A. 精确匹配
            if any(t == elem_clean or t == elem_raw for t in search_terms):
                score += 100.0
            # B. 词根包含匹配
            elif any(t in elem_clean or t in elem_raw for t in search_terms):
                score += 65.0
            elif any(elem_clean in t for t in search_terms if len(elem_clean) >= 2):
                score += 55.0
            # C. AutomationId 匹配
            elif any(t in elem_id for t in search_terms):
                score += 40.0

            # D. 动作类型匹配加权
            if action_type == "input":
                if score == 0.0 and not generic_input:
                    continue  # 具名字段不能仅凭“可输入”类型命中。
                score += 35.0
            elif action_type == "click":
                if elem.control_type in ["ButtonControl", "TabItemControl", "MenuItemControl", "HyperlinkControl"]:
                    score += 20.0

            if score > best_score and score >= 30.0:
                best_score = score
                best_elem = elem
            elif score == best_score and score >= 30.0:
                best_elem = None  # 同分目标无法区分，不能凭遍历顺序动手。

        return best_elem, action_type, input_value

    def execute_action(self, elem: UIElement, action_type: str, value: Optional[str] = None) -> bool:
        """执行动作：UIA 原生模式优先，物理 Win32 与安全剪贴板兜底"""
        cx, cy = elem.center

        if action_type == "click":
            # 尝试 UIA 原生 Invoke
            if elem.raw_control:
                try:
                    pat = getattr(elem.raw_control, "GetInvokePattern", None)
                    if pat and pat():
                        pat().Invoke()
                        time.sleep(0.15)
                        return True
                except Exception:
                    pass
            # 物理点击
            return self._physical_click(cx, cy)

        elif action_type == "input":
            text = value or ""
            val_set = False
            # 尝试 UIA 原生 ValuePattern
            if elem.raw_control:
                try:
                    val_pat = getattr(elem.raw_control, "GetValuePattern", None)
                    if val_pat and val_pat():
                        val_pat().SetValue(text)
                        val_set = True
                except Exception:
                    pass

            if not val_set:
                # 物理聚焦并注入剪贴板
                if not self._physical_click(cx, cy):
                    return False
                time.sleep(0.08)
                self._send_hotkey("ctrl", "a")
                time.sleep(0.04)

                try:
                    old = pyperclip.paste()
                except Exception:
                    old = ""
                try:
                    pyperclip.copy(text)
                    time.sleep(0.03)
                    self._send_hotkey("ctrl", "v")
                    time.sleep(0.06)
                finally:
                    if old:
                        try: pyperclip.copy(old)
                        except Exception: pass

            # 针对搜索框、地址栏、命令行自动触发回车检索
            if any(w in elem.clean_label.lower() for w in ["搜索", "查找", "search", "query", "地址", "url"]):
                time.sleep(0.08)
                user32.keybd_event(0x0D, 0, 0, 0)
                time.sleep(0.04)
                user32.keybd_event(0x0D, 0, 2, 0)

            return True

        return False

    def verify_action(self, elem: UIElement, action_type: str, value: Optional[str] = None) -> dict:
        """只报告可读回的结果；动作调用成功不能证明通用界面任务完成。"""
        if action_type != "input":
            return {"verified": False, "reason": "该控件操作没有可核对的后置条件"}
        if any(word in elem.clean_label.lower() for word in
               ["搜索", "查找", "search", "query", "地址", "url"]):
            return {"verified": False, "reason": "输入后已提交，尚未核验检索或导航结果"}
        try:
            getter = getattr(elem.raw_control, "GetValuePattern", None)
            pattern = getter() if getter else None
            if pattern is not None and pattern.Value == (value or ""):
                return {"verified": True, "gate_name": "UIA_VALUE_READBACK",
                        "reason": "目标输入控件的内容与请求一致"}
        except Exception:
            pass
        return {"verified": False, "reason": "无法从目标输入控件读回一致的内容"}

    @staticmethod
    def _physical_click(x: int, y: int) -> bool:
        try:
            user32.SetCursorPos(x, y)
            time.sleep(0.03)
            user32.mouse_event(0x0002, 0, 0, 0, 0) # LEFTDOWN
            time.sleep(0.03)
            user32.mouse_event(0x0004, 0, 0, 0, 0) # LEFTUP
            time.sleep(0.12)
            return True
        except Exception:
            return False

    @staticmethod
    def _send_hotkey(*keys):
        vk_map = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "a": 0x41, "c": 0x43, "v": 0x56}
        codes = [vk_map.get(k.lower(), ord(k.upper())) for k in keys]
        for vk in codes: user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        for vk in reversed(codes): user32.keybd_event(vk, 0, 2, 0)


# ==============================================================================
#  3. HostAgent: 全局视窗主管控者与自适应调度中枢
# ==============================================================================

class CanaanHostAgent:
    """
    负责全桌面真实视窗发现、激活、当前视窗自适应探测与 AppAgent 闭环协同
    """

    from canaan_gui_task_contract import APP_MAP

    def __init__(self):
        self._app_agents: Dict[int, CanaanAppAgent] = {}

    def get_desktop_windows(self) -> List[dict]:
        """枚举当前桌面所有顶层可见窗口 (严格过滤系统托盘与不可视背景代理)"""
        windows = []

        def enum_cb(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return 1
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.strip()

                cls_buff = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls_buff, 256)
                cls_name = cls_buff.value.strip()

                if (title and cls_name not in ["Progman", "Shell_TrayWnd", "TopLevelWindowForOverflowXamlIsland", "Windows.UI.Core.CoreWindow"]
                    and not cls_name.startswith("HwndWrapper")):
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    if w > 40 and h > 40:
                        windows.append({
                            "hwnd": hwnd,
                            "title": title,
                            "class": cls_name,
                            "rect": (rect.left, rect.top, rect.right, rect.bottom),
                            "width": w,
                            "height": h
                        })
            return 1

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
        user32.EnumDesktopWindows(hd_init, WNDENUMPROC(enum_cb), 0)
        return windows

    def get_active_window(self) -> Optional[dict]:
        """获取当前用户正在操作的前台焦点窗口"""
        fore_hwnd = user32.GetForegroundWindow()
        if not fore_hwnd or not user32.IsWindow(fore_hwnd):
            return None
        length = user32.GetWindowTextLengthW(fore_hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(fore_hwnd, buff, length + 1)
        cls_buff = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(fore_hwnd, cls_buff, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(fore_hwnd, ctypes.byref(rect))
        return {
            "hwnd": fore_hwnd,
            "title": buff.value.strip(),
            "class": cls_buff.value.strip(),
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top
        }

    def find_target_window(self, query_text: str) -> Optional[dict]:
        """
        视窗自适应定位：
        1. 若指令中明确提到了某应用名，优先查找并匹配对应视窗
        2. 若指令提到了某应用但尚未启动，后台自动拉起并绑定
        3. 若指令未指定应用名，自适应继承当前前台活跃视窗
        """
        wins = self.get_desktop_windows()
        q_lower = query_text.strip().lower()

        # 输入正文不进入此查询；显式应用必须由进程身份绑定。
        target_app_key = q_lower if q_lower in self.APP_MAP else None
        if target_app_key is None:
            # 通称的别名池包含品牌；先查具体应用，避免 Chrome 被扩宽成任意浏览器。
            for app_key in sorted(self.APP_MAP, key=lambda key: key == "浏览器"):
                if q_lower in self.APP_MAP[app_key]:
                    target_app_key = app_key
                    break

        if target_app_key:
            from canaan_action_framework.actions.app_actions import _app_window_backend, _focus_process_names
            identity_name = {"文件资源": "资源管理器", "设置": "systemsettings.exe"}.get(target_app_key, target_app_key)
            names = _focus_process_names(identity_name)
            if not names:
                return None

            def matching_windows(visible):
                records = _app_window_backend().list_windows()
                handles = {record["hwnd"] for record in records
                           if names.intersection(identity[1] for identity in record["processes"])}
                return [window for window in visible if window["hwnd"] in handles]

            matches = matching_windows(wins)
            if not matches:
                from canaan_os_executor import CanaanOSExecutor
                CanaanOSExecutor.execute("open_app", target_app_key)
                time.sleep(1.2)
                matches = matching_windows(self.get_desktop_windows())
            if len(matches) == 1:
                return matches[0]
            if matches:
                active = self.get_active_window() or {}
                return next((window for window in matches if window["hwnd"] == active.get("hwnd")), None)
            # 指定应用未能绑定，不能把其他前台窗口当成目标。
            return None

        # 3. 未匹配到明确软件名称时，自适应对齐当前前台视窗
        active = self.get_active_window()
        if active and active["width"] > 100 and not active["class"].startswith("HwndWrapper"):
            return active

        return None

    def activate_window(self, hwnd: int) -> bool:
        """安全激活并提升窗口至前台焦点"""
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        user32.ShowWindow(hwnd, 9) # SW_RESTORE
        time.sleep(0.06)

        fore = user32.GetForegroundWindow()
        cur_t = ctypes.windll.kernel32.GetCurrentThreadId()
        fore_t = user32.GetWindowThreadProcessId(fore, 0)
        user32.AttachThreadInput(cur_t, fore_t, True)

        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.SetFocus(hwnd)

        user32.AttachThreadInput(cur_t, fore_t, False)
        time.sleep(0.08)
        return user32.GetForegroundWindow() == hwnd

    def get_app_agent(self, hwnd: int, app_name: str = "") -> CanaanAppAgent:
        if hwnd not in self._app_agents:
            self._app_agents[hwnd] = CanaanAppAgent(hwnd, app_name)
        return self._app_agents[hwnd]

    def check_step(self, hwnd, title, command_text, elements, target, action_type, value) -> Optional[str]:
        """动手前核对（canaan_step_guard）：前台还是锁定的窗口、屏幕文字里没有冒充指令的话、
        不往密码 / 验证码框里打字、敏感按钮和网址是主人说过的。返回停下的原因，可以做时返回 None。"""
        from canaan_step_guard import LABELS, find_injection, is_secret_field, new_target_ok, relevant_texts, sensitive_click_ok
        active = self.get_active_window() or {}

        def pid(h):
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            return p.value
        # 同一个程序弹出的对话框也算锁定的窗口
        if active.get("hwnd") and active["hwnd"] != hwnd and pid(active["hwnd"]) != pid(hwnd):
            return f"前台窗口变成了“{str(active.get('title', ''))[:20]}”，不是要操作的窗口，先暂停。"
        seen = find_injection([title] + relevant_texts(elements[:300], target))
        if seen and not find_injection([command_text]):
            return f"屏幕上出现了像是指令的文字（“{seen}”），我不照着屏幕上的字做事，先停下，你看一下。"
        if action_type == "input":
            is_pw = False
            try:
                is_pw = bool(target.raw_control and target.raw_control.IsPassword)
            except Exception:
                pass
            if is_secret_field(target.name, target.auto_id, is_password=is_pw):
                return "这是密码或验证码输入框，这个需要你自己来。"
            if not new_target_ok("input", {"text": value or ""}, command_text):
                return "要输入的网址你没提过，先停下。"
        else:
            ok, kind = sensitive_click_ok(target.clean_label or target.name, command_text)
            if not ok:
                return f"这一步要点“{(target.clean_label or target.name)[:12]}”（{LABELS[kind]}），你没让我做这件事，先停下。"
        return None

    @classmethod
    def _parse_atomic_task(cls, command_text: str) -> Optional[dict]:
        from canaan_gui_task_contract import parse_atomic_task
        return parse_atomic_task(command_text, cls.APP_MAP)

    def execute_ufo_task(self, command_text: str, task_id: Optional[str] = None) -> dict:
        """执行受支持的单步操作，只有可观察的后置条件成立才报告任务完成。"""
        detail = {"verified": False, "attempted": False, "phase": "plan"}

        def fail(code, message, reply=None, **extra):
            GLOBAL_TASK_MANAGER.notify_finish(task_id, False, message)
            return {"success": False, "error_code": code, "message": message,
                    "reply": reply or message, "detail": dict(detail), **extra}

        if not task_id:
            task_id, is_dup = GLOBAL_TASK_MANAGER.check_and_create_task(command_text)
            if is_dup:
                return {"success": False, "error_code": "DUPLICATE_TASK", "message": "重复指令已拦截",
                        "reply": "刚才已经听到啦，姐姐正在办呢。", "detail": detail}
        try:
            operation = self._parse_atomic_task(command_text)
            if not operation:
                return fail("NO_PLAN", "这个界面任务还没有完整、可核验的执行方案，尚未操作。")
            detail["phase"] = "window"
            win_info = self.find_target_window(operation["app_query"])
            if not win_info:
                return fail("WINDOW_NOT_FOUND", "未找到要操作的窗口，这次还没执行。")
            hwnd = win_info["hwnd"]
            title = win_info["title"] or win_info["class"]
            short_title = title[:18]
            if not self.activate_window(hwnd):
                return fail("WINDOW_ACTIVATION_FAILED", "目标窗口没有成功切到前台，这次还没执行。")
            GLOBAL_TASK_MANAGER.notify_step(task_id, 1, 3, f"已绑定窗口【{short_title}】")

            detail["phase"] = "perceive"
            app_agent = self.get_app_agent(hwnd, short_title)
            elements = app_agent.perceive()
            GLOBAL_TASK_MANAGER.notify_step(task_id, 2, 3, f"识别到 {len(elements)} 个界面控件")
            detail["phase"] = "ground"
            target, action_type, value = app_agent.ground_intent(operation["text"], elements)
            if target is None:
                return fail("TARGET_NOT_FOUND", "窗口已找到，但没有定位到要操作的控件，任务尚未执行。")
            if action_type != operation["action_type"] or value != operation["value"]:
                return fail("ACTION_MISMATCH", "识别出的控件操作与请求不一致，这次还没执行。")
            detail["phase"] = "guard"
            stop = self.check_step(hwnd, title, command_text, elements, target, action_type, value)
            if stop:
                return fail("SECURITY_BLOCKED", stop, paused=True)
            detail.update(phase="execute", attempted=True)
            if not app_agent.execute_action(target, action_type, value):
                return fail("ACTION_FAILED", "控件操作没有执行成功，任务尚未完成。")
            detail["phase"] = "verify"
            try:
                evidence = app_agent.verify_action(target, action_type, value)
            except Exception:
                evidence = {"verified": False, "reason": "操作后无法读取核验结果"}
            if not isinstance(evidence, dict) or evidence.get("verified") is not True:
                detail["reason"] = (evidence.get("reason") if isinstance(evidence, dict) else None) or "缺少后置条件证据"
                return fail("TASK_UNVERIFIED", "已尝试控件操作，但结果尚未核验，任务还不能算完成。")
            detail.update(evidence)
            message = f"输入控件【{target.clean_label}】的内容已核验"
            GLOBAL_TASK_MANAGER.notify_step(task_id, 3, 3, message)
            GLOBAL_TASK_MANAGER.notify_finish(task_id, True, message)
            return {"success": True, "message": message, "reply": "内容已写入，并核对一致。", "detail": detail}
        except Exception as exc:
            detail["exception_type"] = type(exc).__name__
            return fail("UFO_EXECUTION_ERROR", "界面操作过程中出现异常，任务尚未完成。")


# 全局单例 HostAgent
GLOBAL_HOST_AGENT = CanaanHostAgent()

if __name__ == "__main__":
    print("[*] 正在测试生产级 Canaan UFO³ 视觉多模态通用智能体...")
    wins = GLOBAL_HOST_AGENT.get_desktop_windows()
    print(f"[+] 当前桌面活跃应用视窗: {len(wins)} 个")
    for w in wins[:5]:
        print(f"  - HWND {w['hwnd']}: {w['title'][:32]} ({w['class']})")
