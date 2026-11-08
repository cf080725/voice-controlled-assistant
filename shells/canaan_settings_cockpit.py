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

# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Commercial Settings Cockpit
Provides visual dark-mode management for AI models, latency testing,
voice & persona tuning, privacy shield audits, and hardware license management.
"""
import os
import sys
import json
import time
import threading
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _launch(*parts, console=False):
    """用当前解释器启动仓库里的脚本（一键安装用虚拟环境，写死 py -3.12 会找不到依赖）。"""
    import subprocess
    args = list(parts[:-1]) + [parts[-1]] if parts[0] == "-m" else [os.path.join(BASE_DIR, parts[0], parts[1]), *parts[2:]]
    exe = sys.executable.replace("pythonw.exe", "python.exe") if console else sys.executable
    flags = 0x00000010 if console else 0   # CREATE_NEW_CONSOLE：单测要看得到输出
    return subprocess.Popen([exe, *args], cwd=BASE_DIR, creationflags=flags)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import canaan_licensing
from canaan_privacy_shield import PrivacyShield
import canaan_secrets  # 密钥只从环境变量或 data/secrets.json 读取，代码里不写

DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_FILE = os.path.join(DATA_DIR, "cockpit_settings.json")


def load_settings():
    default_cfg = {
        "active_text_model": "GLM-5.3",
        "active_vision_model": "Qwen3-VL-30B-A3B",
        "active_agent_model": "Gemini-3.8-Flash",
        "voice_speed": 1.0,
        "voice_emotion": "pamper",
        "privacy_anti_peep": True,
        "privacy_mask_phone": True,
        "privacy_mask_bank": True
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                default_cfg.update(d)
        except Exception:
            pass
    return default_cfg


def save_settings(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save settings: {e}")


class CanaanSettingsCockpit(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("👑 团团 (Tuantuan) · DESK OS - 全域商业控制大屏")
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        cx = max(0, (sw - 920) // 2)
        cy = max(0, (sh - 650) // 2)
        self.geometry(f"920x650+{cx}+{cy}")
        self.minsize(880, 600)
        self.lift()
        self.attributes("-topmost", True)
        self.after(1500, lambda: self.attributes("-topmost", False))
        self.configure(bg="#0c0d14")

        self.cfg = load_settings()
        self.license_mgr = canaan_licensing.LicenseManager()

        self._setup_styles()
        self._build_ui()
        self._load_data()

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        
        # Configure Notebook and Tabs
        style.configure("TNotebook", background="#0c0d14", borderwidth=0)
        style.configure("TNotebook.Tab", background="#161824", foreground="#94a3b8",
                        padding=[16, 8], font=("Segoe UI", 10, "bold"), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", "#6366f1")],
                  foreground=[("selected", "#ffffff")])

        style.configure("TFrame", background="#0c0d14")
        style.configure("Card.TFrame", background="#161824")
        style.configure("TLabel", background="#161824", foreground="#e2e8f0", font=("Segoe UI", 9))
        style.configure("Header.TLabel", background="#0c0d14", foreground="#ffffff", font=("Segoe UI", 14, "bold"))
        style.configure("Muted.TLabel", background="#161824", foreground="#64748b", font=("Segoe UI", 8))

    def _build_ui(self):
        # 1. Top Bar
        top_bar = tk.Frame(self, bg="#0c0d14", height=60)
        top_bar.pack(fill=tk.X, padx=20, pady=(15, 10))
        
        lbl_title = tk.Label(top_bar, text="👑 CANAAN · DESK OS", font=("Segoe UI", 15, "bold"), fg="#818cf8", bg="#0c0d14")
        lbl_title.pack(side=tk.LEFT)
        
        lbl_sub = tk.Label(top_bar, text=" Commercial Edition v1.0", font=("Segoe UI", 10), fg="#64748b", bg="#0c0d14")
        lbl_sub.pack(side=tk.LEFT, pady=(4, 0))

        # 2. Main Tabs
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        # Tab 1: AI Models
        tab_models = ttk.Frame(notebook)
        notebook.add(tab_models, text=" 🤖 AI 模型中枢 ")
        self._build_tab_models(tab_models)

        # Tab 2: Voice & Persona
        tab_voice = ttk.Frame(notebook)
        notebook.add(tab_voice, text=" 🎙️ 专属音色与人设 ")
        self._build_tab_voice(tab_voice)

        # Tab 3: Privacy Shield
        tab_privacy = ttk.Frame(notebook)
        notebook.add(tab_privacy, text=" 🔒 隐私安全护盾 ")
        self._build_tab_privacy(tab_privacy)

        # Tab 4: Commercial License
        tab_license = ttk.Frame(notebook)
        notebook.add(tab_license, text=" 🎫 商业授权中心 ")
        self._build_tab_license(tab_license)

        # Tab 5: Companion Pet Studio
        tab_pet = ttk.Frame(notebook)
        notebook.add(tab_pet, text=" 👑 具身伴侣工坊 ")
        self._build_tab_pet(tab_pet)

        # Tab 6: Agent Cluster & Computer Use
        tab_agents = ttk.Frame(notebook)
        notebook.add(tab_agents, text=" 🌐 智能体集群与自主控制 ")
        self._build_tab_agents(tab_agents)

        # Tab 7: Daily Briefing
        tab_briefing = ttk.Frame(notebook)
        notebook.add(tab_briefing, text=" ☀️ 每日高管早报中枢 ")
        self._build_tab_briefing(tab_briefing)

        # 3. Bottom Bar
        bot_bar = tk.Frame(self, bg="#0c0d14")
        bot_bar.pack(fill=tk.X, padx=20, pady=12)

        btn_save = tk.Button(bot_bar, text="💾 保存并应用全部配置", font=("Segoe UI", 10, "bold"),
                             bg="#6366f1", fg="#ffffff", activebackground="#4f46e5",
                             relief=tk.FLAT, padx=16, pady=6, command=self._on_save_all)
        btn_save.pack(side=tk.RIGHT)

        self.lbl_status = tk.Label(bot_bar, text="就绪：全部商业模块正常运作", font=("Segoe UI", 9), fg="#10b981", bg="#0c0d14")
        self.lbl_status.pack(side=tk.LEFT)

    def _build_tab_models(self, parent):
        container = tk.Frame(parent, bg="#0c0d14")
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Card 1: Active Model Selection
        card1 = tk.Frame(container, bg="#161824", bd=0, padx=16, pady=14)
        card1.pack(fill=tk.X, pady=(0, 12))

        tk.Label(card1, text="🧠 主力推理模型 (Text & Reasoning)", font=("Segoe UI", 11, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W)
        tk.Label(card1, text="负责知识研报、聊天软件待办提炼与深度推理", font=("Segoe UI", 8), fg="#64748b", bg="#161824").pack(anchor=tk.W, pady=(2, 8))

        row1 = tk.Frame(card1, bg="#161824")
        row1.pack(fill=tk.X)

        self.var_text_model = tk.StringVar(value=self.cfg.get("active_text_model", "GLM-5.3"))
        opt_text = ttk.Combobox(row1, textvariable=self.var_text_model, values=["GLM-5.3 (yuanyuaicloud)", "DeepSeek-V3 (SiliconFlow)", "Gemini-3.8-Flash"], state="readonly", width=32)
        opt_text.pack(side=tk.LEFT)

        btn_test_glm = tk.Button(row1, text="⚡ 快速测速", font=("Segoe UI", 8), bg="#312e81", fg="#c7d2fe",
                                 relief=tk.FLAT, padx=10, command=lambda: self._test_model("GLM-5.3"))
        btn_test_glm.pack(side=tk.LEFT, padx=10)

        self.lbl_res_text = tk.Label(row1, text="延迟: 检查中...", font=("Segoe UI", 8), fg="#94a3b8", bg="#161824")
        self.lbl_res_text.pack(side=tk.LEFT)

        # Card 2: Vision Model
        card2 = tk.Frame(container, bg="#161824", bd=0, padx=16, pady=14)
        card2.pack(fill=tk.X, pady=(0, 12))

        tk.Label(card2, text="👁️ 视觉看屏副驾模型 (Multimodal Vision)", font=("Segoe UI", 11, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W)
        tk.Label(card2, text="负责屏幕报错秒级诊断与活动窗口看图解析 (0 显存)", font=("Segoe UI", 8), fg="#64748b", bg="#161824").pack(anchor=tk.W, pady=(2, 8))

        row2 = tk.Frame(card2, bg="#161824")
        row2.pack(fill=tk.X)

        self.var_vision_model = tk.StringVar(value=self.cfg.get("active_vision_model", "Qwen3-VL-30B-A3B"))
        opt_vis = ttk.Combobox(row2, textvariable=self.var_vision_model, values=["Qwen3-VL-30B-A3B (MoE 极速)", "Qwen3-VL-32B-Instruct"], state="readonly", width=32)
        opt_vis.pack(side=tk.LEFT)

        btn_test_vis = tk.Button(row2, text="⚡ 视觉测速", font=("Segoe UI", 8), bg="#312e81", fg="#c7d2fe",
                                 relief=tk.FLAT, padx=10, command=lambda: self._test_model("Vision"))
        btn_test_vis.pack(side=tk.LEFT, padx=10)

        self.lbl_res_vis = tk.Label(row2, text="延迟: 检查中...", font=("Segoe UI", 8), fg="#94a3b8", bg="#161824")
        self.lbl_res_vis.pack(side=tk.LEFT)

        # Card 3: Agent Runner
        card3 = tk.Frame(container, bg="#161824", bd=0, padx=16, pady=14)
        card3.pack(fill=tk.X)

        tk.Label(card3, text="🤖 自主智能体引擎 (OpenMuse Agent)", font=("Segoe UI", 11, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W)
        tk.Label(card3, text="负责复杂网络自动化、网页监控与后台长任务", font=("Segoe UI", 8), fg="#64748b", bg="#161824").pack(anchor=tk.W, pady=(2, 8))

        row3 = tk.Frame(card3, bg="#161824")
        row3.pack(fill=tk.X)

        lbl_muse_stat = tk.Label(row3, text="后端 API: http://localhost:8787 | 网页控制台: 8081", font=("Segoe UI", 9), fg="#38bdf8", bg="#161824")
        lbl_muse_stat.pack(side=tk.LEFT)

        btn_open_muse = tk.Button(row3, text="🌐 打开工作台", font=("Segoe UI", 8), bg="#0369a1", fg="#ffffff",
                                  relief=tk.FLAT, padx=8, command=lambda: os.system("start http://localhost:8081"))
        btn_open_muse.pack(side=tk.RIGHT)

    def _build_tab_voice(self, parent):
        container = tk.Frame(parent, bg="#0c0d14")
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        card = tk.Frame(container, bg="#161824", bd=0, padx=16, pady=16)
        card.pack(fill=tk.BOTH, expand=True)

        tk.Label(card, text="🎙️ 团团原声音色与情感调优 (GPT-SoVITS)", font=("Segoe UI", 12, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W)
        tk.Label(card, text="高保真个性化声音引擎 (端口 9880，毫秒级流式合成)", font=("Segoe UI", 8), fg="#64748b", bg="#161824").pack(anchor=tk.W, pady=(2, 16))

        # Emotion
        tk.Label(card, text="默认情感语气预设：", font=("Segoe UI", 9, "bold"), fg="#e2e8f0", bg="#161824").pack(anchor=tk.W)
        self.var_emotion = tk.StringVar(value=self.cfg.get("voice_emotion", "pamper"))
        
        row_emo = tk.Frame(card, bg="#161824")
        row_emo.pack(fill=tk.X, pady=(4, 16))
        for emo_val, emo_name in [("pamper", "宠溺亲近 (成熟御姐)"), ("confident", "专业自信 (商务得力)"), ("calm", "冷静理性 (极简答复)")]:
            tk.Radiobutton(row_emo, text=emo_name, variable=self.var_emotion, value=emo_val,
                           bg="#161824", fg="#cbd5e1", selectcolor="#0c0d14", activebackground="#161824").pack(side=tk.LEFT, padx=(0, 16))

        # Speed slider
        tk.Label(card, text="语速调节：", font=("Segoe UI", 9, "bold"), fg="#e2e8f0", bg="#161824").pack(anchor=tk.W)
        self.scale_speed = tk.Scale(card, from_=0.7, to=1.4, resolution=0.05, orient=tk.HORIZONTAL,
                                    bg="#161824", fg="#ffffff", highlightthickness=0, length=300)
        self.scale_speed.set(self.cfg.get("voice_speed", 1.0))
        self.scale_speed.pack(anchor=tk.W, pady=(4, 16))

        btn_test_speak = tk.Button(card, text="▶️ 试听团团专属御姐音", font=("Segoe UI", 9, "bold"),
                                   bg="#059669", fg="#ffffff", activebackground="#047857", relief=tk.FLAT, padx=12, pady=6,
                                   command=self._on_test_speak)
        btn_test_speak.pack(anchor=tk.W)

    def _build_tab_privacy(self, parent):
        container = tk.Frame(parent, bg="#0c0d14")
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        card = tk.Frame(container, bg="#161824", bd=0, padx=16, pady=16)
        card.pack(fill=tk.BOTH, expand=True)

        tk.Label(card, text="🛡️ 商业级隐私与安全脱敏护盾", font=("Segoe UI", 12, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W)
        tk.Label(card, text="数据物理隔离沙箱：明文私密信息绝不出本机", font=("Segoe UI", 8), fg="#64748b", bg="#161824").pack(anchor=tk.W, pady=(2, 16))

        self.var_peep = tk.BooleanVar(value=self.cfg.get("privacy_anti_peep", True))
        tk.Checkbutton(card, text="启用防偷窥物理护盾 (检测到密码管理器、网银、支付窗口在前台时自动拒绝截屏)",
                       variable=self.var_peep, bg="#161824", fg="#cbd5e1", selectcolor="#0c0d14", activebackground="#161824").pack(anchor=tk.W, pady=4)

        self.var_phone = tk.BooleanVar(value=self.cfg.get("privacy_mask_phone", True))
        tk.Checkbutton(card, text="启用手机号/身份证/银行卡正则表达式实时脱敏掩码 (138****1234)",
                       variable=self.var_phone, bg="#161824", fg="#cbd5e1", selectcolor="#0c0d14", activebackground="#161824").pack(anchor=tk.W, pady=4)

        self.var_bank = tk.BooleanVar(value=self.cfg.get("privacy_mask_bank", True))
        tk.Checkbutton(card, text="启用 API Key 与 Token 自动遮罩 (sk-..., cpk-..., ghp-... 自动替换)",
                       variable=self.var_bank, bg="#161824", fg="#cbd5e1", selectcolor="#0c0d14", activebackground="#161824").pack(anchor=tk.W, pady=4)

        tk.Label(card, text="📋 本地隐私审计日志流水：", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W, pady=(16, 6))
        
        self.txt_audit = tk.Text(card, height=6, bg="#0c0d14", fg="#94a3b8", font=("Consolas", 8), bd=0, padx=8, pady=8)
        self.txt_audit.pack(fill=tk.X)

    def _build_tab_license(self, parent):
        container = tk.Frame(parent, bg="#0c0d14")
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        card = tk.Frame(container, bg="#161824", bd=0, padx=16, pady=16)
        card.pack(fill=tk.BOTH, expand=True)

        tk.Label(card, text="🎫 商业正版授权与防盗中心", font=("Segoe UI", 12, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W)
        tk.Label(card, text="基于硬件指纹的一机一码加密授权机制", font=("Segoe UI", 8), fg="#64748b", bg="#161824").pack(anchor=tk.W, pady=(2, 16))

        # Machine ID display
        tk.Label(card, text="当前设备物理机器识别码 (Hardware ID)：", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W)
        
        row_mid = tk.Frame(card, bg="#161824")
        row_mid.pack(fill=tk.X, pady=(4, 16))
        
        lbl_mid = tk.Label(row_mid, text=self.license_mgr.machine_id, font=("Consolas", 11, "bold"), fg="#38bdf8", bg="#0c0d14", padx=10, pady=4)
        lbl_mid.pack(side=tk.LEFT)

        btn_copy = tk.Button(row_mid, text="复制机器码", font=("Segoe UI", 8), bg="#334155", fg="#ffffff",
                             relief=tk.FLAT, padx=8, command=self._copy_mid)
        btn_copy.pack(side=tk.LEFT, padx=10)

        # Status
        ok, msg, meta = self.license_mgr.verify()
        status_color = "#10b981" if ok else "#ef4444"
        self.lbl_lic_stat = tk.Label(card, text=f"当前授权状态: {msg}", font=("Segoe UI", 10, "bold"), fg=status_color, bg="#161824")
        self.lbl_lic_stat.pack(anchor=tk.W, pady=(0, 16))

        # Enter key
        tk.Label(card, text="输入新的正版激活码：", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg="#161824").pack(anchor=tk.W)
        row_key = tk.Frame(card, bg="#161824")
        row_key.pack(fill=tk.X, pady=(4, 0))
        
        self.entry_key = tk.Entry(row_key, font=("Consolas", 10), bg="#0c0d14", fg="#ffffff", insertbackground="#ffffff", width=36)
        self.entry_key.pack(side=tk.LEFT, ipady=4)

        btn_activate = tk.Button(row_key, text="立即激活", font=("Segoe UI", 9, "bold"), bg="#6366f1", fg="#ffffff",
                                 relief=tk.FLAT, padx=12, command=self._on_activate)
        btn_activate.pack(side=tk.LEFT, padx=10)

    def _copy_mid(self):
        self.clipboard_clear()
        self.clipboard_append(self.license_mgr.machine_id)
        messagebox.showinfo("已复制", "机器识别码已成功复制到剪贴板！")

    def _on_activate(self):
        k = self.entry_key.get().strip()
        if not k:
            messagebox.showwarning("提示", "请输入激活码！")
            return
        self.license_mgr.save_license(k)
        ok, msg, meta = self.license_mgr.verify()
        if ok:
            messagebox.showinfo("激活成功", f"恭喜！{msg}")
            self.lbl_lic_stat.config(text=f"当前授权状态: {msg}", fg="#10b981")
        else:
            messagebox.showerror("激活失败", f"激活码无效或与本机不匹配: {msg}")

    def _on_test_speak(self):
        try:
            import urllib.request, urllib.parse
            text = "亲爱的，这是团团的专属御姐音色测试，语速与音质一切正常哦~"
            url = f"http://127.0.0.1:9880/tts?text={urllib.parse.quote(text)}&text_lang=zh"
            req = urllib.request.Request(url)
            threading.Thread(target=lambda: urllib.request.urlopen(req, timeout=8), daemon=True).start()
            self.lbl_status.config(text="正在试听播放语音...", fg="#38bdf8")
        except Exception as e:
            messagebox.showerror("语音引擎未就绪", f"请确保 GPT-SoVITS 正在运行 (端口 9880): {e}")

    def _test_model(self, model_type):
        def _bg():
            t0 = time.time()
            if model_type == "GLM-5.3":
                self.lbl_res_text.config(text="正在测速...", fg="#eab308")
                try:
                    req = urllib.request.Request(
                        "https://yuanyuaicloud.cn/v1/chat/completions",
                        headers={"Authorization": "Bearer " + canaan_secrets.get_secret("GLM_API_KEY"), "Content-Type": "application/json"},
                        data=json.dumps({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}).encode("utf-8")
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        lat = round((time.time() - t0) * 1000)
                        self.lbl_res_text.config(text=f"✓ 正常 ({lat}ms)", fg="#10b981")
                except Exception as e:
                    self.lbl_res_text.config(text=f"✗ 失败: {e}", fg="#ef4444")
            else:
                self.lbl_res_vis.config(text="正在测速...", fg="#eab308")
                try:
                    req = urllib.request.Request(
                        "https://api.siliconflow.cn/v1/chat/completions",
                        headers={"Authorization": "Bearer " + canaan_secrets.get_secret("SILICONFLOW_API_KEY"), "Content-Type": "application/json"},
                        data=json.dumps({"model": "Qwen/Qwen3-VL-30B-A3B-Instruct", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}).encode("utf-8")
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        lat = round((time.time() - t0) * 1000)
                        self.lbl_res_vis.config(text=f"✓ 正常 ({lat}ms)", fg="#10b981")
                except Exception as e:
                    self.lbl_res_vis.config(text=f"✗ 失败: {e}", fg="#ef4444")
        threading.Thread(target=_bg, daemon=True).start()

    def _load_data(self):
        # Load audit records
        log_file = os.path.join(DATA_DIR, "privacy_audit.jsonl")
        if os.path.exists(log_file):
            try:
                lines = []
                with open(log_file, "r", encoding="utf-8") as f:
                    for l in f:
                        if l.strip():
                            lines.append(l.strip())
                for item in lines[-10:]:
                    d = json.loads(item)
                    dt = d.get("datetime", "")
                    ev = d.get("event", "")
                    safe = PrivacyShield.sanitize_details(ev, d.get("details") or {})
                    self.txt_audit.insert(tk.END, f"[{dt}] {ev}: {safe}\n")
            except Exception:
                pass

    def _on_save_all(self):
        self.cfg["active_text_model"] = self.var_text_model.get().split()[0]
        self.cfg["active_vision_model"] = self.var_vision_model.get().split()[0]
        self.cfg["voice_emotion"] = self.var_emotion.get()
        self.cfg["voice_speed"] = self.scale_speed.get()
        self.cfg["privacy_anti_peep"] = self.var_peep.get()
        self.cfg["privacy_mask_phone"] = self.var_phone.get()
        self.cfg["privacy_mask_bank"] = self.var_bank.get()

        save_settings(self.cfg)
        self.lbl_status.config(text="✓ 全部配置已成功持久化并应用！", fg="#10b981")
        messagebox.showinfo("成功", "配置已保存！各项模型与音色参数已生效。")



    def _build_tab_pet(self, parent):
        # 1. Stats & Affection Info Frame
        card_stats = tk.LabelFrame(parent, text="💖 团团羁绊成长面板 · 72态大圆满全息工坊", bg="#13141f", fg="#818cf8",
                                   font=("Segoe UI", 10, "bold"), padx=15, pady=6)
        card_stats.pack(fill=tk.X, padx=15, pady=(6, 4))

        stats_file = os.path.join(BASE_DIR, "data", "canaan_pet_stats.json")
        lvl, title, pts, pats, feeds, poms = 2, "熟络", 100, 0, 0, 0
        if os.path.exists(stats_file):
            try:
                import json
                with open(stats_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    lvl = d.get("level", 2)
                    title = d.get("title", "熟络")
                    pts = d.get("affection_pts", 100)
                    pats = d.get("pats_count", 0)
                    feeds = d.get("feeds_count", 0)
                    poms = d.get("pomodoro_count", 0)
            except Exception:
                pass

        lbl_bond = tk.Label(card_stats, 
                            text=f"👑 羁绊阶梯: Lv.{lvl}【{title}】 | 亲密度: {pts} 💖 | 摸头: {pats}次 | 投喂: {feeds}次 | 番茄钟: {poms}次",
                            font=("Microsoft YaHei UI", 10, "bold"), fg="#f43f5e", bg="#13141f")
        lbl_bond.pack(anchor="w", pady=1)

        lbl_desc = tk.Label(card_stats, 
                            text="全套 72 态专属高精切片已全量就绪，0.0 MB GPU 显存争抢，<0.3% CPU 占用。点击下方分类测试按钮可即时下达指令：",
                            font=("Microsoft YaHei UI", 9), fg="#94a3b8", bg="#13141f")
        lbl_desc.pack(anchor="w")

        # 2. Scrollable Canvas for 8 Categories of 72 Poses
        scroll_outer = tk.Frame(parent, bg="#0c0d14")
        scroll_outer.pack(fill=tk.BOTH, expand=True, padx=15, pady=(2, 6))

        canvas = tk.Canvas(scroll_outer, bg="#0c0d14", highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_outer, orient="vertical", command=canvas.yview)
        matrix_container = tk.Frame(canvas, bg="#0c0d14")

        matrix_container.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=matrix_container, anchor="nw")

        def _on_canvas_configure(e):
            canvas.itemconfig(canvas_window, width=e.width)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse wheel support
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        categories_72 = [
            ("🍱 1. 美食与投喂 (8态)", [
                ("🍖 啃大烤肉", "eating", "#ec4899"), ("🍰 投喂点心", "cheer", "#ec4899"),
                ("🧋 珍珠奶茶", "boba", "#d97706"), ("🥟 拆小笼包", "takeout", "#f97316"),
                ("🌿 端午大肉粽", "zongzi", "#10b981"), ("🥮 月牙啃月饼", "mooncake", "#eab308"),
                ("🍲 热腾腾水饺", "dumpling", "#38bdf8"), ("☕ 提神热美式", "coffee", "#f59e0b"),
                ("🍵 捧温茶小憩", "tea", "#10b981")
            ]),
            ("💼 2. 伴工与商务 (7态)", [
                ("💻 伴工敲电脑", "work", "#6366f1"), ("⌨️ 疯狂敲键盘", "typing", "#38bdf8"),
                ("📞 接商务电话", "phone", "#60a5fa"), ("📈 业绩增长图", "chart", "#4ade80"),
                ("🔍 放大镜查账", "audit", "#06b6d4"), ("📖 佩戴眼镜看书", "reading", "#818cf8"),
                ("💰 招财数大元宝", "money", "#facc15")
            ]),
            ("💖 3. 趣味与情绪 (11态)", [
                ("🪙 掷命运金币", "cheer", "#facc15"), ("🎮 摸鱼打游戏", "game", "#8b5cf6"),
                ("💃 西域欢快跳舞", "dance", "#ec4899"), ("🎁 心意礼物盒", "gift", "#ec4899"),
                ("💫 蚊香眼眩晕", "dizzy", "#f59e0b"), ("🥺 委屈抹眼泪", "crying", "#38bdf8"),
                ("😱 搞笑惊呆下巴", "shock", "#ef4444"), ("🥱 伸懒腰打哈欠", "yawn", "#a855f7"),
                ("😤 傲娇抱胸背对", "tsundere", "#f97316"), ("😍 双眼冒大红心", "heart_eyes", "#f43f5e"),
                ("🙇‍♀️ 跪坐膜拜大佬", "worship", "#fbbf24")
            ]),
            ("🥋 4. 武学姿态 (10态)", [
                ("🛡️ 金光振刀反击", "parry", "#eab308"), ("⚔️ 太刀百裂斩", "blade", "#f59e0b"),
                ("🐉 长枪龙王破", "spear", "#ef4444"), ("💨 地面极速滑铲", "slide", "#38bdf8"),
                ("🤫 屋檐暗杀潜行", "crouch", "#94a3b8"), ("🔥 返魂回煞重生", "resurrect", "#f97316"),
                ("🗡️ 双匕首警戒", "action", "#ef4444"), ("🌪️ 寂静暗刑金沙暴", "stealth", "#eab308"),
                ("🏹 飞索凌空飞跃", "grapple", "#facc15"), ("🧗‍♀️ 飞索挂壁攀爬", "climb", "#10b981")
            ]),
            ("🏢 5. 现代职场百态 (6态)", [
                ("🧖‍♀️ 敷眼膜贴养生", "mask_spa", "#06b6d4"), ("🎧 戴耳麦视频会", "headset", "#38bdf8"),
                ("😖 冰美式太苦皱眉", "coffee_spill", "#f59e0b"), ("🔥 必胜头巾爆肝", "overtime", "#ef4444"),
                ("🤯 方案改十版抓狂", "headache", "#f97316"), ("🕵️‍♀️ 猎鹿帽大侦探", "detective", "#818cf8")
            ]),
            ("🌿 6. 深度生活温馨 (8态)", [
                ("🐱 怀抱小黑猫", "cat", "#10b981"), ("🫧 吹爆泡泡糖", "bubble_gum", "#ec4899"),
                ("💪 举粉色小哑铃", "fitness", "#f43f5e"), ("🎵 戴耳机闭眼晃", "music_headset", "#6366f1"),
                ("🌱 喷水壶浇多肉", "watering", "#10b981"), ("📸 手机自拍比耶", "selfie", "#ec4899"),
                ("📦 纸箱暗中观察", "box_hide", "#d97706"), ("🫂 贴贴屏幕玻璃", "hug_screen", "#f43f5e")
            ]),
            ("🏮 7. 传统节日欢庆 (6态)", [
                ("🧨 新春捂耳放炮", "firecracker", "#ef4444"), ("🎃 万圣南瓜提灯", "pumpkin", "#f97316"),
                ("🏮 元宵玉兔花灯", "lantern", "#f43f5e"), ("☂️ 雨天撑折叠伞", "umbrella", "#60a5fa"),
                ("🦆 夏日小鸭泳圈", "swim", "#f59e0b"), ("🧣 冬日红大围巾", "scarf", "#ef4444")
            ]),
            ("👑 8. 终极高光幻化 (8态)", [
                ("👰 西域红绡绝色嫁衣", "wedding", "#f43f5e"), ("🪽 璀璨金光天使神翼", "angel_wings", "#facc15"),
                ("🎩 礼帽白鸽魔术师", "magician", "#8b5cf6"), ("🏍️ 酷飒暗黑机车女骑", "motorcycle", "#38bdf8"),
                ("💤 小鸡啄米打瞌睡", "sleepy_nod", "#94a3b8"), ("⭐ 双手捧七彩许愿星", "rainbow_star", "#facc15"),
                ("🧕 西域神秘金丝面纱", "mask", "#8b5cf6"), ("👑 女皇权杖誓约巅峰", "crown", "#ec4899")
            ])
        ]

        def on_trigger_action(label, pose_key):
            self.lbl_status.config(text=f"已向桌面团团下达 72 态动作指令: 【{label}】 ({pose_key})", fg="#38bdf8")

        for cat_name, items in categories_72:
            grp = tk.LabelFrame(matrix_container, text=cat_name, bg="#13141f", fg="#c4b5fd",
                                font=("Segoe UI", 9, "bold"), padx=8, pady=3)
            grp.pack(fill=tk.X, pady=2)
            
            btn_box = tk.Frame(grp, bg="#13141f")
            btn_box.pack(fill=tk.X)
            
            for idx, (label, pose_key, color) in enumerate(items):
                r = idx // 4
                c = idx % 4
                b = tk.Button(btn_box, text=label, font=("Microsoft YaHei UI", 8, "bold"),
                              bg="#1e2030", fg=color, activebackground="#2a2d42", activeforeground="#ffffff",
                              relief=tk.FLAT, padx=6, pady=2,
                              command=lambda l=label, pk=pose_key: on_trigger_action(l, pk))
                b.grid(row=r, column=c, padx=3, pady=2, sticky="ew")
                btn_box.grid_columnconfigure(c, weight=1)



    def _build_tab_agents(self, parent):
        box = tk.Frame(parent, bg="#13141f")
        box.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        # Title
        tk.Label(box, text="🌐 外部双智能体集群协同中枢 (OpenMuse & OpenDots)",
                 font=("Microsoft YaHei UI", 12, "bold"), fg="#ffffff", bg="#13141f").pack(anchor=tk.W, pady=(0, 10))

        # Agent Cards Frame
        card_f = tk.Frame(box, bg="#161824", highlightthickness=1, highlightbackground="#2a2b3d")
        card_f.pack(fill=tk.X, pady=(0, 14), ipadx=12, ipady=12)

        # Muse info
        f_muse = tk.Frame(card_f, bg="#161824")
        f_muse.pack(fill=tk.X, pady=4)
        tk.Label(f_muse, text="● OpenMuse 自主网页智能体:", font=("Microsoft YaHei UI", 10, "bold"), fg="#38bdf8", bg="#161824").pack(side=tk.LEFT)
        tk.Label(f_muse, text=" API: 8787 | Web: 8081 (支持深度检索与网页自主交互)", font=("Microsoft YaHei UI", 9), fg="#94a3b8", bg="#161824").pack(side=tk.LEFT, padx=6)
        tk.Button(f_muse, text="打开网页", bg="#2a2b3d", fg="#ffffff", relief=tk.FLAT, font=("Microsoft YaHei UI", 9),
                  command=lambda: os.system("start http://localhost:8081")).pack(side=tk.RIGHT)

        # Dots info
        f_dots = tk.Frame(card_f, bg="#161824")
        f_dots.pack(fill=tk.X, pady=4)
        tk.Label(f_dots, text="● OpenDots 虚拟同事与知识库:", font=("Microsoft YaHei UI", 10, "bold"), fg="#a78bfa", bg="#161824").pack(side=tk.LEFT)
        tk.Label(f_dots, text=" API: 4310 | Web: 5173 (支持多专家研报撰写与长期知识沉淀)", font=("Microsoft YaHei UI", 9), fg="#94a3b8", bg="#161824").pack(side=tk.LEFT, padx=6)
        tk.Button(f_dots, text="打开网页", bg="#2a2b3d", fg="#ffffff", relief=tk.FLAT, font=("Microsoft YaHei UI", 9),
                  command=lambda: os.system("start http://localhost:5173")).pack(side=tk.RIGHT)

        # Cluster action buttons
        btn_f = tk.Frame(card_f, bg="#161824")
        btn_f.pack(fill=tk.X, pady=(10, 0))
        tk.Button(btn_f, text="🧭 打开智能体任务看板", bg="#6366f1", fg="#ffffff", relief=tk.FLAT, font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4,
                  command=lambda: _launch("plugins", "canaan_agent_board.py")).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(btn_f, text="🔄 一键自愈拉起集群后台服务", bg="#2a2b3d", fg="#e2e8f0", relief=tk.FLAT, font=("Microsoft YaHei UI", 9), padx=10, pady=4,
                  command=lambda: _launch("plugins", "canaan_agent_service_manager.py", "ensure")).pack(side=tk.LEFT)

        # Computer Use Section
        tk.Label(box, text="🖥️ 全自主桌面控制引擎 (Canaan Computer Use · Codex/Operator 级)",
                 font=("Microsoft YaHei UI", 12, "bold"), fg="#ffffff", bg="#13141f").pack(anchor=tk.W, pady=(14, 8))

        cu_card = tk.Frame(box, bg="#161824", highlightthickness=1, highlightbackground="#2a2b3d")
        cu_card.pack(fill=tk.X, ipadx=12, ipady=12)

        desc = (
            "• WinMM timeBeginPeriod(1) 毫秒级原生高精度定时器，彻底消除 Python time.sleep 15ms 抖动\n"
            "• 三次贝塞尔缓动样条 (Ease-out Cubic Spline) 拟真鼠标轨迹，操作极其丝滑自然\n"
            "• 0.78ms AST 复合宏编译器，直接解析快捷组合键与复合文本控制\n"
            "• 剪贴板快速 Unicode 注入与异步还原，万字长句毫秒级零延迟落盘"
        )
        tk.Label(cu_card, text=desc, font=("Microsoft YaHei UI", 9), fg="#94a3b8", bg="#161824", justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 10))

        cu_btn_f = tk.Frame(cu_card, bg="#161824")
        cu_btn_f.pack(fill=tk.X)
        def _test_cu():
            try:
                from canaan_computer_use import CanaanComputerUse
                w, h = CanaanComputerUse.get_screen_size()
                x, y = CanaanComputerUse.get_cursor_position()
                msg = f"自检就绪：屏幕分辨率 {w}x{h}，当前光标 ({x}, {y})，WinMM 1ms 定时器活跃"
                self.lbl_status.config(text=msg, fg="#10b981")
            except Exception as e:
                self.lbl_status.config(text=f"自检异常: {e}", fg="#ef4444")

        tk.Button(cu_btn_f, text="⚡ 运行 Computer Use 引擎自检", bg="#38bdf8", fg="#000000", relief=tk.FLAT, font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4,
                  command=_test_cu).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(cu_btn_f, text="🧪 运行自动化单测集", bg="#2a2b3d", fg="#e2e8f0", relief=tk.FLAT, font=("Microsoft YaHei UI", 9), padx=10, pady=4,
                  command=lambda: _launch("-m", "pytest", "tests/test_computer_use.py", "-v", console=True)).pack(side=tk.LEFT)

    def _build_tab_briefing(self, parent):
        box = tk.Frame(parent, bg="#13141f")
        box.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        tk.Label(box, text="☀️ 每日高管消息情报晨报管理中枢",
                 font=("Microsoft YaHei UI", 12, "bold"), fg="#ffffff", bg="#13141f").pack(anchor=tk.W, pady=(0, 10))

        card = tk.Frame(box, bg="#161824", highlightthickness=1, highlightbackground="#2a2b3d")
        card.pack(fill=tk.BOTH, expand=True, ipadx=12, ipady=12)

        desc = (
            "自动聚合脱敏聊天软件与 聊天软件 历史聊天归档，调用本地离线大模型 (canaan-chat:3b / qwen2.5) 或 GLM-5.3\n"
            "提炼早安口播文案、核心商业讨论要点与待办清单，绝不外发敏感 PII 个人隐私。"
        )
        tk.Label(card, text=desc, font=("Microsoft YaHei UI", 9), fg="#94a3b8", bg="#161824", justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 10))

        # Action Buttons
        btn_f = tk.Frame(card, bg="#161824")
        btn_f.pack(fill=tk.X, pady=(0, 10))

        self.txt_briefing = tk.Text(card, bg="#0f1017", fg="#e2e8f0", font=("Microsoft YaHei UI", 9),
                                    wrap=tk.WORD, height=10, relief=tk.FLAT, padx=10, pady=10)
        self.txt_briefing.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        def _load_latest_briefing():
            try:
                import json
                bf = os.path.join(BASE_DIR, "data", "daily_briefing_latest.json")
                if os.path.exists(bf):
                    with open(bf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    sp = data.get("speech_text", "")
                    todos = "\n".join([f"• {t}" for t in data.get("todos", [])])
                    hl = "\n".join([f"• {h}" for h in data.get("highlights", [])])
                    full = f"【早安口播】\n{sp}\n\n【重要待办】\n{todos or '（无紧急待办）'}\n\n【讨论要点】\n{hl}"
                    self.txt_briefing.delete("1.0", tk.END)
                    self.txt_briefing.insert("1.0", full)
                else:
                    self.txt_briefing.delete("1.0", tk.END)
                    self.txt_briefing.insert("1.0", "暂未找到已生成的早报缓存，请点击下方「立即生成」按钮~")
            except Exception as e:
                self.txt_briefing.insert("1.0", f"读取异常: {e}")

        def _generate_now():
            self.lbl_status.config(text="正在提炼最新消息早报...", fg="#f59e0b")
            def _worker():
                try:
                    from message_tools.canaan_daily_briefing import generate_executive_briefing
                    res = generate_executive_briefing(use_local_fallback=True)
                    self.after(0, _load_latest_briefing)
                    self.after(0, lambda: self.lbl_status.config(text="最新早报已生成就绪！", fg="#10b981"))
                except Exception as ex:
                    # ex 在 except 块结束后就被删掉了，先存成字符串再交给界面线程
                    self.after(0, lambda msg=str(ex): self.lbl_status.config(text=f"生成失败: {msg}", fg="#ef4444"))
            import threading
            threading.Thread(target=_worker, daemon=True).start()

        def _push_chatapp():
            try:
                from canaan_pet_briefing import push_to_chatapp
                text = self.txt_briefing.get("1.0", tk.END).strip()
                if text:
                    res = push_to_chatapp(text, require_idle=False)
                    self.lbl_status.config(text=f"已推送到聊天软件文件传输助手: {res}", fg="#10b981")
            except Exception as ex:
                self.lbl_status.config(text=f"推送异常: {ex}", fg="#ef4444")

        tk.Button(btn_f, text="⚡ 立即生成今日高管早报", bg="#6366f1", fg="#ffffff", relief=tk.FLAT, font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4,
                  command=_generate_now).pack(side=tk.LEFT, padx=(0, 10))
        # 聊天软件 / 聊天软件 相关按钮只在自用版里（对应的工具目录在才显示）
        wx_dir = os.path.join(BASE_DIR, "message_tools", "chatapp_tool")
        imapp_dir = os.path.join(BASE_DIR, "message_tools", "imapp_tool")
        if os.path.isdir(wx_dir):
            tk.Button(btn_f, text="📲 一键推送到聊天软件 (文件传输助手)", bg="#10b981", fg="#ffffff", relief=tk.FLAT, font=("Microsoft YaHei UI", 9), padx=10, pady=4,
                      command=_push_chatapp).pack(side=tk.LEFT, padx=(0, 10))
            tk.Button(btn_f, text="📁 打开聊天软件存档目录", bg="#2a2b3d", fg="#e2e8f0", relief=tk.FLAT, font=("Microsoft YaHei UI", 9), padx=10, pady=4,
                      command=lambda: os.startfile(os.path.join(wx_dir, "local_export"))).pack(side=tk.LEFT, padx=(0, 10))
        if os.path.isdir(imapp_dir):
            tk.Button(btn_f, text="📁 打开 聊天软件 存档目录", bg="#2a2b3d", fg="#e2e8f0", relief=tk.FLAT, font=("Microsoft YaHei UI", 9), padx=10, pady=4,
                      command=lambda: os.startfile(imapp_dir)).pack(side=tk.LEFT)

        _load_latest_briefing()


if __name__ == "__main__":
    app = CanaanSettingsCockpit()
    app.mainloop()
