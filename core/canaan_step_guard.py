# -*- coding: utf-8 -*-
"""自动操作电脑时的逐步核对（防屏幕注入）。canaan_computer_use 和 canaan_ufo_agent 每一步执行前都过这里。

三道限制：
1. 窗口锁定：只在任务开始时的窗口、或计划里自己打开 / 切换到的窗口里操作；前台窗口被别的东西换掉就暂停并告诉主人。
2. 屏幕指令检测：窗口标题、控件文字里出现像指令的话（“忽略之前的要求”“授权你”“输入密码”“转账到”……）就停下，
   不照着屏幕上的字做事，并把看到的那句（截短）告诉主人。
3. 步骤白名单：输入密码 / 验证码一律不做；点“发送 / 支付 / 删除 / 授权”这类按钮、打开新程序或新网址，
   都要主人原话里说过同类的词才行。
都是纯函数或小对象，方便单独测试。
"""
import re

INJECTION_RE = re.compile(
    r"(忽略|无视|忘掉|忘记)(之前|以上|前面|先前|所有|上面)的?(所有)?(指令|要求|说明|设定|规则|提示)"
    r"|ignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+(instructions|prompts?|rules)"
    r"|disregard\s+(all\s+)?(previous|prior|above)"
    r"|(我|现在)?(已经)?(授权|允许)你[^，,。的]{0,2}(执行|操作|转账|支付|付款|删除|发送|输入)"
    r"|you\s+are\s+(now\s+)?authori[sz]ed"
    r"|system\s*prompt|新的(系统)?指令|管理员指令|开发者模式"
    r"|(把|将)(你的|您的|主人的)?(密码|验证码|口令)(发|告诉|填到|输入到)"
    r"|(立即|马上|请)?(转账|汇款|打款)(到|给|至)",
    re.I)

SENSITIVE_CLICK = {
    "send": (re.compile(r"发送|send|发出|提交|submit|post|发布", re.I), re.compile(r"发送|发出|发给|发到|发一|发条|发个|提交|send|发布|回复|reply", re.I)),
    "pay": (re.compile(r"支付|付款|pay|转账|购买|下单|提交订单|结算|checkout|充值", re.I), re.compile(r"支付|付款|pay|转账|购买|买|下单|结算|充值", re.I)),
    "delete": (re.compile(r"删除|delete|移除|清空|卸载|remove|uninstall", re.I), re.compile(r"删除|删掉|删了|delete|移除|清空|卸载|remove", re.I)),
    "grant": (re.compile(r"授权|允许|allow|authori[sz]e|同意|accept|信任", re.I), re.compile(r"授权|允许|allow|同意|accept|信任", re.I)),
}
SECRET_RE = re.compile(r"密码|口令|验证码|校验码|动态码|password|passcode|\bpin\b|otp|verification\s*code|cvv|安全码", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>]+|www\.[^\s\"'<>]+", re.I)
INPUT_ACTIONS = {"click", "left_click", "double_click", "dblclick", "right_click", "rclick", "click_element", "click_button",
                 "drag", "mouse_drag", "scroll", "wheel", "hotkey", "press_keys", "shortcut", "type", "type_text", "input_text"}
RETARGET_ACTIONS = {"launch_app", "open_app", "focus", "focus_window", "activate_window", "wait_window", "sync_window"}


def find_injection(texts):
    """在屏幕文字里找像指令的话。返回第一句（截到 40 字）或 None。"""
    for t in texts or ():
        if not t:
            continue
        m = INJECTION_RE.search(str(t))
        if m:
            s = str(t)
            start = max(0, m.start() - 6)
            return s[start:start + 40]
    return None


INTERACTIVE = {"ButtonControl", "EditControl", "HyperlinkControl", "MenuItemControl", "TabItemControl", "ListItemControl",
               "CheckBoxControl", "RadioButtonControl", "ComboBoxControl", "SplitButtonControl", "TreeItemControl"}


def relevant_texts(elements, target=None, radius=160):
    """窗口智能体要检查的屏幕文字：可点 / 可输入的控件，加上目标控件附近的文字。
    网页正文、文章里讲到“忽略之前的指令”这种（离目标远的纯文本）不算，免得整页都停。"""
    out = []
    tc = getattr(target, "center", None)
    for e in elements or ():
        name = getattr(e, "name", "") or ""
        if not name:
            continue
        near = False
        c = getattr(e, "center", None)
        if tc and c:
            near = abs(c[0] - tc[0]) <= radius and abs(c[1] - tc[1]) <= radius
        if getattr(e, "control_type", "") in INTERACTIVE or near or e is target:
            out.append(name)
    return out


def sensitive_click_ok(label, instruction):
    """点“发送 / 支付 / 删除 / 授权”这类按钮：主人原话里要有同类的词。返回 (是否可以, 类别)。"""
    for kind, (label_re, said_re) in SENSITIVE_CLICK.items():
        if label_re.search(label or "") and not said_re.search(instruction or ""):
            return False, kind
    return True, ""


def is_secret_field(*texts, is_password=False):
    return bool(is_password) or any(SECRET_RE.search(str(t or "")) for t in texts)


def new_target_ok(action, params, instruction):
    """打开新程序 / 新网址：名字或网址要出现在主人原话里。"""
    instruction = (instruction or "").lower()
    if action in ("launch_app", "open_app"):
        app = str(params.get("app", "")).strip().lower()
        return bool(app) and app in instruction
    for v in params.values():
        for url in URL_RE.findall(str(v)):
            if url.lower().rstrip("/") not in instruction:
                return False
    return True


LABELS = {"send": "发送", "pay": "支付 / 付款", "delete": "删除", "grant": "授权"}


class StepGuard:
    """一次任务一个。get_window() 返回当前前台窗口 {"hwnd", "title"}。"""

    def __init__(self, instruction, get_window, extra_texts=None):
        self.instruction = instruction or ""
        self.get_window = get_window
        self.extra_texts = extra_texts
        self.allowed = set()
        self.relock()

    def _window(self):
        try:
            return self.get_window() or {}
        except Exception:
            return {}

    def relock(self):
        """计划自己打开 / 切换窗口之后，把新的前台窗口也算进允许范围。"""
        w = self._window()
        if w.get("hwnd"):
            self.allowed.add(w["hwnd"])

    def check(self, action, params):
        """执行这一步之前调用。返回 None（可以做）或 {"error_code", "message"}（停下，告诉主人）。"""
        action = (action or "").lower().strip()
        params = params if isinstance(params, dict) else {}
        w = self._window()
        title = w.get("title", "")
        texts = [title] + list(self.extra_texts() if callable(self.extra_texts) else self.extra_texts or [])
        seen = find_injection(texts)
        if seen and not find_injection([self.instruction]):
            return {"error_code": "SCREEN_INSTRUCTION",
                    "message": f"屏幕上出现了像是指令的文字（“{seen}”），我不照着屏幕上的字做事，先停下，你看一下。"}
        if action in INPUT_ACTIONS and self.allowed and w.get("hwnd") and w["hwnd"] not in self.allowed:
            return {"error_code": "WINDOW_CHANGED",
                    "message": f"前台窗口变成了“{title[:20]}”，不是刚才要操作的窗口，先暂停，要继续的话再说一次。"}
        if action in ("type", "type_text", "input_text") and is_secret_field(title):
            return {"error_code": "SECRET_INPUT", "message": "这一步是在输入密码或验证码的窗口里打字，这个需要你自己来。"}
        if action in ("click_element", "click_button"):
            ok, kind = sensitive_click_ok(str(params.get("name", "")), self.instruction)
            if not ok:
                return {"error_code": "NOT_REQUESTED",
                        "message": f"这一步要点“{str(params.get('name', ''))[:12]}”（{LABELS[kind]}），你没让我做这件事，先停下。"}
        if action in ("launch_app", "open_app", "type", "type_text", "input_text") and not new_target_ok(action, params, self.instruction):
            return {"error_code": "NOT_REQUESTED", "message": "这一步要打开你没提过的程序或网址，先停下。"}
        return None

    def after(self, action):
        if (action or "").lower().strip() in RETARGET_ACTIONS:
            self.relock()
