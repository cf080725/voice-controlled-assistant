# -*- coding: utf-8 -*-
"""
================================================================================
   👑 团团 (Tuantuan) · Desk OS 全局统一便携式配置中枢 (Canaan Portable Config)
================================================================================
   - 保证系统在任意盘符、任意路径、任意用户电脑下解压即用
   - 自动发现与探测：
     1. 工作区根目录 (BASE_DIR)
     2. GPT-SoVITS 语音引擎路径
     3. 离线唤醒模型 (KWS Zipformer)
     4. 团团专属模型与母带音频库
================================================================================
"""

import os
import sys
from pathlib import Path

# 当前工程根目录（自适应）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 团团语音资产目录
VOICEPACK_DIR = os.path.join(BASE_DIR, "tuantuan_voicepack")
EMOTIONS_DIR = os.path.join(VOICEPACK_DIR, "emotions")
CACHE_DIR = os.path.join(VOICEPACK_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 离线唤醒模型目录 (3.3M 超轻量 Zipformer)
KWS_DIR = os.path.join(BASE_DIR, "kws_models", "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")

# 助手身份：显示名、唤醒词、人设。换成原创角色时只改这里（并换掉 tuantuan_model/ 下的音色权重）
ASSISTANT_NAME = "团团"
# 界面提示里展示的唤醒词（第一个为主唤醒词）；实际识别由 KWS_KEYWORDS_FILE 决定，
# 新唤醒词需按 sherpa-onnx 格式（拼音 token @显示文本）写进该文件
WAKE_PHRASES = ("团团", "你好团团", "团团在吗", "呼叫团团")
KWS_KEYWORDS_FILE = os.path.join(KWS_DIR, "keywords_tuantuan.txt")
# 唤醒灵敏度：阈值越低越容易叫醒、也越容易被别的话误触发。2026-10-07 本人 57 条录音 + 26 小时公开音频实测：
#   0.38（原值）唤醒率 54%（42~67%），误唤醒 0.35 次/小时；0.20 唤醒率 75%（63~85%），误唤醒 0.46 次/小时
#   同一批录音上 0.20 多叫醒 12 条、少叫醒 0 条。开着电视（远）仍只有 2/6，需要降噪才能改善
KWS_THRESHOLD = 0.28
KWS_SCORE = 2.0
# 播报中打断（“停 / 别说了 / 团团”）：先把团团自己的声音从麦克风里减掉再检测。以前没有这一项，一直没开。
# 10-24 tools/bench_aec.py（模拟回声，本人唤醒词 57 条，阈值 0.2）团团和主人一样响时：
#   不消回声 6/57=11%；numpy 消回声 28/57=49%；WebRTC 8/57=14%（回声压掉 37 dB，主人的声音也一起压掉）。
#   只放团团声音 5.9 分钟，消回声后误触发 0 次；numpy 每 100 毫秒块约 2 毫秒。真实房间未测。
BARGEIN_AEC = os.environ.get("CANAAN_BARGEIN_AEC", "1") != "0"
BARGEIN_AEC_ENGINE = "numpy"
BARGEIN_KWS_THRESHOLD = 0.2
# 语音识别常把名字听成同音词；句首出现这些词（可带“你好”）时当唤醒词去掉
WAKE_ASR_ALIASES = ("团团", "加南", "江南", "家南", "佳南", "冠男")
# 陪聊人设卡：UTF-8 文本文件（相对路径以工程根目录为准），内容整体作为陪聊系统提示词；留空用 canaan_companion.py 内置人设
PERSONA_PROMPT_FILE = "personas/tuantuan.txt"
# 人设守卫（canaan_persona.py）：禁用称呼、错误自称、中英夹杂。第一句出戏则低温重生成一次，仍不行说兜底句
CHAT_PERSONA_GUARD = True
# 亲昵称呼（亲爱的/宝贝）每条回复最多一次；最近这么多条回复里用过，这条就不用（1 → 不会连着两条都叫；
# 评测中实际约四分之一的回复带称呼，见 docs/EVALUATION.md）
PET_NAME_GAP = 1
# 可选覆盖：PERSONA_FORBIDDEN_TERMS = (...)、PERSONA_LATIN_WHITELIST = (...)

# 版本：personal 自用版（本机，默认）/ public 公开版。公开版由 tools/export_public.py 导出时在根目录写 canaan_edition.json，
# 覆盖名字、唤醒词、人设等；自用版没有这个文件，以上设置照旧。
EDITION = "personal"
EDITION_FILE = os.path.join(BASE_DIR, "canaan_edition.json")
_EDITION_KEYS = {"edition": "EDITION", "assistant_name": "ASSISTANT_NAME", "wake_phrases": "WAKE_PHRASES",
                 "wake_asr_aliases": "WAKE_ASR_ALIASES", "persona_prompt_file": "PERSONA_PROMPT_FILE",
                 "kws_keywords_file": "KWS_KEYWORDS_FILE", "pet_name_gap": "PET_NAME_GAP"}


def _apply_edition(path=EDITION_FILE, target=None):
    """读 canaan_edition.json（没有就什么都不做），只接受上面列出的几项；格式不对的项忽略。"""
    import json as _json
    target = globals() if target is None else target
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    for key, name in _EDITION_KEYS.items():
        value = data.get(key)
        if value is None:
            continue
        if key in ("wake_phrases", "wake_asr_aliases"):
            if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
                target[name] = tuple(value)
        elif key == "kws_keywords_file":
            if isinstance(value, str) and value and ".." not in value.replace("\\", "/").split("/"):
                in_repo = os.path.join(BASE_DIR, value)  # 优先用仓库里的（如 personas/keywords_tuantuan.txt）
                target[name] = in_repo if os.path.exists(in_repo) else os.path.join(KWS_DIR, os.path.basename(value))
        elif key == "pet_name_gap":
            if type(value) is int:
                target[name] = value
        elif key == "edition":
            if value in ("personal", "public"):
                target[name] = value
        elif isinstance(value, str) and value:
            target[name] = value
    return True


_apply_edition()

ARCHIVED_MODELS_DIR =os.path.join(BASE_DIR, "archive", "models")


def resolve_model_dir(name):
    """模型/检查点目录：优先根目录，其次 archive/models（历史阶段产物已归档到那里）。
    两处都没有时返回根目录路径（训练脚本把它当输出目录时会在根目录新建）。"""
    for root in (BASE_DIR, ARCHIVED_MODELS_DIR):
        p = os.path.join(root, name)
        if os.path.isdir(p):
            return p
    return os.path.join(BASE_DIR, name)


# 团团大语言模型目录 (Stage 4 独立合并模型)，不存在时退回已归档的 v3
LLM_MODEL_DIR = resolve_model_dir("canaan_commercial_model_v4")
if not os.path.exists(LLM_MODEL_DIR):
    LLM_MODEL_DIR = resolve_model_dir("canaan_commercial_model_v3")

# 自动发现 GPT-SoVITS 部署根目录
def discover_gpt_sovits():
    candidates = [
        os.environ.get("GPT_SOVITS_HOME", ""),
        r"D:\GPT-SoVITS\GPT-SoVITS-v4-20250529-nvidia50",
        r"C:\GPT-SoVITS\GPT-SoVITS-v4-20250529-nvidia50",
        os.path.join(BASE_DIR, "GPT-SoVITS"),
        os.path.join(os.path.dirname(BASE_DIR), "GPT-SoVITS"),
    ]
    for c in candidates:
        if c and os.path.exists(os.path.join(c, "api_v2.py")):
            return c
    return r"D:\GPT-SoVITS\GPT-SoVITS-v4-20250529-nvidia50"

GPT_SOVITS_DIR = discover_gpt_sovits()

# 端点检测（说完话后多久判定为一句结束）
# 优先使用 silero VAD（sherpa_onnx.VoiceActivityDetector），模型缺失时自动退回能量阈值法
VAD_MODEL_PATH = os.path.join(BASE_DIR, "kws_models", "vad", "silero_vad.onnx")
VAD_THRESHOLD = 0.5
ENDPOINT_HANGOVER_SHORT_SEC = 0.8   # 已说内容不足 ENDPOINT_LONG_SPEECH_SEC 时的静音等待（给思考停顿留余量）
ENDPOINT_HANGOVER_LONG_SEC = 0.6    # 已说内容较长时的静音等待
ENDPOINT_LONG_SPEECH_SEC = 1.6
ENDPOINT_NO_SPEECH_TIMEOUT_SEC = 4.0  # 唤醒后一直不开口的最长等待
ENDPOINT_MAX_RECORD_SEC = 25.0
# 对短句在原有端点静音等待中提前识别；只复用最终音频完全相同的结果。
# CANAAN_ASR_PREFETCH=0 可回到完全串行识别；声纹锁开启时仍先校验声纹。
ASR_PREFETCH_ENABLED = os.environ.get("CANAAN_ASR_PREFETCH", "1") == "1"
# SenseVoice 识别语种。"zh" 固定中文：100 句开发录音安静错字率 14.4%→13.7%（配对重采样 95% 区间 -1.56～-0.11 个百分点），
# 音乐/粉红噪声 10dB 降约 1.6 个百分点，不再冒出 "video"/"Google" 之类英文；B站/VS Code/WPS 照常识别。
# 见 reports/asr_noise_frontend_20261023/。CANAAN_ASR_LANGUAGE=auto 回到自动判断语种。
ASR_LANGUAGE = os.environ.get("CANAAN_ASR_LANGUAGE", "zh")
# 只对明显缺少对象/消息正文的片段延长监听，完整句端点不变。
# 平时默认关闭；演示模式未显式设置本环境变量时用 1.5 秒静音上限；显式 0 可关闭。
ENDPOINT_PARTIAL_GRACE_SEC = float(os.environ.get("CANAAN_ENDPOINT_PARTIAL_GRACE", "0"))

# 判断“说完了没有”："fixed" 固定静音等待（上面两个值）；"smart_turn" 用 Smart Turn v3 模型判断（CPU）。
# 本人 84 句带停顿录音：fixed 截断 24%、等待 P50 1.1s；smart_turn 截断 17%、等待 P50 0.8s（都未达截断 ≤3% 的目标，默认不改）
ENDPOINT_MODE = "fixed"
SMART_TURN_VAD_STOP_SEC = 0.3   # 静音这么久时问一次模型
SMART_TURN_THRESHOLD = 0.95     # 模型把握 ≥ 这个值才算说完
SMART_TURN_FALLBACK_SEC = 1.5   # 模型一直觉得没说完时，静音这么久强制结束

# 连续对话：团团播报完后这么多秒内直接说话即可，无需再喊唤醒词；设为 0 关闭
FOLLOWUP_LISTEN_SEC = 5.0

# 陪聊：大模型判定为闲聊后，用单独的人设提示词 + 情境 + 长期记忆生成回复，边生成边说
COMPANION_ENABLED = True
# 默认直接生成正文，避免固定开场语占住正文出声时间；设 True 恢复预合成开场语。
# 本机六句静默实测：正文首音 P50 1.44s → 0.89s（小样本，不含 ASR / 意图解析）。
CHAT_OPENER = False
# 陪聊模型。实测（RTX 5060 8GB，与 GPT-SoVITS 同时运行，tools/bench_chat.py，开场语后正文开始时间 P50/P90）：
#   canaan-chat:3b      1.85s / 2.21s，回复贴题、会主动提出操作，显存合计约 6.4GB（默认）
#   qwen2.5:7b          3.0~5.0s / 4.1~6.6s，回复最自然，显存合计约 7.4~7.6GB
#   canaan-desk:1.5b  1.63s / 2.03s，回复模板化、会照抄上一句
# canaan-chat:3b 由 resources/Modelfile_chat_3b 创建（Qwen2.5-3B-Instruct Q4_K_M + 支持预填续写的 Qwen 模板）：
#   ollama pull modelscope.cn/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M
#   ollama create canaan-chat:3b -f resources/Modelfile_chat_3b
CHAT_MODEL = "canaan-chat:3b"  # 第五轮陪聊判分 v6 3.375 / v3 3.150 / v7 3.075，陪聊留 v6。
# 默认只用选定模型；确需备用时可在这里或 CANAAN_CHAT_FALLBACK_MODEL 显式设置。
CHAT_FALLBACK_MODEL = ""
# 陪聊模型空闲多久后从显存卸载（Ollama keep_alive）。意图模型（约 1.1GB）仍常驻，保证指令响应；
# 陪聊 3B 约 2GB 只在聊天时占用，空闲 10 分钟后让出显存给游戏 / 渲染。想一直常驻可设 "24h"
CHAT_KEEP_ALIVE = "10m"
# 陪聊采样参数（tools/run_persona_eval.py 评测：0.8 时口癖与出戏明显更多）
CHAT_TEMPERATURE = 0.6
# 陪聊回复最多几句（只在句末截断）；用户反馈“太啰嗦”后设为 2。0 = 不限制
CHAT_MAX_SENTENCES = 2
CHAT_REPEAT_PENALTY = 1.1
CHAT_REGEN_TEMPERATURE = 0.3

# 本地对话记录 logs/conversations.jsonl（识别文本与团团的回复，只存本机），用于排查答非所问；False 关闭
CONVERSATION_LOG = True
# 对话原话保留多少天（主人定 14 天）；每次启动清一次，旧审计记录里的参数也一并清掉（canaan_retention）
CONVERSATION_RETENTION_DAYS = 30
# 多人记忆（canaan_people）：别人没置顶的事实多少天没被用到就删（主人定 90 天）；口头切到别人后多久没人说话自动回到主人
PEOPLE_MEMORY_RETENTION_DAYS = 90
PEOPLE_IDLE_RESET_SEC = 1800

# 声纹锁定：只响应录入过声纹的人（旁边其他人说话、喊唤醒词都忽略）。录入：py -3.12 tools/enroll_voice.py
# 默认关闭：要在本人真实录音上测得“本人被拒”≤5% 后再打开；托盘菜单也可临时开关（data/speaker_lock.flag）
SPEAKER_LOCK_ENABLED = False
SPEAKER_LOCK_THRESHOLD = 0.6        # 整句指令：本人录音 79 句被拒 0%（见 docs/EVALUATION.md 第 9 节）
SPEAKER_LOCK_WAKE_THRESHOLD = 0.35  # 唤醒词只有两个字，声纹不稳：0.6 时本人被拒 48%，0.35 时 0%

# 意图识别专用小模型（紧凑输出 {"a","p"}，见 docs/decisions/）；留空则用原来的 canaan-desk 模型链
INTENT_COMPACT_MODEL = "canaan-chat:3b"  # 第五轮 1788 条 v7 更好（动作 0.7724 对 0.7360），但 `canaan_intent._num_ctx()` 要求意图与陪聊同模型才能共用 8192 上下文，分档会掉回 1024 并让 Ollama 来回重载 3B，故仍统一 v6；要上 v7 得先改那条约束。失败时明确不可用，不自动换模型。；失败时明确不可用，不自动换模型。
# 意图模型对所选动作的概率低于此值时不执行，先问一句（说“好”再执行）。只用 dev/dev2/unseen 标定（docs/decisions/004），0 = 关闭
INTENT_MIN_CONFIDENCE = 0.6

# 端口配置
OLLAMA_API_URL = "http://127.0.0.1:11434/api/chat"
TTS_API_URL = "http://127.0.0.1:9880/tts"
DEFAULT_OLLAMA_MODEL = "canaan-desk:7b"

# 动态生成便携式 TTS 推理配置文件
def get_runtime_tts_yaml():
    runtime_yaml = os.path.join(BASE_DIR, "tuantuan_tts_infer_runtime.yaml")
    t2s = os.path.join(BASE_DIR, "tuantuan_model", "tuantuan-e15.ckpt").replace("\\", "/")
    vits = os.path.join(BASE_DIR, "tuantuan_model", "tuantuan_e12_s204.pth").replace("\\", "/")
    bert = os.path.join(GPT_SOVITS_DIR, "GPT_SoVITS", "pretrained_models", "chinese-roberta-wwm-ext-large").replace("\\", "/")
    hubert = os.path.join(GPT_SOVITS_DIR, "GPT_SoVITS", "pretrained_models", "chinese-hubert-base").replace("\\", "/")
    
    yaml_content = f"""custom:
  bert_base_path: {bert}
  cnhuhbert_base_path: {hubert}
  device: cuda
  is_half: true
  t2s_weights_path: {t2s}
  version: v2
  vits_weights_path: {vits}
v2:
  bert_base_path: {bert}
  cnhuhbert_base_path: {hubert}
  device: cuda
  is_half: true
  t2s_weights_path: {t2s}
  version: v2
  vits_weights_path: {vits}
"""
    with open(runtime_yaml, "w", encoding="utf-8") as f:
        f.write(yaml_content)
    return runtime_yaml


# 显存分档（见 canaan_tiers.py）："auto" 启动时按显卡显存自动选择，也可写死 "8gb" / "6gb" / "4gb"
VRAM_TIER = "auto"
LLM_ENABLED = True   # 4gb 档为 False：不调用任何大模型，只走规则指令
TTS_MODE = "gpu"     # 4gb 档为 "cpu"：用 Windows 自带中文语音
# 每次回复的第一段边收边播（GPT-SoVITS 流式返回，参数与整段合成相同、音质不变；实测首段出声 P50 0.77→0.56s）。False = 原整段合成
TTS_STREAM_FIRST = True
# 可审查的参考准备缓存候选；真实输出对照通过前仅在启动进程中显式开启。
TTS_REFERENCE_CACHE = os.environ.get("CANAAN_TTS_REFERENCE_CACHE", "0").strip().lower() in ("1", "true", "on", "yes")
try:
    import canaan_tiers as _tiers
    ACTIVE_TIER, DETECTED_VRAM_MB = _tiers.apply(globals(), VRAM_TIER)
except Exception as _e:  # 检测失败按现配置运行
    ACTIVE_TIER, DETECTED_VRAM_MB = "8gb", 0

# 大模型方式（canaan_bootstrap 首次运行时选，存在 data/llm_providers.json 的 "mode"）：
#   local 本机 Ollama（默认，按上面的显存档位）/ api 云端（没有独显也能对话、识别指令）/ rules 不用大模型
# LOCAL_LLM_ENABLED 管本机模型（预热、记忆整理）；LLM_ENABLED 管“要不要问大模型”，云端也算。
LOCAL_LLM_ENABLED = LLM_ENABLED
LLM_MODE = "local"
try:
    import canaan_llm_provider as _llm
    _llm_cfg = _llm.load_config()
    LLM_MODE = _llm_cfg.get("mode") if _llm_cfg.get("mode") in ("local", "api", "rules") else "local"
    if LLM_MODE == "rules":
        LLM_ENABLED = LOCAL_LLM_ENABLED = COMPANION_ENABLED = False
    else:
        _cloud_chat, _cloud_intent = _llm.role_provider("chat", _llm_cfg), _llm.role_provider("intent", _llm_cfg)
        if LLM_MODE == "api" and (_cloud_chat or _cloud_intent):
            LOCAL_LLM_ENABLED = False   # 选了云端：不去加载本机模型
        if _cloud_chat or _cloud_intent:
            LLM_ENABLED = True
        if _cloud_chat:
            COMPANION_ENABLED = True
except Exception as _e:  # 配置读不了：按显存档位
    pass

# 没装 GPT-SoVITS（公开版、别人的电脑）：直接用 Windows 自带语音。
# 否则托盘会反复去拉一个不存在的语音服务，还要等它失败三次（约 45 秒）才启动主程序。
GPT_SOVITS_AVAILABLE = os.path.exists(os.path.join(GPT_SOVITS_DIR, "api_v2.py"))
if not GPT_SOVITS_AVAILABLE:
    TTS_MODE = "cpu"

# 说话中途停顿被误判为说完时的补救（canaan_turnmerge.py，见 docs/EVALUATION.md 10.4）：默认关闭
TURN_MERGE_ENABLED = False
TURN_MERGE_WINDOW_SEC = 1.0             # 判定说完后再听这么久，又开口就拼接重来
TURN_MERGE_INCOMPLETE_WINDOW_SEC = 2.5  # “发聊天软件说…/告诉某某…”这类内容缺失时等更久

# Explicit model settings take precedence over automatic VRAM tiers.
OLLAMA_API_URL = os.environ.get("CANAAN_OLLAMA_API_URL", OLLAMA_API_URL)
# Windows 上 localhost 先试 IPv6（::1），而 Ollama / Muse / Dots 只听 127.0.0.1：每次请求白等约 2 秒（实测 2070ms 对 1~15ms）
OLLAMA_API_URL = OLLAMA_API_URL.replace("//localhost:", "//127.0.0.1:")
CHAT_MODEL = os.environ.get("CANAAN_CHAT_MODEL", CHAT_MODEL)
CHAT_FALLBACK_MODEL = os.environ.get("CANAAN_CHAT_FALLBACK_MODEL", CHAT_FALLBACK_MODEL)
CHAT_KEEP_ALIVE = os.environ.get("CANAAN_CHAT_KEEP_ALIVE", CHAT_KEEP_ALIVE)
INTENT_COMPACT_MODEL = os.environ.get("CANAAN_INTENT_MODEL", INTENT_COMPACT_MODEL)
# 10-26：消息汇总 / 知识库问答也用陪聊同一个 3B，不再加载 qwen2.5:7b（4.7GB，会把 3B 挤出显存）；回退改回 "qwen2.5:7b"
MESSAGE_SUMMARY_MODEL = os.environ.get("CANAAN_MESSAGE_SUMMARY_MODEL", CHAT_MODEL)
# 用陪聊这个模型的所有请求（陪聊、识别、记忆整理、界面操作、知识库、消息汇总）共用同一个上下文长度：
# 不一样 Ollama 就会为同一个模型反复重载。8192 是因为消息汇总要放约 5000 字聊天记录，4096 放不下
# （10-26 实测 3B 汇总：4096 时 3 次成功 1 次，8192 时 3 次成功 2 次）；3B 的 KV 缓存多占约 150 MB
SHARED_NUM_CTX = int(os.environ.get("CANAAN_SHARED_NUM_CTX", "8192"))
# 后台请求（消息汇总等）用陪聊模型时带上和识别一样的常驻时间：不带的话 Ollama 会把常驻时间改回默认 5 分钟
SHARED_KEEP_ALIVE = os.environ.get("CANAAN_SHARED_KEEP_ALIVE", "24h")
