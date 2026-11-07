# -*- coding: utf-8 -*-
"""Claude Code 钩子：等你处理 / 一轮做完时，报给本机的团团（灵动岛提示）。

用法见同目录 README.md。只用标准库；2 秒内报不出去就算了，始终返回 0，绝不卡住 Claude Code。
令牌放在环境变量 CANAAN_PROGRESS_TOKEN（为 Claude Code 单独配对、只有 progress 能力）。
只发往本机地址；只报状态、项目文件夹名和一句提示（截短），不报对话内容。
"""
import json
import os
import sys
import urllib.parse
import urllib.request

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def build(event):
    """钩子输入 → 要报的进度；不认识的事件返回 None。"""
    name = event.get("hook_event_name") or event.get("hookEventName") or ""
    project = os.path.basename(os.path.normpath(event.get("cwd") or "")) or "项目"
    if name == "Notification":
        msg = str(event.get("message") or "等你处理")
        return {"kind": "waiting", "title": f"{project}：{msg}"[:80], "key": f"cc:{event.get('session_id', '')}"[:64]}
    if name in ("Stop", "SubagentStop"):
        what = "子任务做完了" if name == "SubagentStop" else "这一轮做完了"
        return {"kind": "done", "title": f"{project}：{what}"[:80], "key": f"cc:{event.get('session_id', '')}"[:64]}
    return None


def send(payload, url=None, token=None, timeout=2.0):
    url = url or os.environ.get("CANAAN_API_URL", "http://127.0.0.1:8789")
    token = token or os.environ.get("CANAAN_PROGRESS_TOKEN", "")
    host = urllib.parse.urlsplit(url).hostname or ""
    if not payload or not token or host not in LOCAL_HOSTS:
        return False
    req = urllib.request.Request(url.rstrip("/") + "/api/progress", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    send(build(event))
    return 0


if __name__ == "__main__":
    sys.exit(main())
