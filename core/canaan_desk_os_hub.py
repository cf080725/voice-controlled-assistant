import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import ctypes
user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    hw_init = user32.OpenWindowStationW('WinSta0', False, 0x10000000)
    if hw_init: user32.SetProcessWindowStation(hw_init)
    hd_init = user32.OpenDesktopW('Default', 0, False, 0x10000000)
    if hd_init: user32.SetThreadDesktop(hd_init)
except Exception:
    pass

import os
import sys

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
# 必须在导入 numpy 之前。10-24 本机 18 线程：numpy 自带的 OpenBLAS 按线程预留缓冲，光 import 就占 595 MB 提交内存，
# 4 线程 145 MB、1 线程 49 MB；之前出现过“Unable to allocate 756 KiB”。主程序的矩阵运算都很小，4 线程够用。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import time
import json
import re
import queue
import subprocess
import webbrowser
import collections
import threading
import urllib.request
import urllib.parse
import winsound
import ctypes
from ctypes import wintypes
from pathlib import Path
from canaan_filelock import file_lock
import numpy as np
import sounddevice as sd
import soundfile as sf
import tkinter as tk
from tkinter import ttk

# ----------------- 单进程单实例互斥锁（从根源杜绝多进程并发与重音干扰） -----------------
SINGLE_INSTANCE_MUTEX = None

def ensure_single_instance():
    global SINGLE_INSTANCE_MUTEX
    try:
        kernel32 = ctypes.windll.kernel32
        CreateMutexW = kernel32.CreateMutexW
        CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        CreateMutexW.restype = wintypes.HANDLE
        GetLastError = kernel32.GetLastError

        mutex_name = "Global\\Canaan_Desk_OS_Hub_SingleInstance_Mutex_2026"
        SINGLE_INSTANCE_MUTEX = CreateMutexW(None, False, mutex_name)
        if GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            print("=" * 66)
            print("[!] ⚠️ 警告：检测到【团团 Desk OS 全局整机语音管控中枢】已在后台运行中！")
            print("[*] 正在直接激活现有实例。禁止多进程并发，彻底杜绝麦克风冲突与语音重叠！")
            print("=" * 66)
            sys.exit(0)
    except Exception as e:
        print(f"[!] 互斥锁检测提示: {e}")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tuantuan_voice_engine import TuantuanVoiceEngine, EMOTION_CONFIG, GPT_SOVITS_DIR, SpeechTextSanitizer
from canaan_os_executor import CanaanOSExecutor
from canaan_wake_word_engine import CanaanWakeWordDetector
from canaan_task_manager import GLOBAL_TASK_MANAGER
from canaan_latency import TurnTracer, NULL_TRACER
from canaan_endpoint import create_endpointer
from canaan_asr import transcribe
import canaan_config
import canaan_gamemode
from canaan_agent_service_manager import is_port_listening
from canaan_companion import (CompanionChat, UserProfile, build_context, extract_facts, handle_memory_command,
                              pick_opener, all_openers)

# 助手显示名与唤醒词（canaan_config.py 可改）
ASSISTANT_NAME = getattr(canaan_config, "ASSISTANT_NAME", "团团")
WAKE_PHRASES = tuple(getattr(canaan_config, "WAKE_PHRASES", ()) or (ASSISTANT_NAME,))
WAKE_WORD = WAKE_PHRASES[0]
# 这些动作做完不进长期记忆（太琐碎，记了只会挤掉有用的）
TRIVIAL_ACTIONS = frozenset({"chat", "volume", "volume_up", "volume_down", "mute", "set_brightness", "media_control",
                             "lock_screen", "screenshot", "stop", "cancel", "confirm", "time", "weather"})

STOP_WORDS = {
    "停", "停下", "停一下", "暂停", "打住", "闭嘴",
    "别说了", "别说", "别念了", "别吵", "别吵了", "安静",
    "闭麦", "算了", "不用了", "不用", "够了", "好了",
    "等一下", "等等", "先别说", "听我说"
}

QUICK_ACTION_MAP = {
    "锁屏": ("lock_screen", ""),
    "截图": ("screenshot", ""),
    "静音": ("volume_mute", ""),
    "下一首": ("media_control", "next"),
    "大点声": ("volume_up", "5"),
    "小点声": ("volume_down", "5"),
}
_WAKE_ALIASES = tuple(getattr(canaan_config, "WAKE_ASR_ALIASES", ()) or (ASSISTANT_NAME,))
WAKE_PREFIX_RE = re.compile(r"^(你好)?(" + "|".join(map(re.escape, _WAKE_ALIASES)) + r")[，, 。\s]*")

# Ensure ffmpeg from GPT-SoVITS is in PATH if available
if GPT_SOVITS_DIR and os.path.exists(GPT_SOVITS_DIR):
    if GPT_SOVITS_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = GPT_SOVITS_DIR + ";" + os.environ.get("PATH", "")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from faster_whisper import WhisperModel
import keyboard

SAMPLE_RATE = 16000
CHANNELS = 1
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

# 系统提示词与大模型调用已移至 canaan_intent.py（此处重新导出，兼容旧脚本）
from canaan_intent import (
    DESK_SYSTEM_PROMPT, OllamaIntentClient, ConversationMemory,
    SLOW_ACTIONS, PREWARM_PHRASES, ack_phrase_for, resolve_references, PendingConfirmation,
    fast_match_intent, apply_command_guard, is_noise_utterance, describe_action, resend_request,
    keeps_pending, ChatAppDraft, draft_fill, ClarifyContext, remember_clarify, clarify_merge,
)
from canaan_reply_policy import failure_detail
import canaan_turn_pipeline  # noqa: E402  一句话的决策链（纯函数，可单独测）
import canaan_contact_match  # noqa: E402  听错的联系人名按拼音对到已知联系人


def fact_store_of(hub):
    """当前说话的人的长期事实：说过“我是小明”就是小明自己的（canaan_people），否则是主人的 profile。"""
    binding = getattr(getattr(hub, '_turn_local', None), 'binding', None)
    if binding is not None:
        return binding.store
    people = getattr(hub, "people", None)
    try:
        return (people.active_store() if people is not None else None) or hub.profile
    except Exception as e:
        raise RuntimeError('当前使用者记忆不可用，请重新选择使用者') from e


def people_command_of(hub, text):
    """“我是小明 / 换回主人 / 现在是谁在用”；换了人就清掉短期多轮记忆，免得接着上一个人的话题。"""
    people = getattr(hub, "people", None)
    if people is None:
        return None
    before = people.active()
    decision = people.command(text)
    if decision and people.active() != before and getattr(hub, "memory", None) is not None:
        context = getattr(hub, 'user_context', None)
        if context is not None:
            context.sync()
        else:
            hub.memory.clear()
            for name in ('chatapp_draft', 'clarify'):
                draft = getattr(hub, name, None)
                if draft is not None:
                    draft.clear()
            pending = getattr(hub, 'pending', None)
            if pending is not None:
                pending.resolve('取消')
    return decision


class CanaanDeskOSHub:
    def __init__(self, root):
        self.root = root
        self.root.title(f"👑 {ASSISTANT_NAME} · Desk OS 全局整机语音管控中枢")
        
        # Windows Transparent Floating Pill Window (Apple 灵动悬浮岛架构)
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-alpha", 0.96)
        
        self.TRANS_COLOR = "#010203"
        self.root.configure(bg=self.TRANS_COLOR)
        try:
            self.root.wm_attributes("-transparentcolor", self.TRANS_COLOR)
        except Exception:
            pass

        # Dimensions and Position: Sleek Apple Dynamic Island Floating Capsule
        screen_w = self.root.winfo_screenwidth()
        self.screen_w = screen_w
        self.mini_w = 540
        self.mini_h = 42
        self.expanded_w = 880
        self.expanded_h = 134
        self.win_y = 10

        self.is_collapsed = True
        self.is_hidden = False
        self._autofold_job = None

        init_x = (screen_w - self.mini_w) // 2
        self.root.geometry(f"{self.mini_w}x{self.mini_h}+{init_x}+{self.win_y}")
        self.root.hub_instance = self

        self.engine = TuantuanVoiceEngine()
        self.kws_detector = None
        self.asr_model = None
        self.sense_voice = None
        self._asr_ready = threading.Event()
        self._asr_load_error = ""
        self._ui_last_tick = 0.0
        self._mic_stream_active = False
        self._mic_last_read = 0.0
        # 声纹锁定（默认关；托盘菜单或配置打开，未录入声纹时自动放行）
        from canaan_speaker import SpeakerGate
        self.speaker_gate = SpeakerGate(enabled=getattr(canaan_config, "SPEAKER_LOCK_ENABLED", False),
                                        threshold=getattr(canaan_config, "SPEAKER_LOCK_THRESHOLD", 0.6))
        self._wake_ring = collections.deque(maxlen=20)  # 最近 2 秒音频（20 × 100ms），唤醒时做声纹判定
        if getattr(canaan_config, "INTENT_COMPACT_MODEL", ""):
            from canaan_intent import create_compact_intent_client
            # 用户选定的模型失败时不进入旧模型／商业服务自动回退链。
            self.intent_client = create_compact_intent_client(canaan_config.INTENT_COMPACT_MODEL)
        else:
            self.intent_client = OllamaIntentClient()
        # 闲聊交给单独的陪聊模块（人设提示词 + 情境 + 长期记忆 + 主动建议，边生成边说）
        self.companion = CompanionChat(model=getattr(canaan_config, "CHAT_MODEL", "canaan-chat:3b"),
                                       fallback_model=getattr(canaan_config, "CHAT_FALLBACK_MODEL", None))
        # 长期记忆：SQLite + 向量/全文混合检索（canaan_memory）；不可用时退回旧的 JSON 记忆
        try:
            import canaan_memory
            self.profile = canaan_memory.get_store()
            self.memory_mgr = None if not getattr(canaan_config, "LOCAL_LLM_ENABLED", getattr(canaan_config, "LLM_ENABLED", True)) else canaan_memory.MemoryManager(
                self.profile, model=getattr(canaan_config, "CHAT_MODEL", "canaan-chat:3b"))
            threading.Thread(target=self._memory_tick_loop, daemon=True).start()
        except Exception as e:
            print(f"[!] 长期记忆库不可用（{e}），使用简易记忆")
            self.profile = UserProfile()
            self.memory_mgr = None
        # 多人记忆：说“我是小明”后小明的事实单独存 data/people.db（canaan_people），主人的仍在上面的 profile
        try:
            import canaan_people
            self.people = canaan_people.get_people(self.profile if hasattr(self.profile, "privacy_revision") else None)
        except Exception as e:
            print(f"[!] 多人记忆不可用（{e}），只记主人的")
            self.people = None
        # 短期多轮记忆：最近 4 轮，空闲 5 分钟过期（支持“再大声点”“刚才那个人”这类追问）
        revision = (lambda: self.profile.privacy_revision) if hasattr(self.profile, "privacy_revision") else None
        self.memory = ConversationMemory(max_turns=4, idle_expire=300.0, revision=revision)
        # Unattributed legacy JSONL is retained, but never restored into a person's history.
        # 等待口头确认的高风险操作（30 秒有效）
        self.pending = PendingConfirmation(ttl=30.0)
        self.chatapp_draft = ChatAppDraft(ttl=600.0)
        self.clarify = ClarifyContext(ttl=600.0)
        
        self.is_kws_enabled = True
        self.is_recording_command = False
        self.is_processing = False
        self.is_speaking = False
        self.speech_cooldown_until = 0.0
        self.ambient_noise_floor = 0.002
        # 连续对话窗口截止时间（0 表示未开启）
        self.followup_until = 0.0
        self.audio_frames = []
        self.ui_queue = queue.Queue()
        self._pending_voice_prepared = None
        self._pending_voice_audio_generation = 0
        self._speech_playback_session = {"token": object(), "pending": None, "state": "idle"}
        self._ack_playback_session = None
        self._turn_local = threading.local()
        self.user_context = None
        try:
            from canaan_context_store import ContextStore
            from canaan_user_context import UserContext
            self.context_store = ContextStore()
            self.context_store.recover_inflight()
            self.context_store.purge()
            persist = lambda: not getattr(self.profile, 'incognito', False)
            self.chatapp_draft = ChatAppDraft(ttl=600., store=self.context_store, persist_enabled=persist)
            self.clarify = ClarifyContext(ttl=600., store=self.context_store, persist_enabled=persist)
            self.user_context = UserContext(self, self.context_store)
        except Exception as e:
            print(f'[!] 会话数据库不可用：{type(e).__name__}；本次只保留临时上下文')
        threading.Thread(target=self._prepare_pending_voice_listener, daemon=True, name="pending-voice-warmup").start()

        self.init_ui()
        self.bind_global_hotkeys()

        # 挂载团团桌面具身伴侣小姐姐透明挂件 (Companion Avatar)
        try:
            import canaan_companion_pet
            def _on_pet_interrupt():
                from canaan_voice_capture import interrupt_voice_state
                request = interrupt_voice_state(self, require_speaking=True)
                if request is None:
                    return
                try:
                    sd.stop()
                except Exception:
                    pass
                print("[⚡ 桌宠点击打断] 已请求停止播报")

            self.pet = canaan_companion_pet.CanaanCompanionPet(
                self.root,
                is_speaking_provider=lambda: self.is_speaking,
                on_interrupt=_on_pet_interrupt,
                on_exit=lambda: stop_intentionally(self.root, "桌宠退出菜单"),
                companion=self.companion
            )
            print("[✓] 团团桌面具身伴侣挂件 (Companion Avatar) 启动成功")
        except Exception as e:
            print(f"[!] 挂载桌面伴侣挂件失败: {e}")
            self.pet = None

        # 统一状态事件：桌宠、看板（/api/status）、托盘（状态文件）、可选的共创平台桥接都订阅执行器发出的状态
        import canaan_events
        canaan_events.default_tracker()
        canaan_events.subscribe(canaan_events.StatusFileSink(os.path.join(BASE_DIR, "data", "canaan_status.json")))
        if self.pet is not None:
            canaan_events.subscribe(self.pet.on_status_event)
        import canaan_notify  # 等确认、失败弹 Windows 系统通知（托盘负责弹出）
        canaan_events.subscribe(canaan_notify.status_event_sink)
        try:
            self._setup_island_and_notices()
        except Exception as e:
            print(f"[!] 灵动岛 / 提示中心未启动：{type(e).__name__}，继续用旧版胶囊")
        try:
            import canaan_openpets_bridge
            canaan_openpets_bridge.start_if_enabled(os.path.join(BASE_DIR, "data"))
        except Exception as e:
            print(f"[!] 共创平台桥接未启动: {e}")

        self.root.after(100, self.process_ui_queue)
        self.root.after(200, self.async_load_models)
        self.root.after(1000, self.update_vram_telemetry)
        self.root.after(1200, self._start_service_watcher)
        self.root.after(500, self._start_siri_animation)
        self.root.after(1500, self._start_integrations)

    # ---------- 苹果式灵动岛 + 统一提示中心 ----------
    def _setup_island_and_notices(self):
        """灵动岛（原生毛玻璃窗口，独立进程）+ 提示中心 + 系统通知读取 + 数据保留。任何一步失败都不影响主程序。"""
        import canaan_events
        import canaan_island_state
        import canaan_notice_center
        from canaan_island_bridge import IslandBridge
        self.notice_center = canaan_notice_center.default()
        self.island_state = canaan_island_state.IslandState()
        self.island_bridge = IslandBridge(self.island_state, self.notice_center, quiet_probe=self._screen_busy)
        self.island_actions = canaan_island_state.ActionReader()
        canaan_events.subscribe(self.island_bridge.on_status_event)
        self.notice_center.subscribe(self.island_bridge.on_notices)
        if getattr(self, "pet", None) is not None:
            self.notice_center.subscribe(lambda ev, n, _lst: self.pet.on_notice(ev, n) if n else None)
            self.pet.on_notice_click = lambda nid: self._island_action({"kind": "notice_action", "id": nid, "action": "open"})
        self._apply_notice_settings(canaan_notice_center.load_settings())
        canaan_notice_center.on_settings(self._apply_notice_settings)
        self.island_state.update(mode="idle", notices=[], present=None, secondary=None)   # 上次退出时留下的内容清掉
        self._icon_sources = set()
        self.island_proc = None
        if os.environ.get("CANAAN_ISLAND", "1") != "0":
            threading.Thread(target=self._launch_island, daemon=True, name="island-launch").start()
        threading.Thread(target=self._start_notice_sources, daemon=True, name="notice-sources").start()

    def _launch_island(self):
        """拉起灵动岛（原生毛玻璃窗口）；3 秒后还活着就把旧的 tkinter 胶囊藏起来，起不来就继续用旧的。"""
        try:
            flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
            env = dict(os.environ, CANAAN_PARENT_PID=str(os.getpid()))   # 主程序没了，岛自己退出
            env["OPENBLAS_NUM_THREADS"] = "1"   # 岛只模糊一小块截图：1 线程省下约 550 MB 提交内存
            self.island_proc = subprocess.Popen([sys.executable.replace("pythonw.exe", "python.exe"),
                                                 os.path.join(BASE_DIR, "canaan.py"), "island"],
                                                cwd=BASE_DIR, creationflags=flags, env=env,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            if self.island_proc.poll() is None:
                self.ui_queue.put(("island_ready", True))
            else:
                print("[!] 灵动岛没起来，继续用旧版胶囊")
        except Exception as e:
            print(f"[!] 灵动岛启动失败（{type(e).__name__}），继续用旧版胶囊")

    def _start_notice_sources(self):
        try:
            import canaan_retention
            print(f"[✓] 数据保留：{canaan_retention.run()}")
        except Exception as e:
            print(f"[!] 数据保留清理失败：{type(e).__name__}")
        try:
            import canaan_toast_listener
            self.toast_listener = canaan_toast_listener.ToastListener(self.notice_center)
            self.toast_listener.start()
        except Exception as e:
            print(f"[!] 系统通知读取未启动：{type(e).__name__}")

    def _screen_busy(self):
        """游戏 / 全屏 / 演示 / 勿扰：岛隐藏、提示只计数。"""
        try:
            if canaan_gamemode.active():
                return True
            import canaan_proactive
            return not canaan_proactive.notification_ok()
        except Exception:
            return False

    def _notice_query(self, text):
        """“刚才谁找我 / 有什么提示”：按提示中心回答，只说来源、条数、标题类别，不念正文。"""
        center = getattr(self, "notice_center", None)
        if center is None:
            return None
        import canaan_notice_center
        if not canaan_notice_center.is_notice_query(text):
            return None
        return {"action": "chat", "param": "", "emotion": "confident", "reply": center.summary_text()}

    def _apply_notice_settings(self, s):
        """提示设置：灵动岛的提示音 / 背景模糊，桌宠的“重要的事说出来”。"""
        self.island_state.update(settings={"sound": bool(s.get("sound")), "blur": bool(s.get("blur", True))})
        if getattr(self, "pet", None) is not None:
            self.pet.speak_important = bool(s.get("speak_important"))

    def _island_tick(self):
        bridge = getattr(self, "island_bridge", None)
        if bridge is None:
            return
        try:
            bridge.tick()
            for act in self.island_actions.read_new():
                self._island_action(act)
            level = self._voice_level(bridge.mode)
            if level is not None:
                self.island_state.level(level)
            now = time.monotonic()
            if now - getattr(self, "_focus_check_at", 0.0) > 1.0:
                self._focus_check_at = now
                self._clear_seen_chat()
                self._cache_icons()
                # 新灵动岛在用时，旧胶囊必须完全藏起来（实测它会被最小化成屏幕底部的一个小标题条，比如“显示桌面”之后）
                if getattr(self, "_web_island", False) and self.root.state() != "withdrawn":
                    self.root.withdraw()
            proc = getattr(self, "island_proc", None)
            if getattr(self, "_web_island", False) and proc is not None and proc.poll() is not None:
                # 新灵动岛意外退出：先把旧胶囊放回来（主人收起过顶部栏就不放），过 5 秒再拉起，最多重试 3 次
                self._web_island = False
                if not self.is_hidden:
                    self.root.deiconify()
                self._island_restarts = getattr(self, "_island_restarts", 0) + 1
                print(f"[!] 灵动岛退出了（返回码 {proc.returncode}），已换回旧版胶囊"
                      + ("，5 秒后重新拉起" if self._island_restarts <= 3 else "，不再重试"))
                if self._island_restarts <= 3:
                    self.root.after(5000, lambda: threading.Thread(target=self._launch_island, daemon=True,
                                                                   name="island-relaunch").start())
        except Exception as e:
            print(f"[!] 灵动岛更新出错：{type(e).__name__}")

    def _voice_level(self, mode):
        """灵动岛波形的音量（0–1）：在听时用麦克风，说话时用正在播放的声音（回声消除那边存着的参考信号）。"""
        try:
            if mode == "listening":
                rms = getattr(self, "_mic_level", 0.0)
            elif mode == "speaking":
                import canaan_aec
                ref = canaan_aec.REF.take(time.perf_counter(), 1600)
                rms = float(np.sqrt(np.mean(ref ** 2))) if ref.size else 0.0
            else:
                return None
            return max(0.0, min(1.0, rms * 6.0))
        except Exception:
            return None

    def _clear_seen_chat(self):
        """主人切到聊天软件 / 聊天软件 / 邮件这些应用的窗口了，就当看过了：清掉这个来源的“N 条新消息”。"""
        try:
            import psutil
            import canaan_app_icons
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(pid))
            exe = psutil.Process(pid.value).name()
            src = canaan_app_icons.source_for_exe(exe)
            if src:
                self.notice_center.dismiss_source(src, chat_only=True)
        except Exception:
            pass

    def _cache_icons(self):
        """给岛准备来源的应用图标（只取图标，存成 PNG，文件名只有来源名）。每个来源只取一次。"""
        try:
            import canaan_app_icons
            for n in self.notice_center.ordered():
                src = n.get("source")
                if src and src not in self._icon_sources:
                    self._icon_sources.add(src)
                    threading.Thread(target=canaan_app_icons.ensure_icon, args=(src,), daemon=True).start()
        except Exception:
            pass

    def _island_action(self, act):
        """岛（或桌宠气泡）上点了什么。只做打开、稍后、忽略、确认 / 取消、快捷操作这些；
        打开程序照样走动作框架的权限检查；确认 / 取消和说出来走同一条路，而且只认岛上正在显示的那一次确认。"""
        kind = act.get("kind")
        if kind == "click":
            self.open_control_center()
            return
        if kind in ("confirm", "cancel"):
            if self.island_bridge.confirm_matches(act.get("id", "")):
                self.process_audio_data(None, TurnTracer(source="island"), text="确认" if kind == "confirm" else "取消")
            return
        if kind == "quick":
            handlers = {"control": self.open_control_center, "briefing": self.trigger_briefing, "muse": self.open_muse_web,
                        "dots": self.open_dots_web, "kws": self.toggle_kws, "hide": self.hide_top_bar, "phone": self.open_device_pairing}
            fn = handlers.get(act.get("action"))
            if fn:
                fn()
            return
        action, notice = self.notice_center.act(act.get("id", ""), act.get("action", ""))
        if notice and getattr(self, "pet", None) is not None:
            self.pet.notice_done(notice.get("id"))
        if action == "list":
            return
        if action != "open" or not notice:
            return
        source = notice.get("source", "")
        if source in ("Muse", "Dots", "提示") or notice.get("variant") == "p0" and source in ("聊天软件", "IMApp", "飞书"):
            self.open_control_center()             # 智能体任务和重要消息：看板里看（收件箱 / 任务）
            return
        from canaan_action_framework import ActionRegistry
        threading.Thread(target=lambda: ActionRegistry.dispatch("open_app", source, source="rule", user_text=f"打开{source}"),
                         daemon=True).start()

    def voice_readiness(self):
        """API 线程只读状态；不调用 Tk，也不公开设备、音频或错误详情。"""
        now = time.monotonic()
        ui_tick = getattr(self, "_ui_last_tick", 0.0)
        mic_read = getattr(self, "_mic_last_read", 0.0)
        asr_event = getattr(self, "_asr_ready", None)
        loaded = getattr(self, "sense_voice", None) is not None or getattr(self, "asr_model", None) is not None
        state = {
            "attached": True,
            "ui_ready": bool(ui_tick and 0 <= now - ui_tick < 3.0),
            "asr_ready": bool(asr_event is not None and asr_event.is_set() and loaded
                              and not getattr(self, "_asr_load_error", "")),
            "wake_ready": getattr(self, "kws_detector", None) is not None,
            "listening_enabled": bool(getattr(self, "is_kws_enabled", False) and not listen_paused()),
            "microphone_ready": bool(getattr(self, "_mic_stream_active", False) and mic_read
                                     and 0 <= now - mic_read < 3.0),
            "game_mode": bool(canaan_gamemode.active()),
        }
        state["ready"] = all(value for key, value in state.items() if key != "game_mode") and not state["game_mode"]
        return state

    def _start_integrations(self):
        def start():
            try:
                from canaan_api_server import IntegrationRuntime
                from canaan_device_gateway import DeviceTextGateway
                def transcribe_message(audio):
                    return self._transcribe_message(audio, runtime.voices.stop_event)
                runtime = IntegrationRuntime(
                    notify=lambda message: self.ui_queue.put(("important_message", message)),
                    transcriber=transcribe_message, readiness=self.voice_readiness,
                    memory=getattr(self, 'profile', None), people=getattr(self, 'people', None),
                    context=getattr(self, 'context_view', None), switch_user=getattr(self, 'switch_person', None),
                    clear_context=getattr(self, 'clear_context', None), context_store=getattr(self, 'context_store', None),
                    device_factory=lambda runtime: DeviceTextGateway.for_hub(runtime, self))
                self.root.canaan_integration_runtime = runtime.start()
                if getattr(self, "notice_center", None) is not None:
                    # 已采集的聊天软件 / 聊天软件 / 飞书新消息进提示中心：只计数（来源 + 条数），不带正文
                    names = {"chatapp": "聊天软件", "imapp": "IMApp", "feishu": "飞书", "claw": "Claw"}
                    center = self.notice_center
                    runtime.messages.on_new = lambda src, sender, conv: center.post(
                        names.get(src, src), "message", sender=sender, conversation=conv)
            except Exception as ex:
                self.ui_queue.put(("status", ("idle", f"控制中心未启动：{type(ex).__name__}，请检查接口端口与配置")))
        threading.Thread(target=start, name="canaan-integrations", daemon=True).start()

    def _transcribe_message(self, audio, stop_event):
        deadline = time.monotonic() + 180
        while True:
            if stop_event.is_set():
                raise ValueError("语音服务已关闭，停止等待本机 ASR 模型")
            if self._asr_ready.is_set():
                sense_voice, whisper = self.sense_voice, self.asr_model
                if sense_voice is None and whisper is None:
                    raise ValueError(self._asr_load_error or "本机 ASR 模型加载失败")
                return transcribe(audio, sense_voice=sense_voice, whisper=whisper, command=False)
            if time.monotonic() >= deadline:
                raise ValueError("本机 ASR 模型加载超时，请等待模型就绪后重试")
            self._asr_ready.wait(.1)

    def open_device_pairing(self):
        runtime = getattr(self.root, "canaan_integration_runtime", None)
        if runtime is None:
            self.ui_queue.put(("status", ("idle", "手机连接入口尚未就绪，请稍后重试。")))
            return
        try:
            from clients.desktop_relay.application_pairing import show_pairing_for_runtime
            return show_pairing_for_runtime(self.root, runtime)
        except Exception:
            self.ui_queue.put(("status", ("idle", "手机连接暂时不可用，请核对本机授权和电脑连接地址。")))

    def open_control_center(self):
        runtime = getattr(self.root, "canaan_integration_runtime", None)
        if runtime is None:
            self.ui_queue.put(("status", ("idle", "控制中心尚未就绪，请检查 API 端口 8789")))
            return
        import webbrowser
        webbrowser.open(runtime.dashboard_url())

    def _draw_rounded_capsule(self, x1, y1, x2, y2, r, fill, outline, width=1.2):
        r = max(2, min(r, abs(x2 - x1) // 2, abs(y2 - y1) // 2))
        points = [
            x1 + r, y1,  x1 + r, y1,
            x2 - r, y1,  x2 - r, y1,
            x2, y1,      x2, y1 + r,
            x2, y1 + r,  x2, y2 - r,
            x2, y2 - r,  x2, y2,
            x2 - r, y2,  x2 - r, y2,
            x1 + r, y2,  x1 + r, y2,
            x1, y2,      x1, y2 - r,
            x1, y2 - r,  x1, y1,
            x1, y1 + r,  x1, y1 + r,
            x1, y1
        ]
        return self.canvas.create_polygon(points, smooth=True, fill=fill, outline=outline, width=width, tags="island_bg")

    def _redraw_island(self):
        if not hasattr(self, 'canvas') or not self.canvas:
            return
        self.canvas.delete("island_bg")
        w = self.expanded_w if not self.is_collapsed else self.mini_w
        h = self.expanded_h if not self.is_collapsed else self.mini_h
        r = 24 if not self.is_collapsed else 20
        # Apple 顶级灵动岛：深邃曜石纯黑底色 + 柔和科技双层微光晕边缘
        self._draw_rounded_capsule(1, 1, w - 1, h - 1, r=r, fill="#000000", outline="#32384e", width=1.4)
        self._draw_rounded_capsule(2, 2, w - 2, h - 2, r=r - 1, fill="#000000", outline="#1c2032", width=1.0)
        self.canvas.tag_lower("island_bg")

    def trigger_briefing(self):
        print("[📰 灵动岛快捷触发] 正在生成今日高管消息早报...")
        self.expand_capsule()
        self.ui_queue.put(("status", ("processing", "正在为您提炼聊天软件与聊天软件关键消息早报...")))
        threading.Thread(
            target=lambda: self.execute_quick_action("daily_briefing", "generate", "今日早报"),
            daemon=True
        ).start()

    def trigger_call_test(self):
        print("[🎙️ 灵动岛呼叫测试] 触发团团亲昵应答...")
        self.expand_capsule()
        self.play_canaan_greeting()

    def open_muse_web(self):
        import webbrowser
        from canaan_agent_board_data import MUSE_WEB
        webbrowser.open(MUSE_WEB)

    def open_dots_web(self):
        import webbrowser
        from canaan_agent_board_data import DOTS_WEB
        webbrowser.open(DOTS_WEB)

    def _start_service_watcher(self):
        def _start_installed():
            from canaan_agent_service_manager import ensure_all_running
            ensure_all_running(only_installed=True)
        threading.Thread(target=_start_installed, name="canaan-agent-startup", daemon=True).start()
        def _check():
            while True:
                try:
                    from canaan_agent_board_data import fetch_muse, fetch_dots
                    muse_on = fetch_muse()["online"]
                    dots_on = fetch_dots()["online"]
                    self.ui_queue.put(("service_status", (muse_on, dots_on)))
                except Exception:
                    pass
                time.sleep(4.0)
        threading.Thread(target=_check, daemon=True).start()

    def _start_siri_animation(self):
        self._siri_phase = 0
        colors = ["#38bdf8", "#818cf8", "#a855f7", "#ec4899", "#10b981"]
        def _tick():
            try:
                self._siri_phase = (self._siri_phase + 1) % 60
                if self.is_collapsed and not self.is_speaking and not self.is_processing:
                    c_idx = (self._siri_phase // 12) % len(colors)
                    if hasattr(self, 'mini_dot') and self.mini_dot:
                        self.mini_dot.config(fg=colors[c_idx])
                if hasattr(self, 'siri_bars') and self.siri_bars:
                    active = self.is_speaking or self.is_recording_command or self.is_processing
                    for idx, bar in enumerate(self.siri_bars):
                        if active:
                            bar.config(fg=colors[idx % len(colors)])
                        else:
                            bar.config(fg="#334155")
            except Exception:
                pass
            self.root.after(100, _tick)
        self.root.after(500, _tick)

    def init_ui(self):
        # Apple 灵动悬浮岛外层画布（无矩形黑边，真正圆润胶囊透屏）
        self.canvas = tk.Canvas(
            self.root,
            width=self.mini_w,
            height=self.mini_h,
            bg=getattr(self, "TRANS_COLOR", "#010203"),
            highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)

        # 内部内容挂载容器卡片
        self.card = tk.Frame(self.canvas, bg="#000000")
        self.container_win_id = self.canvas.create_window(
            self.mini_w // 2, self.mini_h // 2,
            window=self.card,
            width=self.mini_w - 20,
            height=self.mini_h - 8
        )
        self._redraw_island()

        # ---------------- 1. Mini Island (灵动微岛模式: 320x40) ----------------
        self.mini_capsule = tk.Frame(self.card, bg="#000000", cursor="hand2")
        self.mini_capsule.pack(fill="both", expand=True)

        self.mini_dot = tk.Label(self.mini_capsule, text="●", font=("Arial", 10), fg="#38bdf8", bg="#000000")
        self.mini_dot.pack(side="left", padx=(8, 4))
        self.status_dot = self.mini_dot

        self.mini_brand = tk.Label(
            self.mini_capsule,
            text=f"✨ {ASSISTANT_NAME}",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#f8fafc",
            bg="#000000"
        )
        self.mini_brand.pack(side="left")

        self.mini_status = tk.Label(
            self.mini_capsule,
            text="待命 · 叫我“团团”",
            font=("Microsoft YaHei UI", 8),
            fg="#94a3b8",
            bg="#000000"
        )
        self.mini_status.pack(side="left", padx=(6, 0))

        self.mini_exit_btn = tk.Button(
            self.mini_capsule, text="退出", command=lambda: stop_intentionally(self.root, "点击退出按钮"),
            fg="#fda4af", bg="#161928", relief="flat", bd=0, padx=4, cursor="hand2")
        self.mini_exit_btn.pack(side="right", padx=3)
        self.mini_control_btn = tk.Button(
            self.mini_capsule, text="看板", command=self.open_control_center,
            fg="#a5c8ff", bg="#161928", relief="flat", bd=0, padx=4, cursor="hand2")
        self.mini_control_btn.pack(side="right", padx=3)
        self.mini_phone_btn = tk.Button(
            self.mini_capsule, text="手机", command=self.open_device_pairing,
            fg="#a5c8ff", bg="#161928", relief="flat", bd=0, padx=4, cursor="hand2")
        self.mini_phone_btn.pack(side="right", padx=3)

        # Right micro service pills on mini capsule
        self.mini_briefing_btn = tk.Label(
            self.mini_capsule,
            text="📰早报",
            font=("Microsoft YaHei UI", 8),
            fg="#38bdf8",
            bg="#0f172a",
            padx=5,
            pady=1,
            cursor="hand2"
        )
        self.mini_briefing_btn.pack(side="right", padx=(2, 6))
        self.mini_briefing_btn.bind("<Button-1>", lambda e: self.trigger_briefing())

        self.mini_dots_lbl = tk.Label(
            self.mini_capsule,
            text="● Dots",
            font=("Microsoft YaHei UI", 8),
            fg="#22c55e",
            bg="#0f172a",
            padx=5,
            pady=1,
            cursor="hand2"
        )
        self.mini_dots_lbl.pack(side="right", padx=2)
        self.mini_dots_lbl.bind("<Button-1>", lambda e: self.open_dots_web())

        self.mini_muse_lbl = tk.Label(
            self.mini_capsule,
            text="● Muse",
            font=("Microsoft YaHei UI", 8),
            fg="#22c55e",
            bg="#0f172a",
            padx=5,
            pady=1,
            cursor="hand2"
        )
        self.mini_muse_lbl.pack(side="right", padx=2)
        self.mini_muse_lbl.bind("<Button-1>", lambda e: self.open_muse_web())

        # Click anywhere on mini capsule to expand
        for w in (self.mini_capsule, self.mini_dot, self.mini_brand, self.mini_status):
            w.bind("<Button-1>", lambda e: self.expand_capsule())

        # ---------------- 2. Expanded Card (展开大岛模式: 760x126) ----------------
        self.expanded_panel = tk.Frame(self.card, bg="#000000")

        # Top Bar in Expanded Card (高度 30px)
        self.top_bar = tk.Frame(self.expanded_panel, bg="#000000")
        self.top_bar.pack(fill="x", padx=8, pady=(4, 2))

        self.brand_lbl = tk.Label(
            self.top_bar,
            text=f"✨ {ASSISTANT_NAME} OS",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#f8fafc",
            bg="#000000"
        )
        self.brand_lbl.pack(side="left")

        self.action_badge = tk.Label(
            self.top_bar,
            text=f"[● 待命 · 叫我“{WAKE_WORD}”]",
            font=("Microsoft YaHei UI", 8),
            fg="#38bdf8",
            bg="#0e1726",
            padx=6,
            pady=1
        )
        self.action_badge.pack(side="left", padx=6)

        self.emotion_lbl = tk.Label(
            self.top_bar,
            text="",
            bg="#000000"
        )

        # Right quick controls in expanded card
        self.close_btn = tk.Button(
            self.top_bar, text="✕", font=("Microsoft YaHei UI", 8), fg="#94a3b8", bg="#161928",
            activebackground="#7f1d1d", activeforeground="#ffffff", relief="flat", bd=0, padx=6, pady=1, cursor="hand2",
            command=lambda: stop_intentionally(self.root, "点击关闭按钮")
        )
        self.close_btn.pack(side="right", padx=(2, 0))

        self.fold_btn = tk.Button(
            self.top_bar, text="▴", font=("Microsoft YaHei UI", 8), fg="#818cf8", bg="#161928",
            activebackground="#272d45", activeforeground="#ffffff", relief="flat", bd=0, padx=6, pady=1, cursor="hand2",
            command=self.collapse_capsule
        )
        self.fold_btn.pack(side="right", padx=2)

        self.btn_briefing = tk.Button(
            self.top_bar,
            text="📰 提炼早报",
            font=("Microsoft YaHei UI", 8),
            fg="#38bdf8",
            bg="#0e1d33",
            activebackground="#1e3a5f",
            relief="flat",
            bd=0,
            padx=6,
            pady=1,
            cursor="hand2",
            command=self.trigger_briefing
        )
        self.btn_briefing.pack(side="right", padx=3)

        self.btn_dots = tk.Button(
            self.top_bar,
            text="🧠 Dots: 在线",
            font=("Microsoft YaHei UI", 8),
            fg="#22c55e",
            bg="#0f291e",
            activebackground="#164e33",
            relief="flat",
            bd=0,
            padx=6,
            pady=1,
            cursor="hand2",
            command=self.open_dots_web
        )
        self.btn_dots.pack(side="right", padx=3)

        self.btn_muse = tk.Button(
            self.top_bar,
            text="🌐 Muse: 在线",
            font=("Microsoft YaHei UI", 8),
            fg="#22c55e",
            bg="#0f291e",
            activebackground="#164e33",
            relief="flat",
            bd=0,
            padx=6,
            pady=1,
            cursor="hand2",
            command=self.open_muse_web
        )
        self.btn_muse.pack(side="right", padx=3)

        self.kws_btn = tk.Button(
            self.top_bar,
            text="🎙️ 唤醒: 开",
            font=("Microsoft YaHei UI", 8),
            fg="#22c55e",
            bg="#0f291e",
            activebackground="#164e33",
            relief="flat",
            bd=0,
            padx=6,
            pady=1,
            cursor="hand2",
            command=self.toggle_kws
        )
        self.kws_btn.pack(side="right", padx=3)

        # Content Line (Middle Row, 高度 46px，配 Siri 律动光条)
        self.content_frame = tk.Frame(self.expanded_panel, bg="#000000")
        self.content_frame.pack(fill="x", padx=8, pady=(2, 3))

        # Dynamic Siri audio wave bars
        self.wave_frame = tk.Frame(self.content_frame, bg="#000000", width=36)
        self.wave_frame.pack(side="left", padx=(2, 8))
        self.siri_bars = []
        bar_colors = ["#38bdf8", "#818cf8", "#c084fc", "#f472b6", "#34d399"]
        for bc in bar_colors:
            b = tk.Label(self.wave_frame, text="█", font=("Arial", 6), fg=bc, bg="#000000")
            b.pack(side="left", padx=1)
            self.siri_bars.append(b)

        self.text_lbl = tk.Label(
            self.content_frame,
            text=f"直接呼叫“{WAKE_WORD}”，或按住 Alt+Space 说话~",
            font=("Microsoft YaHei UI", 9),
            fg="#f8fafc",
            bg="#000000",
            wraplength=760,
            justify="left",
            anchor="w"
        )
        self.text_lbl.pack(side="left", fill="x", expand=True)

        # Compact Telemetry & Quick Chips Line (Bottom Row, 高度 28px)
        self.telemetry_bar = tk.Frame(self.expanded_panel, bg="#05060a")
        self.telemetry_bar.pack(fill="x", padx=6, pady=(1, 2))

        self.step_dot = tk.Label(self.telemetry_bar, text="⚡", font=("Segoe UI Emoji", 8), fg="#38bdf8", bg="#05060a")
        self.step_dot.pack(side="left", padx=(4, 2))

        self.step_lbl = tk.Label(
            self.telemetry_bar,
            text="RTX 5060 本地离线引擎 | 隐私护盾已封锁 | 双工即时打断",
            font=("Microsoft YaHei UI", 8),
            fg="#64748b",
            bg="#05060a"
        )
        self.step_lbl.pack(side="left", padx=2)

        # Quick action pills on bottom right
        self.chip_mute = tk.Label(
            self.telemetry_bar, text="🤫 静音", font=("Microsoft YaHei UI", 8),
            fg="#f87171", bg="#1a1215", padx=6, pady=1, cursor="hand2"
        )
        self.chip_mute.pack(side="right", padx=(2, 4))
        self.chip_mute.bind("<Button-1>", lambda e: self.interrupt())

        self.chip_test = tk.Label(
            self.telemetry_bar, text="🎙️ 呼叫测试", font=("Microsoft YaHei UI", 8),
            fg="#38bdf8", bg="#101c2b", padx=6, pady=1, cursor="hand2"
        )
        self.chip_test.pack(side="right", padx=2)
        self.chip_test.bind("<Button-1>", lambda e: self.trigger_call_test())

        self.vram_lbl = tk.Label(
            self.telemetry_bar,
            text="显存: 监测中..",
            font=("Microsoft YaHei UI", 8),
            fg="#94a3b8",
            bg="#05060a"
        )
        self.vram_lbl.pack(side="right", padx=6)

        # Allow dragging the HUD capsule around the screen
        for comp in (self.canvas, self.card, self.mini_capsule, self.expanded_panel, self.top_bar, self.content_frame, self.telemetry_bar):
            comp.bind("<ButtonPress-1>", self.start_drag)
            comp.bind("<B1-Motion>", self.do_drag)

    def expand_capsule(self):
        if self.is_hidden:
            return
        if self._autofold_job:
            try:
                self.root.after_cancel(self._autofold_job)
            except Exception:
                pass
            self._autofold_job = None
        if self.is_collapsed:
            self.is_collapsed = False
            if hasattr(self, 'mini_capsule') and hasattr(self.mini_capsule, 'pack_forget'):
                self.mini_capsule.pack_forget()
            if hasattr(self, 'expanded_panel') and hasattr(self.expanded_panel, 'pack'):
                self.expanded_panel.pack(fill="both", expand=True)
            cur_x = self.root.winfo_x()
            cur_y = self.root.winfo_y()
            new_x = max(10, cur_x - (self.expanded_w - self.mini_w) // 2)
            self.root.geometry(f"{self.expanded_w}x{self.expanded_h}+{new_x}+{cur_y}")
            if hasattr(self, 'canvas') and self.canvas:
                self.canvas.config(width=self.expanded_w, height=self.expanded_h)
                self._redraw_island()
                if hasattr(self, 'container_win_id') and self.container_win_id:
                    self.canvas.coords(self.container_win_id, self.expanded_w // 2, self.expanded_h // 2)
                    self.canvas.itemconfig(self.container_win_id, width=self.expanded_w - 28, height=self.expanded_h - 10)

    def collapse_capsule(self):
        if self.is_hidden:
            return
        if self._autofold_job:
            try:
                self.root.after_cancel(self._autofold_job)
            except Exception:
                pass
            self._autofold_job = None
        if not self.is_collapsed:
            self.is_collapsed = True
            if hasattr(self, 'expanded_panel') and hasattr(self.expanded_panel, 'pack_forget'):
                self.expanded_panel.pack_forget()
            if hasattr(self, 'mini_capsule') and hasattr(self.mini_capsule, 'pack'):
                self.mini_capsule.pack(fill="both", expand=True)
            cur_x = self.root.winfo_x()
            cur_y = self.root.winfo_y()
            new_x = max(10, cur_x + (self.expanded_w - self.mini_w) // 2)
            self.root.geometry(f"{self.mini_w}x{self.mini_h}+{new_x}+{cur_y}")
            if hasattr(self, 'canvas') and self.canvas:
                self.canvas.config(width=self.mini_w, height=self.mini_h)
                self._redraw_island()
                if hasattr(self, 'container_win_id') and self.container_win_id:
                    self.canvas.coords(self.container_win_id, self.mini_w // 2, self.mini_h // 2)
                    self.canvas.itemconfig(self.container_win_id, width=self.mini_w - 20, height=self.mini_h - 8)

    def schedule_autofold(self, delay_sec=3.5):
        if self._autofold_job:
            try:
                self.root.after_cancel(self._autofold_job)
            except Exception:
                pass
            self._autofold_job = None
        def _fold():
            if not getattr(self, "is_speaking", False) and not getattr(self, "is_processing", False) and not getattr(self, "is_recording_command", False):
                self.collapse_capsule()
        try:
            self._autofold_job = self.root.after(int(delay_sec * 1000), _fold)
        except Exception:
            pass

    def hide_top_bar(self):
        self.root.withdraw()
        self.is_hidden = True
        if getattr(self, "island_bridge", None) is not None:      # 新灵动岛一起收起
            self.island_bridge.hidden_by_user = True
            self.island_bridge._publish()
        print("[i] 顶部微岛已收起（已切换为纯桌面伴侣极简模式）")
        if getattr(self, "pet", None):
            self.pet.say("顶部状态栏已收起，有我在桌面陪你，随时叫我就好~", 3.0, speak=False)

    def show_top_bar(self):
        self.is_hidden = False
        if getattr(self, "island_bridge", None) is not None:
            self.island_bridge.hidden_by_user = False
            self.island_bridge._publish()
        if getattr(self, "_web_island", False):
            return                                 # 新灵动岛在运行：只把它放出来，旧胶囊继续藏着（不出现两个岛）
        self.root.deiconify()
        self.collapse_capsule()
        if getattr(self, "pet", None):
            self.pet.say("顶部灵动胶囊已展开~", 2.5, speak=False)

    def toggle_top_bar(self):
        if self.is_hidden:
            self.show_top_bar()
        elif self.is_collapsed:
            self.expand_capsule()
        else:
            self.collapse_capsule()

    def update_vram_telemetry(self):
        try:
            vram_info = GLOBAL_TASK_MANAGER.get_vram_telemetry_quick()
            self.vram_lbl.config(text=vram_info)
        except Exception:
            pass
        self.root.after(3000, self.update_vram_telemetry)

    def start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def do_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def bind_global_hotkeys(self):
        def on_hotkey():
            self.ui_queue.put(("hotkey_trigger", None))
        
        try:
            keyboard.add_hotkey("alt+space", on_hotkey, suppress=True)
            keyboard.add_hotkey("f8", on_hotkey, suppress=True)
            keyboard.add_hotkey("ctrl+alt+q", lambda: self.ui_queue.put(("exit_requested", None)), suppress=False)
            def on_esc():
                from canaan_computer_use import CanaanComputerUse
                CanaanComputerUse.request_stop()
                from canaan_voice_capture import interrupt_voice_state
                request = interrupt_voice_state(self, cancel_processing=True)
                if request is None:
                    return
                if not request.was_speaking:
                    print("[⚡ ESC] 本轮不再播报")
                    return
                try:
                    sd.stop()
                except Exception:
                    pass
                if request.current(self) and getattr(self, "pet", None):
                    self.pet.interrupt(notify=False)
                print("[⚡ ESC 键瞬间掐断播音]")
            keyboard.add_hotkey("esc", on_esc, suppress=False)
            print("[+] 全局快捷键注册成功: Alt+Space / F8 / Esc(即时打断)")
        except Exception as e:
            print(f"[!] 快捷键注册提示: {e}")

    def async_load_models(self):
        def _load():
            try:
                # 0. 启动并校验 GPT-SoVITS 团团专属原声引擎 (确保 100% 团团音色)
                # 游戏模式检测：由托盘监管程序（canaan_tray.py）负责；直接用 .bat 启动时 hub 自己检测
                if os.environ.get("CANAAN_SUPERVISED") != "1" and getattr(canaan_config, "GAME_MODE_ENABLED", True):
                    self.game_watcher = canaan_gamemode.GameModeWatcher(
                        start_tts=self.engine.start_service_background,
                        on_change=lambda on, why: self.ui_queue.put(
                            ("status", ("ready", f"游戏模式（{why}）：显卡已让出，只听简单指令" if on else "已退出游戏模式"))))
                    self.game_watcher.step()
                    self.game_watcher.start()
                if not canaan_gamemode.active() and getattr(canaan_config, "TTS_MODE", "gpu") == "gpu":
                    print("[*] 正在预热与校验 GPT-SoVITS 团团原声语音服务...")
                    self.engine.ensure_service_running()

                # 1. Load KWS model
                self.kws_detector = CanaanWakeWordDetector()
                self.ui_queue.put(("status", ("processing", "唤醒模型已加载，正在准备语音识别...")))

                # 2. Load SenseVoice ASR (Supports Mandarin and Chinese Dialects: Cantonese, Sichuanese, Dongbei, etc.)
                try:
                    from canaan_asr_loader import load_sensevoice
                    from modelscope.hub.snapshot_download import snapshot_download
                    print("[*] 正在加载阿里 SenseVoiceSmall 多方言高精语音识别引擎...")
                    local_sensevoice = snapshot_download("iic/SenseVoiceSmall", local_files_only=True)
                    self.sense_voice = load_sensevoice(local_sensevoice)
                    print("[✓] SenseVoiceSmall 多方言高精语音识别引擎已就绪！")
                    self._asr_ready.set()
                except Exception as e:
                    print(f"[!] SenseVoiceSmall 加载跳过: {e}，使用 Whisper 备选")
                    self.sense_voice = None

                try:
                    local_whisper_path = os.path.expanduser(r"~/.cache/huggingface/hub/models--Systran--faster-whisper-tiny/snapshots/d90ca5fe260221311c53c58e660288d3deb8d356")
                    if os.path.exists(local_whisper_path):
                        self.asr_model = WhisperModel(local_whisper_path, device="cpu", compute_type="int8", local_files_only=True)
                    else:
                        self.asr_model = WhisperModel("tiny", device="cpu", compute_type="int8")
                    print("[✓] Whisper 语音引擎离线就绪！")
                    self._asr_ready.set()
                except Exception as e:
                    print(f"[!] Whisper 加载跳过: {e}")
                    self.asr_model = None
                if self.sense_voice is None and self.asr_model is None:
                    raise ValueError("本机 ASR 模型加载失败")
                if self.sense_voice is not None:
                    # 加载时留下约 2 GB 已释放但没还给系统的内存，空跑几次让它马上还（见 canaan_asr.release_load_memory）
                    threading.Thread(target=self._release_asr_load_memory, daemon=True, name="asr-release").start()

                # 3. Start Background Real-time Wake Word Audio Stream
                threading.Thread(target=self.kws_audio_loop, daemon=True).start()

                # 4. 显卡后台极速热身（保证模型常驻 RTX 5060 显存，杜绝首次加载卡顿）
                threading.Thread(target=self.warmup_ollama, daemon=True).start()

                # 5. 预合成短应答等常用语音，命中缓存时首音不再等 TTS
                threading.Thread(target=self.prewarm_tts_cache, daemon=True).start()
                # 5.1 真正跑一次语音合成把 GPU 热起来（常用语都命中缓存，不预热的话第一句新话要等十几秒），并定时保温
                threading.Thread(target=self._tts_keep_warm, daemon=True).start()

                # 6. 主动开口（提醒/问候/久坐提醒）
                try:
                    self._start_proactive()
                except Exception as e:
                    print(f"[!] 主动发言模块启动失败: {e}")

            except Exception as e:
                if self.sense_voice is None and self.asr_model is None:
                    self._asr_load_error = "本机 ASR 模型加载失败，请检查本机模型文件"
                    self._asr_ready.set()
                print(f"[!] 模型加载失败: {e}")
                self.ui_queue.put(("status", ("error", f"唤醒引擎加载失败: {e}")))

        threading.Thread(target=_load, daemon=True).start()

    def ensure_ollama_alive(self):
        return self.intent_client.ensure_ollama_alive()

    def warmup_ollama(self):
        if canaan_gamemode.active() or not getattr(canaan_config, "LOCAL_LLM_ENABLED", getattr(canaan_config, "LLM_ENABLED", True)):
            print("[🎮] 游戏模式、低显存档或用云端 API：跳过本机大模型预热")
            return
        try:  # 后台 7B 总结用完后，把意图 / 陪聊模型重新预热回显存
            import canaan_gpu_priority
            canaan_gpu_priority.register_rewarm(self.warmup_ollama)
        except Exception:
            pass
        # 预热选定模型；只有显式配置了陪聊备用模型才会尝试它。
        self.intent_client.warmup()
        if getattr(canaan_config, "COMPANION_ENABLED", True):
            self.companion.warmup()

    def toggle_kws(self):
        self.is_kws_enabled = not self.is_kws_enabled
        if not self.is_kws_enabled:
            getattr(self, '_pending_voice_audio_gap', lambda: None)()
        if self.is_kws_enabled:
            if hasattr(self, 'kws_btn') and self.kws_btn:
                self.kws_btn.config(text="🎙️ 唤醒: 开", fg="#22c55e", bg="#0f291e")
            self.action_badge.config(text=f"[语音唤醒就绪 · 呼叫“{WAKE_WORD}”]", fg="#60a5fa")
        else:
            if hasattr(self, 'kws_btn') and self.kws_btn:
                self.kws_btn.config(text="🎙️ 唤醒: 关", fg="#94a3b8", bg="#161928")
            self.action_badge.config(text="[唤醒已关闭 · 使用 Alt+Space]", fg="#71717a")

    def kws_audio_loop(self):
        """全天候超轻量级语音唤醒后台监听线程 (CPU < 1%)"""
        samples_per_read = 1600  # 100ms chunk

        while True:
            # 暂停聆听（托盘菜单或录音工具写 data/listen_paused.flag）：关闭麦克风，模型保持加载
            if listen_paused():
                self._mic_stream_active = False
                if not getattr(self, "_paused_shown", False):
                    self._paused_shown = True
                    getattr(self, '_pending_voice_audio_gap', lambda: None)()
                    print("[⏸] 已暂停聆听，麦克风已关闭")
                    self.ui_queue.put(("status", ("processing", "已暂停聆听（托盘菜单“继续聆听”恢复）")))
                time.sleep(0.5)
                continue
            if getattr(self, "_paused_shown", False):
                self._paused_shown = False
                self.ui_queue.put(("status", ("ready", "继续聆听了，随时叫我~")))
            try:
                with sd.InputStream(channels=1, dtype="float32", samplerate=SAMPLE_RATE) as s:
                    self._mic_last_read = 0.0
                    self._mic_stream_active = True
                    announced = False
                    print("[✓] 麦克风全天候唤醒监听已启动...")
                    while True:
                        if listen_paused():
                            break  # 关闭输入流，回到外层等待
                        # 硬件音频流持续实时抽取，彻底避免声卡内部 Ring Buffer 累积旧声音引发回声！
                        try:
                            samples, overflowed = s.read(samples_per_read)
                        except Exception as e:
                            self._mic_stream_active = False
                            getattr(self, '_pending_voice_audio_gap', lambda: None)()
                            print(f"[!] 麦克风流读取异常: {e}，正在重新建立音频流...")
                            time.sleep(0.1)
                            break

                        if not s.active:
                            getattr(self, '_pending_voice_audio_gap', lambda: None)()
                            print("[!] 麦克风流处于非活跃状态，正在重建...")
                            break

                        self._mic_last_read = time.monotonic()
                        if overflowed:
                            self._mic_overflow_count = getattr(self, "_mic_overflow_count", 0) + 1
                            getattr(self, "_pending_voice_audio_gap", lambda: None)()
                            continue  # 缺了中间一块，不能把截断的回答当成完整确认。
                        if not announced and self.is_kws_enabled and not listen_paused():
                            self.ui_queue.put(("status", ("ready", "语音识别与麦克风已就绪，随时叫我或按 Alt+Space~")))
                            announced = True
                        current_time = time.time()
                        if not self.is_kws_enabled or self.is_recording_command:
                            continue

                        pending_obj = getattr(self, "pending", None)
                        pending_now = pending_obj.peek() if pending_obj is not None else None
                        if pending_now is not None:
                            getattr(self, "_pending_voice_ducked_expire", lambda _: None)(pending_now)
                        pending_window = (pending_now is not None
                                          or getattr(self, "_pending_voice_tail_active", lambda: False)()
                                          or getattr(self, "_pending_voice_preplay_active", lambda: False)()
                                          or getattr(self, "_pending_voice_ducked_active", lambda: False)())
                        pending_frame_handled = False
                        if self.is_speaking or pending_window:
                            pending_frame_handled = self._pending_voice_bargein(samples.reshape(-1))
                        # The frame is already in memory. Hear a new onset before
                        # dispatching any old confirmation; otherwise it can race
                        # ahead of a cancellation that started in this very frame.
                        getattr(self, "_consume_pending_voice_result", lambda: False)()
                        if pending_frame_handled or (not self.is_speaking and pending_window):
                            continue  # 短答不走普通冷却和 0.4 秒连续对话门限。
                        if self.is_processing and not self.is_speaking:
                            continue

                        self._wake_ring.append(samples.reshape(-1).copy())
                        # 若正在播音中，开启全双工即时打断 (Barge-in)：支持止语词/唤醒词/直达指令直接打断！
                        if self.is_speaking:
                            observed_session = getattr(self, '_speech_playback_session', None)
                            samples_flat = samples.reshape(-1)
                            if self.kws_detector:
                                pending = getattr(self, "pending", None)
                                if pending is not None and pending.has_pending():
                                    listener = getattr(self, "_pending_voice_listener", None)
                                    detector = self.kws_detector
                                    feed = getattr(listener, "bargein_audio", np.zeros(0, np.float32))
                                else:
                                    detector, feed = self._bargein_input(samples_flat)
                                detected_keyword = detector.process_samples(feed, SAMPLE_RATE) if len(feed) else None
                                if detected_keyword and not self._speaker_ok("打断"):
                                    self.kws_detector.reset()
                                    detected_keyword = None
                                if detected_keyword:
                                    print(f"[⚡ 全双工打断] 播报中捕捉到打断词：“{detected_keyword}”，已请求停止播报")
                                    from canaan_voice_capture import interrupt_voice_state
                                    request = interrupt_voice_state(self, require_speaking=True,
                                                                    expected_session=observed_session)
                                    self.kws_detector.reset()
                                    if request is None:
                                        continue
                                    try:
                                        sd.stop()
                                    except Exception:
                                        pass
                                    if not request.current(self):
                                        continue
                                    if getattr(self, "pet", None):
                                        self.pet.interrupt(notify=False)
                                    if not request.current(self):
                                        continue

                                    # 1. 自然止语词
                                    if detected_keyword in STOP_WORDS:
                                        print(f"[🤫 止语闭嘴] 用户指示“{detected_keyword}”，立刻静默归位")
                                        if getattr(self, "pet", None):
                                            self.pet._spawn_floating_particle(f"🤫 {detected_keyword}", color="#94a3b8")
                                        self.ui_queue.put(("status", ("ready", "好的，我闭嘴啦~")))
                                        self._listen_stop_continuation(s, detected_keyword)
                                        continue

                                    # 2. 常用直达指令
                                    elif detected_keyword in QUICK_ACTION_MAP:
                                        action_name, action_param = QUICK_ACTION_MAP[detected_keyword]
                                        print(f"[⚡ 语音直达指令] 打断并执行: {action_name} ({detected_keyword})")
                                        if getattr(self, "pet", None):
                                            self.pet._spawn_floating_particle(f"⚡ {detected_keyword}", color="#38bdf8")
                                        self.ui_queue.put(("status", ("processing", f"正在执行: {detected_keyword}")))
                                        self.speech_cooldown_until = current_time + 0.5
                                        threading.Thread(
                                            target=self.execute_quick_action,
                                            args=(action_name, action_param, detected_keyword), kwargs={"keyword_only": True},
                                            daemon=True
                                        ).start()
                                        continue

                                    # 3. 唤醒词（团团/你好团团）：打断播音并录制新指令
                                    else:
                                        self.ui_queue.put(("wake_word_triggered", detected_keyword))
                                        self.record_command_after_wake(s, is_bargein=True)
                                        if self.kws_detector:
                                            self.kws_detector.reset()
                            continue

                        if current_time < self.speech_cooldown_until:
                            # 追问刚播完时，只有同一已结束会话里的近端人声才可提前接答。
                            # 单凭存在草稿不能绕过回声冷却。
                            filtered = getattr(self, "_pending_voice_clarification_filter", lambda _: None)(samples.reshape(-1))
                            if filtered is not None and len(filtered) and np.any(filtered):
                                self.record_followup(s, filtered, near_end_filtered=True)
                            continue

                        samples_flat = samples.reshape(-1)

                        # 连续对话：刚播报完的几秒内无需唤醒词，直接接着说
                        if self.followup_until and current_time < self.followup_until:
                            self.record_followup(s, samples_flat)
                            if self.kws_detector:
                                self.kws_detector.reset()
                            continue
                        elif self.followup_until:
                            self.followup_until = 0.0
                            self.ui_queue.put(("followup_ended", None))

                        # 用较安静帧估计底噪，避免待机环境人声抬高后续确认门限。
                        rms = float(np.sqrt(np.mean(samples_flat**2)))
                        estimator = getattr(self, '_ambient_noise_estimator', None)
                        if estimator is None:
                            from canaan_pending_bargein import AmbientNoiseFloor
                            estimator = self._ambient_noise_estimator = AmbientNoiseFloor()
                        self.ambient_noise_floor = estimator.update(rms)

                        # 流式送入 KWS 检测器（连续音频流，切勿人为切断抽帧）
                        if self.kws_detector:
                            detected_keyword = self.kws_detector.process_samples(samples_flat, SAMPLE_RATE)
                            if detected_keyword and not self._speaker_ok("唤醒"):
                                self.kws_detector.reset()
                                detected_keyword = None
                            if detected_keyword:
                                self.kws_detector.reset()
                                if detected_keyword in STOP_WORDS:
                                    print(f"[🤫 待命静默] 忽略待命期的止语词“{detected_keyword}”，保持唤醒敏锐度")
                                    continue
                                elif detected_keyword in QUICK_ACTION_MAP:
                                    action_name, action_param = QUICK_ACTION_MAP[detected_keyword]
                                    print(f"[⚡ 语音直达指令] 秒级执行: {action_name} ({detected_keyword})")
                                    if getattr(self, "pet", None):
                                        self.pet._spawn_floating_particle(f"⚡ {detected_keyword}", color="#38bdf8")
                                    self.ui_queue.put(("status", ("processing", f"正在执行: {detected_keyword}")))
                                    self.speech_cooldown_until = current_time + 0.5
                                    threading.Thread(
                                        target=self.execute_quick_action,
                                        args=(action_name, action_param, detected_keyword), kwargs={"keyword_only": True},
                                        daemon=True
                                    ).start()
                                    continue
                                else:
                                    print(f"[👑 唤醒成功] 捕捉到唤醒词: “{detected_keyword}”")
                                    if getattr(self, "pet", None):
                                        self.pet._spawn_floating_particle(f"👑 {detected_keyword}", color="#38bdf8")
                                    self.ui_queue.put(("wake_word_triggered", detected_keyword))
                                    self.record_command_after_wake(s)
                                    if self.kws_detector:
                                        self.kws_detector.reset()

            except Exception as e:
                self._mic_stream_active = False
                getattr(self, '_pending_voice_audio_gap', lambda: None)()
                print(f"[!] 麦克风唤醒流异常: {e}, 1秒后重试...")
                time.sleep(1)
            finally:
                self._mic_stream_active = False

    def _begin_asr_prefetch(self, min_speech_sec=.25):
        model = getattr(self, "sense_voice", None)
        if not getattr(canaan_config, "ASR_PREFETCH_ENABLED", False) or model is None:
            return None
        if getattr(getattr(self, "speaker_gate", None), "enabled", False):
            return None
        if getattr(canaan_config, "ENDPOINT_MODE", "fixed") != "fixed":
            return None
        from canaan_asr_prefetch import ASRPrefetch, RecordingPrefetch
        if getattr(self, "_asr_prefetch_manager", None) is None:
            self._asr_prefetch_manager = ASRPrefetch()
        grace_sec = getattr(canaan_config, "ENDPOINT_PARTIAL_GRACE_SEC", 0)
        import os
        if "CANAAN_ENDPOINT_PARTIAL_GRACE" not in os.environ:
            from canaan_gpu_priority import demo_mode
            if demo_mode():
                grace_sec = max(grace_sec, 1.5)
        pending = getattr(self, "pending", None)
        if pending is not None and pending.has_pending():
            grace_sec = 0  # 确认/取消优先，不因短答或填充词多等。
        return RecordingPrefetch(self._asr_prefetch_manager, model, min_speech_sec=min_speech_sec, grace_sec=grace_sec)

    def _listen_stop_continuation(self, stream, stop_word):
        """止语词打断后的后续监听：如果主人紧接着说了新指令（例如“别说了，帮我打开记事本”），
        顺畅接续执行；如果主人只是让团团闭嘴（说完停顿），则完全保持静默。"""
        from canaan_voice_capture import VoiceCaptureContext
        capture = VoiceCaptureContext(self)
        self.is_recording_command = True
        prefetch = None
        try:
            endpointer = create_endpointer(noise_floor=self.ambient_noise_floor, no_speech_timeout=0.6)
            prefetch = self._begin_asr_prefetch()
            state = "waiting"
            while state not in ("end", "no_speech", "max_len"):
                if not capture.current():
                    if prefetch:
                        prefetch.discard()
                    return
                samples, overflowed = stream.read(1600)
                if not capture.current():
                    if prefetch:
                        prefetch.discard()
                    return
                chunk = samples.reshape(-1)
                if not len(chunk) or overflowed:
                    getattr(self, '_pending_voice_audio_gap', lambda: None)()
                    raise OSError("麦克风录音中断，请重新说完整指令")
                self._mic_level = float(np.sqrt(np.mean(chunk ** 2)))
                state = endpointer.feed(chunk)
                if prefetch:
                    state = prefetch.observe(endpointer)
        except Exception as e:
            if prefetch:
                prefetch.discard()
            print(f"[!] 止语词后接续录音失败，请重试: {e}")
            self.ui_queue.put(("status", ("ready", "麦克风录音失败，请重试")))
            self.speech_cooldown_until = time.time() + 0.3
            return
        finally:
            self.is_recording_command = False

        if not capture.current(final=True):
            if prefetch:
                prefetch.discard()
            return

        if state == "max_len":
            if prefetch:
                prefetch.discard()
            print("[!] 达到接续录音时长上限，请分成短句再说")
            self.ui_queue.put(("status", ("ready", "这句话较长，请分成短句再说")))
            self.speak("这句话有点长，请分成短句再说。", "confident")
            return

        if endpointer.is_valid_utterance():
            print(f"[*] 止语词“{stop_word}”后捕捉到接续指令，立即识别执行...")
            tracer = TurnTracer(source="bargein_continuation")
            audio_data = endpointer.get_audio(keep_tail_sec=0.3)
            kwargs = {"asr_ticket": prefetch} if prefetch and prefetch.ticket is not None else {}
            if capture.token is not None:
                kwargs["expected_context_token"] = capture.token
            self.process_audio_data(audio_data, tracer, **kwargs)
        else:
            if prefetch:
                prefetch.discard()
            print(f"[*] 止语词“{stop_word}”后无后续指令，保持安静")
            self.speech_cooldown_until = time.time() + 0.3

    def execute_quick_action(self, action, param, text, keyword_only=False):
        """语音直达秒级动作执行通道"""
        try:
            # KWS gives a fragment, so it cannot distinguish "锁屏" from "不要锁屏".
            decision = apply_command_guard(text, {"action": action, "param": param}, require_confidence=keyword_only)
            if decision.get("action") == "chat":
                if decision.get("_pending"):
                    self.set_pending(decision["_pending"], "rule", text)
                self.ui_queue.put(("user_speech", text))
                self.speak(decision.get("reply", "请确认要执行的操作。"), "confident")
                return
            task_id, _ = GLOBAL_TASK_MANAGER.check_and_create_task(text)
            self.ui_queue.put(("user_speech", text))
            exec_res = CanaanOSExecutor.execute(action, param, source="rule", user_text=text)
            GLOBAL_TASK_MANAGER.notify_finish(task_id, exec_res.get("success", True), exec_res.get("message", ""))
            self.ui_queue.put(("task_finished", (task_id, exec_res.get("success", True), exec_res.get("message", ""))))
            self.ui_queue.put(("action_executed", (action, exec_res.get("message", ""))))
            if exec_res.get("speech_override"):
                self.speak(exec_res["speech_override"], "confident")
        except Exception as e:
            print(f"[!] 快速动作执行异常: {e}")

    def record_command_after_wake(self, stream, is_bargein=False):
        """唤醒后持续自适应采集用户的自然语音指令（支持思考停顿与智能端点截断）"""
        from canaan_voice_capture import VoiceCaptureContext
        capture = VoiceCaptureContext(self)
        self.is_recording_command = True
        prefetch = None
        try:
            import canaan_events
            canaan_events.publish("listening")

            # 普通唤醒清掉提示音残响；打断时没有提示音，应保留首块指令。
            if not is_bargein:
                try:
                    winsound.Beep(1150, 80)
                except Exception:
                    pass
                prompt_audio, overflowed = stream.read(1600)
                if not capture.current():
                    if prefetch:
                        prefetch.discard()
                    return
                if not len(prompt_audio) or overflowed:
                    getattr(self, '_pending_voice_audio_gap', lambda: None)()
                    raise OSError("麦克风录音中断，请重新说完整指令")

            # 端点检测：优先 silero VAD，模型缺失时自动退回能量阈值法。
            endpointer = create_endpointer(noise_floor=self.ambient_noise_floor)
            prefetch = self._begin_asr_prefetch()
            state = "waiting"
            while state not in ("end", "no_speech", "max_len"):
                if not capture.current():
                    if prefetch:
                        prefetch.discard()
                    return
                samples, overflowed = stream.read(1600)
                if not capture.current():
                    if prefetch:
                        prefetch.discard()
                    return
                chunk = samples.reshape(-1)
                if not len(chunk) or overflowed:
                    getattr(self, '_pending_voice_audio_gap', lambda: None)()
                    raise OSError("麦克风录音中断，请重新说完整指令")
                self._mic_level = float(np.sqrt(np.mean(chunk ** 2)))
                state = endpointer.feed(chunk)
                if prefetch:
                    state = prefetch.observe(endpointer)
            state = self._merge_wait(stream, endpointer, state, prefetch=prefetch, recording_valid=capture.current)
        except Exception as e:
            if prefetch:
                prefetch.discard()
            print(f"[!] 指令录音失败，请重试: {e}")
            self.ui_queue.put(("status", ("ready", "麦克风录音失败，请重试")))
            return
        finally:
            self.is_recording_command = False

        if not capture.current(final=True):
            if prefetch:
                prefetch.discard()
            return

        if state == "max_len":
            if prefetch:
                prefetch.discard()
            print("[!] 达到录音时长上限，请分成短句再说")
            self.ui_queue.put(("status", ("ready", "这句话较长，请分成短句再说")))
            self.speak("这句话有点长，请分成短句再说。", "confident")
            return

        if endpointer.is_valid_utterance():
            tracer = TurnTracer(source="kws")
            tracer.set("endpointer", endpointer.kind)
            tracer.set("endpoint_ms", endpointer.endpoint_wait_ms())
            tracer.set("record_ms", endpointer.record_ms())
            audio_data = endpointer.get_audio(keep_tail_sec=0.3)
            kwargs = {"asr_ticket": prefetch} if prefetch and prefetch.ticket is not None else {}
            if capture.token is not None:
                kwargs["expected_context_token"] = capture.token
            self.process_audio_data(audio_data, tracer, **kwargs)
        else:
            if prefetch:
                prefetch.discard()
            if is_bargein:
                # 播报中喊唤醒词打断，但未给新指令：保持安静归位，绝不反问“在呢亲爱的”
                print("[*] 唤醒打断未检测到后续指令，保持静默归位...")
                self.ui_queue.put(("status", ("ready", "在呢，随时吩咐~")))
                self.speech_cooldown_until = time.time() + 0.3
            else:
                # 用户仅呼叫唤醒词未下达具体指令 -> 触发亲昵应答
                print("[*] 用户仅唤醒未下达指令，触发亲昵应答...")
                self.play_canaan_greeting()

    def _merge_wait(self, stream, endpointer, state, prefetch=None, recording_valid=None):
        """拼接后半句（canaan_config.TURN_MERGE_ENABLED，默认关）：判定说完后再听一会儿，
        窗口内又开口就接着录（同一句），直到窗口内一直安静。撤不回的动作因此不会在没说完时执行。"""
        if recording_valid is not None and not recording_valid():
            if prefetch:
                prefetch.discard()
            return "cancelled"
        if state != "end" or not getattr(canaan_config, "TURN_MERGE_ENABLED", False):
            return state
        import canaan_turnmerge
        # 整数样本预算避免 0.1 累加成 0.999999...，白等第 11 块。
        window_samples = max(0, int(round(canaan_turnmerge.window_sec() * SAMPLE_RATE)))
        max_samples = int(round(endpointer.max_record_sec * SAMPLE_RATE))
        if endpointer.total_samples >= max_samples:
            # 正常端点恰好落在上限时，本句已经完整；不再追加窗口音频。
            return state
        while window_samples:
            quiet_samples = 0
            resumed = False
            while quiet_samples < window_samples:
                if recording_valid is not None and not recording_valid():
                    if prefetch:
                        prefetch.discard()
                    return "cancelled"
                remaining = max_samples - endpointer.total_samples
                if remaining <= 0:
                    endpointer.state = "max_len"
                    return "max_len"
                samples, overflowed = stream.read(min(1600, window_samples - quiet_samples, remaining))
                if recording_valid is not None and not recording_valid():
                    if prefetch:
                        prefetch.discard()
                    return "cancelled"
                chunk = samples.reshape(-1)
                if not len(chunk) or overflowed:
                    getattr(self, '_pending_voice_audio_gap', lambda: None)()
                    raise OSError("麦克风录音中断，请重新说完整指令")
                # 一块只喂 VAD 一次；首块恢复语音和窗口静音均走统一计数。
                state = endpointer.feed(chunk)
                if prefetch:
                    state = prefetch.observe(endpointer)
                if state == "max_len":
                    return state
                if state == "speaking":
                    resumed = True
                    break
                quiet_samples += len(chunk)
            if not resumed:
                return "end"
            print("[🔗 拼接] 停顿后又接着说，继续录同一句")
            state = "speaking"
            while state not in ("end", "no_speech", "max_len"):
                if recording_valid is not None and not recording_valid():
                    if prefetch:
                        prefetch.discard()
                    return "cancelled"
                remaining = max_samples - endpointer.total_samples
                if remaining <= 0:
                    endpointer.state = "max_len"
                    return "max_len"
                samples, overflowed = stream.read(min(1600, remaining))
                if recording_valid is not None and not recording_valid():
                    if prefetch:
                        prefetch.discard()
                    return "cancelled"
                chunk = samples.reshape(-1)
                if not len(chunk) or overflowed:
                    getattr(self, '_pending_voice_audio_gap', lambda: None)()
                    raise OSError("麦克风录音中断，请重新说完整指令")
                state = endpointer.feed(chunk)
                if prefetch:
                    state = prefetch.observe(endpointer)
            if state != "end":
                return state
        return state

    def record_followup(self, stream, first_chunk, *, near_end_filtered=False):
        """连续对话窗口内的录音：不响提示音，用 VAD 判断有没有开口；窗口内一直没说话就结束，回到等待唤醒。"""
        from canaan_voice_capture import VoiceCaptureContext
        capture = VoiceCaptureContext(self)
        clarify = getattr(self, "clarify", None)
        snapshot = getattr(clarify, "snapshot", None)
        # None records absence; Ellipsis is only compatibility for older ports.
        question = snapshot() if callable(snapshot) else Ellipsis
        if near_end_filtered:
            question = getattr(self, "_clarify_voice_question", question)
        def recording_current(final=False):
            if not capture.current(final=final):
                return False
            # Persisted question versions are read at capture boundaries, not
            # on every audio frame; the owner cancel event remains immediate.
            if not final or question is Ellipsis:
                return True
            current = getattr(self, "clarify", None)
            matches = getattr(current, "matches", None)
            return current is clarify and callable(matches) and matches(question)
        if not recording_current(final=True):
            return
        remaining = max(0.5, self.followup_until - time.time())
        short_answer = bool(getattr(self, "_pending_voice_clarification_active", lambda: False)())
        minimum = .2 if short_answer else .4
        context = capture.token
        if near_end_filtered and context != getattr(self, "_clarify_voice_context", None):
            return  # 首帧属于刚切走的使用者，不能补进另一个人的草稿。
        self.followup_until = 0.0
        self.is_recording_command = True
        slot_capture = {"capture": capture, "session": getattr(self, "_speech_playback_session", None),
                        "active": short_answer}
        self._clarify_voice_recording = slot_capture
        prefetch = None
        try:
            endpointer = create_endpointer(noise_floor=self.ambient_noise_floor, no_speech_timeout=remaining)
            prefetch = self._begin_asr_prefetch(min_speech_sec=minimum)
            if short_answer and not near_end_filtered:
                filtered = self._pending_voice_clarification_filter(first_chunk)
                if filtered is None:
                    return
                first_chunk = filtered
            state = endpointer.feed(first_chunk)
            if prefetch:
                state = prefetch.observe(endpointer)
            while state not in ("end", "no_speech", "max_len"):
                if not recording_current():
                    if prefetch:
                        prefetch.discard()
                    return
                samples, overflowed = stream.read(1600)
                if not recording_current():
                    if prefetch:
                        prefetch.discard()
                    return
                chunk = samples.reshape(-1)
                if not len(chunk) or overflowed:
                    getattr(self, '_pending_voice_audio_gap', lambda: None)()
                    raise OSError("麦克风录音中断，请重新说完整指令")
                if short_answer:
                    filtered = self._pending_voice_clarification_filter(chunk)
                    if filtered is None:
                        raise OSError("追问会话已变更")
                    chunk = filtered
                state = endpointer.feed(chunk)
                if prefetch:
                    state = prefetch.observe(endpointer)
            state = self._merge_wait(stream, endpointer, state, prefetch=prefetch, recording_valid=recording_current)
        except Exception as e:
            if prefetch:
                prefetch.discard()
            print(f"[!] 连续对话录音失败，结束监听: {e}")
            self.ui_queue.put(("followup_ended", None))
            return
        finally:
            self.is_recording_command = False
            if getattr(self, "_clarify_voice_recording", None) is slot_capture:
                self._clarify_voice_recording = None

        if not recording_current(final=True):
            if prefetch:
                prefetch.discard()
            return

        if state == "max_len":
            if prefetch:
                prefetch.discard()
            print("[!] 达到连续对话录音时长上限，请分成短句再说")
            self.ui_queue.put(("followup_ended", None))
            self.ui_queue.put(("status", ("ready", "这句话较长，请分成短句再说")))
            self.speak("这句话有点长，请分成短句再说。", "confident")
            return

        # 连续对话没有唤醒词把关，要求更长的有效语音，减少环境人声误触发
        if endpointer.is_valid_utterance(min_speech_sec=minimum):
            print("[↻ 连续对话] 无需唤醒，继续处理下一句")
            tracer = TurnTracer(source="followup")
            tracer.set("endpointer", endpointer.kind)
            tracer.set("endpoint_ms", endpointer.endpoint_wait_ms())
            tracer.set("record_ms", endpointer.record_ms())
            kwargs = {"asr_ticket": prefetch} if prefetch and prefetch.ticket is not None else {}
            if capture.token is not None:
                kwargs["expected_context_token"] = capture.token
            if question is not Ellipsis:
                kwargs["expected_clarification"] = question
            self.process_audio_data(endpointer.get_audio(keep_tail_sec=0.3), tracer, **kwargs)
        else:
            if prefetch:
                prefetch.discard()
            self.ui_queue.put(("followup_ended", None))

    def arm_followup(self):
        sec = float(getattr(canaan_config, "FOLLOWUP_LISTEN_SEC", 0) or 0)
        if sec > 0 and self.is_kws_enabled:
            # 从冷却期结束开始计时，避免录到自己播报的尾音
            self.followup_until = self.speech_cooldown_until + sec
            self.ui_queue.put(("followup_listening", sec))

    def play_canaan_greeting(self):
        """纯唤醒时的亲昵极简应答（对标小爱/豆包：0.5秒直出，绝不念冗长开场白）"""
        self.ui_queue.put(("status", ("speaking", "在呢，亲爱的。")))
        self.action_badge.config(text=f"[👑 {ASSISTANT_NAME}在呢，请吩咐]", fg="#f472b6")
        self.speak("在呢，亲爱的。", "pamper")
        if getattr(self, "pet", None):
            self.pet.say("在呢，亲爱的。", 3.0, speak=False)

    def _prepare_pending_voice_listener(self):
        """把首次加载回声处理库移到启动后台，避免吞掉主人第一句确认的开头。"""
        try:
            from canaan_pending_bargein import PendingSpeechListener
            self._pending_voice_prepared = PendingSpeechListener(noise_floor=self.ambient_noise_floor, max_record_sec=25.0)
        except Exception as exc:
            print(f"[!] 确认回答监听预热未完成：{type(exc).__name__}")

    def _voice_output_busy(self):
        """Logical playback completion cannot release another session's device."""
        pipeline = getattr(getattr(self, "engine", None), "pipeline", None)
        player = getattr(pipeline, "player", None)
        return getattr(player, "output_busy", False) is True

    def _pending_voice_preplay_active(self):
        session = getattr(self, "_speech_playback_session", None)
        pending = getattr(self, "pending", None)
        item = pending.peek() if pending is not None else None
        ack = getattr(self, "_ack_playback_session", None)
        ack_finished = ack.get("finished_event") if ack else None
        return bool(not getattr(self, "_voice_output_busy", lambda: False)()
                    and item is not None and session and session.get("pending") is item
                    and session.get("state") == "preparing" and not session.get("cancelled")
                    and (not ack or (ack.get("state") == "finished"
                                     and (ack_finished is None or ack_finished.is_set()))))

    def _pending_voice_tail_active(self):
        session = getattr(self, "_speech_playback_session", None)
        pending = getattr(self, "pending", None)
        item = pending.peek() if pending is not None else None
        ack = getattr(self, "_ack_playback_session", None)
        finished = session.get("finished_event") if session else None
        ack_finished = ack.get("finished_event") if ack else None
        return bool(not self.is_speaking and not getattr(self, "_voice_output_busy", lambda: False)()
                    and item is not None and session
                    and session is getattr(self, "_pending_voice_session", None)
                    and session.get("pending") is item and item is getattr(self, "_pending_voice_item", None)
                    and session.get("state") == "finished"
                    and (finished is None or finished.is_set())
                    and (not ack or (ack.get("state") == "finished"
                                     and (ack_finished is None or ack_finished.is_set()))))

    def _pending_voice_ducked_active(self):
        """停播请求不等于设备静音；播放器完成交接后才能接收无参考的原始收音。"""
        ducked = getattr(self, "_pending_voice_ducked", None)
        pending = getattr(self, "pending", None)
        item = pending.peek() if pending is not None else None
        session = ducked.get("session") if ducked else None
        finished = session.get("finished_event") if session else None
        ack = getattr(self, "_ack_playback_session", None)
        ack_finished = ack.get("finished_event") if ack else None
        return bool(not getattr(self, "_voice_output_busy", lambda: False)()
                    and ducked and item is not None and ducked["item"] is item and not self.is_speaking
                    and session and session is getattr(self, "_speech_playback_session", None)
                    and session.get("pending") is item and session.get("state") == "finished"
                    and (finished is None or finished.is_set())
                    and (not ack or (ack.get("state") == "finished"
                                     and (ack_finished is None or ack_finished.is_set())))
                    and 0 <= time.monotonic() - ducked["at"] <= 3.0)

    def _duck_pending_prompt(self, item, session):
        """主人开口先请求停提示；设备退出期间继续使用回声参考收完整回答。"""
        from canaan_voice_capture import interrupt_voice_state, voice_state_lock
        with voice_state_lock(self):
            request = interrupt_voice_state(self, clear_speaking=False,
                                            expected_session=session, expected_pending=item)
            if request is None:
                return
            now = time.monotonic()
            self._pending_voice_ducked = {"item": item, "session": session, "at": now, "resolved": False}
            self._pending_voice_stop_requested_at = now
        print("[确认回答] 听到主人开口，先停掉确认提示")
        try:
            sd.stop()
        except Exception:
            pass
        # Keep the echo reference until the physical owner has actually exited.
        if request.current(self) and getattr(self, "pet", None):
            self.pet.interrupt(notify=False)

    def _pending_voice_ducked_expire(self, item):
        """停掉提示后没收到确认 / 取消：开连续对话，主人可以不喊唤醒词直接再说一遍。"""
        ducked = getattr(self, "_pending_voice_ducked", None)
        if (ducked and not ducked["resolved"] and ducked["item"] is item and not self.is_speaking
                and time.monotonic() - ducked["at"] > 3.0):
            ducked["resolved"] = True
            print("[确认回答] 停掉提示后没听清确认或取消，直接再说一遍就行")
            self.arm_followup()

    def _pending_voice_context_token(self):
        """Capture identity/privacy generation, never infer the speaker's identity from ASR."""
        provider = getattr(self, "_context_token", None)
        if callable(provider):
            return provider()
        return (getattr(self, "_voice_context_generation", None),
                getattr(getattr(self, "profile", None), "privacy_revision", None))

    def _pending_voice_clarification_active(self):
        """A missing slot permits a short reply only after the actual prompt finished."""
        session = getattr(self, "_speech_playback_session", None)
        ack = getattr(self, "_ack_playback_session", None)
        finished = session.get("finished_event") if session else None
        if (getattr(self, "is_speaking", False) or getattr(self, "is_processing", False)
                or getattr(self, "_voice_output_busy", lambda: False)()
                or not session or session.get("state") != "finished" or session.get("cancelled")
                or (finished is not None and not finished.is_set())
                or (ack and ack.get("state") != "finished")):
            return False
        recording = getattr(self, "_clarify_voice_recording", None)
        if recording is not None and getattr(self, "is_recording_command", False):
            # The recording start already established the missing slot.  Keep
            # checking output/owner gates here, and query persistence at handoff.
            return bool(recording["active"] and recording["session"] is session
                        and recording["capture"].current())
        draft = getattr(self, "chatapp_draft", None)
        part = draft.peek() if draft is not None else None
        if part and bool(part.get("contact")) != bool(part.get("message")):
            return True
        clarify = getattr(self, "clarify", None)
        return bool(clarify is not None and clarify.peek())

    def _pending_voice_clarification_filter(self, microphone):
        """Keep short names immediately after a question, rejecting the speaker's echo tail."""
        if not self._pending_voice_clarification_active():
            return None
        session = self._speech_playback_session
        context = self._pending_voice_context_token()
        if (getattr(self, "_clarify_voice_question_session", None) is not session
                or getattr(self, "_clarify_voice_question_context", None) != context):
            snapshot = getattr(getattr(self, "clarify", None), "snapshot", None)
            self._clarify_voice_question = snapshot() if callable(snapshot) else Ellipsis
            self._clarify_voice_question_session, self._clarify_voice_question_context = session, context
        question = self._clarify_voice_question
        raw = np.asarray(microphone, np.float32).reshape(-1)
        # An acknowledged finished stream proves no new output. Keep AEC gating
        # through the reference tap's normal 0.6 s acoustic tail margin.
        if time.monotonic() - session.get("finished_at", time.monotonic()) >= .6:
            self._clarify_voice_context = context
            self._clarify_voice_question = question
            return raw
        listener = getattr(self, "_clarify_voice_listener", None)
        if (listener is None or getattr(self, "_clarify_voice_session", None) is not session
                or getattr(self, "_clarify_voice_context", None) != context
                or getattr(self, "_clarify_voice_question", Ellipsis) != question):
            from canaan_pending_bargein import PendingSpeechListener
            listener = self._clarify_voice_listener = PendingSpeechListener(noise_floor=self.ambient_noise_floor)
            self._clarify_voice_session, self._clarify_voice_context = session, context
            self._clarify_voice_question = question
        listener.feed(raw, playback_silent=True)
        clean = listener.bargein_audio
        return clean if len(clean) else np.zeros_like(raw)

    def _pending_voice_audio_gap(self):
        self._pending_voice_audio_generation = getattr(self, '_pending_voice_audio_generation', 0) + 1
        self._pending_voice_deferred = None
        listener = getattr(self, "_pending_voice_listener", None)
        if listener is not None:
            reset = getattr(listener, "discontinuity", None)
            if callable(reset):
                reset()
        pending = getattr(self, 'pending', None)
        if pending is not None and pending.peek() is not None:
            print('[确认回答] 收音中断，旧的识别结果作废；任务保留，恢复后请重新确认')
            ui = getattr(self, 'ui_queue', None)
            if ui is not None:
                ui.put(('status', ('processing', '收音中断，恢复后请重新确认或取消')))

    def _pending_voice_recognize(self, request):
        """Runs only on the bounded ASR worker; microphone reads never wait for it."""
        item = request["item"]
        if (self.pending.peek() is not item
                or self._pending_voice_context_token() != request["context"]
                or request.get('audio_generation', 0) != getattr(self, '_pending_voice_audio_generation', 0)):
            return {"rejected": "stale"}
        if not self._speaker_ok("确认回答", request["audio"]):
            return {"rejected": "speaker"}
        # Speaker verification may outlive this task/utterance. Do not spend an
        # ASR call on obsolete audio while the latest reply waits in the queue.
        if (self.pending.peek() is not item
                or self._pending_voice_context_token() != request["context"]
                or request.get('audio_generation', 0) != getattr(self, '_pending_voice_audio_generation', 0)):
            return {"rejected": "stale"}
        def cancelled():
            return (self.pending.peek() is not item
                    or self._pending_voice_context_token() != request["context"]
                    or request.get('audio_generation', 0) != getattr(self, '_pending_voice_audio_generation', 0))
        # Short consent uses plain decoding in the Whisper fallback. The main
        # SenseVoice path is unchanged; full commands retain their own prompt.
        text, _ = transcribe(request["audio"], sense_voice=getattr(self, "sense_voice", None),
                             whisper=getattr(self, "asr_model", None), command=False, should_stop=cancelled)
        if request.get('audio_generation', 0) != getattr(self, '_pending_voice_audio_generation', 0):
            return {'rejected': 'audio_gap'}
        text = WAKE_PREFIX_RE.sub("", str(text or "")).strip('。，, !！')
        kind, current = self.pending.classify(text, expected_item=item)
        # 明确的新要求可以替换任务，不能让整个待确认窗口吞掉“打开记事本”。
        # 播报中的“请说确认执行、取消放弃”等元提示不进入普通指令链。
        ordinary = bool(text and re.match(
            r"^(?:帮我|请你|麻烦|给我|打开|关闭|查询|搜索|把|改成|换成|发给|发送|"
            r"不要发|别发|我想|我要|现在|先|停止|我是|切换|换回|(?:正文|内容|消息)(?:改成|改为|换成|换为))", text))
        return {"text": text, "kind": kind if current is item else None, "ordinary": ordinary}

    def _submit_deferred_pending_voice(self):
        """At most one newer full reply waits while the single ASR worker is busy."""
        request = getattr(self, '_pending_voice_deferred', None)
        if request is None:
            return False
        item = request['item']
        if (self.pending.peek() is not item
                or self._pending_voice_context_token() != request['context']
                or request.get('audio_generation', 0) != getattr(self, '_pending_voice_audio_generation', 0)
                or getattr(self, '_pending_voice_dispatched_item', None) is item):
            self._pending_voice_deferred = None
            return False
        worker = getattr(self, '_pending_voice_worker', None)
        if worker is not None and worker.busy:
            return False
        if worker is None:
            from canaan_pending_voice_worker import PendingVoiceWorker
            worker = self._pending_voice_worker = PendingVoiceWorker(self._pending_voice_recognize)
        if not worker.submit(request['audio'], item=item, context=request['context'],
                             session=request['session'], stats=request['stats'],
                             audio_generation=request.get('audio_generation', 0),
                             captured_at=request.get('captured_at'),
                             acoustic_timing=request.get('acoustic_timing')):
            return False
        self._pending_voice_deferred = None
        print('[确认回答] 已继续识别最新完整回答')
        return True

    def _consume_pending_voice_result(self):
        """Poll completed ASR without waiting; retain valid answers until old playback releases."""
        getattr(self, '_submit_deferred_pending_voice', lambda: False)()
        worker = getattr(self, "_pending_voice_worker", None)
        result = worker.peek() if worker is not None else None
        if result is None:
            return False
        item = result["item"]
        def still_current():
            return (self.pending.peek() is item
                    and self._pending_voice_context_token() == result["context"]
                    and result.get('audio_generation', 0) == getattr(self, '_pending_voice_audio_generation', 0))
        if not still_current():
            discarded = worker.discard(result)
            self._pending_voice_metrics = {"outcome": "stale"}
            if discarded:
                # A newer complete reply may already be queued. Reuse the free
                # worker now rather than waiting for the next microphone block.
                getattr(self, '_submit_deferred_pending_voice', lambda: False)()
            return False
        newer = getattr(self, '_pending_voice_deferred', None)
        if (newer and newer['item'] is item and newer['context'] == result['context']):
            # The user finished another utterance before we dispatched this one.
            # Never let an old "confirm" overtake a newer "cancel" or correction.
            if worker.discard(result):
                self._pending_voice_metrics = {'outcome': 'superseded'}
                getattr(self, '_submit_deferred_pending_voice', lambda: False)()
            return False
        kind, current = self.pending.classify(result.get("text", ""), expected_item=item)
        ordinary = bool(result.get("ordinary"))
        if result.get("error") or result.get("rejected") or (kind not in ("confirm", "cancel") and not ordinary):
            worker.discard(result)
            self._pending_voice_metrics = {"outcome": result.get("error") or result.get("rejected") or "unrecognized"}
            print("[确认回答] 没有收到完整确认或新的明确要求，待确认项保留")
            return False
        if not result.get("stop_requested"):
            from canaan_voice_capture import interrupt_voice_state, voice_state_lock
            with voice_state_lock(self):
                request = interrupt_voice_state(self, clear_speaking=False, expected_pending=item)
                if request is None:
                    worker.discard(result)
                    return False
                result["stop_requested"] = True
                result["stop_requested_at"] = time.monotonic()
                if request.session is not None and request.session.get("pending") is item:
                    result["stop_session"] = request.session
                if request.ack is not None:
                    result["stop_ack_session"] = request.ack
            try:
                sd.stop()
            except Exception:
                pass
            # Native stop can overlap a new task, user or correction. Recheck
            # before touching replacement acknowledgements or the companion UI.
            if not still_current():
                worker.discard(result)
                return False
            if request.current(self) and getattr(self, "pet", None):
                self.pet.interrupt(notify=False)
        if not still_current():
            worker.discard(result)
            return False
        session = result.get("stop_session")
        finished = session.get("finished_event") if session else None
        listener = getattr(self, '_pending_voice_listener', None)
        receiving_new_reply = (getattr(self, '_pending_voice_item', None) is item
                               and bool(getattr(listener, 'capturing', False)))
        ack_waiting = False
        # Keep the output we interrupted, even if a replacement is registered
        # while it releases. Also stop/wait for any current short output.
        for ack in (result.get("stop_ack_session"), getattr(self, "_ack_playback_session", None)):
            if ack is not None:
                ack_finished = ack.get("finished_event")
                if ack.get("state") != "finished" or (ack_finished is not None and not ack_finished.is_set()):
                    ack["cancelled"] = True
                    ack_waiting = True
        if (self.is_speaking or getattr(self, "is_processing", False) or ack_waiting
                or getattr(self, "_voice_output_busy", lambda: False)()
                or (finished is not None and not finished.is_set()) or receiving_new_reply):
            return False  # 不丢掉已经识别的回答；麦克风下一块再查，直至同一待确认项过期。
        if not still_current():
            worker.discard(result)
            return False
        if not worker.discard(result):
            return False
        ducked = getattr(self, "_pending_voice_ducked", None)
        if ducked and ducked["item"] is item:
            ducked["resolved"] = True
        self._pending_voice_dispatched_item = item
        now = time.monotonic()
        self._pending_voice_metrics = {
            "outcome": kind if kind in ("confirm", "cancel") else "new_request",
            "asr_ms": round((result["recognized_at"] - result["asr_started_at"]) * 1000, 1),
            "queue_wait_ms": round(max(0., result['asr_started_at'] - result['captured_at']) * 1000, 1),
            "handoff_wait_ms": round((now - result["recognized_at"]) * 1000, 1),
            "capture_to_dispatch_ms": round((now - result["captured_at"]) * 1000, 1)}
        timing = result.get("acoustic_timing")
        if timing is not None:
            # Relative acoustic durations bridge the listener's performance
            # clock and the hub's monotonic clock without mixing absolute epochs.
            self._pending_voice_metrics.update(
                endpoint_wait_ms=round(timing["endpoint_wait_sec"] * 1000, 1),
                speech_frame_ms=round(timing["frame_sec"] * 1000, 1),
                detected_speech_end_to_dispatch_ms=round(
                    (now - result["captured_at"] + timing["endpoint_wait_sec"]) * 1000, 1))
        stopped_at = session.get("finished_at") if session else None
        requested_at = (getattr(self, "_pending_voice_stop_requested_at", None)
                        if ducked and ducked["item"] is item else result.get("stop_requested_at"))
        if stopped_at is not None and requested_at is not None and stopped_at >= requested_at:
            self._pending_voice_metrics["playback_release_ms"] = round((stopped_at - requested_at) * 1000, 1)
        tracer = TurnTracer(source="pending_bargein")
        for key, value in self._pending_voice_metrics.items():
            if hasattr(tracer, "set"):
                tracer.set("pending_voice_" + key, value)
        kwargs = {"text": result["text"]}
        if kind in ("confirm", "cancel"):
            kwargs["expected_pending"] = item
        if callable(getattr(self, "_context_token", None)):
            kwargs["expected_context_token"] = result["context"]
        self.process_audio_data(result["audio"], tracer, **kwargs)
        return True

    def _pending_voice_bargein(self, microphone):
        """待确认全程收短答；声学检测在读音频线程，识别与交接始终异步。"""
        pending = getattr(self, "pending", None)
        item = pending.peek() if pending is not None else None
        if item is None:
            self._pending_voice_listener = None
            self._pending_voice_item = None
            self._pending_voice_deferred = None
            return False
        try:
            if getattr(self, "_pending_voice_dispatched_item", None) is item:
                return True
            session = getattr(self, "_speech_playback_session", None)
            context = self._pending_voice_context_token()
            audio_generation = getattr(self, '_pending_voice_audio_generation', 0)
            if (getattr(self, "_pending_voice_item", None) is not item
                    or getattr(self, "_pending_voice_session", None) is not session
                    or getattr(self, "_pending_voice_context", context) != context):
                prepared = getattr(self, "_pending_voice_prepared", None)
                if prepared is None:
                    from canaan_pending_bargein import PendingSpeechListener
                    prepared = PendingSpeechListener(noise_floor=self.ambient_noise_floor, max_record_sec=25.0)
                self._pending_voice_listener = prepared
                self._pending_voice_prepared = None
                self._pending_voice_item = item
                self._pending_voice_session = session
                self._pending_voice_context = context
            silent = (getattr(self, "_pending_voice_preplay_active", lambda: False)()
                      or getattr(self, "_pending_voice_tail_active", lambda: False)()
                      or getattr(self, "_pending_voice_ducked_active", lambda: False)())
            feed_started_at = time.monotonic()
            audio = (self._pending_voice_listener.feed(microphone, playback_silent=True) if silent
                     else self._pending_voice_listener.feed(microphone))
            if (self.pending.peek() is not item or self._pending_voice_context_token() != context
                    or getattr(self, '_pending_voice_audio_generation', 0) != audio_generation):
                return False  # A profile/task switch during AEC must not stop the new person's speech.
            if getattr(self._pending_voice_listener, "onset", False):
                self._pending_voice_listener.onset = False
                # A new near-end utterance supersedes uncommitted older consent,
                # even if this correction later exceeds the limit or loses its tap.
                # Keep the current waveform; only old ASR/deferred audio becomes stale.
                audio_generation += 1
                self._pending_voice_audio_generation = audio_generation
                self._pending_voice_deferred = None
                ducked = getattr(self, "_pending_voice_ducked", None)
                ack = getattr(self, "_ack_playback_session", None)
                ack_finished = ack.get("finished_event") if ack else None
                ack_playing = bool(ack and (ack.get("state") != "finished"
                                           or (ack_finished is not None and not ack_finished.is_set())))
                if (self.is_speaking or ack_playing) and not (ducked and ducked["item"] is item):
                    self._duck_pending_prompt(item, session)
            if audio is None:
                dropped = getattr(self._pending_voice_listener, "dropped", None)
                if dropped:
                    stats = getattr(self._pending_voice_listener, "stats_text", lambda: "")()
                    print(f"[确认回答] 播报中听到一段声音但没收成整句：{dropped}（{stats}）")
                    self._pending_voice_listener.dropped = None
                return False
            stats = getattr(self._pending_voice_listener, "stats_text", lambda: "")()
            worker = getattr(self, "_pending_voice_worker", None)
            if worker is None:
                from canaan_pending_voice_worker import PendingVoiceWorker
                worker = self._pending_voice_worker = PendingVoiceWorker(self._pending_voice_recognize)
            captured_at = time.monotonic()
            acoustic_timing = getattr(self._pending_voice_listener, 'last_timing', None)
            if isinstance(acoustic_timing, dict):
                wait = acoustic_timing.get('endpoint_wait_sec')
                elapsed = captured_at - feed_started_at
                if isinstance(wait, (int, float)) and 0 <= wait <= 30 and 0 <= elapsed <= 30:
                    acoustic_timing = {**acoustic_timing, 'endpoint_wait_sec': wait + elapsed}
                else:
                    acoustic_timing = None
            submitted = worker.submit(audio, item=item, context=context, session=session, stats=stats,
                                      audio_generation=audio_generation, captured_at=captured_at,
                                      acoustic_timing=acoustic_timing)
            if submitted:
                print(f"[确认回答] 完整短答已交给后台识别（{stats}）")
            elif worker.busy and 0 < len(audio) <= 26 * 16000 and np.isfinite(audio).all():
                self._pending_voice_deferred = {'audio': audio.copy(), 'item': item,
                    'context': context, 'session': session, 'stats': stats,
                    'audio_generation': audio_generation, 'captured_at': captured_at,
                    'acoustic_timing': dict(acoustic_timing) if isinstance(acoustic_timing, dict) else None}
                print('[确认回答] 正在识别前一句，已保留最新完整回答')
            return False
        except Exception as exc:
            # 本入口失效时保留原来的唤醒词/快捷键/界面确认，不从未知音频推断同意。
            self._pending_voice_listener = None
            self._pending_voice_item = None
            print(f"[!] 确认回答监听暂不可用：{type(exc).__name__}")
            return False

    def _release_asr_load_memory(self):
        try:
            from canaan_asr import release_load_memory_rounds
            runs, before, after, rounds = release_load_memory_rounds(self.sense_voice)
            if before is not None and after is not None:
                print(f"[✓] 语音识别加载内存已归还：{before} → {after} MB（空跑 {runs} 次，{rounds} 轮）")
        except Exception as e:
            print(f"[!] 语音识别加载内存归还失败：{type(e).__name__}")

    def _bargein_input(self, mic):
        """说话期间的打断检测输入：开了回声消除就先把团团自己的声音从麦克风里减掉（参考信号来自播放器，只在内存里），
        再交给打断专用的唤醒检测器（阈值 BARGEIN_KWS_THRESHOLD）；出任何问题都退回原来的做法。"""
        if not getattr(canaan_config, "BARGEIN_AEC", False):
            return self.kws_detector, mic
        try:
            if getattr(self, "_aec", None) is None:
                import canaan_aec
                from canaan_wake_word_engine import CanaanWakeWordDetector
                self._aec_mod = canaan_aec
                # 整个运行期间复用，不每句重置（房间回声路径不变）。默认 numpy：WebRTC 会把主人的声音一起压掉
                self._aec = canaan_aec.make_canceller(getattr(canaan_config, "BARGEIN_AEC_ENGINE", "numpy"))
                self._bargein_kws = CanaanWakeWordDetector(
                    keywords_threshold=getattr(canaan_config, "BARGEIN_KWS_THRESHOLD", 0.2))
            # 麦克风这一块大约是 30 毫秒前录下的：取对应时间段正在播放的声音做参考
            ref = self._aec_mod.REF.take(time.perf_counter() - 0.03, len(mic))
            return self._bargein_kws, self._aec.process_stream(mic, ref)
        except Exception as e:
            print(f"[!] 回声消除不可用，打断改用原方式: {e}")
            canaan_config.BARGEIN_AEC = False
            return self.kws_detector, mic

    def _speech_interrupted(self):
        return getattr(self, "interrupt_speech_flag", False)

    def _on_tts_error(self, text, *, event=None):
        """TTS 合成失败：文字已在 HUD 上，提示音响一声，错误已写入 logs/tts_errors.log。"""
        from canaan_voice_capture import VoiceUIEvent
        if event is None:
            local = getattr(self, '_turn_local', None)
            binding = getattr(local, 'binding', None)
            if binding is None and callable(getattr(self, '_capture_context', None)):
                binding = self._capture_context()
            event = VoiceUIEvent("tts_failed", text, binding=binding,
                                 turn_owner=getattr(local, 'turn_owner', None) or getattr(self, '_active_turn_owner', None))
        if not event.current(self):
            return
        print("[!] TTS 合成失败，仅显示文字")
        self.ui_queue.put(event)
        try:
            winsound.Beep(640, 220)
        except Exception:
            pass

    def speak(self, text, emotion="confident", tracer=NULL_TRACER, show=None, *, voice_request=None):
        """逐句合成、逐句播放；ESC / 唤醒词随时打断。show 为要同步显示的回复（第一段声音出来时显示）。"""
        options = {"voice_request": voice_request} if voice_request is not None else {}
        return self._speak_with(lambda **kw: self.engine.speak(text, emotion, **kw), tracer, show=show, **options)

    def create_companion_stream(self, user_text, history, tracer=NULL_TRACER, *, binding=None):
        from canaan_device_turn import turn_binding
        binding = binding or getattr(getattr(self, '_turn_local', None), 'binding', None) or self._capture_context()
        memory = binding.memory
        try:
            with turn_binding(self, binding):
                store = fact_store_of(self)
                facts = store.context_lines(user_text) if hasattr(store, 'context_lines') else store.texts()
        except Exception as error:
            print(f'[!] 记忆检索失败: {error}')
            facts = []
        context = build_context(recent_actions=memory.recent_actions(), profile_facts=facts)
        opener = pick_opener(user_text) if getattr(canaan_config, 'CHAT_OPENER', True) else None
        return self.companion.stream(user_text, history=history, context=context, tracer=tracer, opener=opener), opener

    def finish_companion_stream(self, stream, text, cancelled, tracer=NULL_TRACER):
        tracer.set('chat_model', self.companion.used_model)
        if stream.guard_action:
            tracer.set('persona_guard', stream.guard_action)
        unavailable = (stream.error or not text) and not cancelled
        fallback = bool(unavailable and not text)
        if unavailable:
            print(f'[!] 陪聊生成失败: {stream.error}')
            stream.suggestion = None
            if fallback:
                text = '当前选择的聊天模型暂时不可用，请稍后再试。'
        suggestion = dict(stream.suggestion, emotion='confident', reply='') if stream.suggestion and not cancelled else None
        return text, suggestion, fallback

    def companion_text_response(self, user_text, history, *, binding, tracer=NULL_TRACER):
        from canaan_device_turn import turn_binding, CompanionTextUnavailable
        from canaan_execution_guard import execution_allowed
        from canaan_learning import profile_scope
        with turn_binding(self, binding), profile_scope(binding.profile_key):
            stream, _ = self.create_companion_stream(user_text, history, tracer, binding=binding)
            for _ in stream.iter_text():
                if not self._context_valid(binding) or not execution_allowed():
                    stream.cancel()
                    raise RuntimeError('The companion text turn was cancelled')
            text, suggestion, _ = self.finish_companion_stream(stream, stream.full_text, stream.cancelled, tracer)
            if stream.cancelled or not self._context_valid(binding) or not execution_allowed():
                raise RuntimeError('The companion text turn was cancelled')
            if stream.error or not stream.full_text:
                raise CompanionTextUnavailable(text)
            return text, suggestion

    def speak_companion(self, user_text, history, tracer=NULL_TRACER, fallback_reply="我在呢。", expected_revision=None):
        """陪聊：边生成边说。生成失败时保留已播内容，无输出则明确说明不可用。"""
        from canaan_voice_capture import VoiceUIEvent
        local = getattr(self, '_turn_local', None)
        binding = getattr(local, 'binding', None)
        if binding is None and callable(getattr(self, '_capture_context', None)):
            binding = self._capture_context()
        turn_owner = getattr(local, 'turn_owner', None) or getattr(self, '_active_turn_owner', None)
        stream_session = [None]
        def event(kind, data, *, session_bound=True):
            return VoiceUIEvent(kind, data, binding=binding, turn_owner=turn_owner,
                                session=stream_session[0] if session_bound else None)
        memory = binding.memory if binding else self.memory
        if not event("canaan_reply", None).current(self):
            return ''
        cs, opener = self.create_companion_stream(user_text, history, tracer, binding=binding)
        shown = []

        def _on_segment(seg):
            if (not event("canaan_reply", None).current(self) or self._speech_interrupted()
                    or stream_session[0] and stream_session[0].get('cancelled')):
                return
            shown.append(seg)
            title = EMOTION_CONFIG.get(cs.emotion or "pamper", EMOTION_CONFIG["pamper"])["title"]
            self.ui_queue.put(event("canaan_reply", ("".join(shown), title, "chat", "陪聊")))

        def play_stream(**kw):
            stream_session[0] = getattr(self, '_speech_playback_session', None)
            return self.engine.pipeline.speak_stream(
                cs.iter_text(), lambda: cs.emotion or "pamper", on_segment=_on_segment,
                first_segment=opener[0] if opener else None, on_cancel=cs.cancel, **kw)
        playback = self._speak_with(play_stream, tracer)
        if isinstance(playback, dict) and (playback.get("interrupted") or playback.get("failed")):
            cs.cancel()  # 包括播放管线启动前就被取消的轮次。
        cancelled = self._speech_interrupted() or cs.cancelled
        # 文字会预取到播放队列前面；被打断时不能把尚未播报的段落写进对话记忆。
        text = "".join(shown) if cancelled else cs.full_text
        text, suggestion, fallback = self.finish_companion_stream(cs, text, cancelled, tracer)
        if fallback and not self._speech_interrupted():
            self.ui_queue.put(event("canaan_reply", (text, "从容掌控", "chat", "陪聊"), session_bound=False))
            self.speak(text, "pamper", tracer)
        elif fallback:
            text = ''
        if binding and not self._context_valid(binding):
            return text
        memory.add_turn(user_text, {"action": "chat", "param": "", "reply": text}, expected_revision=expected_revision)
        if remember_clarify(getattr(self, "clarify", None), user_text, {"action": "chat", "reply": text}):
            print("[🧩 接上文] 陪聊追问了缺的信息，记下原话")
        if suggestion and event("suggestion", None, session_bound=False).current(self):
            # 回复里问了“要不要我……”：30 秒内说“好/行/要”就执行这个低风险操作
            sug = suggestion
            self.set_pending(sug, "llm", user_text)
            tracer.set("suggestion", f"{sug['action']}|{sug['param']}")
            self.ui_queue.put(event("suggestion", sug, session_bound=False))
        return text

    def _proactive_command(self, text):
        policy = getattr(self, "proactive_policy", None)
        if policy is None:
            return None
        import canaan_proactive
        from contextlib import nullcontext
        coordinator = getattr(self, 'user_context', None)
        with coordinator.lock if coordinator is not None else nullcontext():
            binding = getattr(getattr(self, '_turn_local', None), 'binding', None)
            if binding is None and callable(getattr(self, '_capture_context', None)):
                binding = self._capture_context()
            if binding is not None:
                if not self._context_valid(binding):
                    return None
                policy = canaan_proactive.default_policy(binding.profile_key,
                    directory=canaan_proactive.policy_directory(binding, context=coordinator))
            try:
                return canaan_proactive.handle_command(text, policy)
            except OSError:
                return dict(action='chat', param='', emotion='confident', _guard='reminder_storage',
                            reply='提醒暂时没有保存成功，请稍后再试。')

    def _start_proactive(self):
        """主动开口：提醒、每日问候、久坐提醒、游戏后问候（规则见 canaan_proactive.py）。"""
        if not getattr(canaan_config, "PROACTIVE_ENABLED", True):
            return
        import canaan_proactive
        binding = self._capture_context()
        self.proactive_policy = canaan_proactive.default_policy('owner',
            directory=canaan_proactive.policy_directory(binding, context=getattr(self, 'user_context', None)))   # 和“设提醒”动作共用一份，不互相覆盖

        def deliver(text, kind, voice_context=None):
            from canaan_voice_capture import BackgroundVoiceRequest, VoiceUIEvent
            request = BackgroundVoiceRequest(self, context=getattr(text, 'voice_context', None) or voice_context)
            if not request.current(self, final=True):
                return False
            show = (text, "今天要记得", "chat", kind) if kind == "recall" else (text, "主动提醒", "chat", kind)
            def notice():
                self.ui_queue.put(VoiceUIEvent("proactive_notice", (text, kind),
                    binding=request.context.binding, turn_owner=request.owner, request=request))
            if kind == "recall" and getattr(getattr(self, "pet", None), "calm", False):
                notice()
                self.ui_queue.put(VoiceUIEvent("canaan_reply", show,
                    binding=request.context.binding, turn_owner=request.owner, request=request))
                return True
            try:
                winsound.MessageBeep(0x40)
            except Exception:
                pass
            time.sleep(0.6)
            if not request.current(self, final=True):
                return False
            if kind == "recall":
                notice()
            # Both callers already run in background schedulers. Returning the
            # actual outcome lets them retry a reminder interrupted by user input.
            try:
                result = self.speak(text, "pamper", show=show, voice_request=request)
                return bool(result is not None and not result.get("interrupted") and not result.get("failed"))
            except Exception as exc:
                print(f"[!] Background voice delivery failed: {type(exc).__name__}")
                return False

        import canaan_daily_recall
        self.recall_state = canaan_daily_recall.RecallState()
        self.proactive = canaan_proactive.PersonalProactiveScheduler(self, deliver,
                                                                    is_game=canaan_gamemode.active)
        self.proactive.start()

        # 启动智能体异步任务完工主动播报监听器 (AgentTaskWatcher)
        try:
            import canaan_agent_watcher
            revision = (lambda: self.profile.privacy_revision) if hasattr(self.profile, "privacy_revision") else None
            def deliver_owner(text, kind):
                from canaan_voice_capture import VoiceCaptureContext
                capture = VoiceCaptureContext(self)
                if capture.binding is not None and capture.binding.profile_key != 'owner':
                    return False
                return deliver(text, kind, voice_context=capture)
            self.agent_watcher = canaan_agent_watcher.AgentTaskWatcher(deliver_owner, remember=self.remember_task, memory_revision=revision)
            self.agent_watcher.start()
            print("[✓] 智能体异步完工监听器 (AgentTaskWatcher) 启动成功")
        except Exception as e:
            print(f"[!] 启动 AgentTaskWatcher 失败: {e}")

    def daily_recall_text(self, *, voice_context=None):
        """拼“今天要记得”：今天的提醒、等你处理的任务、上次之后智能体办好的、像待办的置顶记忆（最多 3 条）。"""
        import canaan_daily_recall
        import canaan_events
        from canaan_voice_capture import VoiceCaptureContext, ContextVoiceText
        capture = voice_context if voice_context is not None else VoiceCaptureContext(self)
        if not capture.current(final=True):
            return ""
        store = capture.binding.store if capture.binding is not None else self.profile
        import canaan_proactive
        profile_key = capture.binding.profile_key if capture.binding is not None else 'owner'
        directory = canaan_proactive.policy_directory(capture.binding, context=getattr(self, 'user_context', None))
        policy = canaan_proactive.default_policy(profile_key, directory=directory) if getattr(self, 'proactive_policy', None) else None
        reminders = list(policy.state.get('reminders', [])) if policy is not None else []
        pending, done = [], []
        runtime = getattr(self.root, "canaan_integration_runtime", None)
        if runtime is not None and profile_key == 'owner':
            try:
                recent = time.time() - 3 * 86400   # 只提最近 3 天的，放了很久没人管的不再天天念
                pending += [canaan_events.action_label(t["action"]) for t in runtime.tasks.list()
                            if t["state"] == "awaiting_confirmation" and t.get("created_at", 0) >= recent]
            except Exception:
                pass
        watcher = getattr(self, "agent_watcher", None)
        recall_state = getattr(self, 'recall_state', None)
        if profile_key != 'owner':
            recall_state = canaan_daily_recall.RecallState(canaan_proactive.personal_state_path(profile_key, recall=True, directory=directory))
        since = recall_state.last_seen() if recall_state is not None else 0
        if watcher is not None and profile_key == 'owner':
            for rec in list(watcher.seen_tasks.values()):
                who = {"muse": "Muse", "dots": "Dots"}.get(rec.get("provider"), "智能体")
                if rec.get("status") == "waiting":
                    pending.append(f"{who}【{rec.get('title') or '任务'}】")
                elif rec.get("status") == "done" and rec.get("time", 0) > since and since:
                    done.append(f"{who}【{rec.get('title') or '任务'}】")
        pinned = []
        if hasattr(store, "active_facts") and not getattr(store, "incognito", False):
            try:
                pinned = [f["text"] for f in store.active_facts() if f.get("pinned")]
            except Exception:
                pass
        items = canaan_daily_recall.build_recall(reminders, pending, done, pinned)
        if not capture.current(final=True):
            return ""
        return ContextVoiceText(canaan_daily_recall.recall_text(items), capture)

    def remember_task(self, text, expected_revision=None, origin=None, store=None):
        """做过的任务写进长期记忆的会话摘要（先脱敏；无痕模式下 add_episode 自己不写）。"""
        store = store or fact_store_of(self)
        if store is None or not hasattr(store, "add_episode"):
            return
        if expected_revision is None and hasattr(store, "privacy_revision"):
            expected_revision = store.privacy_revision
        try:
            from canaan_privacy_shield import PrivacyShield
            text = PrivacyShield.mask_text(text)[0]
        except Exception:
            return
        if hasattr(store, "privacy_revision"):
            store.add_episode(text[:160], expected_revision=expected_revision, origin=origin)
        else:
            store.add_episode(text[:160])

    def _memory_tick_loop(self):
        """每分钟检查一次：会话空闲超过 5 分钟就在后台整理成摘要与事实（游戏模式下不调用大模型）。"""
        while True:
            time.sleep(60)
            try:
                import canaan_gpu_priority
                busy = any(getattr(self, name, False) for name in ("is_recording_command", "is_processing", "is_speaking"))
                if self.memory_mgr is not None and not busy and not canaan_gamemode.active() and not canaan_gpu_priority.demo_mode():
                    self.memory_mgr.tick()
            except Exception as e:
                print(f"[!] 记忆整理失败: {e}")

    def log_conversation(self, tracer, user_text, decision, spoken, expected_revision=None):
        """本地对话记录（logs/conversations.jsonl），用于排查“答非所问/没反应”。可在配置中关闭。"""
        from contextlib import nullcontext
        binding = getattr(getattr(self, '_turn_local', None), 'binding', None)
        if binding and not self._context_valid(binding):
            return
        store = binding.store if binding else self.profile
        if expected_revision is None:
            expected_revision = getattr(store, "privacy_revision", None)
        # 先入缓冲，避免持有数据库锁时等待 MemoryManager 锁；缓冲本身也校验版本。
        # 别人在用时不交给主人的会话整理（否则别人的话会被整理进主人的记忆）
        if getattr(self, "memory_mgr", None) is not None and not canaan_gamemode.active() \
                and (binding.profile_key == 'owner' if binding else getattr(getattr(self, "people", None), "active", lambda: None)() is None):
            self.memory_mgr.on_turn(user_text, spoken if decision.get("action") == "chat" else "",
                                    expected_revision=expected_revision, defer_consolidation=True)
        try:
            lock = store._transaction() if hasattr(store, "_transaction") else nullcontext()
            with lock:
                if getattr(store, "incognito", False):
                    return
                if expected_revision is not None and getattr(store, "privacy_revision", expected_revision) != expected_revision:
                    return
                if not getattr(canaan_config, "CONVERSATION_LOG", True):
                    return
                info = getattr(tracer, "info", {})
                logged_at = time.time()
                rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(logged_at)),
                       "timestamp": logged_at, "source": getattr(tracer, "source", ""),
                       "profile_key": binding.profile_key if binding else 'owner',
                       "user": user_text, "intent_source": info.get("intent_source"), "action": decision.get("action"),
                       "param": decision.get("param"), "spoken": spoken, "model": info.get("chat_model") or info.get("model"),
                       "suggestion": info.get("suggestion")}
                import canaan_crypto   # 原话 / 参数 / 回复加密后再落盘（时间、动作名留明文给保留期清理和排查用）
                rec = canaan_crypto.seal_record(rec, canaan_crypto.default_cipher())
                os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
                conv_path = os.path.join(BASE_DIR, "logs", "conversations.jsonl")
                with file_lock(conv_path):
                    with open(conv_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[!] 对话记录写入失败: {e}")

    def _tts_keep_warm(self):
        """启动后预热一次；之后语音合成空闲超过 TTS_KEEP_WARM_SEC（默认 5 分钟）就再跑一次，避免空闲后第一句变慢。"""
        idle_limit = float(getattr(canaan_config, "TTS_KEEP_WARM_SEC", 300))
        first = True
        while True:
            try:
                idle = time.time() - TuantuanVoiceEngine.last_tts_call
                if (first or idle > idle_limit) and not self.is_speaking and not self.is_processing \
                        and not getattr(self, "is_recording_command", False) \
                        and not canaan_gamemode.active() and getattr(canaan_config, "TTS_MODE", "gpu") == "gpu":
                    ms = self.engine.warm_up()
                    if ms is not None:
                        if first:
                            print(f"[✓] 语音合成 GPU 预热完成：{ms:.0f}ms")
                        first = False
            except Exception as e:
                print(f"[!] 语音保温失败: {e}")
            time.sleep(20 if first else 60)

    def _speak_with(self, run, tracer=NULL_TRACER, show=None, *, voice_request=None):
        """show：要显示在悬浮窗上的回复，等第一段声音开始播放时再显示，文字和声音同步（合成失败也会显示）。"""
        from canaan_voice_capture import VoiceUIEvent, voice_state_lock
        local = getattr(self, '_turn_local', None)
        binding = (voice_request.context.binding if voice_request is not None
                   else getattr(local, 'binding', None))
        if voice_request is None and binding is None and callable(getattr(self, '_capture_context', None)):
            binding = self._capture_context()
        turn_owner = (voice_request.owner if voice_request is not None else
                      getattr(local, 'turn_owner', None) or getattr(self, '_active_turn_owner', None))
        def event(kind, data, session=None):
            return VoiceUIEvent(kind, data, binding=binding, session=session, turn_owner=turn_owner,
                                request=voice_request)
        def owns_turn():
            return ((turn_owner is None or turn_owner is getattr(self, '_active_turn_owner', None))
                    and (voice_request is None or voice_request.current(self)))
        with voice_state_lock(self):
            if voice_request is not None and not voice_request.current(self, final=True):
                return {'interrupted': True}
            if not owns_turn() or binding and not self._context_valid(binding):
                return {'interrupted': True}
            if getattr(self, "turn_cancelled", False):
                # 主人在她想/做的时候按了 ESC：只显示文字，不出声
                self.turn_cancelled = False
                existing = getattr(self, '_speech_playback_session', None)
                pending = getattr(self, 'pending', None)
                item = pending.peek() if pending is not None else None
                # Only a prompt not yet handed to a player can finish here.
                # An older active output must acknowledge release in its own finally.
                if (existing and existing.get('state') == 'preparing' and existing.get('pending') is item
                        and existing.pop('awaiting_speak', False)):
                    existing.update(state='finished', finished_at=time.monotonic(),
                                    awaiting_speak=False, cancelled=True)
                    existing.setdefault('finished_event', threading.Event()).set()
                if show is not None:
                    self.ui_queue.put(event("canaan_reply", show))
                return {"interrupted": True}
            pending = getattr(self, "pending", None)
            item = pending.peek() if pending is not None else None
            session = getattr(self, "_speech_playback_session", None)
            # Taking the one-shot marker is the ownership transfer. Cleanup and
            # playback cannot both claim the same prepared prompt.
            if not (session and session.get("pending") is item and session.get("state") == "preparing"
                    and session.pop("awaiting_speak", False)):
                session = {"token": object(), "pending": item, "state": "preparing"}
                self._speech_playback_session = session
            if voice_request is not None:
                voice_request.session = session
            session.setdefault('finished_event', threading.Event())
            session["awaiting_speak"] = False
            if session.get("cancelled"):
                session.update(state="finished", finished_at=time.monotonic())
                session['finished_event'].set()
                if show is not None:
                    self.ui_queue.put(event("canaan_reply", show, session))
                return {"interrupted": True}
            self.is_speaking = True
            self.interrupt_speech_flag = False
            self.ui_queue.put(event("speaking", None, session))
        interrupted = False
        shown = [show is None]
        res = {'interrupted': False, 'failed': True, 'segments': 0, 'played': 0}

        def live():
            return (event("speaking", None, session).current(self)
                    and not session.get('cancelled') and not self._speech_interrupted())

        def _first_audio():
            with voice_state_lock(self):
                if not live() or session.get("state") != "preparing":
                    return
                session["state"] = "playing"
                tracer.mark("tts_first_audio")
                if not shown[0]:
                    shown[0] = True
                    self.ui_queue.put(event("canaan_reply", show, session))

        def _err(text):
            if not live():
                return
            if not shown[0]:
                shown[0] = True
                self.ui_queue.put(event("canaan_reply", show, session))
            self._on_tts_error(text, event=event("tts_failed", text, session))

        try:
            res = run(
                should_stop=lambda: (bool(session.get('cancelled'))
                                     or session is not getattr(self, '_speech_playback_session', None)
                                     or self._speech_interrupted() or not owns_turn()
                                     or bool(binding and not self._context_valid(binding))),
                on_first_audio=_first_audio,
                on_error=_err,
            )
            tracer.set("tts_segments", res.get("segments"))
            interrupted = bool(res.get("interrupted"))
            if interrupted:
                print("[⚡ 全双工即时打断 Barge-in] 已掐断当前播报")
        except Exception as e:
            print(f"[!] 播放异常: {e}")
        finally:
            with voice_state_lock(self):
                session.update(state="finished", finished_at=time.monotonic())
                session['finished_event'].set()
                if session is not getattr(self, '_speech_playback_session', None):
                    return {**res, 'interrupted': True}
                valid_ui = event("playback_finished", None, session).current(self)
                if not shown[0] and valid_ui:  # 同一使用者打断后仍保留文字。
                    self.ui_queue.put(event("canaan_reply", show, session))
                interrupted = interrupted or not valid_ui or bool(session.get('cancelled')) or self._speech_interrupted()
                self.speech_cooldown_until = time.time() + 0.3
                if self.kws_detector:
                    self.kws_detector.reset()
                self.is_speaking = False
                self.ui_queue.put(event("playback_finished", None, session))
                # 正常播完才进入连续对话；被 ESC 或唤醒词打断时不开启
                if not interrupted:
                    self.arm_followup()
        return {**res, 'interrupted': interrupted}

    def trigger_hotkey_record(self):
        """全局热键手动触发录音（若正在播音则瞬间掐断闭嘴并切入录音）"""
        if self.is_speaking:
            from canaan_voice_capture import interrupt_voice_state
            request = interrupt_voice_state(self, require_speaking=True)
            if request is None:
                return
            try:
                sd.stop()
            except Exception:
                pass
            if not request.current(self):
                return
            if getattr(self, "pet", None):
                self.pet.interrupt(notify=False)
            time.sleep(0.05)
            if not request.current(self):
                return
            if self.start_manual_recording(expected_interruption=request) is not False:
                winsound.Beep(980, 80)
            return
        if self.is_processing:
            return
        if not self.is_recording_command:
            self.start_manual_recording()
            winsound.Beep(980, 80)
        else:
            self.stop_manual_recording()

    def start_manual_recording(self, *, expected_interruption=None):
        from canaan_voice_capture import voice_state_lock
        token = getattr(self, '_context_token', None)
        session = {'frames': [], 'failed': False, 'finished': threading.Event(),
                   'awaiting_submit': True, 'context': token() if callable(token) else None}
        with voice_state_lock(self):
            if expected_interruption is not None and (
                    not expected_interruption.current(self) or self.is_speaking
                    or self.is_recording_command):
                return False
            self.followup_until = 0.0
            self.is_recording_command = True
            self._manual_recording_session = session
            self.audio_frames = session['frames']
        self.status_dot.config(fg="#ef4444")
        self.text_lbl.config(text=f"正在倾听... 请对{ASSISTANT_NAME}说出你的指令", fg="#93c5fd")
        self.action_badge.config(text="[🎙️ 正在聆听指令...]", fg="#38bdf8")

        def _rec():
            def cb(indata, frames, t, status):
                if (not self.is_recording_command or session['failed']
                        or session is not getattr(self, '_manual_recording_session', None)):
                    return
                # The device callback only marks failure; no logging/model work here.
                if status or not len(indata) or not np.isfinite(indata).all():
                    session['failed'] = True
                    session['frames'].clear()
                    return
                session['frames'].append(indata.copy())
            try:
                with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, callback=cb):
                    while (self.is_recording_command and not session['failed']
                           and session is getattr(self, '_manual_recording_session', None)):
                        time.sleep(0.05)
            except Exception as exc:
                session['failed'] = True
                session['frames'].clear()
                print(f"[!] 手动录音中断：{type(exc).__name__}")
            finally:
                session['finished'].set()

        try:
            threading.Thread(target=_rec, daemon=True).start()
        except Exception:
            session['failed'] = True
            session['finished'].set()

    def stop_manual_recording(self):
        session = getattr(self, '_manual_recording_session', None)
        if session is None or not session.pop('awaiting_submit', False):
            return
        self.is_recording_command = False
        self.status_dot.config(fg="#f59e0b")
        self.text_lbl.config(text="正在解析指令并执行...", fg="#fde047")
        self.action_badge.config(text="[⚡ 正在调度系统核心...]", fg="#f59e0b")
        # Usually already closed. A slow driver must not block the UI or lose
        # valid audio; the owned handoff waits off the microphone/UI thread.
        if session['finished'].is_set():
            self._finish_manual_recording(session)
        else:
            try:
                threading.Thread(target=self._finish_manual_recording, args=(session,), daemon=True).start()
            except Exception:
                session['frames'].clear()
                getattr(self, '_pending_voice_audio_gap', lambda: None)()
                self.ui_queue.put(('status', ('ready', '录音交接失败，请重新录音')))

    def _finish_manual_recording(self, session):
        token = getattr(self, '_context_token', None)
        while not session['finished'].wait(.05):
            if (session is not getattr(self, '_manual_recording_session', None)
                    or callable(token) and session['context'] != token()):
                session['frames'].clear()
                return
        if session is not getattr(self, '_manual_recording_session', None):
            session['frames'].clear()
            return
        if callable(token) and session['context'] != token():
            session['frames'].clear()
            self.ui_queue.put(('status', ('ready', '使用者已切换，请重新录音')))
            return
        if session['failed']:
            session['frames'].clear()
            getattr(self, '_pending_voice_audio_gap', lambda: None)()
            self.ui_queue.put(('status', ('ready', '录音中断，请重新说完整指令')))
            return
        if session['frames']:
            audio = np.concatenate(session['frames'], axis=0)
            session['frames'].clear()
            tracer = TurnTracer(source="hotkey")
            tracer.set("endpoint_ms", 0)
            tracer.set("record_ms", int(len(audio) / SAMPLE_RATE * 1000))
            kwargs = {'expected_context_token': session['context']} if callable(token) else {}
            self.process_audio_data(audio, tracer, **kwargs)
        else:
            self.ui_queue.put(('status', ('ready', '没有录到声音，请重新录音')))

    def _speaker_ok(self, what, audio=None):
        """声纹锁定：不是录入的那个人就静默忽略（只写日志）。"""
        gate = getattr(self, "speaker_gate", None)
        if gate is None or not gate.enabled:
            return True
        th = None
        if audio is None:  # 唤醒词片段只有两个字，用更宽松的阈值
            audio = np.concatenate(list(self._wake_ring)) if self._wake_ring else np.zeros(0, np.float32)
            th = getattr(canaan_config, "SPEAKER_LOCK_WAKE_THRESHOLD", 0.35)
        ok, score, ms = gate.check(audio, SAMPLE_RATE, threshold=th)
        if score is not None:
            print(f"[🔒 声纹] {what}：相似度 {score:.2f}（阈值 {th or gate.threshold}）{'放行' if ok else '不是主人，忽略'}，{ms:.0f}ms")
        return ok

    def _context_token(self):
        context = getattr(self, 'user_context', None)
        if context is not None:
            return context.token()
        revision = getattr(getattr(self, 'profile', None), 'privacy_revision', None)
        return ('owner', 0, revision, revision)

    def _capture_context(self):
        context = getattr(self, 'user_context', None)
        if context is not None:
            return context.sync()
        from canaan_user_context import TurnContext
        profile = getattr(self, 'profile', None)
        revision = getattr(profile, 'privacy_revision', None)
        return TurnContext('owner', 0, revision, '', profile, getattr(self, 'memory', None),
                           threading.Event(), owner_privacy_revision=revision)

    def _context_valid(self, binding):
        context = getattr(self, 'user_context', None)
        return context.valid(binding) if context is not None else not binding.cancelled.is_set()

    def context_view(self):
        context = getattr(self, 'user_context', None)
        if context is None:
            return {'available': False, 'error': '会话数据库不可用，请查看启动记录'}
        return {**context.view(), 'available': True,
                'speaker_locked': bool(getattr(getattr(self, 'speaker_gate', None), 'enabled', False))}

    def switch_person(self, name=None):
        context = getattr(self, 'user_context', None)
        if context is None:
            raise RuntimeError('会话数据库不可用，暂不能切换使用者')
        snapshot = context.switch(name)
        self.ui_queue.put(('status', ('ready', f"当前使用者：{snapshot['name']}")))
        return snapshot

    def clear_context(self):
        context = getattr(self, 'user_context', None)
        if context is None:
            raise RuntimeError('会话数据库不可用')
        return context.clear_history()

    def _context_rule(self, text, *, draft=None, binding=None):
        binding = binding or getattr(getattr(self, '_turn_local', None), 'binding', None)
        draft = self.chatapp_draft if draft is None else draft
        context = getattr(self, 'user_context', None)
        if context is None or binding is None or getattr(binding.store, 'incognito', False):
            return None
        if re.fullmatch(r'(?:恢复|继续|接着)(?:刚才|之前|上次|上一条)?(?:那条|的)?(?:聊天软件|消息|任务|草稿)(?:吧)?[。！!？?]?', text):
            part = draft.restore_explicit()
            if not part:
                reply = '没有可以恢复的聊天软件草稿。'
                return dict(action='chat', param='', reply=reply, _guard='context_restore')
            if part.get('contact') and part.get('message'):
                send = dict(action='send_chatapp', param=json.dumps({k: part[k] for k in ('contact', 'message')}, ensure_ascii=False), reply='')
                snapshot = draft.task_snapshot()
                if snapshot:
                    send['_context_task'] = snapshot
                return dict(action='chat', param='', _pending=send, _guard='context_restore',
                            reply=f"上次准备给{part['contact']}发：{part['message']}。说确认发送，取消放弃。")
            return dict(action='chat', param='', _guard='context_restore',
                        reply='要发给谁？' if part.get('message') else f"要给{part['contact']}发什么内容？")
        recall = context.db.recall(binding.profile_key, text)
        if recall:
            return dict(action='chat', param='', reply=recall, _guard='context_recall')
        return None

    @staticmethod
    def clean_text_turn(text):
        user_text = re.sub(r'[\r\n\t]+', ' ', text).strip()
        clean = WAKE_PREFIX_RE.sub('', user_text).strip().strip('。，, !！')
        return clean or user_text.strip('。，, !！')

    def prepare_text_identity(self, text, binding, *, deferred=False):
        from canaan_device_turn import prepare_identity
        return prepare_identity(self, text, binding, globals(), deferred=deferred)

    def prepare_text_turn(self, text, *, binding, **options):
        from canaan_device_turn import prepare_hub_turn
        from canaan_os_executor import cancel_reply
        from canaan_learning import profile_scope
        with profile_scope(binding.profile_key):
            return prepare_hub_turn(self, text, binding=binding, ports=dict(globals(), cancel_reply=cancel_reply), **options)

    def execute_text_turn(self, prepared, **options):
        from canaan_device_turn import execute_hub_turn
        from canaan_learning import profile_scope
        from canaan_user_context import execution_scope
        context = options.get('execution_context') or getattr(self, 'user_context', None)
        with profile_scope(prepared.binding.profile_key), execution_scope(context, prepared.binding):
            return execute_hub_turn(self, prepared, **options)

    def process_audio_data(self, audio_data, tracer=None, text=None, asr_ticket=None, expected_pending=None, expected_context_token=None,
                           expected_clarification=Ellipsis):
        """一句话的完整处理。text 不为空时跳过语音识别（灵动岛上点“确认 / 取消”走这里，和说出来完全同一条路）。"""
        from canaan_voice_capture import voice_state_lock, clear_voice_processing
        tracer = tracer or TurnTracer(source="unknown")
        binding = self._capture_context()
        if text is None and expected_clarification is Ellipsis:
            snapshot = getattr(getattr(self, "clarify", None), "snapshot", None)
            if callable(snapshot):
                expected_clarification = snapshot()
        def clarification_current():
            if expected_clarification is Ellipsis:
                return True
            matches = getattr(getattr(self, "clarify", None), "matches", None)
            return callable(matches) and matches(expected_clarification)
        if expected_context_token is not None and binding.token != expected_context_token:
            if asr_ticket:
                asr_ticket.discard()
            return
        if not clarification_current():
            if asr_ticket:
                asr_ticket.discard()
            return
        if text is None and not self._speaker_ok("指令", np.asarray(audio_data, dtype=np.float32).reshape(-1)):
            if asr_ticket:
                asr_ticket.discard()
            tracer.set("speaker_rejected", True)
            self.ui_queue.put(("status", ("ready", "听到别人的声音，已忽略")))
            return
        turn_owner = object()
        import canaan_events
        with voice_state_lock(self):
            self.is_processing = True
            self._active_turn_owner = turn_owner
            self.turn_cancelled = False
            self.ui_queue.put(("status", ("processing", "正在解析指令...")))
            canaan_events.publish("thinking")

        def _worker():
            nonlocal binding
            handed_over = False
            learning_scope = None
            if not hasattr(self, '_turn_local'):
                self._turn_local = threading.local()
            self._turn_local.binding = binding
            self._turn_local.turn_owner = turn_owner
            def asr_cancelled():
                return (not self._context_valid(binding) or self.turn_cancelled
                        or getattr(self, '_active_turn_owner', None) is not turn_owner)
            try:
                if asr_cancelled() or not clarification_current():
                    if asr_ticket:
                        asr_ticket.discard()
                    return
                # 1. 语音识别：内存数组直接送入 SenseVoice（失败退回 Whisper），不再写临时 wav
                if text is not None:
                    if asr_ticket:
                        asr_ticket.discard()
                    user_text, asr_engine = text, "pending_voice" if expected_pending is not None else "island"
                else:
                    prefetched = None
                    if asr_ticket:
                        try:
                            prefetched = asr_ticket.resolve(audio_data, getattr(self, "sense_voice", None), should_stop=asr_cancelled)
                        except Exception as e:
                            tracer.set("asr_prefetch_error", type(e).__name__)
                        finally:
                            asr_ticket.discard()
                        tracer.set("asr_prefetch_hit", prefetched is not None)
                    if asr_cancelled() or not clarification_current():
                        return
                    user_text, asr_engine = prefetched if prefetched is not None else transcribe(
                        audio_data, sense_voice=getattr(self, "sense_voice", None), whisper=self.asr_model,
                        should_stop=asr_cancelled)
                tracer.mark("asr")
                tracer.set("asr_engine", asr_engine)
                if asr_engine == "cancelled" or asr_cancelled() or not clarification_current():
                    return
                runtime = getattr(self.root, "canaan_integration_runtime", None)
                if runtime and text is None:
                    try:
                        runtime.voices.record_command(audio_data, user_text, asr_engine)
                    except Exception as ex:
                        runtime.messages.mark_health("microphone", error=f"纠错缓存保存失败：{type(ex).__name__}")
                user_text = re.sub(r'[\r\n\t]+', ' ', user_text).strip()
                # Clean leading wake word if present (e.g. "团团帮我打开计算器" -> "帮我打开计算器")
                clean_text = WAKE_PREFIX_RE.sub("", user_text).strip()
                clean_text = clean_text.strip('。，, !！')
                if not clean_text:
                    clean_text = user_text.strip('。，, !！')
                if not self._context_valid(binding):
                    return
                # Identity selection precedes slot filling and confirmation consumption.
                identity_decision, binding, _ = self.prepare_text_identity(clean_text, binding)
                if identity_decision and identity_decision.get('_guard') == 'person_switch':
                    self._turn_local.binding = binding
                    self.turn_cancelled = False
                from canaan_learning import profile_scope
                learning_scope = profile_scope(binding.profile_key)
                learning_scope.__enter__()

                if not clean_text or len(clean_text) < 1:
                    self.ui_queue.put(("status", ("idle", "没听清你说什么呢，再叫我一声吧~")))
                    if not clear_voice_processing(self, turn_owner):
                        return
                    tracer.finish(intent_source="empty")
                    return
                # 识别出的只是碎片（“.”“什”“Yeah”）：不回一整段话；在等主人确认时“嗯/好”照常处理
                if is_noise_utterance(clean_text) and not self.pending.has_pending():
                    print("[~] 忽略语音碎片")
                    self.ui_queue.put(("status", ("idle", f"没听清（只听到“{clean_text[:6]}”），再说一遍吧~")))
                    if not clear_voice_processing(self, turn_owner):
                        return
                    tracer.finish(intent_source="noise")
                    return

                if expected_pending is not None:
                    kind, item = self.pending.classify(clean_text, expected_item=expected_pending)
                    if kind not in ("confirm", "cancel") or item is not expected_pending:
                        return  # 陈旧确认在创建任务/步骤卡之前退出。

                # 1. 任务防抖去重校验 (Debounce & Idempotency Filter)
                dedupe_pending = expected_pending or self.pending.peek()
                dedupe_scope = f'{binding.profile_key}:{id(dedupe_pending) if dedupe_pending else "command"}'
                task_id, is_dup = GLOBAL_TASK_MANAGER.check_and_create_task(clean_text, scope=dedupe_scope)
                if is_dup:
                    self.ui_queue.put(("task_duplicate", clean_text))
                    if not clear_voice_processing(self, turn_owner):
                        return
                    tracer.finish(intent_source="duplicate")
                    return

                turn_failed = [False]
                def finish_cancelled_turn(*, attempted=False):
                    if not asr_cancelled():
                        return False
                    if not turn_failed[0]:
                        turn_failed[0] = True
                        message = ("处理期间会话已变更，执行结果尚未确认。" if attempted else
                                   "这次回答已取消或会话已变更，尚未执行。")
                        GLOBAL_TASK_MANAGER.notify_finish(task_id, False, message)
                        self.ui_queue.put(("task_finished", (task_id, False, message)))
                    return True
                if finish_cancelled_turn():
                    return

                print(f"[>] 收到用户语音指令 [任务号: {task_id}]")
                memory_revision = binding.privacy_revision
                self.ui_queue.put(("user_speech", clean_text))

                with GLOBAL_TASK_MANAGER.os_exec_lock:
                    if finish_cancelled_turn():
                        return
                    last_step = [None]

                    def _step_callback(step: int, total: int, desc: str):
                        last_step[0] = (step, total, desc)  # 失败时用来说清卡在哪一步
                        GLOBAL_TASK_MANAGER.notify_step(task_id, step, total, desc)
                        self.ui_queue.put(("task_step", (task_id, step, total, desc)))

                    _step_callback(1, 4, f"正在进行智能语义意图解析: 【{clean_text[:12]}】...")

                    # 2. 待确认操作：上一轮被权限闸门拦下的操作，本轮说“确认/取消”即可处理
                    ack_state = {"played": False}

                    def _on_action(act, source="llm"):
                        # 动作类型一确定、且属于慢动作，就先播一句“收到”类短应答，执行和生成继续
                        if ack_state["played"] or act not in SLOW_ACTIONS:
                            return
                        if source == "rule" and act == "run_cmd":
                            return  # 规则直出的 run_cmd 都是秒开类命令，不需要应答
                        ack_state["played"] = True
                        self.start_ack(act, tracer)

                    def _llm(text):
                        if finish_cancelled_turn():
                            return {"action": "chat", "param": "", "reply": "", "_guard": "turn_cancelled"}
                        d = self.query_canaan_os(text, tracer=tracer, on_action=_on_action,
                                                 cancellation=finish_cancelled_turn)
                        tracer.set("model", d.get("_model", ""))
                        if (ack_state["played"] and d.get("_model") == "fallback"
                                and d.get("_guard") != "model_unavailable"):
                            d["reply"] = "刚才没太听明白，你再说一遍？"
                        return d

                    from canaan_device_turn import TextTurnAborted
                    def text_binding_changed(updated):
                        nonlocal binding, memory_revision
                        binding = updated
                        memory_revision = binding.privacy_revision
                        self._turn_local.binding = binding
                    try:
                        text_prepared = self.prepare_text_turn(
                            clean_text, binding=binding, identity_decision=identity_decision,
                            expected_pending=expected_pending, expected_clarification=expected_clarification,
                            tracer=tracer, cancellation=finish_cancelled_turn, on_action=_on_action,
                            llm_override=_llm, on_binding_change=text_binding_changed)
                    except TextTurnAborted as aborted:
                        if aborted.kind != 'cancelled':
                            GLOBAL_TASK_MANAGER.notify_finish(task_id, False, str(aborted))
                            self.ui_queue.put(('task_finished', (task_id, False, str(aborted))))
                        return
                    binding = text_prepared.binding
                    self._turn_local.binding = binding
                    memory_revision = binding.privacy_revision
                    clean_text = text_prepared.text
                    turn = text_prepared.turn
                    decision, exec_source = turn["decision"], turn["exec_source"]
                    confirmed, exec_user_text = turn["confirmed"], turn["exec_user_text"]
                    pending_kind = turn.get("pending_kind")
                    if finish_cancelled_turn():
                        return
                    tracer.set("intent_source", turn["intent_source"])
                    if turn["intent_source"] == "confirm":
                        print("[✓] 用户已确认待执行操作")
                    elif turn["intent_source"] == "rule":
                        print(f"[⚡ 秒级极速通道命中] {decision['action']}")
                        _on_action(decision.get("action", "chat"), source="rule")
                    if decision.get("_pending"):
                        print(f"[🛡 非命令句] {decision.get('_guard')}：{decision['_pending'].get('action')} 改为先确认")
                        tracer.set("command_guard", decision.get("_guard"))

                    action = decision.get("action", "chat")
                    if action == "quit_assistant":
                        # 语音让团团退出：说完再主动退出（托盘不会自动重启）
                        if not clear_voice_processing(self, turn_owner):
                            return
                        self.speak(decision.get("reply", "好，我先下线了。"), "pamper", tracer)
                        self.root.after(0, lambda: stop_intentionally(self.root, "语音指令退出"))
                        return
                    param = decision.get("param", "")
                    emotion = decision.get("emotion", "confident")
                    reply = decision.get("reply", "我在呢。")
                    tracer.set("action", action)
                    # 闲聊及原话校验后改回的非命令交给陪聊；固定寒暄、取消、记忆管理仍走原规则。
                    use_companion = (action == "chat" and (exec_source == "llm" or decision.get("_guard_rule") == "noncommand_source") and pending_kind is None
                                     and getattr(canaan_config, "COMPANION_ENABLED", True)
                                     and not canaan_gamemode.active() and not decision.get("_guard"))
                    chat_history = binding.memory.messages() if use_companion else None

                    print(f"[✓] 调度决策就绪: source={exec_source}")
                    try:
                        execution = self.execute_text_turn(text_prepared, on_step=_step_callback,
                                                           cancellation=finish_cancelled_turn)
                    except TextTurnAborted:
                        finish_cancelled_turn()
                        return
                    exec_res = execution["result"]
                    forget_verified = execution.get("forget_verified") is True
                    if execution.get("claim_failed"):
                        GLOBAL_TASK_MANAGER.notify_finish(task_id, False, '确认已变更或执行过，请重新核对。')
                        self.ui_queue.put(('task_finished', (task_id, False, '确认已失效。')))
                        return
                    if action in ('forget_memory', 'forget_person_memory') and (forget_verified or self._context_token()[:2] == binding.token[:2]):
                        binding = self._capture_context()
                        self._turn_local.binding = binding
                        memory_revision = binding.privacy_revision
                        self.turn_cancelled = False
                    if finish_cancelled_turn(attempted=True):
                        return
                    tracer.mark("action")
                    print(f"[✓] 执行结果: success={bool(exec_res.get('success'))}, code={exec_res.get('error_code') or 'OK'}")
                    if exec_res.get("success") and action not in TRIVIAL_ACTIONS and not forget_verified:
                        # 做过的任务进长期记忆（原话 + 结论前 60 字），以后能问“上次让你做的那个”。
                        # 写记忆要算向量（CPU 上几十毫秒），放后台，不拖慢播报结果
                        threading.Thread(target=self.remember_task, daemon=True, name="remember-task",
                                         args=(f"[任务] {exec_user_text[:40]} → {str(exec_res.get('message', ''))[:60]}",),
                                         kwargs={"expected_revision": memory_revision, "origin": f"task:{task_id}",
                                                 "store": binding.store}).start()

                    if exec_res.get("error_code") == "CONFIRM_REQUIRED":
                        # 复合动作只挂起需要确认的那一步，已执行的步骤不会重复执行
                        exec_res["message"] = exec_res.get("message", "") + "（30 秒内说“确认”执行，“取消”放弃）"

                    orig_msg = exec_res.get("message", "")
                    if not exec_res.get("success", True) and exec_res.get("error_code") != "CONFIRM_REQUIRED":
                        # 字幕框写清卡在第几步、什么原因
                        exec_res["message"] = failure_detail(orig_msg, last_step[0])
                    GLOBAL_TASK_MANAGER.notify_finish(task_id, exec_res.get("success", True), exec_res.get("message", ""))
                    self.ui_queue.put(("task_finished", (task_id, exec_res.get("success", True), exec_res.get("message", ""))))
                    self.ui_queue.put(("action_executed", (action, exec_res["message"])))

                    ok = exec_res.get("success", True)
                    if not use_companion and not forget_verified:  # 遗忘终态不能把目标重新种回记忆。
                        binding.memory.add_turn(clean_text, decision, result_note=canaan_turn_pipeline.result_note(exec_res),
                                             expected_revision=memory_revision)

                    # 5. 播报内容：动作的结论只来自执行结果（speech_override）；大模型的 reply 不用来报“完成”
                    # 先干活再说话、最多一句：成功只念第一句；失败说清卡在哪一步；完整结果写在字幕框里
                    final_speech = canaan_turn_pipeline.spoken_result(
                        action, {**decision, "reply": reply}, canaan_turn_pipeline.clone_execution_result(exec_res, message=orig_msg), exec_source,
                        last_step[0], describe=describe_action)

                    emo_title = EMOTION_CONFIG.get(emotion, EMOTION_CONFIG["tease"])["title"]
                    if not use_companion:
                        # 不先显示：等第一段声音开始播放时再显示，文字和声音同步
                        self._reply_payload = (final_speech, emo_title, action, exec_res["message"])

                # 5. 逐句合成播放。先退出“处理中”状态，否则唤醒监听线程会丢弃音频帧，喊“团团”无法打断播报
                self.wait_ack()
                if not self._context_valid(binding):
                    return
                if not clear_voice_processing(self, turn_owner):
                    return
                handed_over = True
                if use_companion:
                    final_speech = self.speak_companion(clean_text, chat_history, tracer, fallback_reply=final_speech,
                                                       expected_revision=memory_revision)
                    recorded_speech = final_speech
                else:
                    payload, self._reply_payload = getattr(self, "_reply_payload", None), None
                    from canaan_query_answers import native_query_speech
                    playback = self.speak(native_query_speech(exec_res, final_speech), emotion, tracer,
                               show=payload or (final_speech, EMOTION_CONFIG.get(emotion, EMOTION_CONFIG["tease"])["title"],
                                                "chat", ""))
                    recorded_speech = ('' if isinstance(playback, dict) and
                                       (playback.get('interrupted') or playback.get('failed')) else final_speech)
                if self._context_valid(binding) and not forget_verified:
                    self.log_conversation(tracer, clean_text, decision, recorded_speech, expected_revision=memory_revision)
                    if getattr(self, 'user_context', None) is not None:
                        self.user_context.record(binding, clean_text, decision, recorded_speech)

            except Exception as e:
                print(f"[!] 处理异常: {e}")
                if getattr(self, '_active_turn_owner', None) is turn_owner and self._context_valid(binding):
                    from canaan_voice_capture import VoiceUIEvent
                    self.ui_queue.put(VoiceUIEvent("status", ("error", f"执行异常: {e}"),
                                                  binding=binding, turn_owner=turn_owner))
            finally:
                if learning_scope is not None:
                    learning_scope.__exit__(None, None, None)
                self._turn_local.binding = None
                self._turn_local.turn_owner = None
                with voice_state_lock(self):
                    if not handed_over and getattr(self, '_active_turn_owner', None) is turn_owner:
                        self.is_processing = False
                        session = getattr(self, '_speech_playback_session', None)
                        pending = getattr(self, 'pending', None)
                        item = pending.peek() if pending is not None else None
                        if (session and session.get('state') == 'preparing' and not getattr(self, 'is_speaking', False)
                                and session.get('pending') is item and session.pop('awaiting_speak', False)):
                            session.update(state='finished', finished_at=time.monotonic(), awaiting_speak=False)
                            if session.get('finished_event'):
                                session['finished_event'].set()
                    if getattr(self, '_active_turn_owner', None) is turn_owner:
                        canaan_events.publish("idle")
                tracer.finish()

        threading.Thread(target=_worker, daemon=True).start()

    def fast_match_intent(self, text):
        """规则快速通道，实现见 canaan_intent.fast_match_intent。"""
        return fast_match_intent(text)

    def query_canaan_os(self, user_text, tracer=NULL_TRACER, on_action=None, cancellation=None, initial_decision=None):
        """Use the selected intent client and complete a partial plan before execution."""
        if canaan_gamemode.active() or not getattr(canaan_config, "LLM_ENABLED", True):
            # 游戏模式 / 4GB 显存档：不调大模型（否则会把模型加载进显存）
            return {"action": "chat", "param": "", "emotion": "confident", "_model": "game_mode",
                    "reply": "打游戏呢，我先只听简单指令，比如大点声、截图、下一首。"}
        options = dict(
            history=(getattr(getattr(self, '_turn_local', None), 'binding', None).memory
                     if getattr(getattr(self, '_turn_local', None), 'binding', None) else self.memory).messages(),
            on_action=on_action,
            tracer=tracer,
        )
        if getattr(self.intent_client, "supports_planning", False):
            options["cancellation"] = cancellation
        if initial_decision is not None and getattr(self.intent_client, "supports_planning", False):
            decision = self.intent_client.complete_partial(user_text, initial_decision, **options)
        else:
            decision = self.intent_client.decide(user_text, **options)
        if decision.get("_model") == "fallback":
            return dict(decision, _guard="model_unavailable",
                        reply="当前选择的指令模型暂时不可用，请稍后再试。")
        return decision

    def prewarm_tts_cache(self):
        """预合成短应答与常用回复（按播放时同样的切句方式逐段缓存），首次启动后长期有效。"""
        try:
            n = self.engine.prewarm(PREWARM_PHRASES)
            # 陪聊开场语按“整段”缓存（与 speak_stream 的 first_segment 一致）
            for text, emo in all_openers():
                n += 1 if self.engine.synthesize_clean(SpeechTextSanitizer.sanitize(text), emo) else 0
            print(f"[✓] 常用语音预合成/校验完成: {n} 段")
        except Exception as e:
            print(f"[!] 常用语音预合成失败: {e}")

    def set_pending(self, decision, source, user_text):
        from contextlib import nullcontext
        from canaan_voice_capture import voice_state_lock
        coordinator = getattr(self, 'user_context', None)
        with coordinator.lock if coordinator is not None else nullcontext():
            context = getattr(self, 'user_context', None)
            binding = getattr(getattr(self, '_turn_local', None), 'binding', None)
            if context is not None:
                binding = binding or context.sync()
                decision = context.prepare_pending(binding, decision, source, user_text)
            self.pending.set(decision, source, user_text)
            if binding is not None:
                self.pending.peek()['_context_token'] = binding.token
            with voice_state_lock(self):
                session = getattr(self, "_speech_playback_session", None)
                ack = getattr(self, "_ack_playback_session", None)
                if (not self.is_speaking and session and session.get("state") in ("idle", "finished")
                        and (not ack or ack.get("state") == "finished")):
                    self._speech_playback_session = {"token": object(), "pending": self.pending.peek(),
                                                     "state": "preparing", "awaiting_speak": True,
                                                     'finished_event': threading.Event()}

    def resolve_pending(self, text, expected_item=None):
        item = self.pending.peek()
        if item and item.get('_context_token') is not None and item['_context_token'] != self._context_token():
            self.pending.resolve('取消', expected_item=item)
            return None, None
        if expected_item is not None:
            resolved = self.pending.resolve(text, expected_item=expected_item)
        else:
            resolved = self.pending.resolve(text)
        if resolved[0] == 'cancel' and getattr(self, 'user_context', None) is not None:
            task = resolved[1]['decision'].get('_context_task')
            if task:
                self.context_store.transition_task(task['profile_key'], task['id'], task['version'], 'cancelled')
        return resolved

    def start_ack(self, action, tracer=NULL_TRACER):
        """慢动作的短应答（“好，这就去办。”/“稍等，我看看。”），后台播放，不阻塞执行。
        只表示收到，不表示完成；合成失败时静默跳过。"""
        phrase = ack_phrase_for(action)
        tracer.set("ack", phrase)
        from canaan_voice_capture import voice_state_lock
        local = getattr(self, '_turn_local', None)
        binding = getattr(local, 'binding', None)
        if binding is None and callable(getattr(self, '_capture_context', None)):
            binding = self._capture_context()
        owner = getattr(local, 'turn_owner', None) or getattr(self, '_active_turn_owner', None)
        def current():
            return (owner is getattr(self, '_active_turn_owner', None)
                    and not getattr(self, 'turn_cancelled', False)
                    and not bool(binding and not self._context_valid(binding)))
        ack_session = {"token": object(), "state": "active", "finished_event": threading.Event()}
        def stopped():
            return (not current() or self._speech_interrupted() or ack_session.get('cancelled', False)
                    or ack_session is not getattr(self, '_ack_playback_session', None))
        def first_audio():
            with voice_state_lock(self):
                if not stopped() and ack_session['state'] == 'active' and not ack_session.get('first_audio'):
                    ack_session['first_audio'] = True
                    tracer.mark("tts_first_audio")
        def _run():
            try:
                if not stopped():
                    self.engine.speak(phrase, "confident", should_stop=stopped, on_first_audio=first_audio)
            except Exception as e:
                print(f"[!] 短应答播放失败: {e}")
            finally:
                with voice_state_lock(self):
                    ack_session.update(state="finished", finished_at=time.monotonic())
                    ack_session["finished_event"].set()

        with voice_state_lock(self):
            if not current():
                return
            previous = getattr(self, "_ack_playback_session", None)
            if previous is not None:
                previous["cancelled"] = True
            self._ack_playback_session = ack_session
        try:
            thread = threading.Thread(target=_run, daemon=True)
            with voice_state_lock(self):
                ack_session["thread"] = thread
                if ack_session is getattr(self, '_ack_playback_session', None):
                    self._ack_thread = thread
            thread.start()
        except Exception as exc:
            with voice_state_lock(self):
                ack_session.update(state="finished", finished_at=time.monotonic())
                ack_session["finished_event"].set()
                if ack_session is getattr(self, '_ack_playback_session', None):
                    self._ack_thread = None
            print(f"[!] 短应答线程未启动：{type(exc).__name__}")

    def wait_ack(self, timeout=.15):
        t = getattr(self, "_ack_thread", None)
        ack = getattr(self, "_ack_playback_session", None)
        if t is not None:
            # A ready answer takes priority over optional "received" output.
            if ack is not None and ack.get("thread") is t:
                ack["cancelled"] = True
            t.join(timeout)
            # A live output keeps its handle; the player retains the device
            # until close. Never erase a replacement registered during join.
            if not t.is_alive() and t is getattr(self, "_ack_thread", None):
                self._ack_thread = None


    def process_ui_queue(self):
        from canaan_voice_capture import VoiceUIEvent
        self._ui_last_tick = time.monotonic()
        while not self.ui_queue.empty():
            message = self.ui_queue.get()
            if isinstance(message, VoiceUIEvent) and not message.current(self):
                continue
            msg_type, data = message
            if getattr(self, "island_bridge", None) is not None:
                try:
                    self.island_bridge.on_ui(msg_type, data)   # 同一条界面消息同时驱动灵动岛
                except Exception:
                    pass

            if msg_type == "island_ready":
                # 新灵动岛起来了：旧的 tkinter 胶囊藏起来（岛进程退出时 _island_tick 会把它放回来）
                self._web_island = True
                self.root.withdraw()
                continue
            if msg_type == "exit_requested":
                stop_intentionally(self.root, "Ctrl+Alt+Q")
                return
            elif msg_type == "important_message":
                runtime = getattr(self.root, "canaan_integration_runtime", None)
                if runtime is None:
                    continue
                try:
                    if not 0 <= time.time() - data["timestamp"] <= 300:
                        runtime.messages.notification_result(data["id"], "failed")
                        continue
                    with runtime.messages.lock:
                        row = runtime.messages.db.execute("SELECT * FROM messages WHERE id=?", (data["id"],)).fetchone()
                        if row is None or runtime.messages._with_importance(dict(row))["importance"]["priority"] != "P0":
                            runtime.messages.notification_result(data["id"], "failed")
                            continue
                    if getattr(self, "_web_island", False) and getattr(self, "notice_center", None) is not None:
                        # 新灵动岛在：重要消息走岛的“来电式”卡片 + 桌宠反应，不再弹旧的小窗；只给来源，不给正文
                        names = {"chatapp": "聊天软件", "imapp": "IMApp", "feishu": "飞书", "claw": "Claw"}
                        self.notice_center.post(names.get(data["source"], data["source"]), "message", important=True)
                        import canaan_notify
                        canaan_notify.post("message", "有重要消息需核对", names.get(data["source"], data["source"]),
                                           key=f"message:{data['id']}")
                        runtime.messages.notification_result(data["id"], "shown")
                        continue
                    popup = tk.Toplevel(self.root)
                    popup.title("团团 · 重要消息")
                    popup.attributes("-topmost", True)
                    popup.configure(bg="#f7f6f1")
                    names = {"chatapp": "聊天软件", "imapp": "IMApp", "feishu": "飞书", "claw": "Claw"}
                    tk.Label(popup, text=f"{names.get(data['source'], data['source'])} · 有重要消息需核对", bg="#f7f6f1", fg="#27392f", padx=24, pady=20).pack()
                    tk.Button(popup, text="打开收件箱", command=lambda p=popup: (self.open_control_center(), p.destroy())).pack(pady=(0, 16))
                    popup.bind("<Escape>", lambda event, p=popup: p.destroy())
                    popup.update_idletasks()
                    popup.geometry(f"+{max(0, popup.winfo_screenwidth() - popup.winfo_reqwidth() - 24)}+{max(0, popup.winfo_screenheight() - popup.winfo_reqheight() - 70)}")
                    popup.after(10000, popup.destroy)
                    import canaan_notify  # 系统通知只写来源，不带消息正文
                    canaan_notify.post("message", "有重要消息需核对", names.get(data["source"], data["source"]), key=f"message:{data['id']}")
                    runtime.messages.notification_result(data["id"], "shown")
                except Exception:
                    runtime.messages.notification_result(data["id"], "failed")
            elif msg_type == "proactive_personal_notice":
                kind, title, body, key = data
                try:
                    import canaan_notify
                    canaan_notify.post(kind, title, body, key=key)
                except Exception as exc:
                    print(f"[!] Personal notification unavailable: {type(exc).__name__}")
            elif msg_type == "proactive_notice":
                notice_text, kind = data
                if kind == "recall":
                    try:
                        import canaan_notify
                        canaan_notify.post("recall", "今天要记得", notice_text.replace("今天要记得：", ""),
                                           key=f"recall:{time.strftime('%Y-%m-%d')}")
                    except Exception as exc:
                        print(f"[!] Recall notification unavailable: {type(exc).__name__}")
            elif msg_type == "wake_word_triggered":
                self.expand_capsule()
                self.status_dot.config(fg="#ef4444")
                self.action_badge.config(text=f"[👑 已唤醒: “{data}” · 正在聆听...]", fg="#f472b6")
                self.text_lbl.config(text="正在倾听你的指令，请吩咐...", fg="#38bdf8")
                if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#f472b6")
                if hasattr(self, 'mini_status'): self.mini_status.config(text="已唤醒 · 倾听中...")
                if getattr(self, "pet", None):
                    self.pet.wake_up()

            elif msg_type == "hotkey_trigger":
                self.expand_capsule()
                if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#38bdf8")
                if hasattr(self, 'mini_status'): self.mini_status.config(text="录音聆听中...")
                self.trigger_hotkey_record()

            elif msg_type == "user_speech":
                self.expand_capsule()
                self.text_lbl.config(text=f"你: “{data}”", fg="#93c5fd")
                if hasattr(self, 'mini_status'): self.mini_status.config(text=f"你: {str(data)[:10]}")

            elif msg_type == "action_executed":
                action, action_msg = data
                self.action_badge.config(text=f"[⚡ {action_msg}]", fg="#4ade80")

            elif msg_type == "canaan_reply":
                self.expand_capsule()
                reply_text, emo_title, action, action_msg = data
                self.emotion_lbl.config(text=f"【{emo_title}】")
                self.text_lbl.config(text=f"{ASSISTANT_NAME}: {reply_text}", fg="#ffffff")
                self.status_dot.config(fg="#a855f7")
                if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#a855f7")
                if hasattr(self, 'mini_status'): self.mini_status.config(text="回复中...")
                if getattr(self, "pet", None):
                    self.pet.say(reply_text, speak=False)

            elif msg_type == "task_duplicate":
                self.action_badge.config(text="[⚠️ 重复指令拦截 · 防抖保护]", fg="#facc15")
                self.step_lbl.config(text=f"⚠️ 指令“{data[:14]}”高频重复触发，已自动防抖拦截", fg="#facc15")

            elif msg_type == "task_step":
                self.expand_capsule()
                task_id, step, total, desc = data
                self.action_badge.config(text=f"[进度 {step}/{total}]", fg="#f59e0b")
                self.step_lbl.config(text=f"⚡ 第 {step}/{total} 步：{desc}", fg="#38bdf8")
                if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#f59e0b")
                if hasattr(self, 'mini_status'): self.mini_status.config(text=f"执行中 {step}/{total}")

            elif msg_type == "service_status":
                muse_on, dots_on = data
                m_fg = "#22c55e" if muse_on else "#64748b"
                d_fg = "#22c55e" if dots_on else "#64748b"
                m_txt = "● Muse" if muse_on else "○ Muse"
                d_txt = "● Dots" if dots_on else "○ Dots"
                if hasattr(self, 'mini_muse_lbl') and self.mini_muse_lbl:
                    self.mini_muse_lbl.config(text=m_txt, fg=m_fg)
                if hasattr(self, 'mini_dots_lbl') and self.mini_dots_lbl:
                    self.mini_dots_lbl.config(text=d_txt, fg=d_fg)
                if hasattr(self, 'btn_muse') and self.btn_muse:
                    self.btn_muse.config(text=f"🌐 Muse: {'在线' if muse_on else '离线'}", fg=m_fg)
                if hasattr(self, 'btn_dots') and self.btn_dots:
                    self.btn_dots.config(text=f"🧠 Dots: {'在线' if dots_on else '离线'}", fg=d_fg)

            elif msg_type == "task_finished":
                task_id, success, message = data
                if success:
                    self.step_lbl.config(text=f"✓ 完成：{message}", fg="#4ade80")
                    if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#22c55e")
                    if hasattr(self, 'mini_status'): self.mini_status.config(text="任务已完成")
                else:
                    self.step_lbl.config(text=f"✗ 没办成：{message}", fg="#ef4444")
                    if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#ef4444")
                    if hasattr(self, 'mini_status'): self.mini_status.config(text="任务受阻")
                self.schedule_autofold(3.5)

            elif msg_type == "speaking":
                self.status_dot.config(fg="#a855f7")
                if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#a855f7")

            elif msg_type == "tts_failed":
                self._tts_failed_at = time.time()
                self.step_lbl.config(text="⚠ 语音合成失败（详见 logs/tts_errors.log），本句仅显示文字", fg="#facc15")

            elif msg_type == "suggestion":
                self.step_lbl.config(text=f"💡 说“好”我就去办：{data['action']} {data['param']}", fg="#fde047")

            elif msg_type == "followup_listening":
                self.expand_capsule()
                self.status_dot.config(fg="#38bdf8")
                self.action_badge.config(text=f"[继续说吧 · {int(data)} 秒内无需唤醒]", fg="#38bdf8")
                if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#38bdf8")
                if hasattr(self, 'mini_status'): self.mini_status.config(text=f"连麦聆听中 ({int(data)}s)")

            elif msg_type == "followup_ended":
                self.status_dot.config(fg="#22c55e")
                self.action_badge.config(text=f"[语音唤醒就绪 · 随时呼叫“{WAKE_WORD}”]", fg="#60a5fa")
                if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#10b981")
                if hasattr(self, 'mini_status'): self.mini_status.config(text="待命 (Alt+Space)")
                self.schedule_autofold(3.5)

            elif msg_type == "playback_finished":
                self.status_dot.config(fg="#22c55e")
                self.action_badge.config(text=f"[语音唤醒就绪 · 随时呼叫“{WAKE_WORD}”]", fg="#60a5fa")
                if hasattr(self, 'mini_dot'): self.mini_dot.config(fg="#10b981")
                if hasattr(self, 'mini_status'): self.mini_status.config(text="待命 (Alt+Space)")
                if time.time() - getattr(self, "_tts_failed_at", 0) > 8:
                    self.step_lbl.config(text="[系统就绪] 待命中", fg="#7dd3fc")
                self.schedule_autofold(3.5)

            elif msg_type == "status":
                state, text = data
                self.text_lbl.config(text=text)
                if state == "ready":
                    self.status_dot.config(fg="#22c55e")
                elif state == "error":
                    self.status_dot.config(fg="#ef4444")
                elif state == "processing":
                    self.status_dot.config(fg="#f59e0b")

        self._island_tick()
        self.root.after(80, self.process_ui_queue)

STOP_FLAG = os.path.join(BASE_DIR, "data", "hub_stopped.flag")      # 用户主动关闭：托盘不再自动重启
PAUSE_FLAG = os.path.join(BASE_DIR, "data", "listen_paused.flag")    # 暂停聆听：麦克风关闭
_pause_cache = {"t": 0.0, "v": False}


def listen_paused():
    now = time.monotonic()
    if now - _pause_cache["t"] > 0.5:
        _pause_cache["v"] = os.path.exists(PAUSE_FLAG)
        _pause_cache["t"] = now
    return _pause_cache["v"]


def stop_intentionally(root=None, reason="用户关闭"):
    """主动退出：先写标志文件（托盘看到后不重启），再结束进程。"""
    try:
        os.makedirs(os.path.dirname(STOP_FLAG), exist_ok=True)
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}")
    except Exception as e:
        print(f"[!] 写停止标志失败: {e}")
    print(f"[■] 团团已退出（{reason}）")
    try:   # 灵动岛的状态文件清空（提示标题、回答都不留在磁盘上）；岛看到主程序退出后自己关
        import canaan_island_state
        canaan_island_state.IslandState().update(mode="hidden", text="", detail="", notices=[], present=None,
                                                 secondary=None, confirm_id=None)
    except Exception:
        pass
    try:
        if root is not None:
            try:
                from clients.desktop_relay.application_pairing import close_runtime_pairing
                close_runtime_pairing(root)
            except Exception:
                pass
            runtime = getattr(root, "canaan_integration_runtime", None)
            if runtime is not None:
                runtime.stop()
            root.destroy()
    finally:
        os._exit(0)


def main():
    import canaan_crashlog  # 崩溃原因（异常类型 + 调用栈，不含原话）写到 logs/hub_crash.log
    canaan_crashlog.install(os.path.join(BASE_DIR, "logs"))
    ensure_single_instance()
    root = tk.Tk()
    root.report_callback_exception = canaan_crashlog.tk_callback_hook
    app = CanaanDeskOSHub(root)
    # 任务栏右键关闭 / Alt+F4：视为主动退出
    root.protocol("WM_DELETE_WINDOW", lambda: stop_intentionally(root, "关闭窗口"))
    root.mainloop()
    # mainloop 正常结束（窗口被关闭）也视为主动退出
    stop_intentionally(None, "窗口关闭")

if __name__ == "__main__":
    main()
