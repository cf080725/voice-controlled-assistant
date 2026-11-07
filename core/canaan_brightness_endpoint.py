"""Bounded, fixed-command Windows PowerShell 5 WMI brightness endpoint.

Only a validated mode and integer enter the script. Monitor identities stay CIM
data, and a successful result requires the same unique active monitor and exact
readback. Importing this module performs no process, COM or WMI work.

WMI contracts: https://learn.microsoft.com/windows/win32/wmicoreprov/wmimonitorbrightness
https://learn.microsoft.com/windows/win32/wmicoreprov/wmimonitorbrightnessmethods
https://learn.microsoft.com/windows/win32/wmicoreprov/wmisetbrightness-method-in-class-wmimonitorbrightnessmethods
"""
import base64
import json
import math
import os
import re
import subprocess


class BrightnessEndpointError(RuntimeError):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = {} if evidence is None else evidence


def parse_brightness_request(action, param):
    """Validate the complete actor parameter before starting any child process."""
    if action not in ("set_brightness", "brightness_up", "brightness_down") or not isinstance(param, str):
        raise ValueError("Unknown brightness action or non-string parameter")
    if action == "set_brightness" and param in ("min", "max"):
        return param, 0
    if re.fullmatch(r"[0-9]{1,3}", param) and 5 <= int(param) <= 100:
        return "absolute", int(param)
    if action != "set_brightness":
        sign = 1 if action == "brightness_up" else -1
        if param == "":
            return "relative", sign * 20
        match = re.fullmatch(r"delta:([+-])([0-9]{1,3})", param)
        if match and match[1] == ("+" if sign == 1 else "-") and 1 <= int(match[2]) <= 100:
            return "relative", sign * int(match[2])
    raise ValueError("Brightness target must be an exact 5..100 percentage or a valid brightness mode")


def _success_matches(mode, value, result):
    if not isinstance(result, dict):
        return False
    identity = result.get("monitor_id")
    if not isinstance(identity, str) or not identity.strip() or any(result.get(key) != identity for key in (
            "monitor_id_before", "monitor_id_after", "method_id_before", "method_id_after")):
        return False
    if any(type(result.get(key)) is not int or result[key] != 1 for key in (
            "active_count_before", "active_count_after", "method_count_before", "method_count_after")):
        return False
    levels = result.get("supported_levels")
    if (not isinstance(levels, list) or not levels or
            any(type(x) is not int or not 0 <= x <= 100 for x in levels) or len(set(levels)) != len(levels)):
        return False
    current, target, actual = (result.get(key) for key in ("current", "target", "actual"))
    if (any(type(x) is not int for x in (current, target, actual)) or current not in levels or
            not 5 <= target <= 100 or target not in levels or actual != target):
        return False
    if mode == "relative" and ((value > 0 and target < current) or (value < 0 and target > current)):
        return False
    allowed = [x for x in levels if 5 <= x <= 100]
    expected = (value if mode == "absolute" else min(allowed) if mode == "min" else
                max(allowed) if mode == "max" else max(min(allowed), min(max(allowed), current + value)))
    changed = current != target
    accepted = (type(result.get("return_value")) is int and result["return_value"] == 0) if changed else (
        "return_value" in result and result["return_value"] is None)
    return (target == expected and result.get("request_mode") == mode and
            type(result.get("request_value")) is int and result["request_value"] == value and
            result.get("write_attempted") is changed and result.get("changed") is changed and
            accepted and result.get("verified") is True and not result.get("error_code"))


# These are the only substitutions: a fixed enum, an integer, and a time budget.
# Never interpolate user text, monitor InstanceName, a path or a WMI expression.
_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$mode = '__MODE__'; $value = __VALUE__; $budget = __BUDGET_MS__
$failure = 'BRIGHTNESS_BACKEND_UNAVAILABLE'
$sid = $null; $afterSid = $null; $mid = $null; $afterMid = $null
$before = $null; $target = $null; $actual = $null; $levels = @()
$beforeCount = 0; $afterCount = 0; $beforeMethodCount = 0; $afterMethodCount = 0
$writeAttempted = $false; $changed = $false; $returnValue = $null; $verified = $false
$clock = [Diagnostics.Stopwatch]::StartNew()
function Fail([string]$code) { $script:failure = $code; throw $code }
function Remaining {
    $left = $budget - [int]$clock.ElapsedMilliseconds
    if ($left -le 0) { Fail 'BRIGHTNESS_TIMEOUT' }
    return $left
}
function Seconds { return [uint32][Math]::Max(1, [Math]::Ceiling((Remaining) / 1000.0)) }
function IsInteger($n) {
    return ($n -is [byte] -or $n -is [sbyte] -or $n -is [int16] -or $n -is [uint16] -or
            $n -is [int32] -or $n -is [uint32] -or $n -is [int64] -or $n -is [uint64])
}
function Snapshot {
    $bs = @(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -OperationTimeoutSec (Seconds) |
            Where-Object { $_.Active -eq $true })
    $ms = @(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -OperationTimeoutSec (Seconds) |
            Where-Object { $_.Active -eq $true })
    if ($bs.Count -eq 0 -or $ms.Count -eq 0) { Fail 'BRIGHTNESS_NO_MONITOR' }
    if ($bs.Count -ne 1 -or $ms.Count -ne 1) { Fail 'BRIGHTNESS_AMBIGUOUS_MONITOR' }
    $b = $bs[0]; $m = $ms[0]
    if ($b.InstanceName -isnot [string] -or [string]::IsNullOrWhiteSpace($b.InstanceName) -or
        $m.InstanceName -isnot [string] -or [string]::IsNullOrWhiteSpace($m.InstanceName) -or
        $b.InstanceName -cne $m.InstanceName) { Fail 'BRIGHTNESS_MONITOR_CHANGED' }
    if (-not (IsInteger $b.CurrentBrightness) -or $b.CurrentBrightness -lt 0 -or
        $b.CurrentBrightness -gt 100) { Fail 'BRIGHTNESS_STATE_UNAVAILABLE' }
    $supported = @($b.Level)
    if ($supported.Count -eq 0) { Fail 'BRIGHTNESS_STATE_UNAVAILABLE' }
    foreach ($n in $supported) {
        if (-not (IsInteger $n) -or $n -lt 0 -or $n -gt 100) { Fail 'BRIGHTNESS_STATE_UNAVAILABLE' }
    }
    $sorted = @($supported | ForEach-Object { [int]$_ } | Sort-Object -Unique)
    if ($sorted.Count -ne $supported.Count -or [int]$b.CurrentBrightness -notin $sorted) {
        Fail 'BRIGHTNESS_STATE_UNAVAILABLE'
    }
    return @{id=$b.InstanceName;methodId=$m.InstanceName;current=[int]$b.CurrentBrightness;
             levels=$sorted;methods=$m;count=$bs.Count;methodCount=$ms.Count}
}
try {
    $initial = Snapshot
    $sid = $initial.id; $mid = $initial.methodId; $before = $initial.current; $levels = @($initial.levels)
    $beforeCount = $initial.count; $beforeMethodCount = $initial.methodCount
    $allowed = @($levels | Where-Object { $_ -ge 5 -and $_ -le 100 })
    if ($allowed.Count -eq 0) { Fail 'BRIGHTNESS_TARGET_UNSUPPORTED' }
    if ($mode -eq 'min') { $target = [int]$allowed[0] }
    elseif ($mode -eq 'max') { $target = [int]$allowed[-1] }
    elseif ($mode -eq 'absolute') { $target = [int]$value }
    else { $target = [int][Math]::Max($allowed[0], [Math]::Min($allowed[-1], $before + $value)) }
    if ($target -notin $allowed) { Fail 'BRIGHTNESS_TARGET_UNSUPPORTED' }
    if ($mode -eq 'relative' -and (($value -gt 0 -and $target -lt $before) -or
            ($value -lt 0 -and $target -gt $before))) { Fail 'BRIGHTNESS_TARGET_UNSUPPORTED' }
    $prewrite = Snapshot
    if ($prewrite.id -cne $sid -or $prewrite.methodId -cne $mid -or $prewrite.current -ne $before -or
        ($prewrite.levels -join ',') -cne ($levels -join ',')) { Fail 'BRIGHTNESS_MONITOR_CHANGED' }
    $changed = $before -ne $target
    if ($changed) {
        [void](Remaining)
        $writeAttempted = $true
        $reply = Invoke-CimMethod -InputObject $prewrite.methods -MethodName WmiSetBrightness -Arguments @{
            Timeout=[uint32]1; Brightness=[byte]$target
        } -OperationTimeoutSec (Seconds)
        if ($null -eq $reply -or -not (IsInteger $reply.ReturnValue)) { Fail 'BRIGHTNESS_REQUEST_REJECTED' }
        $returnValue = [long]$reply.ReturnValue
        if ($returnValue -ne 0) { Fail 'BRIGHTNESS_REQUEST_REJECTED' }
    }
    do {
        [void](Remaining)
        $after = Snapshot
        $afterSid = $after.id; $afterMid = $after.methodId; $actual = $after.current
        $afterCount = $after.count; $afterMethodCount = $after.methodCount
        if ($afterSid -cne $sid -or $afterMid -cne $mid -or
            ($after.levels -join ',') -cne ($levels -join ',')) { Fail 'BRIGHTNESS_MONITOR_CHANGED' }
        if ($actual -eq $target) { $verified = $true; break }
        Start-Sleep -Milliseconds ([Math]::Min(50, (Remaining)))
    } while ($true)
} catch {
    $verified = $false
}
@{request_mode=$mode;request_value=$value;monitor_id=$sid;monitor_id_before=$sid;monitor_id_after=$afterSid;
  method_id_before=$mid;method_id_after=$afterMid;active_count_before=$beforeCount;active_count_after=$afterCount;
  method_count_before=$beforeMethodCount;method_count_after=$afterMethodCount;current=$before;target=$target;
  actual=$actual;supported_levels=@($levels);write_attempted=$writeAttempted;changed=$changed;
  return_value=$returnValue;verified=$verified;error_code=$(if ($verified) { $null } else { $failure })} |
    ConvertTo-Json -Compress -Depth 4
if (-not $verified) { exit 1 }
'''


def _run_bridge(script, timeout):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    # Use built-in Windows PowerShell 5, never a PATH-selected pwsh or executable.
    executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return subprocess.run([executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def control_brightness(action, param, timeout=6.0):
    """Apply a supported exact target and return evidence, or fail unverified.

    A timeout or failed readback can follow an accepted write. No retry, fallback
    key, reversal, rollback or claim of non-execution is made here.
    """
    mode, value = parse_brightness_request(action, param)
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or not .5 <= timeout <= 10:
        raise ValueError("Brightness timeout must be within 0.5..10 seconds")
    script = (_SCRIPT.replace("__MODE__", mode).replace("__VALUE__", str(value))
              .replace("__BUDGET_MS__", str(max(100, int((timeout - .25) * 1000)))))
    uncertain = {"action": action, "param": param, "write_attempted": None, "verified": False}
    try:
        response = _run_bridge(script, timeout)
    except subprocess.TimeoutExpired as exc:
        raise BrightnessEndpointError("BRIGHTNESS_TIMEOUT", "亮度请求的最终状态尚未核实。", uncertain) from exc
    except Exception as exc:
        raise BrightnessEndpointError("BRIGHTNESS_BACKEND_UNAVAILABLE", "亮度请求的最终状态尚未核实。", uncertain) from exc
    try:
        output = response.stdout
        if not isinstance(output, str) or len(output) > 65536:
            raise ValueError("Invalid brightness output")
        result = json.loads(output.lstrip("\ufeff"))
        if not isinstance(result, dict):
            raise ValueError("Invalid brightness evidence")
    except (TypeError, ValueError) as exc:
        raise BrightnessEndpointError("BRIGHTNESS_STATE_UNAVAILABLE", "亮度请求的最终状态尚未核实。", uncertain) from exc
    result.update(action=action, param=param)
    if response.returncode != 0 or not _success_matches(mode, value, result):
        code = result.get("error_code")
        known = {"BRIGHTNESS_TIMEOUT", "BRIGHTNESS_NO_MONITOR", "BRIGHTNESS_AMBIGUOUS_MONITOR",
                 "BRIGHTNESS_STATE_UNAVAILABLE", "BRIGHTNESS_MONITOR_CHANGED", "BRIGHTNESS_TARGET_UNSUPPORTED",
                 "BRIGHTNESS_REQUEST_REJECTED", "BRIGHTNESS_BACKEND_UNAVAILABLE"}
        raise BrightnessEndpointError(code if isinstance(code, str) and code in known else "BRIGHTNESS_STATE_UNAVAILABLE",
                                      "亮度请求的最终状态尚未核实。", result)
    result["ok"] = True
    return result
