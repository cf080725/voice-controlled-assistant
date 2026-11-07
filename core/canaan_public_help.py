"""Small public Windows help references; no I/O, personal data or action authority."""
import re

_DEVICE = r"(?:(?:windows(?:\s*(?:10|11))?|win(?:10|11)|电脑|计算机|笔记本|这台电脑|我的电脑|pc)(?:上|里|的)?)?"
_PREFIX = r"(?:(?:请问|请告诉我|告诉我|我想知道|想问一下|麻烦告诉我)[，,\s]*)?"
_CAPTURE = r"(?:截图|截屏)"
_HELP = re.compile(
    _PREFIX + _DEVICE + r"\s*(?:"
    + _CAPTURE + r"(?:的)?(?:快捷键|组合键|按键)(?:是)?(?:哪个|什么|哪几个|哪些)(?:键)?"
    + r"|(?:怎么|如何)(?:用(?:快捷键|键盘))?" + _CAPTURE
    + r"|" + _CAPTURE + r"(?:要|该|应该)?(?:按|用)(?:哪个|什么|哪些|哪几个)(?:键|快捷键|组合键)"
    + r"|(?:怎么|如何)(?:用(?:快捷键|键盘))?(?:截取|截)(?:屏幕的)?(?:一部分|局部|区域)"
    + r")[？?。！!\s]*", re.I,
)

_SCREENSHOT_HELP = (
    '【电脑截图帮助】\n'
    'Windows 10/11 电脑上，同时按 Win + Shift + S 打开截图工具，选择需要的屏幕区域即可截图。\n'
    '来源：https://support.microsoft.com/en-us/windows/apps/use-snipping-tool-to-capture-screenshots\n'
    '核对日期：2026-10-31。'
)


def context_for(user_text):
    """Return bounded public facts only for a complete desktop screenshot-help question."""
    if not isinstance(user_text, str) or len(user_text) > 160:
        return ""
    text = re.sub(r"[ \t]+", " ", user_text.strip())
    return _SCREENSHOT_HELP if _HELP.fullmatch(text) else ""


_SCREENSHOT_REPLY = "[confident] 电脑截图帮助：Windows 10 或 11 上，按 Win + Shift + S，选择区域即可截图。"


def answer_for(user_text):
    """Use a verified public procedure only for the same complete help question."""
    return _SCREENSHOT_REPLY if context_for(user_text) else ""
