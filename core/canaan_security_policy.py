# -*- coding: utf-8 -*-
"""
PowerShell / CMD 命令安全分级（用于大模型生成的 run_cmd）。

三档：
    READONLY  只读查询，直接执行（Get-*、ipconfig、ping、tasklist ……）
    CONFIRM   可能改动系统，需要用户语音确认后才执行（默认档）
    BLOCKED   高危破坏性操作，即使用户确认也拒绝执行（格式化、diskpart、删卷影 ……）

判定方式：
    1. 先匹配硬拦截规则（对原始命令，大小写不敏感）
    2. 去掉引号内的字符串字面量后，检查重定向、调用运算符、静态/实例方法调用
    3. 按 | ; & && || { } ( $( 换行 切分，取每一段的第一个词（命令位置），
       全部命中只读白名单才判为 READONLY
来自确定性规则（source="rule"，如 taskmgr、start ms-settings:、Clear-RecycleBin）的命令
是代码里写死的，不经过本模块。
"""
import re
from dataclasses import dataclass

READONLY = "readonly"
CONFIRM = "confirm"
BLOCKED = "blocked"


@dataclass
class PolicyDecision:
    level: str
    reason: str = ""

    @property
    def allowed_without_confirm(self):
        return self.level == READONLY


# ---- 硬拦截：确认了也不执行 ----
_BLOCK_RULES = [
    (r"(?<![\w-])format(\.com)?\s+[a-z]:", "格式化磁盘"),
    (r"\bformat-volume\b", "格式化卷"),
    (r"\bdiskpart\b", "diskpart 磁盘分区"),
    (r"\bbcdedit\b", "修改启动配置"),
    (r"\b(clear|initialize)-disk\b", "清空/初始化磁盘"),
    (r"\bvssadmin\b.*\bdelete\b", "删除卷影副本"),
    (r"\bwmic\b.*\bshadowcopy\b.*\bdelete\b", "删除卷影副本"),
    (r"\bwbadmin\b.*\bdelete\b", "删除系统备份"),
    (r"\bcipher\b.*/w", "擦除磁盘空闲空间"),
    (r"\breg(\.exe)?\s+delete\s+(hklm|hkey_local_machine)", "删除 HKLM 注册表"),
    (r"\bremove-item\b.*\b(hklm|hkey_local_machine):", "删除 HKLM 注册表"),
    (r"\bremove-itemproperty\b.*\bhklm:", "删除 HKLM 注册表"),
    (r"-(e|ec|en|enc|enco|encod|encode|encoded|encodedc|encodedcommand)\s+[a-z0-9+/=]{16,}", "Base64 编码命令"),
    (r"\bfrombase64string\b", "Base64 解码执行"),
    (r"\b(rd|rmdir)\s+(/s|/q|\s)+\s*[a-z]:\\?\s*($|[;&|])", "删除整个驱动器"),
    (r"\bdel\s+(/[fsq]\s+)+[a-z]:\\?\*?\s*($|[;&|])", "删除整个驱动器文件"),
    (r"\bset-executionpolicy\b\s+(unrestricted|bypass)\b.*-scope\s+localmachine", "放开全机脚本策略"),
    (r"\bnet\s+user\s+\S+\s+/delete\b", "删除系统账户"),
    (r"\bvssadmin\b.*\bresize\b", "压缩卷影存储"),
]


def _is_drive_root_recursive_delete(cmd):
    """Remove-Item / rm / del / ri ... -Recurse 指向盘符根目录、系统目录或用户目录根。"""
    if not re.search(r"\b(remove-item|ri|rm|del|erase|rd|rmdir)\b", cmd, re.I):
        return False
    if not re.search(r"-r(ecurse)?\b|/s\b", cmd, re.I):
        return False
    roots = r"""['"]?([a-z]:\\?\*?|[a-z]:\\windows\\?\*?|[a-z]:\\users\\?\*?|\$env:(systemroot|windir|userprofile)\\?\*?|~\\?\*?|\\\*?)['"]?(\s|$|;|\|)"""
    return bool(re.search(r"(remove-item|ri|rm|del|erase|rd|rmdir)\b[^|;]*?\s" + roots, cmd, re.I))


def _is_root_listing_piped_to_delete(cmd):
    """Get-ChildItem C:\\ -Recurse | Remove-Item ... 这类先列出整个盘/系统目录再管道删除的写法。"""
    m = re.search(r"\b(get-childitem|gci|ls|dir)\b([^|;]*)\|.*\b(remove-item|ri|rm|del|erase)\b", cmd, re.I)
    if not m:
        return False
    target = m.group(2)
    return bool(re.search(r"""(^|\s|-path\s+|-literalpath\s+)['"]?([a-z]:\\?\*?|[a-z]:\\(windows|users|program files[^'"]*)\\?\*?|\$env:(systemroot|windir|userprofile)\\?|~\\?)['"]?(\s|$)""", target, re.I))


# ---- 只读白名单（命令位置） ----
_READONLY_PREFIXES = ("get-",)
_READONLY_COMMANDS = {
    # PowerShell
    "test-connection", "test-netconnection", "test-path", "resolve-dnsname",
    "select-object", "select", "where-object", "where", "?", "sort-object", "sort",
    "measure-object", "measure", "format-table", "ft", "format-list", "fl", "format-wide", "fw",
    "group-object", "group", "out-string", "out-host", "convertto-json", "convertto-csv",
    "foreach-object", "foreach", "%", "write-output", "echo", "select-string", "sls",
    "compare-object", "tee-object", "findstr", "findstr.exe",
    # 常用别名
    "gci", "ls", "dir", "gps", "ps", "gsv", "gcm", "gc", "cat", "type", "gi", "gp", "gwmi", "gcim",
    "pwd", "gl", "gdr", "gv", "gal", "date",
    # 外部只读程序
    "ipconfig", "ping", "hostname", "whoami", "systeminfo", "tasklist", "nslookup", "netstat",
    "tracert", "pathping", "getmac", "ver", "arp", "route", "nbtstat", "driverquery", "where.exe",
    "ipconfig.exe", "ping.exe", "hostname.exe", "whoami.exe", "systeminfo.exe", "tasklist.exe",
    "nslookup.exe", "netstat.exe", "tracert.exe", "getmac.exe",
}
# 这些外部程序带特定参数时会改系统
_READONLY_ARG_EXCLUSIONS = {
    "ipconfig": r"/(release|renew|flushdns|registerdns|setclassid)",
    "ipconfig.exe": r"/(release|renew|flushdns|registerdns|setclassid)",
    "route": r"\b(add|delete|change|-f)\b",
    "arp": r"\s-(d|s)\b",
    "tee-object": r".",  # 会写文件
}
# 允许的方法调用（纯计算/格式化）
_SAFE_METHODS = {
    "tostring", "trim", "trimend", "trimstart", "substring", "toupper", "tolower", "split",
    "replace", "contains", "startswith", "endswith", "gettype", "padleft", "padright",
    "indexof", "round", "floor", "ceiling", "abs", "max", "min", "truncate", "join", "format",
    "getvalue", "equals", "add_days", "adddays", "addhours", "toshortdatestring", "tolongdatestring",
}
_SAFE_STATIC_METHODS = _SAFE_METHODS | {
    # DateTime / Math / String / Convert / Environment 里的纯读取或纯计算
    "now", "utcnow", "today", "parse", "parseexact", "fromfiletime", "fromfiletimeutc",
    "getenvironmentvariable", "expandenvironmentvariables", "newline", "machinename", "username",
    "concat", "isnullorempty", "isnullorwhitespace", "toint16", "toint32", "toint64",
    "todouble", "todecimal", "tostring", "tochar", "tobase64string",
    "pow", "sqrt", "sign", "log", "log10", "exp", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "ceiling", "floor", "abs", "max", "min", "round", "truncate",
}
_SAFE_STATIC_TYPES = {"math", "system.math", "string", "system.string", "datetime", "system.datetime",
                      "environment", "system.environment", "int", "double", "convert"}


def _outside_single_quotes(cmd, needle):
    """needle 是否出现在单引号字面量之外。PowerShell 单引号里不展开变量/子表达式。"""
    in_single = False
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "'":
            if in_single and i + 1 < n and cmd[i + 1] == "'":
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue
        if not in_single and cmd.startswith(needle, i):
            return True
        i += 1
    return False


def has_dynamic_structure(cmd):
    """命令里是否有无法静态判定的结构：子表达式、反引号、here-string、--%、变量赋值。

    这些结构既可能执行任意命令，也可能把用户输入拼进命令；只读白名单不能直接放行。
    单引号里的内容不展开，所以不算。
    """
    raw = cmd or ""
    if _outside_single_quotes(raw, "$("):
        return True
    if _outside_single_quotes(raw, "`"):
        return True
    if "--%" in raw:
        return True
    if re.search(r'(?m)@["\']\s*$', raw):
        return True
    # 命令位置上的变量赋值：$x = Remove-Item ...（$ 开头会被当成表达式跳过）
    if re.search(r'(?m)(^|[;|&(){}\n])\s*\$(\{[^}]+\}|[\w:]+)\s*=', raw):
        return True
    if re.search(r'(?i)\b(invoke-expression|iex|add-type|new-object)\b', raw):
        return True
    if re.search(r'(?m)(^|[;|&(){}\n])\s*[&.]\s*\{', raw):
        return True
    return False


def has_unsafe_static_call(cmd):
    """是否有不在安全白名单里的 .NET 静态方法调用（如 [Environment]::Exit）。"""
    s = _strip_strings((cmd or "").lower())
    for m in re.finditer(r"\[([\w.]+)\]::(\w+)", s):
        if m.group(1) not in _SAFE_STATIC_TYPES or m.group(2).lower() not in _SAFE_STATIC_METHODS:
            return True
    return False


def _strip_strings(cmd):
    """把引号内的字面量替换成占位，避免字符串里的 | ; > 干扰切分。"""
    out = re.sub(r"'(?:[^']|'')*'", "'S'", cmd)
    out = re.sub(r'"(?:[^"`]|`.)*"', '"S"', out)
    return out


def _command_heads(stripped):
    """返回 [(分隔符, 命令位置的第一个词, 整段)]。"""
    segs = re.split(r"(\|\||&&|\$\(|@\(|[|;&{}()\n])", stripped)
    heads = []
    sep = "^"
    for s in segs:
        if s in ("||", "&&", "$(", "@(", "|", ";", "&", "{", "}", "(", ")", "\n"):
            sep = s
            continue
        seg = s.strip()
        if not seg:
            continue
        head = seg.split()[0]
        heads.append((sep, head, seg))
    return heads


def classify_command(cmd):
    """对一条 PowerShell/CMD 命令分级，返回 PolicyDecision。"""
    raw = (cmd or "").strip()
    if not raw:
        return PolicyDecision(BLOCKED, "空命令")
    low = raw.lower()

    for pat, why in _BLOCK_RULES:
        if re.search(pat, low, re.I):
            return PolicyDecision(BLOCKED, why)
    if _is_drive_root_recursive_delete(low):
        return PolicyDecision(BLOCKED, "递归删除盘符根目录或系统目录")
    if _is_root_listing_piped_to_delete(low):
        return PolicyDecision(BLOCKED, "枚举盘符根目录/系统目录后批量删除")
    if has_dynamic_structure(raw):
        return PolicyDecision(CONFIRM, "包含子表达式、反引号、here-string、--% 或变量赋值，无法静态判定")

    s = _strip_strings(low)
    s = re.sub(r"\d?>>?\s*\$null", " ", s)  # 丢弃输出到 $null 不写文件
    if re.search(r"(?<![-\w])\d?>{1,2}|\*>", s):
        return PolicyDecision(CONFIRM, "包含输出重定向（会写文件）")
    if "`" in s:
        return PolicyDecision(CONFIRM, "包含反引号转义，无法静态判定")

    for m in re.finditer(r"\[([\w.]+)\]::(\w+)", s):
        if m.group(1) not in _SAFE_STATIC_TYPES or m.group(2).lower() not in _SAFE_STATIC_METHODS:
            return PolicyDecision(CONFIRM, f"调用 .NET 静态方法 [{m.group(1)}]::{m.group(2)}")
    for m in re.finditer(r"\.(\w+)\s*\(", s):
        if m.group(1) not in _SAFE_METHODS:
            return PolicyDecision(CONFIRM, f"调用对象方法 .{m.group(1)}()")

    heads = _command_heads(s)
    if not heads:
        return PolicyDecision(CONFIRM, "无法解析命令")
    for sep, head, seg in heads:
        if sep == "&" or head in ("&", "."):
            return PolicyDecision(CONFIRM, "使用调用运算符 & / .")
        # 表达式（变量、数字、字符串、类型字面量、比较）不是命令
        if re.match(r"^(\$|\d|'s'|\"s\"|\[|-|@)", head):
            continue
        if head.startswith(_READONLY_PREFIXES) or head in _READONLY_COMMANDS:
            excl = _READONLY_ARG_EXCLUSIONS.get(head)
            if excl and re.search(excl, seg, re.I):
                return PolicyDecision(CONFIRM, f"{head} 带有会修改系统的参数")
            continue
        return PolicyDecision(CONFIRM, f"命令 {head} 不在只读白名单")
    return PolicyDecision(READONLY, "只读查询")
