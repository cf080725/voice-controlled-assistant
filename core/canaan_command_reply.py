"""Bounded pure summaries of captured output from exact fixed local queries.

This module executes nothing. Output text is data, never a command or authority.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from ipaddress import IPv4Address
import re


class CommandOutcome(str, Enum):
    COMPLETED = "completed"
    QUERY_RESULT_UNAVAILABLE = "query_result_unavailable"
    COMMAND_FAILED = "command_failed"


@dataclass(frozen=True)
class CommandReply:
    message: str
    speech: str
    detail: object
    outcome: CommandOutcome
    answer: object = None


class _Unreadable(ValueError):
    pass


MAX_OUTPUT_CHARS = 65536
MAX_OUTPUT_LINES = 1024
MAX_LINE_CHARS = 1024


def _family(command):
    command = " ".join(str(command or "").split())
    drive = re.fullmatch(r"Get-PSDrive -Name ([A-Za-z])\s*\|\s*Select-Object Used\s*,\s*Free", command, re.I)
    if drive:
        return "drive_free", drive.group(1).upper()
    templates = {
        "get-ciminstance win32_logicaldisk | select-object deviceid,size,freespace": "disks_free",
        "(get-date) - (get-ciminstance win32_operatingsystem).lastbootuptime": "uptime",
        "get-netipaddress -addressfamily ipv4 | select-object ipaddress": "ipv4_table",
        "ipconfig": "ipconfig", "ipconfig /all": "ipconfig",
    }
    return templates.get(command.lower()), None


def _lines(stdout):
    if len(stdout) > MAX_OUTPUT_CHARS:
        raise _Unreadable("output_limit")
    lines = stdout.splitlines()
    if len(lines) > MAX_OUTPUT_LINES or any(len(line) > MAX_LINE_CHARS for line in lines):
        raise _Unreadable("output_limit")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in stdout):
        raise _Unreadable("control_character")
    return [line.strip() for line in lines if line.strip()]


def _uint(text):
    if not re.fullmatch(r"[0-9]{1,20}", text):
        raise _Unreadable("non_integer")
    value = int(text)
    if value > 2 ** 64 - 1:
        raise _Unreadable("number_range")
    return value


def _table(lines, headers):
    if len(lines) < 2 or lines[0].lower().split() != headers:
        raise _Unreadable("table_header")
    rows = lines[1:]
    separator = rows[0].split()
    if len(separator) == len(headers) and all(re.fullmatch(r"-+", value) for value in separator):
        rows = rows[1:]
    if not rows:
        raise _Unreadable("empty_table")
    return rows


def _space(value):
    number = value / 1024 ** 3
    unit = "GiB"
    for next_unit in ("TiB", "PiB", "EiB"):
        if number < 1024:
            break
        number /= 1024
        unit = next_unit
    text = f"{number:.1f}".rstrip("0").rstrip(".")
    return text + unit


def _explicit_drive(user_text):
    prefix = r"(?:请|麻烦|帮我|先|再)*(?:查(?:询)?(?:一下|下)?|查看|看看|看一下)?"
    for clause in re.split(r"[，,。；;]|然后|接着|之后", user_text or ""):
        match = re.fullmatch(prefix + r"([A-Za-z])\s*盘(?:的)?(?:还剩|还有|剩余|剩)(?:多少|多大)?(?:容量|容亮|空间)?[呢吗啊呀吧呗嘛？?!！]*", clause.strip())
        if match:
            return match.group(1).upper()
    return None


def _disks(family, drive, lines, user_text):
    values = []
    if family == "drive_free":
        rows = _table(lines, ["used", "free"])
        if len(rows) != 1 or len(rows[0].split()) != 2:
            raise _Unreadable("ambiguous_drive_rows")
        used, free = map(_uint, rows[0].split())
        values.append({"drive": drive, "used_bytes": used, "free_bytes": free})
    else:
        rows = _table(lines, ["deviceid", "size", "freespace"])
        if len(rows) > 26:
            raise _Unreadable("drive_count")
        seen = set()
        for row in rows:
            columns = row.split()
            if len(columns) != 3 or not re.fullmatch(r"[A-Za-z]:", columns[0]):
                raise _Unreadable("drive_row")
            letter = columns[0][0].upper()
            size, free = map(_uint, columns[1:])
            if letter in seen or free > size:
                raise _Unreadable("ambiguous_drive_rows")
            seen.add(letter)
            values.append({"drive": letter, "size_bytes": size, "free_bytes": free})
        requested = _explicit_drive(user_text)
        if requested:
            matching = [value for value in values if value["drive"] == requested]
            if not matching:
                raise _Unreadable("requested_drive_missing")
            shown = matching
        else:
            shown = values
    if family == "drive_free":
        shown = values
    items = [f"{value['drive']}盘余{_space(value['free_bytes'])}" for value in shown]
    speech = "，".join(items) + "。"
    summary_is_complete = len(speech) <= 24
    if len(speech) > 24:
        speech = items[0] + f"等{len(shown)}盘。"
    message = "；".join(f"{value['drive']}盘剩余{value['free_bytes']}字节（{_space(value['free_bytes'])}）" for value in values)
    return message, speech, {"disks": values, "unit_basis": "1024 bytes", "summarized_drives": [value["drive"] for value in shown]}, summary_is_complete


def _uptime(lines):
    joined = "\n".join(lines)
    compact = re.fullmatch(r"(?:(\d{1,8})\.)?(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,7}))?", joined)
    if compact:
        days, hours, minutes, seconds = [int(value or "0") for value in compact.groups()[:4]]
        fraction = Decimal("0." + compact.group(5)) if compact.group(5) else Decimal(0)
        total = Decimal(days * 86400 + hours * 3600 + minutes * 60 + seconds) + fraction
    else:
        fields = {}
        known = {"days", "hours", "minutes", "seconds", "milliseconds", "ticks", "totaldays", "totalhours", "totalminutes", "totalseconds", "totalmilliseconds"}
        for line in lines:
            match = re.fullmatch(r"([A-Za-z]+)\s*:\s*([0-9]+(?:\.[0-9]+)?)", line)
            if not match or match.group(1).lower() not in known or match.group(1).lower() in fields:
                raise _Unreadable("timespan_field")
            fields[match.group(1).lower()] = Decimal(match.group(2))
        names = ("days", "hours", "minutes", "seconds")
        if all(name in fields for name in names):
            if any(fields[name] != int(fields[name]) for name in names):
                raise _Unreadable("timespan_range")
            days, hours, minutes, seconds = [int(fields[name]) for name in names]
            milliseconds = fields.get("milliseconds", Decimal(0))
            if milliseconds >= 1000 or milliseconds != int(milliseconds):
                raise _Unreadable("timespan_range")
            total = Decimal(days * 86400 + hours * 3600 + minutes * 60 + seconds) + milliseconds / 1000
            units_in_seconds = {"totaldays": Decimal(86400), "totalhours": Decimal(3600),
                                "totalminutes": Decimal(60), "totalseconds": Decimal(1),
                                "totalmilliseconds": Decimal("0.001"), "ticks": Decimal("0.0000001")}
            for key, factor in units_in_seconds.items():
                if key in fields and abs(total - fields[key] * factor) > Decimal("0.001"):
                    raise _Unreadable("timespan_inconsistent")
        elif set(fields) == {"totalseconds"}:
            total = fields["totalseconds"]
            whole = int(total)
            days, remainder = divmod(whole, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
        else:
            raise _Unreadable("timespan_incomplete")
    if days > 10675199 or hours > 23 or minutes > 59 or seconds > 59 or not total.is_finite() or total < 0:
        raise _Unreadable("timespan_range")
    if days:
        speech = f"已开机{days}天{hours}小时。"
    elif hours:
        speech = f"已开机{hours}小时{minutes}分。"
    elif minutes:
        speech = f"已开机{minutes}分{seconds}秒。"
    elif seconds:
        speech = f"已开机{seconds}秒。"
    else:
        speech = "已开机不到1秒。"
    return f"本次查询的开机时长：{days}天{hours}小时{minutes}分{seconds}秒。", speech, {
        "days": days, "hours": hours, "minutes": minutes, "seconds": seconds, "total_seconds": str(total)}


def _ipv4(family, lines):
    addresses = []
    if family == "ipv4_table":
        values = _table(lines, ["ipaddress"])
    else:
        values = []
        for line in lines:
            match = re.fullmatch(r"(?:Autoconfiguration\s+|自动配置\s*)?IPv4\s*(?:Address|地址)[\s.．。]*[:：]\s*(\d{1,3}(?:\.\d{1,3}){3})(?:\s*\((?:Preferred|首选|首選|Tentative|暂定)\))?", line, re.I)
            if match:
                values.append(match.group(1))
            elif re.match(r"(?:Autoconfiguration\s+|自动配置\s*)?IPv4\s*(?:Address|地址)", line, re.I):
                raise _Unreadable("ipv4_field")
    for value in values:
        try:
            address = str(IPv4Address(value))
        except ValueError:
            raise _Unreadable("invalid_ipv4") from None
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise _Unreadable("no_ipv4_address")
    count_label = "个IPv4" if len(addresses) < 100 else "个地址"
    speech = f"IPv4是{addresses[0]}。" if len(addresses) == 1 else f"{addresses[0]}等{len(addresses)}{count_label}。"
    message = f"读取到{len(addresses)}个本机IPv4地址：" + "、".join(addresses) + "；未选择默认网络接口。"
    return message, speech, {"ipv4_addresses": addresses, "interface_preference_selected": False}


def command_reply(command, stdout, stderr, returncode, *, user_text=""):
    """Summarize captured output with a typed outcome and command evidence."""
    stdout = stdout if isinstance(stdout, str) else ""
    stderr = stderr if isinstance(stderr, str) else ""
    output = (stdout or stderr).strip()
    family, drive = _family(command)
    if returncode != 0:
        cause = (stderr or stdout).strip()
        first = next((line.strip() for line in cause[:MAX_OUTPUT_CHARS].splitlines()[:MAX_OUTPUT_LINES] if line.strip()), "未返回错误原因")
        first = re.sub(r"[\x00-\x1f\x7f]", "", first)
        speech = "命令失败：" + first[:17].rstrip("。") + "。"
        return CommandReply(f"命令执行失败（返回码 {returncode}）" + (f"：{cause[:120]}" if cause else ""), speech, cause[:500], CommandOutcome.COMMAND_FAILED)
    if not family:
        message = "命令执行完毕" + (f"：{output[:120]}" if output else "")
        return CommandReply(message, "命令执行完成。" if output else "命令执行完成，没有返回内容。", output[:500], CommandOutcome.COMPLETED)
    detail = {"query": family, "command_success": True, "query_available": False,
              "output": output[:500], "output_truncated": len(output) > 500}
    try:
        lines = _lines(stdout)
        if family in ("drive_free", "disks_free"):
            message, speech, result, disks_summary_is_complete = _disks(family, drive, lines, user_text)
        elif family == "uptime":
            message, speech, result = _uptime(lines)
        else:
            message, speech, result = _ipv4(family, lines)
    except (ValueError, InvalidOperation, OverflowError) as error:
        detail["reason"] = str(error) if isinstance(error, _Unreadable) else "unreadable_number"
        name = "磁盘读数" if family in ("drive_free", "disks_free") else "开机时长" if family == "uptime" else "IPv4地址"
        speech = f"命令已完成，未能读取{name}。"
        return CommandReply(speech, speech, detail, CommandOutcome.QUERY_RESULT_UNAVAILABLE)
    detail.update(query_available=True, result=result)
    from canaan_query_answers import issue_answer
    # Values are the exact parsed result, not inferred from the message.
    complete_summary = (len(result.get("ipv4_addresses", ())) == 1 if family in ("ipv4_table", "ipconfig")
                        else disks_summary_is_complete if family in ("drive_free", "disks_free")
                        else True)
    target = (drive + "盘" if drive else "磁盘" if family == "disks_free"
              else "开机时长" if family == "uptime" else "本机IPv4")
    snapshot_summary = speech
    if family in ("drive_free", "disks_free") and len(result["summarized_drives"]) == 1:
        shown = next(row for row in result["disks"] if row["drive"] == result["summarized_drives"][0])
        if shown["free_bytes"] % (1024 ** 3) == 0:
            # Preserve distinct integral GiB samples even across the TiB boundary.
            snapshot_summary = f'{shown["drive"]}盘余{shown["free_bytes"] // (1024 ** 3)}GiB。'
    snapshot = issue_answer(source="run_cmd", kind=family, target=target,
                            summary=snapshot_summary, full_text=message, payload=result,
                            summary_is_complete=complete_summary)
    return CommandReply(message, speech, detail, CommandOutcome.COMPLETED, snapshot)
