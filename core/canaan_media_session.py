"""Fixed-enum Windows GSMTC bridge using built-in Windows PowerShell, no SDK install.

https://learn.microsoft.com/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession
https://learn.microsoft.com/uwp/api/windows.media.control.globalsystemmediatransportcontrolssessionmanager
The child holds one session, checks its identity, and reads the resulting state.
"""
import base64
import json
import math
import subprocess


class MediaSessionError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# Only validated enum/budget literals replace these two tokens. No user text,
# path, app title, model output, endpoint URL or arbitrary PowerShell is accepted.
_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$mode = '__MODE__'
$budget = __BUDGET_MS__
$failure = 'MEDIA_BACKEND_UNAVAILABLE'
$before = $null; $after = $null; $sid = $null; $afterSid = $null
$accepted = $false; $verified = $false
$clock = [Diagnostics.Stopwatch]::StartNew()
function Fail([string]$code) { $script:failure = $code; throw $code }
function Remaining {
    $left = $budget - [int]$clock.ElapsedMilliseconds
    if ($left -le 0) { Fail 'MEDIA_TIMEOUT' }
    return $left
}
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    if ($null -eq $asTask) { Fail 'MEDIA_BACKEND_UNAVAILABLE' }
    function Await($operation, [Type]$resultType) {
        $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
        if (-not $task.Wait((Remaining))) {
            try { $operation.Cancel() } catch {}
            Fail 'MEDIA_TIMEOUT'
        }
        return $task.Result
    }
    function Identity($session) {
        if ($null -eq $session) { Fail 'MEDIA_SESSION_CHANGED' }
        $pointer = [Runtime.InteropServices.Marshal]::GetIUnknownForObject($session)
        try { return $pointer.ToInt64().ToString('X') }
        finally { [void][Runtime.InteropServices.Marshal]::Release($pointer) }
    }
    $manager = Await ($managerType::RequestAsync()) $managerType
    $session = $manager.GetCurrentSession()
    if ($null -eq $session) { Fail 'MEDIA_NO_SESSION' }
    $sid = Identity $session
    $info = $session.GetPlaybackInfo()
    $before = $info.PlaybackStatus.ToString().ToLowerInvariant()
    $target = if ($mode -eq 'pause') { 'paused' } else { 'playing' }
    $already = $before -eq $target
    if ($before -notin @('playing', 'paused') -and -not ($mode -eq 'play' -and $before -eq 'stopped')) {
        Fail 'MEDIA_STATE_UNAVAILABLE'
    }
    [void](Remaining)
    $afterSid = Identity ($manager.GetCurrentSession())
    if ($afterSid -ne $sid) { Fail 'MEDIA_SESSION_CHANGED' }
    if ($already) { $accepted = $true }
    else {
        if ($mode -eq 'pause') {
            if (-not $info.Controls.IsPauseEnabled) { Fail 'MEDIA_CONTROL_DISABLED' }
            $operation = $session.TryPauseAsync()
        } else {
            if (-not $info.Controls.IsPlayEnabled) { Fail 'MEDIA_CONTROL_DISABLED' }
            $operation = $session.TryPlayAsync()
        }
        $accepted = [bool](Await $operation ([bool]))
        if (-not $accepted) { Fail 'MEDIA_REQUEST_REJECTED' }
    }
    do {
        [void](Remaining)
        $afterSid = Identity ($manager.GetCurrentSession())
        if ($afterSid -ne $sid) { Fail 'MEDIA_SESSION_CHANGED' }
        $after = $session.GetPlaybackInfo().PlaybackStatus.ToString().ToLowerInvariant()
        if ($after -eq $target) {
            $verified = $true; break
        }
        Start-Sleep -Milliseconds ([Math]::Min(50, (Remaining)))
    } while ($true)
    @{requested=$mode;session_id=$sid;session_id_before=$sid;session_id_after=$afterSid;
      before=$before;after=$after;accepted=$accepted;verified=$verified;error_code=$null} | ConvertTo-Json -Compress
} catch {
    @{requested=$mode;session_id=$sid;session_id_before=$sid;session_id_after=$afterSid;
      before=$before;after=$after;accepted=$accepted;verified=$false;error_code=$failure} | ConvertTo-Json -Compress
    exit 1
}
'''


def _run_bridge(script, timeout):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def control_media(mode, timeout=6.0):
    """Request explicit pause/play and prove the final state in one bounded child.

    Timeout can leave an already accepted asynchronous request in flight. It is
    reported as unverified failure; never retry with a toggle or reverse it.
    """
    if mode not in ("pause", "play"):
        raise ValueError("Only explicit pause/play are accepted")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.5 <= timeout <= 10:
        raise ValueError("Media timeout must be within 0.5..10 seconds")
    script = _SCRIPT.replace("__MODE__", mode).replace("__BUDGET_MS__", str(max(100, int((timeout - .25) * 1000))))
    try:
        response = _run_bridge(script, timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaSessionError("MEDIA_TIMEOUT", "媒体状态尚未核实，已停止等待") from exc
    except Exception as exc:
        raise MediaSessionError("MEDIA_BACKEND_UNAVAILABLE", "Windows 媒体状态接口不可用") from exc
    try:
        output = response.stdout
        if not isinstance(output, str) or len(output) > 65536:
            raise ValueError("Invalid bridge output")
        result = json.loads(output.lstrip("\ufeff"))
        if not isinstance(result, dict):
            raise ValueError("Invalid bridge result")
    except (ValueError, TypeError) as exc:
        raise MediaSessionError("MEDIA_STATE_UNAVAILABLE", "媒体状态接口没有返回有效状态") from exc
    session = result.get("session_id")
    valid = (isinstance(session, str) and bool(session) and result.get("requested") == mode and
             result.get("session_id_before") == session and result.get("session_id_after") == session and
             result.get("before") in (("playing", "paused", "stopped") if mode == "play" else ("playing", "paused")) and
             result.get("after") == ("paused" if mode == "pause" else "playing") and
             result.get("accepted") is True and result.get("verified") is True and not result.get("error_code"))
    if response.returncode != 0 or not valid:
        code = result.get("error_code")
        allowed = {"MEDIA_TIMEOUT", "MEDIA_NO_SESSION", "MEDIA_SESSION_CHANGED", "MEDIA_STATE_UNAVAILABLE",
                   "MEDIA_CONTROL_DISABLED", "MEDIA_REQUEST_REJECTED", "MEDIA_BACKEND_UNAVAILABLE"}
        raise MediaSessionError(code if code in allowed else "MEDIA_STATE_UNAVAILABLE", "媒体请求的最终状态尚未核实")
    return result
