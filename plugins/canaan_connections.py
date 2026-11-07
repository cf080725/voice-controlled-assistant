"""Portable local settings; each user's credentials stay with their provider."""
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from canaan_message_connectors import validate_collector_config
from canaan_message_priority import SOURCES

DEFAULT_AGENTS = {"muse_api": "http://127.0.0.1:8787", "muse_web": "http://127.0.0.1:8081",
                  "dots_api": "http://127.0.0.1:4310", "dots_web": "http://127.0.0.1:5173"}
_lock = threading.RLock()


def validate_agent_urls(values):
    if not isinstance(values, dict) or set(values) - DEFAULT_AGENTS.keys():
        raise ValueError("未知智能体连接设置")
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > 300:
            raise ValueError(f"{key} 须为本机服务地址")
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or url.hostname not in ("localhost", "127.0.0.1", "::1") or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError(f"{key} 仅接受没有凭据的本机根地址")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("端口超出范围")
    return {k: v.rstrip("/") for k, v in values.items()}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def snapshot(directory):
    directory = Path(directory)
    config = _read(directory / "agent_services.json")
    agents = {key: os.environ.get("CANAAN_" + key.upper() + "_URL", config.get(key, default)) for key, default in DEFAULT_AGENTS.items()}
    collector = {k: v for k, v in _read(directory / "message_connectors.json").items()
                 if k in ("python", "interval_seconds", "enabled_sources", "chatapp_config", "imapp_data_root")}
    collector.setdefault("python", sys.executable)
    collector.setdefault("interval_seconds", 15)
    collector.setdefault("enabled_sources", ["chatapp", "imapp"] if (directory / "message_connectors.json").exists() else [])
    return {"agents": agents, "collectors": collector,
            "environment_overrides": [key for key in agents if os.environ.get("CANAAN_" + key.upper() + "_URL")],
            "sources": SOURCES, "api": {"ingest": "/api/messages", "inbox": "/api/messages/inbox", "propose": "/api/tasks"},
            "mcp": {"transport": "stdio", "script": str(Path(__file__).resolve().parent.parent / "canaan_mcp_server.py")},
            "claw": {"mode": "client", "protocols": ["HTTP Bearer", "MCP stdio"], "confirmation": "操作提案在团团看板确认",
                     "registration": {k: v for k, v in _read(directory / "claw_registration.json").items() if k in ("client", "tools", "probed_at", "cloud_readonly_verified_at", "cloud_proposal_verified_at")}},
            "feishu": {"mode": "SDK 长连接 → 本机 JSONL", "event_file": str(directory / "feishu_events.jsonl"), "text_only": False,
                       "listener": listener_status(directory), "settings": _read(directory / "feishu_listener.json")},
            "voice": {"audio_roots": _read(directory / "voice_settings.json").get("audio_roots", []), "local_only": True}}


def listener_status(directory):
    try:
        state = _read(Path(directory) / "feishu_listener_status.json")
        if not isinstance(state, dict):
            raise ValueError("无效监听状态")
    except (OSError, ValueError):
        return {"state": "error", "error": "监听状态文件无法读取"}
    if not state:
        return {"state": "not_configured"}
    updated = state.get("updated_at", 0)
    if type(updated) not in (int, float) or not math.isfinite(updated):
        return {"state": "error", "error": "监听心跳时间无效"}
    offline = {"state": "offline", "last_event_at": state.get("last_event_at", 0)}
    if time.time() - updated > 20:
        return offline
    pid = state.get("pid")
    if type(pid) is not int or pid <= 0:
        return {"state": "error", "error": "监听进程编号无效"}
    try:
        import psutil
        process = psutil.Process(pid)
        if not process.is_running() or process.create_time() > updated:
            return offline
    except ImportError:
        return {"state": "error", "error": "未安装 psutil，无法核对监听进程"}
    except psutil.NoSuchProcess:
        return offline
    except psutil.AccessDenied:
        return {"state": "error", "error": "无法核对监听进程权限"}
    return {key: state[key] for key in ("state", "updated_at", "last_event_at") if key in state}


def save(directory, body):
    directory = Path(directory)
    if not isinstance(body, dict) or set(body) - {"agents", "collectors", "feishu", "voice"} or not body:
        raise ValueError("连接设置须包含 agents、collectors、feishu 或 voice")
    agents = validate_agent_urls(body["agents"]) if "agents" in body else None
    collectors = validate_collector_config(body["collectors"]) if "collectors" in body else None
    from canaan_feishu_service import validate_feishu
    feishu = validate_feishu(body["feishu"]) if "feishu" in body else None
    voice = body.get("voice")
    if voice is not None:
        if not isinstance(voice, dict) or set(voice) != {"audio_roots"} or not isinstance(voice["audio_roots"], list) or len(voice["audio_roots"]) > 10 or any(not isinstance(p, str) or p.startswith(("\\\\", "//")) or not Path(p).is_absolute() or not Path(p).is_dir() for p in voice["audio_roots"]):
            raise ValueError("audio_roots 须为最多10个已存在的本机目录")
    with _lock:
        for filename, values in (("agent_services.json", agents), ("message_connectors.json", collectors), ("feishu_listener.json", feishu), ("voice_settings.json", voice)):
            if values is None:
                continue
            updated = {**_read(directory / filename), **values}
            temp = (directory / filename).with_suffix(".tmp")
            temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(directory / filename)
        if agents is not None:
            apply_agents(snapshot(directory)["agents"])
    return snapshot(directory)


def apply_agents(agents):
    # Existing callers import constants; update loaded copies and sessions together.
    import canaan_agent_board_data as board
    for key, value in agents.items():
        setattr(board, key.upper(), value)
        board.LOCAL_AGENT_CONFIG[key] = value
    coordinator = sys.modules.get("canaan_agent_coordinator")
    if coordinator:
        for key, value in agents.items():
            setattr(coordinator, key.upper() + "_URL", value)
        for provider in (coordinator.OpenMuseProvider, coordinator.OpenDotsProvider):
            provider._cached_context = None
        coordinator.OpenMuseProvider._token_cache = None
    for name in ("canaan_agent_watcher", "canaan_action_framework.actions.agent_integration_actions"):
        module = sys.modules.get(name)
        if module:
            for key, value in agents.items():
                attr = key.upper() + "_URL"
                if hasattr(module, attr):
                    setattr(module, attr, value)
    bridge = sys.modules.get("canaan_dots_memory_bridge")
    if bridge:
        bridge.DOTS_API = agents["dots_api"]
    manager = sys.modules.get("canaan_agent_service_manager")
    if manager:
        for key, value in agents.items():
            manager.SERVICES[key].update(url=value, port=urlsplit(value).port or (443 if value.startswith("https:") else 80))
