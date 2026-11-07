# -*- coding: utf-8 -*-
"""
智能体任务看板的数据层（不依赖 Tk，可单独测试）：只读地从本机的 OpenMuse / OpenDots 取任务列表，整理成统一格式。

  OpenMuse：POST http://localhost:8787/api/session 取令牌 → GET /api/agent（快照里的 tasks）
  OpenDots：GET http://localhost:4310/api/state（tasks）
只访问本机地址，不改任何任务（不暂停、不取消、不创建）；标题和结果显示前先脱敏。
"""
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "agent_services.json"
logger = logging.getLogger("canaan_agent_board_data")


def load_local_agent_config(path=CONFIG_PATH):
    """配置坏了只回退默认值，不能让看板 import 阶段直接崩掉。"""
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError) as e:
        logger.warning("agent_services.json 读取失败，使用默认本机地址：%s", e)
        return {}


LOCAL_AGENT_CONFIG = load_local_agent_config()


def _loopback(url):
    """Muse / Dots 只听 127.0.0.1；Windows 上 localhost 先试 IPv6，每次请求白等约 2 秒，这里统一换成 127.0.0.1。"""
    return url.rstrip("/").replace("//localhost:", "//127.0.0.1:")


MUSE_API = _loopback(os.environ.get("CANAAN_MUSE_API_URL", LOCAL_AGENT_CONFIG.get("muse_api", "http://127.0.0.1:8787")))
MUSE_WEB = _loopback(os.environ.get("CANAAN_MUSE_WEB_URL", LOCAL_AGENT_CONFIG.get("muse_web", "http://127.0.0.1:8081")))
DOTS_API = _loopback(os.environ.get("CANAAN_DOTS_API_URL", LOCAL_AGENT_CONFIG.get("dots_api", "http://127.0.0.1:4310")))
DOTS_WEB = _loopback(os.environ.get("CANAAN_DOTS_WEB_URL", LOCAL_AGENT_CONFIG.get("dots_web", "http://127.0.0.1:5173")))

# 统一状态：running / queued / waiting（等主人）/ done / failed / paused / cancelled / scheduled
STATUS_MAP = {
    "running": "running", "queued": "queued", "scheduled": "scheduled", "paused": "paused",
    "waiting_approval": "waiting", "waiting_input": "waiting",
    "succeeded": "done", "completed": "done", "failed": "failed", "interrupted": "failed", "cancelled": "cancelled",
}
STATUS_LABEL = {"running": "进行中", "queued": "排队", "waiting": "等你处理", "scheduled": "定时", "paused": "暂停",
                "done": "完成", "failed": "失败", "cancelled": "已取消"}
ORDER = ["waiting", "running", "queued", "scheduled", "paused", "failed", "done", "cancelled"]


MASK_FAILED = "（脱敏失败，内容已隐藏）"


def _mask(text):
    """脱敏出错时隐藏内容，不退回原文。"""
    if not text:
        return ""
    try:
        from canaan_privacy_shield import PrivacyShield
        return PrivacyShield.mask_text(text)[0]
    except Exception:
        return MASK_FAILED


def _ts(v):
    """毫秒时间戳 / ISO 字符串 → 秒时间戳。"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def normalize_muse(snapshot):
    tasks = (snapshot or {}).get("tasks") or []
    out = []
    for t in tasks:
        plan = t.get("plan") or []
        done = sum(1 for s in plan if s.get("status") == "succeeded")
        cur = next((s.get("title") for s in plan if s.get("status") in ("running", "waiting")), "")
        status = STATUS_MAP.get(t.get("status"), "queued")
        note = t.get("question") if status == "waiting" else (t.get("error") if status == "failed" else t.get("result"))
        out.append({"id": t.get("id", ""), "title": _short(_mask(t.get("title") or t.get("prompt") or "未命名任务"), 40),
                    "status": status, "steps": (done, len(plan)), "current": _short(_mask(cur), 30),
                    "updated": _ts(t.get("updatedAt") or t.get("createdAt")), "note": _short(_mask(note or ""), 60)})
    return sort_tasks(out)


def normalize_dots(state):
    tasks = (state or {}).get("tasks") or []
    out = []
    for t in tasks:
        status = STATUS_MAP.get(t.get("status"), "queued")
        if status == "queued" and t.get("intervalSeconds"):
            status = "scheduled"
        out.append({"id": t.get("id", ""), "title": _short(_mask(t.get("prompt") or "未命名任务"), 40), "status": status,
                    "steps": None, "current": "", "updated": _ts(t.get("updatedAt") or t.get("createdAt")),
                    "note": _short(_mask(t.get("error") or ""), 60)})
    return sort_tasks(out)


def sort_tasks(tasks):
    return sorted(tasks, key=lambda t: (ORDER.index(t["status"]) if t["status"] in ORDER else 99, -t["updated"]))


def counts(tasks):
    c = {k: 0 for k in STATUS_LABEL}
    for t in tasks:
        c[t["status"]] = c.get(t["status"], 0) + 1
    return c


def _get_json(url, data=None, headers=None, timeout=2.5):
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_muse(get=_get_json):
    """返回 {"online", "tasks", "error"}。"""
    try:
        if get is _get_json:
            from canaan_agent_coordinator import OpenMuseProvider
            token = OpenMuseProvider.get_token(timeout=2.5)
        else:
            token = get(f"{MUSE_API}/api/session", data=b"{}").get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("该端口不是可登录的 Muse API")
        try:
            snap = get(f"{MUSE_API}/api/agent", headers={"Authorization": f"Bearer {token}"})
        except Exception as e:
            if get is not _get_json or getattr(e, "code", None) != 401:
                raise
            token = OpenMuseProvider.get_token(timeout=2.5, force=True)
            if not token:
                raise ValueError("Muse 会话已失效，重新登录失败") from e
            snap = get(f"{MUSE_API}/api/agent", headers={"Authorization": f"Bearer {token}"})
        if not isinstance(snap, dict) or not isinstance(snap.get("tasks"), list):
            raise ValueError("该端口未返回 Muse 任务状态")
        return {"online": True, "tasks": normalize_muse(snap), "web_url": MUSE_WEB, "error": "", "at": time.time()}
    except Exception as e:
        return {"online": False, "tasks": [], "web_url": MUSE_WEB, "error": _short(str(e), 60), "at": time.time()}


def fetch_dots(get=_get_json):
    try:
        state = get(f"{DOTS_API}/api/state")
        if not isinstance(state, dict) or not isinstance(state.get("tasks"), list):
            raise ValueError("该端口未返回 Dots 任务状态，可能被其他程序占用")
        return {"online": True, "tasks": normalize_dots(state), "web_url": DOTS_WEB, "error": "", "at": time.time()}
    except Exception as e:
        return {"online": False, "tasks": [], "web_url": DOTS_WEB, "error": _short(str(e), 60), "at": time.time()}


def agent_detail(provider, task_id, get=_get_json):
    if provider not in ("muse", "dots") or not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
        raise ValueError("须提供 muse / dots 与合法任务编号")
    if provider == "muse":
        from canaan_agent_coordinator import OpenMuseProvider
        token = OpenMuseProvider.get_token(timeout=2.5) if get is _get_json else get(MUSE_API + "/api/session", data=b"{}").get("token")
        if not token:
            raise ValueError("Muse 尚未连接")
        snapshot = get(MUSE_API + "/api/agent", headers={"Authorization": "Bearer " + token})
        task = next((t for t in snapshot.get("tasks", []) if t.get("id") == task_id), None)
        if task is None:
            raise KeyError("Muse 任务不存在")
        result = {"status": task.get("status"), "text": task.get("result") or task.get("question") or task.get("error") or "尚无结果"}
    else:
        detail = get(DOTS_API + "/api/tasks/" + task_id)
        latest = (detail.get("runs") or [{}])[0]
        output = latest.get("result") or {}
        result = {"status": detail["task"].get("status"), "text": output.get("text") or latest.get("error") or "尚无结果",
                  "sample": output.get("sample"), "sources": output.get("sources", [])}
    from canaan_privacy_shield import PrivacyShield
    return {"provider": provider, "task_id": task_id, **json.loads(PrivacyShield.mask_text(json.dumps(result, ensure_ascii=False))[0])}


def ago(ts, now=None):
    if not ts:
        return ""
    d = max(0, int((now or time.time()) - ts))
    if d < 60:
        return f"{d} 秒前"
    if d < 3600:
        return f"{d // 60} 分钟前"
    if d < 86400:
        return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"
