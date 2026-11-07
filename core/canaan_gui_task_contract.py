"""Pure syntax contract shared by planning and the actual UFO parser.

No desktop imports, OCR initialization, window discovery or model requests.
Syntax eligibility does not prove a target or a successful execution.
"""
import re

_QUOTES = (("“", "”"), ("「", "」"), ("『", "』"), ("‘", "’"), ('"', '"'), ("'", "'"))


def _paired_literal(value):
    for left, right in _QUOTES:
        if not value.startswith(left):
            continue
        depth = 1
        for index in range(1, len(value)):
            char = value[index]
            if left == right:
                if char == right and index < len(value) - 1:
                    backslashes, previous = 0, index - 1
                    while previous >= 0 and value[previous] == "\\":
                        backslashes, previous = backslashes + 1, previous - 1
                    if backslashes % 2:
                        continue
                if left == "'" and index + 1 < len(value) and value[index - 1].isalnum() and value[index + 1].isalnum():
                    continue  # English apostrophes are content, not delimiters.
                if char == right:
                    return index == len(value) - 1
            elif char == left:
                depth += 1
            elif char == right:
                depth -= 1
                if not depth:
                    return index == len(value) - 1
        return False
    return False


def literal_input_value(value):
    """Remove one paired delimiter, preserving the literal's inner whitespace."""
    value = value.strip()
    if _paired_literal(value):
        return value[1:-1]
    return value

APP_MAP = {
        "记事本": ["notepad", "记事本"],
        "计算器": ["calc", "calculator", "计算器"],
        "聊天软件": ["chatapp", "chatapp", "聊天软件"],
        "浏览器": ["chrome", "msedge", "edge"],
        "chrome": ["chrome", "谷歌"],
        "edge": ["msedge", "edge"],
        "设置": ["settings", "设置"],
        "文件资源": ["explorer", "文件资源", "此电脑", "文件管理"],
        "资源管理器": ["explorer", "文件资源"],
        "vscode": ["code", "vscode"],
        "cursor": ["cursor"],
        "trae": ["trae"],
        "obsidian": ["obsidian", "黑曜石"],
        "fishsim": ["fishsim", "fish427", "机器鱼"],
        "visionmaster": ["visionmaster", "海康"]
    }


def parse_atomic_task(command_text, app_map=None):
    """当前驱动只有一次控件操作；没有完整规划的请求不能执行一个猜测的首步。"""
    app_map = APP_MAP if app_map is None else app_map
    if not isinstance(command_text, str):
        return None
    text = re.sub(r"^(?:(?:请|麻烦|帮我|替我|给我)\s*)+", "", command_text.strip())
    app_query = ""
    scoped = re.fullmatch(r"在(.+?)(?:里|中|内)\s*(.+)", text, re.S)
    if scoped:
        scope, operation = scoped.groups()
        aliases = [*app_map, *(alias for values in app_map.values() for alias in values)]
        if scope.lower() in aliases:
            app_query, text = scope, operation
        elif scope in ("当前窗口", "这个窗口", "前台窗口"):
            text = operation
        elif not scope.endswith(("框", "栏", "正文", "文档")):
            return None

    click = re.fullmatch(r"(?:点击|点一下|按一下|选择|点)\s*(?:那个|这个)?\s*(.+?)[。！!]?", text)
    if click:
        label = click.group(1).strip()
        if label and not re.search(r"[,，;；。\n]|然后|接着|并且|再(?:点击|输入|打开|关闭|截图)", label):
            return {"text": text, "app_query": app_query, "action_type": "click", "value": None}
        return None

    # 应用作用域与输入正文分开，正文里的应用名不能改变操作窗口。
    if re.match(r"^(?:输入|写|填入|打入)", text):
        text = "输入框中" + text
    typed = re.fullmatch(r"(?:在|往)?\s*(.+?)\s*(?:里|中|内)?\s*(?:输入|写|填入|打入)\s*(?:一句|一段|点)?(?::|：)?(.*)", text, re.S)
    if not typed:
        return None
    target, value = (part.strip() for part in typed.groups())
    if not target or not value or re.search(r"[,，;；。\n]|然后|接着|打开|启动|点击|关闭", target):
        return None
    quoted = _paired_literal(value)
    if any(value.startswith(left) for left, _ in _QUOTES) and not quoted:
        return None  # A closed literal cannot absorb an independent trailing task.
    if not quoted and re.search(r"写\s*(?:一(?:份|篇|段|个)|份|篇)?[^，,。；;\n]{0,36}(?:周报|报告|文章|文档|总结|方案|大纲|计划书|代码|程序)", text):
        return None  # Generating an artifact needs content, not typing its title.
    if not quoted and re.search(r"(?:[,，;；。]\s*|然后\s*|接着\s*|再\s*)(?:点击|点一下|按一下|打开|关闭|截图|保存|发送|输入)", value):
        return None
    value = literal_input_value(value)
    if not value:
        return None
    return {"text": text, "app_query": app_query, "action_type": "input", "value": value}


def preflight_gui_task(command_text):
    """Return a readable-input syntax contract; never discover or operate UI."""
    operation = parse_atomic_task(command_text)
    if not operation or operation["action_type"] != "input":
        return None
    # Search/address inputs auto-submit in the actual driver. Their results do
    # not have the ValuePattern content postcondition used for normal input.
    target = re.split(r"输入|写|填入|打入", operation["text"], maxsplit=1)[0]
    if re.search(r"搜索|查找|search|query|地址|url|命令行", target, re.I):
        return None
    return operation
