"""Supervise only the Feishu listener started by this runtime."""
import json
import os
import sys
import time
from pathlib import Path


def validate_feishu(values):
    if not isinstance(values, dict) or set(values) - {"enabled", "python", "lark_config"}:
        raise ValueError("未知飞书监听设置")
    if "enabled" in values and type(values["enabled"]) is not bool:
        raise ValueError("飞书 enabled 须为布尔值")
    for key in ("python", "lark_config"):
        if key in values and (not isinstance(values[key], str) or not Path(values[key]).is_file()):
            raise ValueError(f"飞书 {key} 文件不存在")
    return values.copy()


class FeishuService:
    def __init__(self, directory, center):
        from canaan_tray import Child, Job
        self.directory, self.center = Path(directory), center
        self.child = None
        self.Child, self.Job = Child, Job
        self.job = None
        self.config = None
        self.stopped = False
        self.state, self.error = "disabled", ""

    def _external(self):
        path = self.directory / "feishu_listener_status.json"
        if not path.is_file():
            return False
        state = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - state.get("updated_at", 0) > 20 or not state.get("pid"):
            return False
        if self.child and self.child.proc and state["pid"] == self.child.proc.pid:
            return False
        try:
            import psutil
            proc = psutil.Process(state["pid"])
            script = str(Path(__file__).resolve().parent.parent / "message_tools" / "feishu_connector.py").casefold()
            return any(str(Path(arg).resolve()).casefold() == script for arg in proc.cmdline() if "feishu_connector.py" in arg)
        except ImportError:
            return True
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True

    def snapshot(self):
        return {"state": self.state, "owned": bool(self.child and self.child.alive()), "error": self.error}

    def tick(self):
        if self.stopped:
            return
        path = self.directory / "feishu_listener.json"
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"enabled": False}
        validate_feishu(config)
        if self.config != config:
            self.stop_child()
            self.config = config
        if not config.get("enabled"):
            self.state = "external" if self._external() else "disabled"
            return
        if self.child and self.child.proc and not self.child.alive():
            self.child.backoff.failed()
            self.child.proc = None
            self.state, self.error = "backoff", "监听进程退出，按退避间隔重试"
            self.center.mark_health("feishu", error=self.error)
        if self.child and self.child.alive():
            self.state, self.error = "managed", ""
            return
        if self._external():
            self.state, self.error = "external", "独立监听器正在运行；迁移前请先确认停止它"
            return
        if self.child is None:
            root = Path(__file__).resolve().parent.parent
            env = os.environ.copy()
            env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            cmd = [config.get("python", sys.executable), str(root / "message_tools" / "feishu_connector.py"), "--data-dir", str(self.directory)]
            if config.get("lark_config"):
                cmd += ["--lark-config", config["lark_config"]]
            if os.name == "nt":
                self.job = self.Job()
            self.child = self.Child("feishu", cmd, str(root), env=env, job=self.job)
        if self.child.backoff.ready():
            try:
                self.child.start()
                self.state, self.error = "managed", ""
            except OSError as ex:
                self.child.backoff.failed()
                self.state, self.error = "backoff", f"飞书监听启动失败：{type(ex).__name__}"
                self.center.mark_health("feishu", error=self.error)

    def stop_child(self):
        if self.child:
            self.child.stop()
            self.child = None
        if self.job:
            from canaan_tray import kernel32
            kernel32.CloseHandle(self.job.handle)
            self.job = None

    def stop(self):
        self.stopped = True
        self.stop_child()
        self.state = "stopped"
