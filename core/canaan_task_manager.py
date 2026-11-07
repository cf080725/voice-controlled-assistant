# -*- coding: utf-8 -*-
"""
================================================================================
   👑 团团 (Tuantuan) · Desk OS 任务去重、并发管控与实时事实遥测引擎
================================================================================
   融合 GitHub 顶级开源架构 (Trigger.dev / BullMQ / Open-Interpreter)：
   1. 幂等性特征防抖与智能去重 (Idempotent Debounce Window)
   2. 操作系统关键动作互斥锁 (Non-Reentrant Execution Lock)
   3. 毫秒级任务阶段事实状态流 (Step-by-Step Fact Telemetry Stream)
   4. 动态显存负载实时监控 (Real-time VRAM Health Telemetry)
================================================================================
"""

import os
import sys
import time
import hashlib
import threading
from typing import Optional, Callable, Dict, Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

class CanaanTaskManager:
    """团团整机任务调度器与实时事实遥测中枢"""

    def __init__(self, debounce_window: float = 1.2):
        self.debounce_window = debounce_window
        self._recent_fingerprints: Dict[str, float] = {}
        self._fp_lock = threading.Lock()
        
        # 操作系统底层独占操作互斥锁 (防止聊天软件双开、按键抢占与并发乱序)
        self.os_exec_lock = threading.Lock()
        
        # 活跃任务跟踪
        self.current_task_id: Optional[str] = None
        self.current_step_desc: str = "系统待命中"
        self.current_step_index: int = 0
        self.current_total_steps: int = 0
        
        # 遥测回调钩子
        self._telemetry_callbacks = []

    def register_callback(self, cb: Callable[[dict], None]):
        self._telemetry_callbacks.append(cb)

    def check_and_create_task(self, command_text: str, scope: str = '') -> tuple[Optional[str], bool]:
        """
        检查指令是否重复并分配全局唯一任务 ID
        返回: (task_id, is_duplicate)
        """
        clean_cmd = command_text.strip().lower()
        now = time.time()
        fp = hashlib.md5((scope + '\0' + clean_cmd).encode("utf-8")).hexdigest()

        with self._fp_lock:
            # 清理过期的历史指纹
            expired = [k for k, t in self._recent_fingerprints.items() if now - t > self.debounce_window * 3]
            for k in expired:
                del self._recent_fingerprints[k]

            last_time = self._recent_fingerprints.get(fp, 0.0)
            if now - last_time < self.debounce_window:
                # 命中防抖窗口，判定为重复触发
                print(f"[⚠️ 任务去重拦截] 指令 “{command_text}” 在 {round(now - last_time, 2)}s 内重复提交，已自动防抖拦截！")
                self.notify_telemetry({
                    "type": "task_duplicate",
                    "command": command_text,
                    "desc": f"⚠️ 指令“{command_text[:10]}”高频重复，已自动去重拦截"
                })
                return None, True

            self._recent_fingerprints[fp] = now
            task_id = f"T{int(now * 100) % 10000:04d}"
            self.current_task_id = task_id
            return task_id, False

    def notify_step(self, task_id: str, step: int, total_steps: int, step_desc: str, status: str = "running"):
        """发布实时阶段事实遥测数据"""
        self.current_step_index = step
        self.current_total_steps = total_steps
        self.current_step_desc = step_desc
        
        event = {
            "type": "task_step",
            "task_id": task_id,
            "step": step,
            "total_steps": total_steps,
            "desc": step_desc,
            "status": status,
            "timestamp": time.time()
        }
        self.notify_telemetry(event)
        print(f"[{task_id} 事实进展 {step}/{total_steps}] {step_desc}")

    def notify_finish(self, task_id: str, success: bool, message: str):
        """任务完成通知"""
        event = {
            "type": "task_finished",
            "task_id": task_id,
            "success": success,
            "message": message,
            "timestamp": time.time()
        }
        self.notify_telemetry(event)
        self.current_task_id = None
        self.current_step_desc = "待命中"

    def notify_telemetry(self, event: dict):
        for cb in self._telemetry_callbacks:
            try:
                cb(event)
            except Exception:
                pass

    @staticmethod
    def get_vram_telemetry_quick() -> str:
        """轻量级实时提取当前显存负载情况"""
        try:
            import subprocess
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                creationflags=0x08000000 if sys.platform == "win32" else 0,
                timeout=1.0
            ).decode("utf-8").strip()
            used_mb, total_mb = [float(x.strip()) for x in out.split(",")]
            used_gb = round(used_mb / 1024, 1)
            total_gb = round(total_mb / 1024, 1)
            percent = int((used_mb / total_mb) * 100)
            tag = "极佳" if percent < 70 else ("良好" if percent < 88 else "预警")
            return f"显存: {used_gb}G/{total_gb}G ({percent}%) · {tag}"
        except Exception:
            return "显存: 正常"

# 全局单例任务调度器
GLOBAL_TASK_MANAGER = CanaanTaskManager(debounce_window=1.2)
