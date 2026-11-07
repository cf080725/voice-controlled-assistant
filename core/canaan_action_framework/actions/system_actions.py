# -*- coding: utf-8 -*-
"""
系统与硬件类动作：状态查询、音量、亮度、多媒体键、截图、回桌面、锁屏、执行命令。
"""
import os
import re
import math
import time
import tempfile
import ctypes
import subprocess
from pathlib import Path
import psutil

from canaan_action_framework.models import (
    ActionContext, ActionResult, ActionCategory, PermissionLevel, VerificationResult
)
from canaan_action_framework.base_action import BaseAction
from canaan_action_framework.action_registry import ActionRegistry

user32 = ctypes.windll.user32

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3


def send_key(vk_code, times=1, interval=0.0):
    for _ in range(max(0, int(times))):
        user32.keybd_event(vk_code, 0, 0, 0)
        user32.keybd_event(vk_code, 0, 2, 0)  # KEYEVENTF_KEYUP
        if interval:
            time.sleep(interval)


def _int_or(value, default):
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else default


def _run_ps(cmd, timeout=5):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                          capture_output=True, text=True, timeout=timeout, creationflags=0x08000000)


@ActionRegistry.register
class SystemStatusAction(BaseAction):
    name = "system_status"
    aliases = ["status", "sys_status", "hardware_info"]
    description = "查询系统硬件运行状态（CPU、内存、电池、磁盘）"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.SAFE_READONLY

    def execute(self, context: ActionContext) -> ActionResult:
        from canaan_status_query import answer_query, query_kind
        from canaan_query_context import current_query
        from canaan_query_answers import issue_answer, publish_answer
        binding = current_query(context)
        query_text = binding.owned_text if binding is not None else context.user_text
        query = binding.query if binding is not None else query_kind(query_text)
        if query is not None:
            from canaan_execution_guard import execution_allowed
            answer = answer_query(query, psutil, should_stop=lambda: not execution_allowed())
            if answer.success:
                kind, targets = query
                publish_answer(issue_answer(source=self.name, kind=kind,
                    target=(targets[0] if kind == "disk" else {"temperature_cpu": "CPU温度", "temperature_gpu": "显卡温度", "process_cpu": "CPU占用进程", "process_memory": "内存占用进程", "memory_available": "可用内存"}[kind]),
                    summary=answer.speech, full_text=answer.message, payload=answer.detail,
                    summary_is_complete="top_processes" not in answer.detail))
            return ActionResult(success=answer.success, action=self.name, message=answer.message,
                                detail=answer.detail, speech_override=answer.speech,
                                error_code=answer.error_code or None)
        # Put the requested result before the shared short-speech limit.
        disk_query = any(re.fullmatch(
            r"(?:请|麻烦|帮我|先|再)*(?:查(?:询)?(?:一下|下)?|看看|看一下)?"
            r"(?:硬盘|磁盘)(?:的)?(?:还剩|还有|剩余|剩)(?:多少|多大)?(?:容量|容亮|空间)?[啊呀吗吧]?[？?!！]?",
            clause.strip()) and re.search(r"查|看|多少|多大", clause)
            for clause in re.split(r"[，,。；;]|然后|接着|之后", query_text or ""))
        cpu = psutil.cpu_percent(interval=0.2)
        ram = psutil.virtual_memory().percent
        battery = psutil.sensors_battery()
        if not disk_query:
            try:
                battery_percent = battery.percent if battery is not None else None
            except AttributeError:
                battery_percent = None
                valid_readings = False
            else:
                readings = (cpu, ram) + ((battery_percent,) if battery is not None else ())
                valid_readings = all(type(value) in (int, float) and 0 <= value <= 100
                                     and math.isfinite(value) for value in readings)
            if not valid_readings:
                speech = "未能读取有效的系统状态数据。"
                return ActionResult(success=False, action=self.name, message=speech,
                                    detail={"query": "snapshot", "complete": False},
                                    speech_override=speech, error_code="STATUS_UNAVAILABLE")
        bat_str = (f"，电池 {battery.percent}%" if battery else "") if disk_query else (
            f"，电池 {battery_percent}%" if battery is not None else "")
        disks = []
        for drive in ("C:\\", "D:\\"):
            try:
                disks.append((drive[0], psutil.disk_usage(drive).free / (1024 ** 3)))
            except Exception:
                pass
        disk_str = "，".join(f"{d}盘剩余 {free:.1f} GB" for d, free in disks)
        status_text = f"CPU 占用 {cpu}%，内存占用 {ram}%{bat_str}" + (f"，{disk_str}" if disk_str else "")
        speech = f"CPU {cpu}%，内存 {ram}%{bat_str}。"
        if disk_query:
            speech = ("，".join(f"{d}盘余{free:.1f}G" for d, free in disks) + "。"
                      if disks else "未能读取磁盘剩余空间。")
            if len(speech) > 24:
                speech = "，".join(f"{d}盘余{free / 1024:.1f}T" if free >= 1024
                                  else f"{d}盘余{free:.1f}G" for d, free in disks) + "。"
        if disk_query and not disks:
            return ActionResult(success=False, action=self.name, message=speech,
                                detail={"query": "legacy_disks", "complete": False},
                                speech_override=speech, error_code="STATUS_UNAVAILABLE")
        full_text = (disk_str if disk_query else status_text)
        payload = {"disks_free_gb": {d: round(f, 1) for d, f in disks}}
        if not disk_query:
            payload.update(cpu=cpu, ram=ram, battery_percent=battery_percent)
        query_summary = speech if disk_query else f"CPU {cpu:.0f}%，内存 {ram:.0f}%。"
        summary_is_complete = len(disks) <= 1 if disk_query else False
        if disk_query and len(query_summary) > 24:
            query_summary = "磁盘空间已读取，详情见完整结果。"
            summary_is_complete = False
        publish_answer(issue_answer(source=self.name, kind="legacy_disks" if disk_query else "snapshot",
                                    target="磁盘" if disk_query else "系统状态",
                                    summary=query_summary, full_text=full_text, payload=payload,
                                    summary_is_complete=summary_is_complete))
        return ActionResult(
            success=True,
            action=self.name,
            message=status_text,
            detail={"cpu": cpu, "ram": ram, "disks_free_gb": {d: round(f, 1) for d, f in disks}},
            speech_override=speech
        )


class _VolumeStepAction(BaseAction):
    """音量加减：模拟多媒体音量键，每次约 2%。param 为按键次数（默认 5 次≈10%）。"""
    category = ActionCategory.HARDWARE_IO
    permission_level = PermissionLevel.NORMAL_MUTATION
    vk = VK_VOLUME_UP
    speech = "调大了。"
    message = "音量已调高"

    def execute(self, context: ActionContext) -> ActionResult:
        steps = max(1, min(50, _int_or(context.param_value(), 5)))
        send_key(self.vk, steps)
        return ActionResult(success=True, action=self.name, message=f"{self.message}（{steps} 档）",
                            speech_override=self.speech)


@ActionRegistry.register
class VolumeUpAction(_VolumeStepAction):
    name = "volume_up"
    aliases = ["vol_up"]
    description = "调大系统音量"


@ActionRegistry.register
class VolumeDownAction(_VolumeStepAction):
    name = "volume_down"
    aliases = ["vol_down"]
    description = "调小系统音量"
    vk = VK_VOLUME_DOWN
    speech = "调小了。"
    message = "音量已调低"


def _audio_endpoint_factory():
    from canaan_audio_endpoint import open_default_endpoint
    return open_default_endpoint()


def _audio_snapshot(value):
    if not isinstance(value, dict):
        raise ValueError("Missing audio state")
    level, muted, endpoint = value.get("level"), value.get("muted"), value.get("endpoint_id")
    if (not isinstance(endpoint, str) or not endpoint or type(muted) is not bool or
            type(level) not in (float, int) or not math.isfinite(level) or not 0 <= level <= 1):
        raise ValueError("Invalid audio state")
    return {"level": float(level), "muted": muted, "endpoint_id": endpoint}


def _audio_matches(state, level, muted):
    # Float32 readback may differ by a few ulps; allow at most half a percent point.
    level_matches = state["level"] == 0 if level == 0 else abs(state["level"] - level) <= .005
    return level_matches and state["muted"] is muted


def _audio_failure(action, code):
    if not isinstance(code, str) or not code.startswith("AUDIO_"):
        code = "AUDIO_STATE_UNVERIFIED"
    speech = "音频目标无效，请提供零到一百的明确音量或静音开关。" if code == "AUDIO_TARGET_INVALID" else "音频请求的最终状态尚未核实。"
    return ActionResult(success=False, action=action, error_code=code, message=speech, speech_override=speech)


def _audio_verify(action, context):
    observation = context.metadata.get("_audio_observation") or {}
    if observation.get("request_only") and observation.get("action") == action and not context.param_value():
        return VerificationResult(passed=True, verified=False, gate_name="AUDIO_REQUEST_SENT",
                                  reason="仅发送了切换请求，未核实当前状态")
    valid = False
    try:
        before, after = _audio_snapshot(observation["before"]), _audio_snapshot(observation["after"])
        valid = (observation["action"] == action and observation["trace_id"] == context.trace_id and
                 observation["param"] == context.param_value() and before["endpoint_id"] == after["endpoint_id"])
        if action == "volume":
            target = VolumeAction.parse_target(context.param_value()) / 100.0
            valid = valid and _audio_matches(after, target, target == 0)
        else:
            want = context.param_value().lower()
            valid = valid and want in ("on", "off") and after["muted"] is (want == "on")
    except (KeyError, ValueError, TypeError):
        valid = False
    return VerificationResult(passed=valid, verified=valid, gate_name="AUDIO_TARGET_STATE",
        error_code=None if valid else "AUDIO_STATE_UNVERIFIED",
        reason="同一播放端点已读回目标状态" if valid else "缺少本次请求的有效音频状态证据")


@ActionRegistry.register
class VolumeMuteAction(BaseAction):
    name = "volume_mute"
    aliases = ["mute", "toggle_mute"]
    description = "系统静音：param 为 on（打开静音）/ off（取消静音）/ 空（切换）"
    category = ActionCategory.HARDWARE_IO
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        context.metadata.pop("_audio_observation", None)
        want = str(context.param_value() or "").strip().lower()
        if not want:
            send_key(VK_VOLUME_MUTE)
            context.metadata["_audio_observation"] = {"request_only": True, "param": "", "action": self.name}
            speech = "已发送切换静音请求，当前状态未核实。"
            return ActionResult(success=True, action=self.name, message=speech, speech_override=speech)
        if want not in ("on", "off"):
            return _audio_failure(self.name, "AUDIO_TARGET_INVALID")
        target = want == "on"
        try:
            with _audio_endpoint_factory() as port:
                before = _audio_snapshot(port.snapshot())
                if before["muted"] is not target:
                    port.set_mute(target)
                after = _audio_snapshot(port.snapshot())
                if before["endpoint_id"] != after["endpoint_id"] or after["muted"] is not target:
                    return _audio_failure(self.name, "AUDIO_STATE_UNVERIFIED")
            context.metadata["_audio_observation"] = {"action": self.name, "param": context.param_value(),
                "trace_id": context.trace_id, "before": before, "after": after}
        except Exception as exc:
            return _audio_failure(self.name, getattr(exc, "code", "AUDIO_STATE_UNVERIFIED"))
        speech = "静音了。" if target else "取消静音了。"
        return ActionResult(success=True, action=self.name, message=speech, speech_override=speech,
                            detail={"method": "core_audio", "endpoint_id": after["endpoint_id"]})

    def verify(self, context: ActionContext) -> VerificationResult:
        return _audio_verify(self.name, context)


@ActionRegistry.register
class VolumeAction(BaseAction):
    """在同一默认播放端点设置绝对音量，并读回音量与静音状态。"""
    name = "volume"
    aliases = ["set_volume", "vol"]
    description = "把系统音量设为指定百分比（0 为静音）"
    category = ActionCategory.HARDWARE_IO
    permission_level = PermissionLevel.NORMAL_MUTATION

    @staticmethod
    def parse_target(param: str) -> int:
        match = re.fullmatch(r"([0-9]{1,3})\s*[%％]?", str(param or "").strip())
        if not match or not 0 <= int(match[1]) <= 100:
            raise ValueError("Audio target must be an explicit percentage within 0..100")
        return int(match[1])

    def execute(self, context: ActionContext) -> ActionResult:
        context.metadata.pop("_audio_observation", None)
        try:
            target = self.parse_target(context.param_value())
        except ValueError:
            return _audio_failure(self.name, "AUDIO_TARGET_INVALID")
        try:
            with _audio_endpoint_factory() as port:
                before = _audio_snapshot(port.snapshot())
                # Set the level before unmuting so an old loud level is never restored first.
                port.set_level(target / 100.0)
                port.set_mute(target == 0)
                after = _audio_snapshot(port.snapshot())
                if (before["endpoint_id"] != after["endpoint_id"] or
                        not _audio_matches(after, target / 100.0, target == 0)):
                    return _audio_failure(self.name, "AUDIO_STATE_UNVERIFIED")
            context.metadata["_audio_observation"] = {"action": self.name, "param": context.param_value(),
                "trace_id": context.trace_id, "before": before, "after": after}
        except Exception as exc:
            return _audio_failure(self.name, getattr(exc, "code", "AUDIO_STATE_UNVERIFIED"))
        speech = "静音了。" if target == 0 else f"音量调到{target}了。"
        return ActionResult(success=True, action=self.name, message=f"音量已调到 {target}%", speech_override=speech,
                            detail={"method": "core_audio", "endpoint_id": after["endpoint_id"]})

    def verify(self, context: ActionContext) -> VerificationResult:
        return _audio_verify(self.name, context)


def _brightness_request(action, param, timeout=6.0):
    from canaan_brightness_endpoint import control_brightness
    return control_brightness(action, param, timeout=timeout)


def _brightness_spec(action, param):
    # Validate the entire parameter; the rest of a multi-action utterance never
    # determines this actor's mode or target.
    if action not in ("set_brightness", "brightness_up", "brightness_down") or not isinstance(param, str):
        raise ValueError("Invalid brightness action or parameter")
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
    raise ValueError("Invalid brightness target")


def _brightness_matches(action, param, evidence):
    try:
        mode, value = _brightness_spec(action, param)
    except ValueError:
        return False
    if (not isinstance(evidence, dict) or evidence.get("ok") is not True or
            evidence.get("action") != action or evidence.get("param") != param):
        return False
    identity = evidence.get("monitor_id")
    if not isinstance(identity, str) or not identity.strip() or any(evidence.get(key) != identity for key in (
            "monitor_id_before", "monitor_id_after", "method_id_before", "method_id_after")):
        return False
    if any(type(evidence.get(key)) is not int or evidence[key] != 1 for key in (
            "active_count_before", "active_count_after", "method_count_before", "method_count_after")):
        return False
    levels = evidence.get("supported_levels")
    if (not isinstance(levels, list) or not levels or
            any(type(x) is not int or not 0 <= x <= 100 for x in levels) or len(set(levels)) != len(levels)):
        return False
    current, target, actual = (evidence.get(key) for key in ("current", "target", "actual"))
    if (any(type(x) is not int for x in (current, target, actual)) or current not in levels or
            not 5 <= target <= 100 or target not in levels or actual != target):
        return False
    if mode == "relative" and ((value > 0 and target < current) or (value < 0 and target > current)):
        return False
    allowed = [x for x in levels if 5 <= x <= 100]
    expected = (value if mode == "absolute" else min(allowed) if mode == "min" else
                max(allowed) if mode == "max" else max(min(allowed), min(max(allowed), current + value)))
    changed = current != target
    accepted = (type(evidence.get("return_value")) is int and evidence["return_value"] == 0) if changed else (
        "return_value" in evidence and evidence["return_value"] is None)
    return (target == expected and evidence.get("request_mode") == mode and
            type(evidence.get("request_value")) is int and evidence["request_value"] == value and
            evidence.get("write_attempted") is changed and evidence.get("changed") is changed and
            accepted and evidence.get("verified") is True and not evidence.get("error_code"))


def _brightness_failure(action, code, evidence=None):
    known = {"BRIGHTNESS_TARGET_INVALID", "BRIGHTNESS_TIMEOUT", "BRIGHTNESS_NO_MONITOR",
             "BRIGHTNESS_AMBIGUOUS_MONITOR", "BRIGHTNESS_STATE_UNAVAILABLE", "BRIGHTNESS_MONITOR_CHANGED",
             "BRIGHTNESS_TARGET_UNSUPPORTED", "BRIGHTNESS_REQUEST_REJECTED", "BRIGHTNESS_BACKEND_UNAVAILABLE"}
    if not isinstance(code, str) or code not in known:
        code = "BRIGHTNESS_STATE_UNAVAILABLE"
    speech = ("亮度参数无效，请提供五到一百的明确亮度或最低最高。" if code == "BRIGHTNESS_TARGET_INVALID"
              else "亮度请求的最终状态尚未核实。")
    return ActionResult(success=False, action=action, error_code=code, message=speech,
                        speech_override=speech, detail=evidence if isinstance(evidence, dict) else None)


class _BrightnessBase(BaseAction):
    category = ActionCategory.HARDWARE_IO
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        context.metadata.pop("_brightness_observation", None)
        param = context.param_value()
        try:
            mode, value = _brightness_spec(self.name, param)
        except ValueError:
            return _brightness_failure(self.name, "BRIGHTNESS_TARGET_INVALID")
        try:
            evidence = _brightness_request(self.name, param, timeout=6.0)
            if not _brightness_matches(self.name, param, evidence):
                return _brightness_failure(self.name, evidence.get("error_code") if isinstance(evidence, dict) else None,
                                           evidence)
        except Exception as exc:
            return _brightness_failure(self.name, getattr(exc, "code", None), getattr(exc, "evidence", None))
        # Copy the nested Level list as well, so a mutable fake/port result cannot
        # rewrite the evidence after this actor has accepted it.
        observation = dict(evidence)
        observation["supported_levels"] = list(evidence["supported_levels"])
        context.metadata["_brightness_observation"] = {"action": self.name, "param": param,
            "trace_id": context.trace_id, "evidence": observation}
        target = evidence["target"]
        detail = {"method": "wmi", "monitor_id": evidence["monitor_id"], "current": evidence["current"],
                  "target": target, "actual": evidence["actual"], "changed": evidence["changed"],
                  "write_attempted": evidence["write_attempted"]}
        if mode == "relative":
            detail["requested_delta"] = value
        return ActionResult(success=True, action=self.name, message=f"屏幕亮度已核实为 {target}%",
                            speech_override=f"亮度调到{target}了。", detail=detail)

    def verify(self, context: ActionContext) -> VerificationResult:
        observation = context.metadata.get("_brightness_observation") or {}
        valid = (isinstance(observation, dict) and context.action_name == self.name and observation.get("action") == self.name and
                 observation.get("trace_id") == context.trace_id and observation.get("param") == context.param_value() and
                 _brightness_matches(self.name, context.param_value(), observation.get("evidence")))
        return VerificationResult(passed=valid, verified=valid, gate_name="BRIGHTNESS_TARGET_STATE",
            error_code=None if valid else "BRIGHTNESS_STATE_UNAVAILABLE",
            reason="同一活动屏幕已读回目标亮度" if valid else "缺少本次请求的有效亮度状态证据")


@ActionRegistry.register
class BrightnessAction(_BrightnessBase):
    name = "set_brightness"
    aliases = ["brightness", "screen_brightness"]
    description = "把屏幕亮度设为精确百分比，或实际支持的最低/最高亮度（min/max）"


@ActionRegistry.register
class BrightnessUpAction(_BrightnessBase):
    name = "brightness_up"
    description = "调亮屏幕（param 为目标百分比；不给则在当前基础上 +20）"

@ActionRegistry.register
class BrightnessDownAction(_BrightnessBase):
    name = "brightness_down"
    description = "调暗屏幕（param 为目标百分比；不给则在当前基础上 -20）"

@ActionRegistry.register
class MediaControlAction(BaseAction):
    name = "media_control"
    aliases = ["media"]
    description = "多媒体播放控制：pause / play / play_pause / next / prev"
    category = ActionCategory.HARDWARE_IO
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        context.metadata.pop("_media_observation", None)
        mode = context.param_value().lower()
        if mode in ("pause", "play"):
            try:
                evidence = _media_session_request(mode, timeout=6.0)
                if not _media_matches(mode, evidence):
                    return _media_failure("MEDIA_STATE_UNVERIFIED")
                context.metadata["_media_observation"] = {"param": context.param_value(),
                    "trace_id": context.trace_id, "evidence": dict(evidence)}
            except Exception as exc:
                return _media_failure(getattr(exc, "code", "MEDIA_STATE_UNVERIFIED"))
            speech = "已暂停。" if mode == "pause" else "已开始播放。"
            return ActionResult(success=True, action=self.name, message=speech, speech_override=speech,
                                detail={"method": "gsmtc", "session_id": evidence["session_id"]})
        keys = {"next": VK_MEDIA_NEXT, "下一首": VK_MEDIA_NEXT, "下一曲": VK_MEDIA_NEXT,
                "prev": VK_MEDIA_PREV, "previous": VK_MEDIA_PREV, "上一首": VK_MEDIA_PREV,
                "上一曲": VK_MEDIA_PREV, "play_pause": VK_MEDIA_PLAY_PAUSE}
        if mode not in keys:
            return _media_failure("MEDIA_TARGET_INVALID")
        send_key(keys[mode])
        context.metadata["_media_observation"] = {"request_only": True, "param": context.param_value(),
                                                 "trace_id": context.trace_id}
        speech = "已发送媒体控制请求，播放状态未核实。"
        return ActionResult(success=True, action=self.name, message=speech, speech_override=speech)

    def verify(self, context: ActionContext) -> VerificationResult:
        observation = context.metadata.get("_media_observation") or {}
        valid = (observation.get("param") == context.param_value() and
                 observation.get("trace_id") == context.trace_id)
        if valid and observation.get("request_only"):
            return VerificationResult(passed=True, verified=False, gate_name="MEDIA_REQUEST_SENT",
                                      reason="媒体键请求已发送，最终播放状态未核实")
        valid = valid and _media_matches(context.param_value().lower(), observation.get("evidence"))
        return VerificationResult(passed=valid, verified=valid, gate_name="MEDIA_TARGET_STATE",
            error_code=None if valid else "MEDIA_STATE_UNVERIFIED",
            reason="同一媒体会话已读回目标播放状态" if valid else "缺少本次请求的有效媒体状态证据")


def _media_session_request(mode, timeout=6.0):
    from canaan_media_session import control_media
    return control_media(mode, timeout=timeout)


def _media_matches(mode, value):
    if mode not in ("pause", "play") or not isinstance(value, dict):
        return False
    session = value.get("session_id")
    return (isinstance(session, str) and bool(session) and value.get("requested") == mode and
            value.get("session_id_before", session) == session and value.get("session_id_after", session) == session and
            value.get("before") in (("playing", "paused", "stopped") if mode == "play" else ("playing", "paused")) and
            value.get("after") == ("paused" if mode == "pause" else "playing") and
            value.get("accepted") is True and value.get("verified") is True and not value.get("error_code"))


def _media_failure(code):
    if not isinstance(code, str) or not code.startswith("MEDIA_"):
        code = "MEDIA_STATE_UNVERIFIED"
    speech = "媒体控制参数无效。" if code == "MEDIA_TARGET_INVALID" else "媒体请求的最终状态尚未核实。"
    return ActionResult(success=False, action="media_control", error_code=code, message=speech, speech_override=speech)


@ActionRegistry.register
class ScreenshotAction(BaseAction):
    name = "screenshot"
    aliases = ["screen_shot", "capture_screen"]
    description = "全屏截图并保存到桌面"
    category = ActionCategory.FILE_OPERATIONS
    permission_level = PermissionLevel.NORMAL_MUTATION
    rollback_supported = True

    def execute(self, context: ActionContext) -> ActionResult:
        from PIL import ImageGrab
        from canaan_action_framework.actions.app_actions import _user_folder_path
        metadata = getattr(context, "metadata", None)
        if metadata is None:
            metadata = context.metadata = {}
        metadata.pop("screenshot_path", None)
        try:
            img = ImageGrab.grab(all_screens=True)
        except Exception:
            return ActionResult(success=False, action=self.name, error_code="SCREENSHOT_CAPTURE_FAILED",
                                message="无法获取屏幕图像", speech_override="这次没能获取屏幕图像，请稍后再试。")
        try:
            desktop_dir = Path(_user_folder_path("desktop"))
            stem = f"Screenshot_{time.strftime('%Y%m%d_%H%M%S')}"
            save_path = desktop_dir / f"{stem}.png"
            # 独占预留路径，连续或并发截图不能覆盖同秒已有文件。
            try:
                with save_path.open("xb"):
                    pass
            except FileExistsError:
                with tempfile.NamedTemporaryFile(dir=desktop_dir, prefix=stem + "_", suffix=".png", delete=False) as reserved:
                    save_path = Path(reserved.name)
            metadata["screenshot_path"] = str(save_path)
            img.save(str(save_path))
            return ActionResult(success=True, action=self.name, message=f"已截屏保存至桌面: {save_path.name}",
                                detail=str(save_path), speech_override="截好了，在桌面。")
        except Exception:
            self.rollback(context)
            return ActionResult(success=False, action=self.name, error_code="SCREENSHOT_SAVE_FAILED",
                                message="截图文件保存失败", speech_override="截图没有保存成功，请检查保存位置后再试。")
        finally:
            try:
                img.close()
            except Exception:
                pass

    def verify(self, context: ActionContext) -> VerificationResult:
        from PIL import Image
        try:
            path = Path(context.metadata["screenshot_path"])
            with Image.open(path) as saved:
                if saved.format != "PNG":
                    raise ValueError("Screenshot is not PNG")
                saved.verify()
            from canaan_device_attachments import capture_verified_screenshot
            capture_verified_screenshot(path)
            return VerificationResult(passed=True, verified=True, gate_name="SCREENSHOT_SAVED",
                                      reason="截图文件已保存且通过 PNG 完整性检查")
        except Exception:
            return VerificationResult(passed=False, verified=False, gate_name="SCREENSHOT_SAVED",
                                      error_code="SCREENSHOT_VERIFY_FAILED", reason="截图文件缺失或无法读取")

    def rollback(self, context: ActionContext) -> bool:
        # 仅清理本次独占创建的文件，不接触同名旧截图。
        path = (getattr(context, "metadata", None) or {}).pop("screenshot_path", None)
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                return False
        return True


@ActionRegistry.register
class LockScreenAction(BaseAction):
    name = "lock_screen"
    aliases = ["lock", "lockscreen"]
    description = "锁定当前 Windows 工作站"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.NORMAL_MUTATION  # 锁屏可随时用密码解开，不需要确认

    def execute(self, context: ActionContext) -> ActionResult:
        ok = bool(user32.LockWorkStation())
        return ActionResult(success=ok, action=self.name, message="电脑已锁定" if ok else "锁屏调用失败",
                            speech_override="锁好了。" if ok else "没锁上。")


@ActionRegistry.register
class ShowDesktopAction(BaseAction):
    name = "show_desktop"
    aliases = ["desktop", "minimize_all"]
    description = "最小化所有窗口，回到桌面"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.NORMAL_MUTATION

    def execute(self, context: ActionContext) -> ActionResult:
        user32.keybd_event(0x5B, 0, 0, 0)  # Win
        user32.keybd_event(0x44, 0, 0, 0)  # D
        time.sleep(0.05)
        user32.keybd_event(0x44, 0, 2, 0)
        user32.keybd_event(0x5B, 0, 2, 0)
        return ActionResult(success=True, action=self.name, message="已返回桌面", speech_override="回桌面了。")


@ActionRegistry.register
class RunCmdAction(BaseAction):
    name = "run_cmd"
    aliases = ["cmd", "powershell", "execute_command"]
    description = "执行 PowerShell / CMD 系统原生控制台指令"
    category = ActionCategory.SYSTEM_CONTROL
    permission_level = PermissionLevel.CRITICAL_DESTRUCTIVE
    DESTRUCTIVE_RE = re.compile(r"Clear-RecycleBin|Stop-Computer|Restart-Computer|\bshutdown\b|Remove-Item|\brmdir\b|\brd\s|\bdel\s|\berase\s|\bformat\b",
                                re.I)

    @staticmethod
    def _cmd(context: ActionContext) -> str:
        return str(context.raw_param if isinstance(context.raw_param, str) else context.param_value()).strip()

    def authorize(self, context: ActionContext):
        """规则来源：默认信任写死的命令，但高危命令仍拒绝，动态拼接/静态方法仍要确认；
        大模型来源：只读命令直接执行，高危命令确认了也拒绝，其余需口头确认。"""
        from canaan_security_policy import classify_command as _classify, has_dynamic_structure as _dynamic, has_unsafe_static_call as _unsafe_static, BLOCKED as _BLOCKED
        _cmd = self._cmd(context)
        _pre = _classify(_cmd)
        if _pre.level == _BLOCKED:
            return ActionResult(
                success=False, action=self.name, error_code="SECURITY_BLOCKED",
                message=f"已拒绝执行高危命令（{_pre.reason}）：{_cmd[:80]}",
                detail={"command": _cmd, "reason": _pre.reason},
                speech_override="这个命令风险太大，我不会执行的。")
        if not context.confirmed and (_dynamic(_cmd) or _unsafe_static(_cmd)):
            return self.confirm_required(context, f"执行命令 {_cmd[:80]}", "命令包含动态内容，无法静态判定")
        if context.source == "rule":
            if not context.confirmed and self.DESTRUCTIVE_RE.search(self._cmd(context)):
                return self.confirm_required(context, f"执行命令 {self._cmd(context)[:80]}", "会关机、重启或删除东西")
            return None
        from canaan_security_policy import READONLY, BLOCKED
        cmd_str = _cmd
        decision = _pre
        context.metadata["policy"] = decision.level
        if decision.level == BLOCKED:
            return ActionResult(
                success=False,
                action=self.name,
                error_code="SECURITY_BLOCKED",
                message=f"已拒绝执行高危命令（{decision.reason}）：{cmd_str[:80]}",
                detail={"command": cmd_str, "reason": decision.reason},
                speech_override="这个命令风险太大，我不会执行的。"
            )
        if decision.level == READONLY or context.confirmed:
            return None
        return self.confirm_required(context, f"执行命令 {cmd_str[:80]}", decision.reason)

    def execute(self, context: ActionContext) -> ActionResult:
        cmd_str = self._cmd(context)
        from canaan_query_context import current_query
        from canaan_query_answers import publish_answer
        binding = current_query(context)
        query_text = binding.owned_text if binding is not None else context.user_text
        try:
            res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd_str],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=0x08000000)  # CREATE_NO_WINDOW
        except subprocess.TimeoutExpired:
            return ActionResult(success=False, action=self.name, error_code="TIMEOUT",
                                message=f"命令执行超时: {cmd_str[:80]}", speech_override="命令执行超时了。")
        from canaan_command_reply import CommandOutcome, command_reply
        reply = command_reply(cmd_str, res.stdout, res.stderr, res.returncode, user_text=query_text)
        if res.returncode == 0 and reply.outcome is CommandOutcome.COMPLETED and reply.answer is not None:
            publish_answer(reply.answer)
        return ActionResult(
            success=res.returncode == 0 and reply.outcome is CommandOutcome.COMPLETED,
            action=self.name,
            message=reply.message,
            detail=reply.detail,
            speech_override=reply.speech,
            error_code="QUERY_RESULT_UNAVAILABLE" if reply.outcome is CommandOutcome.QUERY_RESULT_UNAVAILABLE else None
        )
