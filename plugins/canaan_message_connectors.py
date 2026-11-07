"""Refresh the installed local database exporters into the existing JSONL input."""
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
EXPORT_OWNER_MARKER = ".canaan-export-owner.json"


def _plain_local_path(path):
    path = Path(path).absolute()
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("导出临时目录须位于本机")
    for item in (path, *path.parents):
        attributes = item.lstat()
        if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("导出临时目录不能包含链接或重解析点")
    return path.resolve(strict=True)


def _mark_export_working(path, root, prefix):
    path, root = _plain_local_path(path), _plain_local_path(root)
    if path.parent != root or not path.name.startswith(prefix):
        raise ValueError("导出临时目录边界无效")
    marker = {"directory": str(path), "pid": os.getpid(),
              "process_created_at": psutil.Process(os.getpid()).create_time(), "created_at": time.time()}
    (path / EXPORT_OWNER_MARKER).write_text(json.dumps(marker), encoding="utf-8")


def cleanup_export_working(directory, now=None):
    """Remove only expired, marked exporter directories whose original owner has exited."""
    now, removed = time.time() if now is None else now, 0
    for folder, prefix in ((Path(directory), "message-"), (Path(directory) / "audio" / "incoming", "desktop-")):
        try:
            root = _plain_local_path(folder)
            candidates = list(root.iterdir())
        except (OSError, ValueError):
            continue
        for candidate in candidates:
            if not candidate.name.startswith(prefix):
                continue
            try:
                path = _plain_local_path(candidate)
                if not path.is_dir() or path.parent != root:
                    continue
                marker_path = _plain_local_path(path / EXPORT_OWNER_MARKER)
                if marker_path.stat().st_size > 4096:
                    continue
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if not isinstance(marker, dict) or set(marker) != {"directory", "pid", "process_created_at", "created_at"}:
                    continue
                if marker["directory"] != str(path) or type(marker["pid"]) is not int or marker["pid"] <= 0:
                    continue
                if any(type(marker[key]) not in (int, float) or not math.isfinite(marker[key]) or marker[key] <= 0
                       for key in ("process_created_at", "created_at")):
                    continue
                if marker["created_at"] < marker["process_created_at"] or now - marker["created_at"] < 1800:
                    continue
                try:
                    if psutil.Process(marker["pid"]).create_time() == marker["process_created_at"]:
                        continue
                except psutil.NoSuchProcess:
                    pass
                pending = [path]
                while pending:
                    for child in pending.pop().iterdir():
                        child = _plain_local_path(child)
                        if not child.is_relative_to(path):
                            raise ValueError("导出临时目录包含外部路径")
                        if child.is_dir():
                            pending.append(child)
                # Recheck the exact child boundary immediately before recursive deletion.
                verified = _plain_local_path(candidate)
                if verified != path or verified.parent != root or not verified.name.startswith(prefix):
                    continue
                if json.loads(_plain_local_path(marker_path).read_text(encoding="utf-8")) != marker:
                    continue
                shutil.rmtree(verified)
                removed += 1
            except (OSError, ValueError, TypeError, OverflowError, psutil.Error):
                continue
    return removed


def validate_collector_config(config):
    if not isinstance(config, dict) or set(config) - {"python", "chatapp_config", "imapp_data_root", "interval_seconds", "enabled_sources"}:
        raise ValueError("未知采集器设置")
    values = {"python": sys.executable, "interval_seconds": 15, "enabled_sources": [], **config}
    if type(values["interval_seconds"]) not in (int, float) or not 5 <= values["interval_seconds"] <= 3600:
        raise ValueError("采集间隔须为 5–3600 秒")
    sources = values["enabled_sources"]
    if not isinstance(sources, list) or any(s not in ("chatapp", "imapp") for s in sources) or len(sources) != len(set(sources)):
        raise ValueError("采集器来源须为 chatapp / imapp")
    for key in ("python", "chatapp_config", "imapp_data_root"):
        if key in values and (not isinstance(values[key], str) or not values[key].strip() or len(values[key]) > 1000):
            raise ValueError(f"{key} 须为本机路径")
    if not Path(values["python"]).is_file():
        raise ValueError("采集器 Python 不存在，请选择本机 Python")
    for source, key in (("chatapp", "chatapp_config"), ("imapp", "imapp_data_root")):
        if source in sources and (key not in values or not Path(values[key]).exists()):
            raise ValueError(f"{source} 的配置或数据目录不存在")
    return values


COLLECTOR_SCRIPTS = {"chatapp": ROOT / "message_tools/chatapp_tool/autoclaw/chatapp-summary/chatapp_context.py",
                     "imapp": ROOT / "message_tools/imapp_tool/聊天软件消息推送工具包/聊天软件导出.py"}


def configured_collector(directory, center):
    config_path = Path(directory) / "message_connectors.json"
    last_run = 0.0

    def collect():
        nonlocal last_run
        feishu_events = Path(directory) / "feishu_events.jsonl"
        if feishu_events.is_file():
            try:
                center.poll_jsonl(feishu_events)
            except (OSError, ValueError) as error:
                center.mark_health("feishu", error=f"飞书事件读取失败：{type(error).__name__}")
        if not config_path.is_file():
            return
        config = json.loads(config_path.read_text(encoding="utf-8"))
        interval = config.get("interval_seconds", 15)
        if type(interval) not in (int, float) or not 5 <= interval <= 3600:
            raise ValueError("消息数据库刷新间隔须为 5–3600 秒")
        python = config.get("python", sys.executable)
        if not isinstance(python, str) or not Path(python).is_file():
            raise ValueError("消息导出的 Python 路径无效")
        if time.monotonic() - last_run < interval:
            return
        last_run = time.monotonic()
        for source in ("chatapp", "imapp"):
            status_key = source + "_database"
            if not COLLECTOR_SCRIPTS[source].is_file():
                continue          # 公开版不带聊天软件 / 聊天软件 采集工具：什么都不做
            if source not in config.get("enabled_sources", ["chatapp", "imapp"]):
                center.source_status[status_key] = "本机数据库采集已暂停"
                continue
            try:
                path = Path(directory) / (source + "_events.jsonl")
                previous = path.stat().st_mtime_ns if path.is_file() else None
                incoming = Path(directory) / "audio" / "incoming"
                incoming.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="message-", dir=directory) as working, tempfile.TemporaryDirectory(prefix="desktop-", dir=incoming) as audio_working:
                    assert Path(working).resolve().is_relative_to(Path(directory).resolve())
                    assert Path(audio_working).resolve().is_relative_to(incoming.resolve())
                    _mark_export_working(working, directory, "message-")
                    _mark_export_working(audio_working, incoming, "desktop-")
                    env = os.environ.copy()
                    env["CANAAN_AUDIO_INCOMING"] = audio_working
                    if source == "chatapp":
                        cfg = json.loads(Path(config["chatapp_config"]).read_text(encoding="utf-8"))
                        cfg.update(state_root=working + "/state", context_root=working + "/context")
                        temporary_config = Path(working) / "chatapp.json"
                        temporary_config.write_text(json.dumps(cfg), encoding="utf-8")
                        cmd = [python, "-X", "utf8", str(COLLECTOR_SCRIPTS["chatapp"]),
                               "--config", str(temporary_config), "--hours", "24", "--events-file", str(Path(directory) / "chatapp_events.jsonl")]
                    else:
                        env["CANAAN_IMApp_DATA_ROOT"] = config["imapp_data_root"]
                        cmd = [python, "-X", "utf8", str(COLLECTOR_SCRIPTS["imapp"])]
                    result = subprocess.run(cmd, env=env, capture_output=True, timeout=60,
                                            creationflags=0x08000000 if os.name == "nt" else 0)
                    if result.returncode:
                        center.source_status[status_key] = "数据库导出失败；本轮未更新消息，请检查登录、路径和本地密钥"
                        center.mark_health(status_key, error=center.source_status[status_key])
                        continue
                    if not path.is_file() or path.stat().st_mtime_ns == previous:
                        center.source_status[status_key] = "导出未更新事件文件；请检查导出配置"
                        center.mark_health(status_key, error=center.source_status[status_key])
                        continue
                    center.poll_jsonl(path)
                    center.source_status[status_key] = f"本地数据库已刷新（最近24小时，{time.strftime('%H:%M:%S')}）"
                    center.mark_health(status_key, success=True)
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                center.source_status[status_key] = f"本地采集失败：{type(error).__name__}"
                center.mark_health(status_key, error=center.source_status[status_key])

    return collect
