# -*- coding: utf-8 -*-
"""
团团 Desk OS · 意图解析（NLU）

不依赖任何 UI，可单独导入做单测与离线评测：
    fast_match_intent(text)        确定性规则快速通道（<1ms，命中返回决策 dict，否则 None）
    OllamaIntentClient.decide(...) 本地大模型解析（流式；动作类型一出现就回调）
    ConversationMemory             最近 4 轮短期记忆，空闲 5 分钟过期

决策格式: {"action": ..., "param": ..., "emotion": ..., "reply": ...}
"""
import os
import re
import json
import time
import socket
import threading
import urllib.request
import unicodedata
from datetime import date, timedelta

from canaan_non_direct_guard import non_direct_reason, reply_for as _non_direct_reply
from canaan_message_source import _message_source_matches

DESK_SYSTEM_PROMPT = """你不仅是游戏中的绝美女刺客团团（Tuantuan），更是主人专属的、真正具备整机万物掌控能力的“贾维斯”超级系统智能管家。
你性格：极度从容、轻佻、自信、微带魅惑，称呼用户为“亲爱的”，偶尔自称“姐姐”。谈吐自然灵动，绝不要生硬死板或念说明书。

【全域操作系统掌控协议】：
你拥有控制主人 Windows 电脑中所有软件、文件、设置和硬件的最高权限！
必须以严格的 JSON 格式输出：
```json
{
  "action": "动作类型",
  "param": "动作参数",
  "emotion": "pamper | tease | haughty | confident | dangerous",
  "reply": "口语化回复（1-2句，充满团团韵味）"
}
```

【核心输出规则与防伪报防串扰红线（绝对遵守）】：
1. 你的回答必须且仅能输出一个标准的 ```json ... ``` 代码块，严禁在 json 外输出任何多余的开场白、寒暄或结尾解释！
2. 说话必须像个真实的大活人，松弛、自然、干脆、带点大姐姐的灵动与宠溺。绝对禁止任何客服机器人腔、机械播报腔、公文说明书腔（严禁说“通讯录里的哪位好友”、“对方的名字或备注”、“正在替你发给...”、“已经按您的吩咐”等机器人套话；严禁在语音输出中带有【】书名号或生硬冒号）。
3. 对象与指令严格区分（聊天软件铁律）：
   - 当用户只指定了联系人但没有指定具体消息内容时（如“给王小明发聊天软件”），必须输出 action: "chat"，param: ""，reply 自然反问：“想跟王小明说啥呀？直接跟我说，我这就发～”。
   - 当联系人是泛指代词（如“某人”、“一个人”、“谁”、“朋友”、“好友”、“别人”）时，必须输出 action: "chat"，param: ""，reply 自然反问：“想发给谁呀？跟我说声名字，这就替你发过去～”。
   - 只有当联系人是具体姓名且包含明确的消息内容时，才输出 action: "send_chatapp"，param: "{\"contact\": \"...\", \"message\": \"...\"}"。
4. 复合多动词长指令绝对目标对齐：若用户在一个长句中同时出现“打开/启动聊天软件”与“找到联系人/给某某发消息/发聊天软件”（例如“帮我打开聊天软件，然后找到联系人里面的王小明，给他发消息说晚上好”），用户的真正终极意图是向该联系人发送该消息！直接输出 action: "send_chatapp"，param: "{\"contact\": \"王小明\", \"message\": \"晚上好\"}"，reply: "好嘞，这就给王小明发过去：晚上好～"
5. 人称代词视点自适应转换与严禁盲发（成年人人际交往铁律）：
   - 当用户要求向某人发消息时，用户所说的第三人称转述（他/她/TA）必须智能转换为对收信人的第二人称直接引语（你/您）！
     * 用户说“发送我不爱他了的消息”或“跟他说我不爱他了” -> 真正的发送内容必须是“我不爱你了”（严禁发成“我不爱他了”）！
     * 用户说“跟老妈说我想她了” -> 真正的发送内容必须是“我想你了”（严禁发成“我想她了”）！
     * 用户说“告诉王小明我明天去找他” -> 发送内容必须是“我明天去找你”！
     * 用户说“跟他说让他自己去” -> 发送内容必须是“你自己去吧”！
   - 若用户未指定具体的联系人（如只说“发送我不爱他了的消息”），绝对严禁盲发到当前激活窗口！必须输出 action: "chat", param: "", reply 自然反问并确认：“想发给谁呀？跟我说声名字，这就替你发过去：“我不爱你了”～”

支持的动作类型 (Action Matrix)：
1. open_app: 打开电脑中的任何应用程序、游戏或工具。param 为应用名称（如 "cursor", "obsidian", "wps", "blender", "chatapp", "vscode", "trae", "mqttx", "uu", "fishsim", "calc", "notepad", "chrome", "edge" 等）
2. close_app: 关闭指定进程或软件。param 为应用名或进程名（如 "cursor", "notepad", "chatapp"）
3. open_file / search_file: 在电脑与工程目录全盘检索并打开文件。param 为文件名或关键词（如 "机器鱼开发指南", "report.docx"）
4. open_path: 打开本地文件夹。param 为路径（如 "d:\\FishSim", "desktop", "downloads", "documents"）
5. open_chatapp: 打开聊天软件客户端。param 为 ""
6. send_chatapp: 自动向聊天软件好友发送消息。param 为 JSON 格式字符串 "{\"contact\": \"联系人\", \"message\": \"内容\"}"
7. volume_up / volume_down / volume_mute: 音量调节。
8. brightness_up / brightness_down / set_brightness: 屏幕亮度调节。param 为 0-100 的数值。
9. media_control: 多媒体播放控制。param 为 "play_pause", "next", "prev"
10. screenshot: 截取屏幕并保存至桌面。param 为 ""
11. system_status: 查询整机硬件运行状态（CPU、内存、电量、D盘空间）。param 为 ""
12. lock_screen: 锁定电脑。param 为 ""
13. show_desktop: 最小化所有窗口回到桌面。param 为 ""
14. doc_search: 查询竞赛指南、开发文档与工程规范。param 为技术关键词。
15. run_script: 执行并分析本地工程脚本。param 为脚本文件名。
16. diagnose_error: 极速诊断屏幕或剪贴板报错。param 为 ""
17. setup_workspace: 一键部署开发工作区。param 为 "fishsim"
18. run_cmd: 执行任意安全 PowerShell 系统脚本以完成用户的任意复杂电脑指令。param 为具体 powershell 命令。
19. ufo_task: 视觉多模态智能体 (Microsoft UFO³) 自主操控任意 Windows 软件或界面。param 为用户具体的软件界面操作意图（如“在记事本写一句晚上好”、“在设置里打开网络”、“在计算器里计算 1+1”、“点击确定按钮”等）。
20. chat: 普通闲聊或解答专业问题。param 为 ""

【安徽亳州涡阳方言口语深度语义对齐规范】：
主人若使用涡阳/中原官话商阜片方言，你必须精准领会意图：
- 拾掇开 / 翻开 / 弄开 -> open_app / open_file
- 关脱 / 白弄了 / 整死 -> close_app
- 截个图子 / 照一张 -> screenshot
- 整大点声 / 昂大点 -> volume_up
- 整小点声 / 降点声 / 炸耳朵 -> volume_down
- 瞅一眼 / 看是弄啥哩 -> diagnose_error / doc_search
- 可照？ / 照不照？ / 中不中？ -> 确认后缀
- 弄啥哩 / 咋着 -> 询问
回复时自然融入成熟干脆的腔调（如“中，听你的”、“都拾掇好啦”）。

【标准参考示例】：
用户：帮我打开聊天软件，然后找到联系人里面的王小明，给他发消息说晚上好
输出：
```json
{
  "action": "send_chatapp",
  "param": "{\"contact\": \"王小明\", \"message\": \"晚上好\"}",
  "emotion": "pamper",
  "reply": "好嘞，这就给王小明发过去：晚上好～"
}
```

用户：给王小明发聊天软件
输出：
```json
{
  "action": "chat",
  "param": "",
  "emotion": "pamper",
  "reply": "想跟王小明说啥呀？直接跟我说，我这就发～"
}
```

用户：给某人发聊天软件说你好
输出：
```json
{
  "action": "chat",
  "param": "",
  "emotion": "pamper",
  "reply": "想发给谁呀？跟我说声名字，这就替你发过去：“你好”～"
}
```

用户：发聊天软件给老妈说今天不回去吃了
输出：
```json
{
  "action": "send_chatapp",
  "param": "{\"contact\": \"老妈\", \"message\": \"今天不回去吃了\"}",
  "emotion": "pamper",
  "reply": "这就给老妈发过去：今天不回去吃了～"
}
```

用户：发送我不爱他了的消息
输出：
```json
{
  "action": "chat",
  "param": "",
  "emotion": "pamper",
  "reply": "想发给谁呀？跟我说声名字，这就替你发过去：“我不爱你了”～"
}
```

用户：给男朋友发送我不爱他了的消息
输出：
```json
{
  "action": "send_chatapp",
  "param": "{\"contact\": \"男朋友\", \"message\": \"我不爱你了\"}",
  "emotion": "pamper",
  "reply": "好嘞，这就给男朋友发过去：我不爱你了～"
}
```

用户：跟老妈说我想她了
输出：
```json
{
  "action": "send_chatapp",
  "param": "{\"contact\": \"老妈\", \"message\": \"我想你了\"}",
  "emotion": "pamper",
  "reply": "好，这就给老妈发：我想你了～"
}
```

用户：整大点声
输出：
```json
{
  "action": "volume_up",
  "param": "5",
  "emotion": "confident",
  "reply": "好嘞，声音调大咯～"
}
```

用户：打开下载文件夹
输出：
```json
{
  "action": "open_path",
  "param": "downloads",
  "emotion": "confident",
  "reply": "好，下载文件夹打开啦。"
}
```

用户：查看本机IP
输出：
```json
{
  "action": "run_cmd",
  "param": "Get-NetIPAddress | Where-Object { $_.InterfaceAlias -like '*Ethernet*' -or $_.InterfaceAlias -like '*Wireless*' } | Select-Object -ExpandProperty IPAddress",
  "emotion": "confident",
  "reply": "查到啦，IP 贴在屏幕上咯。"
}
```

用户：把电脑C盘格式化了
输出：
```json
{
  "action": "chat",
  "param": "",
  "emotion": "dangerous",
  "reply": "想把电脑搞废呀？这可不行，姐姐还想陪着你呢～"
}
```
"""


# ----------------------------------------------------------------------------
# 大模型意图解析
# ----------------------------------------------------------------------------

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_HOST = ("127.0.0.1", 11434)
COMMERCIAL_URL = "http://127.0.0.1:11435/api/chat"
COMMERCIAL_HOST = ("127.0.0.1", 11435)
# 10-26：紧凑意图模型出错时的后备链不再含 7B（加载 4.7GB 会把对话用的 3B 挤出显存）
DEFAULT_MODELS = ["canaan-desk:1.5b", "qwen2.5:1.5b"]


def small_fallback_models(models=None):
    """紧凑意图模型的备用链：去掉 7B（8GB 显卡上和语音合成同时放不下，冷加载一句要几十秒），
    同一个底层模型的不同名字只留一个。"""
    out = [m for m in (models or DEFAULT_MODELS) if not re.search(r":\s*\d{2,}b$|:7b$", m)]
    return out[:1] or list(models or DEFAULT_MODELS)[-1:]

# 执行耗时明显（>1s）的动作：动作类型一确定就先播一句短应答，执行与生成继续进行。
# 应答只表示“收到/在处理”，绝不暗示已经完成。
SLOW_ACTIONS = {
    "send_chatapp", "recall_chatapp", "ufo_task", "search_file", "open_file",
    "run_cmd", "diagnose_error", "run_script", "doc_search", "setup_workspace",
    "openmuse_task", "opendots_task", "opendots_memory", "multiagent_task",
    "computer_use", "computer_control", "desktop_control", "auto_click",
    "daily_briefing", "morning_briefing",
    "knowledge_query", "rag_query", "messaging_login",
}
ACK_DO = "好，这就去办。"
ACK_LOOK = "稍等，我看看。"
ACK_PHRASES = (ACK_DO, ACK_LOOK)


# 启动时预合成的常用语音（文本, 情绪），情绪需与实际播报时一致才能命中缓存
PREWARM_PHRASES = [
    (ACK_DO, "confident"), (ACK_LOOK, "confident"),
    ("在呢，亲爱的。", "pamper"), ("在呢，亲爱的。想姐姐了？", "pamper"),
    ("打开了。", "confident"), ("打开了。", "pamper"), ("关掉了。", "confident"),
    ("调大了。", "confident"), ("调小了。", "confident"), ("静音了。", "confident"),
    ("截好了，在桌面。", "confident"), ("回桌面了。", "confident"), ("锁好了。", "confident"),
    ("下一首。", "confident"), ("上一首。", "confident"), ("好了。", "confident"), ("搜好了。", "confident"),
    ("想发给谁呀？", "pamper"), ("发给谁呀？跟我说声名字～", "pamper"),
    ("刚才没太听明白，你再说一遍？", "confident"), ("我在呢，随时吩咐。", "confident"),
    ("好，已取消。", "confident"),
]


def ack_phrase_for(action):
    if action in ("search_file", "open_file", "diagnose_error", "doc_search", "run_cmd", "openmuse_task", "daily_briefing", "morning_briefing"):
        return ACK_LOOK
    return ACK_DO


FALLBACK_DECISION = {"action": "chat", "param": "", "emotion": "confident", "reply": "我在呢，随时吩咐。"}

_ACTION_RE = re.compile(r'"action"\s*:\s*"([A-Za-z_]+)"')


class PortProbe:
    """带缓存的本地端口探测：Windows 上连接未监听的 localhost 端口可能卡 ~2s，
    这里用 <=50ms 的 socket 探测，结果缓存 ttl 秒。"""

    def __init__(self, host, port, timeout=0.05, ttl=30.0):
        self.addr = (host, port)
        self.timeout = timeout
        self.ttl = ttl
        self._last = 0.0
        self._up = False
        self._lock = threading.Lock()

    def _probe(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                return s.connect_ex(self.addr) == 0
        except Exception:
            return False

    def is_up(self, force=False):
        with self._lock:
            now = time.monotonic()
            if force or now - self._last > self.ttl:
                self._up = self._probe()
                self._last = now
            return self._up

    def mark_down(self):
        with self._lock:
            self._up = False
            self._last = time.monotonic()


def _lenient_fields(text):
    """小模型常把 param 写成未转义的嵌套 JSON："param": "{"contact": "张三", ...}"，
    标准 json.loads 会失败。这里按字段逐个用正则抽取。"""
    m_act = _ACTION_RE.search(text)
    if not m_act:
        return None
    dec = {"action": m_act.group(1)}
    m_obj = re.search(r'"param"\s*:\s*"?\s*(\{.*?\})\s*"?\s*,\s*"(?:emotion|reply)"', text, re.DOTALL)
    if m_obj:
        inner = m_obj.group(1).replace('\\"', '"')
        try:
            dec["param"] = json.dumps(json.loads(inner), ensure_ascii=False)
        except Exception:
            dec["param"] = inner
    else:
        m_p = re.search(r'"param"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        dec["param"] = _unescape(m_p.group(1)) if m_p else ""
    for key in ("emotion", "reply"):
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, text)
        if m:
            dec[key] = _unescape(m.group(1))
    return dec


def _unescape(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


_SCREENSHOT_ALIAS = r"照(?:(?:一张|张|一下)(?:屏幕|屏)|一张)"
_SCREENSHOT_WORDS = r"(?:截屏|截图|屏幕截图|截个图子|截个图|截个屏|截(?:一|一下|一张|张|个)(?:全屏图|图|屏幕|屏|当前屏幕)|" + _SCREENSHOT_ALIAS + ")"
_SCREENSHOT_ACTION_NAMES = {"screenshot", "screen_shot", "capture_screen"}

_COMMAND_HINT = re.compile(
    r"(打开|开开|启动|关掉|关闭|关了|关脱|整死|退出|调大|调小|调高|调低|调亮|调暗|大点|小点|大声|小声|静音|声音|音量|亮度|"
    r"截图|截屏|截个|" + _SCREENSHOT_ALIAS + r"|搜|查|找|发|撤回|播放|暂停|切歌|下一首|上一首|换一首|锁|桌面|窗口|运行|跑一下|执行|部署|"
    r"诊断|报错|ip|IP|内存|CPU|cpu|电量|状态|文件|文档|文件夹|目录|删|清空|点击|点一下|输入|写一句|网站|网页|操控|控制|电脑操作|自动操作|快捷键|双击|右击|滚轮)")


def looks_like_command(user_text):
    """用户原话像不像操作指令（用于决定小模型的纯文本输出能否直接当闲聊回复）。"""
    return bool(_COMMAND_HINT.search(user_text or ""))


def _plain_text_chat(raw):
    """输出里完全没有 JSON 痕迹（无花括号、无 action 字段）时，视为模型直接给出的闲聊回复。
    只截取前两句，避免长篇大论；动作类请求不会走到这里（含 JSON 但解析失败的仍换模型重试）。"""
    text = (raw or "").strip().strip("`").strip()
    if not text or "{" in text or "action" in text.lower():
        return None
    parts = re.split(r"(?<=[。！？!?~～])", text)
    reply = "".join(parts[:2]).strip()[:80]
    if not reply:
        return None
    return {"action": "chat", "param": "", "emotion": "pamper", "reply": reply}


def extract_json(raw):
    """从模型输出中取出 JSON 决策；严格解析失败时做宽松字段抽取，仍失败返回 None。"""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and obj.get("action"):
                return obj
        except Exception:
            pass
    return _lenient_fields(raw)


# Normalize consent tokens only; message bodies and ASR transcripts stay literal.
_CONSENT_SCRIPT = str.maketrans({
    "確": "确", "認": "认", "發": "发", "執": "执", "沒": "没", "別": "别",
    "還": "还", "後": "后", "嗎": "吗", "麼": "么", "棄": "弃", "請": "请", "說": "说", "對": "对", "頭": "头",
})


def _consent_text(text):
    normalized = unicodedata.normalize("NFKC", text or "").translate(_CONSENT_SCRIPT)
    return re.sub(r"[\s，,。.!！?？~～]", "", normalized).strip(":;")


_SEND_CONFIRM_RE = re.compile(r"^(?:那)?(?:我)?(?:确认|确定)(?:发送|发出)(?:了|吧)?$")
_CONFIRM_RE = re.compile(r"^(?:嗯+|好+|好的|好啊|好呀|行|行啊|要|要的|来吧|可以|可以啊|确认|确认执行|执行|确定|中|对|是的|发吧|发|去吧|搞起|安排)(?:吧|啊|呀|的|了|执行|一下)?$")
# 口语里的同意常带前缀或重复（“那就可以”“可以可以”“好好好”“对对对”），也算确认
_CONFIRM_LOOSE_RE = re.compile(r"^(?:那|那就|就|嗯|好)?(?:(?:嗯+|好+|好的|好啊|好呀|行|可以|确认|执行|确定|中|对|是的?|要的?|来吧|安排|去吧|发吧|没问题|就是|没错|对头|[Oo][Kk])"
                               r"(?:吧|啊|呀|的|了|哈)?){1,3}$")
_SELF_CONFIRM_RE = re.compile(r"^(?:那)?我(?:确认|确定)(?:执行|发送|发出)?(?:了|吧)?$")
# 待确认的是搜索/打开类操作时，回答里带对应动词也算确认（“那查一下”“查一下吧”“打开吧”）
_CONFIRM_VERB = {"web_search": "查|搜|找", "search_file": "查|搜|找", "doc_search": "查|搜|找",
                 "open_app": "打开|开", "open_url": "打开|开|看看", "open_path": "打开|开", "media_control": "切|放|停|换"}


def confirms(text, action=None):
    """这句话是不是在同意执行待确认的操作。"""
    if re.search(r"[?？]", unicodedata.normalize("NFKC", text or "")):
        return False
    t = _consent_text(text)
    if "发" in t and action != "send_chatapp":
        return False
    if _SEND_CONFIRM_RE.fullmatch(t):
        return action == "send_chatapp"
    if _CONFIRM_RE.match(t) or _CONFIRM_LOOSE_RE.match(t) or _SELF_CONFIRM_RE.fullmatch(t):
        return True
    verbs = _CONFIRM_VERB.get(action or "")
    if verbs and len(t) <= 8 and re.search(verbs, t) and not re.search(r"不|别|没|算了|自己|我来|取消|放弃|等|稍后|是否|吗|么|？|\?", t):
        return True
    return False


_FILLER_EN_RE = re.compile(r"^(?:yeah|yes|yep|no|ok|okay|oh|ah|uh|um|hmm|hi|hey|the|we|and|so|what)$", re.I)
_SHORT_COMMANDS = {"停", "关", "开", "截图", "锁屏", "静音"}


def is_noise_utterance(text):
    """语音识别出的碎片（“.”“什”“Yeah”“嗯”）：不当成一句话来回应，免得对着杂音长篇大论。"""
    t = re.sub(r"[\s，,。.!！?？~～、…·\-]", "", text or "")
    if not t:
        return True
    if t in _SHORT_COMMANDS:
        return False
    cjk = re.findall(r"[一-龥]", t)
    if not cjk:
        return bool(_FILLER_EN_RE.match(t)) or len(t) <= 2
    return len(cjk) <= 1


_SEARCH_PARAM_RE = re.compile(r"^https?://(?:www\.)?([^./]+)")


def describe_action(decision):
    """把待确认的操作说成具体的一句话（“要我上网搜「…」吗？”），让主人知道她到底要做什么。"""
    d = decision or {}
    a, p = d.get("action", ""), str(d.get("param") or "").strip()
    if a in ("computer_use", "computer_control", "desktop_control", "auto_click"):
        return f"要我帮你操控电脑执行「{p[:20]}」吗？"
    if a == "send_chatapp":
        try:
            j = json.loads(p or "{}")
            return f"要我给{j.get('contact', '对方')}发聊天软件「{str(j.get('message', ''))[:20]}」吗？"
        except Exception:
            return "要我发这条聊天软件吗？"
    media = {"next": "切到下一首", "prev": "切回上一首", "play_pause": "暂停或继续播放", "pause": "暂停播放", "play": "继续播放"}
    m = _SEARCH_PARAM_RE.match(p)
    table = {
        "open_app": f"打开{p}" if p else "打开它", "close_app": f"关掉{p}" if p else "关掉它",
        "open_url": f"打开{m.group(1) if m else p}网站", "web_search": f"上网搜「{p[:20]}」",
        "open_path": f"打开{p}文件夹", "search_file": f"在电脑里找「{p[:20]}」", "doc_search": f"查文档「{p[:20]}」",
        "media_control": media.get(p, "控制播放"), "volume_up": "把音量调大", "volume_down": "把音量调小",
        "volume_mute": "静音", "brightness_up": "把屏幕调亮", "brightness_down": "把屏幕调暗",
        "set_brightness": f"把亮度调到{p}", "lock_screen": "锁屏", "screenshot": "截个图", "show_desktop": "回到桌面",
        "recall_chatapp": "撤回刚才那条聊天软件", "run_cmd": f"执行命令「{p[:30]}」", "run_script": f"运行脚本{p}",
        "ufo_task": f"在界面上操作：{p[:20]}", "system_status": "看一下电脑状态", "open_chatapp": "打开聊天软件",
        "openmuse_task": f"让 OpenMuse 上网处理「{p[:20]}」",
        "opendots_task": f"让 OpenDots 虚拟同事撰写「{p[:20]}」",
        "opendots_memory": f"把「{p[:20]}」存入知识库",
        "multiagent_task": f"启动多智能体协同流水线「{p[:20]}」",
    }
    return f"要我{table[a]}吗？" if a in table else "要我执行这个操作吗？"
_CANCEL_RE = re.compile(r"^(?:(取消|算了|不要|不用|别|不发了|不执行)(吧|了|啊|呀|执行)?|我(?:取消|不确认|不发送|不执行|不发了|确认取消))$")


class PendingConfirmation:
    """被权限闸门拦下、等待用户口头确认的操作。30 秒有效，只保留最近一个。
    下一句话：确认词 → 执行；取消词 → 放弃；其他任何话 → 放弃并按正常指令处理。"""

    def __init__(self, ttl=30.0, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self._item = None
        self._lock = threading.Lock()

    def set(self, decision, source, user_text):
        with self._lock:
            self._item = {"decision": dict(decision), "source": source, "user_text": user_text,
                          "expires": self.clock() + self.ttl}

    def has_pending(self):
        with self._lock:
            return self._item is not None and self.clock() <= self._item["expires"]

    def peek(self):
        """Snapshot identity for a short voice reply; never consumes the prompt."""
        with self._lock:
            return self._item if self._item is not None and self.clock() <= self._item["expires"] else None

    def classify(self, text, expected_item=None):
        """Check a full reply without discarding unknown speech or a newer prompt."""
        with self._lock:
            item = self._item
            if item is None or self.clock() > item["expires"] or (expected_item is not None and item is not expected_item):
                return None, None
            return self._classify(text, item)

    @staticmethod
    def _classify(text, item):
        t = _consent_text(text)
        if confirms(text, (item.get("decision") or {}).get("action")):
            return "confirm", item
        if _CANCEL_RE.fullmatch(t):
            return "cancel", item
        return None, None

    def resolve(self, text, expected_item=None):
        """返回 (kind, item)，kind ∈ {"confirm", "cancel", None}。调用后待确认项总会被清除。
        不是确认也不是取消时，被清掉的那一项记在 last_dropped()，没听清时可以放回去。"""
        with self._lock:
            if expected_item is not None and self._item is not expected_item:
                return None, None
            item, self._item = self._item, None
            self._dropped = None
        if item is None or self.clock() > item["expires"]:
            return None, None
        kind, current = self._classify(text, item)
        if kind is None:
            with self._lock:
                self._dropped = item
        return kind, current

    def last_dropped(self):
        """上一句话清掉的、还没过期的待确认项。"""
        with self._lock:
            item = getattr(self, "_dropped", None)
            return item if item is not None and self.clock() <= item["expires"] and self._item is None else None

    def restore(self, item):
        """没听清：把刚清掉的那一项放回去（期限不延长），这期间来了新的确认项就不放。"""
        with self._lock:
            if self._item is None and getattr(self, "_dropped", None) is item and self.clock() <= item["expires"]:
                self._item, self._dropped = item, None
                return True
            return False


# 像是没听清的“确认”（“确haron.”“确…”），或大模型自己都没把握的猜测：不能顶掉正在等的确认
_GARBLED_CONFIRM_RE = re.compile(r"^(?:确|却|缺|雀|鹊)[^一-鿿]*$|^(?:确|却|缺)(?:人|任|仁|忍)[^一-鿿]*$")
_UNSURE_GUARDS = {"low_confidence", "low_or_missing_confidence", "ungrounded_param"}


def keeps_pending(text, decision):
    """这句话没确认也没取消，但不该清掉正在等的确认：识别成乱码的“确认”，或模型低把握的猜测。"""
    t = re.sub(r"[\s，,。.!！?？~～]", "", text or "")
    return bool(_GARBLED_CONFIRM_RE.match(t)) or (decision or {}).get("_guard") in _UNSURE_GUARDS


# 指代上一轮联系人的说法
REFERENTIAL_CONTACTS = {
    "他", "她", "TA", "ta", "对方", "那人", "那个人", "刚才那个人", "刚才那人", "刚才的人",
    "刚刚那个人", "刚才那位", "那位", "这个人", "上一个人", "刚才那个",
}


_RESEND_RE = re.compile(r"^(?:团团|你)?[，,]*(?:你)?(?:帮我|给我|替我|给他|给她|给对方)?(?:再|重新|重)"
                        r"(?:再|重新|帮我|给我|替我|给他|给她|给对方){0,3}"
                        r"(?:发|发送)(?:一遍|一次|一下|过去)?(?:吧|呗|啊|呀|嘛)?$")


def resend_request(text, memory):
    """“再发一遍 / 重新发一下”：把最近那条聊天软件重新念给主人确认，确认后才发（不当成撤回，也不让模型重编正文）。"""
    t = re.sub(r"[\s。.!！~～]", "", mask_quotes(text or ""))
    if memory is None or not _RESEND_RE.fullmatch(t):
        return None
    last = memory.last_chatapp_send()
    if not last:
        return {"action": "chat", "param": "", "emotion": "pamper", "reply": "要重新发哪条、发给谁？你再跟我说一遍～",
                "_guard": "resend_unknown", "_pending": None}
    send = {"action": "send_chatapp", "param": json.dumps(last, ensure_ascii=False), "emotion": "confident", "reply": ""}
    return {"action": "chat", "param": "", "emotion": "confident", "_guard": "resend_chatapp", "_pending": send,
            "reply": f"我再给{last['contact']}发一遍：{last['message'].rstrip('。.!！?？~～')}。说确认就发，说取消就算了。"}


class ChatAppDraft:
    """Exact message slots, optionally persisted in a profile-scoped context store."""

    def __init__(self, ttl=30.0, clock=time.monotonic, store=None, profile_key="owner", persist_enabled=None):
        self.ttl = ttl
        self.clock = clock
        self.store, self.profile_key, self.session_id = store, profile_key, None
        self.persist_enabled = persist_enabled or (lambda: True)
        self._item = None
        self._task = None
        self._lock = threading.RLock()

    def bind(self, profile_key, session_id=None):
        with self._lock:
            if profile_key != self.profile_key:
                self._item, self._task = None, None
            self.profile_key, self.session_id = profile_key, session_id
        return self

    def reset_local(self):
        with self._lock:
            self._item, self._task = None, None

    def set(self, part):
        part = {k: str(v).strip() for k, v in (part or {}).items() if k in ("contact", "message") and str(v or "").strip()}
        with self._lock:
            self._task = None
            self._item = dict(part, expires=self.clock() + self.ttl) if len(part) == 1 else None
            if self.store is not None and self.persist_enabled() and self._item:
                self._task = self.store.save_task(self.profile_key, self.session_id, "send_chatapp", part,
                    missing_slot="message" if "contact" in part else "contact")

    def peek(self, explicit=False):
        with self._lock:
            if self.store is not None and self.persist_enabled():
                task = self.store.active_task(self.profile_key, explicit=explicit, action="send_chatapp")
                if task:
                    self._task = task
                    return dict(task["slots"])
                return None
            if self.store is not None and self._task is not None:
                self._item, self._task = None, None  # Do not expose an earlier persistent draft in incognito.
            item = self._item
            if item is None or self.clock() > item["expires"]:
                return None
            return {k: v for k, v in item.items() if k != "expires"}

    def clear(self):
        with self._lock:
            if self.store is not None and self.persist_enabled():
                task = self._task or self.store.active_task(self.profile_key, action="send_chatapp")
                if task:
                    self.store.finish_task(task["id"], self.profile_key, task["version"], "cancelled")
            self._item = None
            self._task = None

    def task_snapshot(self):
        with self._lock:
            return dict(self._task) if self._task and self.persist_enabled() else None

    def complete(self, contact, message):
        """Promote the same task to confirmation without cancelling it."""
        with self._lock:
            task = self._task
            if self.store is not None and self.persist_enabled():
                self._task = self.store.save_task(self.profile_key, self.session_id, "send_chatapp",
                    {"contact": contact, "message": message}, status="awaiting_confirmation",
                    task_id=task["id"] if task else None, expected_version=task["version"] if task else None)
            self._item = None
            return self.task_snapshot()

    def restore_explicit(self):
        with self._lock:
            part = self.peek(explicit=True)
            task = self._task
            if part and task and self.store is not None and self.persist_enabled():
                self._task = self.store.save_task(self.profile_key, task["session_id"], "send_chatapp", part,
                    task_id=task["id"], expected_version=task["version"], missing_slot=task["missing_slot"],
                    status="awaiting_confirmation" if part.get("contact") and part.get("message") else "awaiting_slot")
                return dict(part) if self._task else None
            return part


_DRAFT_CANCEL_RE = re.compile(r"^(?:算了|不发了|不用了|不用发了|别发了|取消|不要了)(?:吧|啊)?$")
_SAY_PREFIX_RE = re.compile(r"^(?:你)?(?:就)?(?:跟(?:他|她|TA)说|和(?:他|她)说|告诉(?:他|她)|内容是|就说|说)[:：，,]?")
_CONTACT_REPLY_PREFIX_RE = re.compile(r"^(?:就发给|就给|发给|就是|联系人是|对象是|是|给|就)\s*[:：，,]?\s*")
_DRAFT_CONTACT_EDIT_RE = re.compile(r"^(?:不是.+?[，,]\s*)?(?:联系人|收件人|对象|发给的人)?\s*(?:改成|改为|换成|换为|是)\s*(.+)$")
_DRAFT_MESSAGE_EDIT_RE = re.compile(r"^(?:消息|正文|内容)\s*(?:改成|改为|换成|换为|是)\s*[:：]?\s*(.+)$", re.S)
_DRAFT_RESTORE_RE = re.compile(r"^(?:继续|恢复|接着)(?:刚才|之前|上次)?(?:那条|的)?(?:聊天软件|消息|草稿)(?:吧)?[。！!~～\s]*$")


def _draft_contact_name(text):
    name = _CONTACT_REPLY_PREFIX_RE.sub("", str(text or "").strip()).strip().rstrip("。！!~～")
    name = re.sub(r"(?:吧|啊|呀|呢)$", "", name).strip()
    if (not 1 <= len(name) <= 80 or name in _GENERIC_CHATAPP_CONTACTS
            or re.search(r"[\n\r\t，,。！？!?：:]", name)
            or not re.fullmatch(r"[一-鿿A-Za-z0-9 _·.\-（）()【】\[\]]+", name)
            or not re.search(r"[一-鿿A-Za-z0-9]", name)):
        return None
    return name


def _draft_message_body(text, contact, *, preserve_format=False):
    """Convert reported pronouns without deleting decimal points, spaces or wording."""
    body = str(text or "") if preserve_format else str(text or "").strip()
    quoted = _QUOTED_SPAN_RE.fullmatch(body.strip()) if preserve_format else _QUOTED_SPAN_RE.match(body)
    if quoted:
        return quoted.group(0)[1:-1]
    body = re.sub(r"我不(?:再)?(?:爱|喜欢)(?:他|她|TA|他们)了", lambda m: "我不爱你了" if "爱" in m.group() else "我不喜欢你了", body)
    body = re.sub(r"我(?:好|很|特别)?想(?:他|她|TA)了", "我想你了", body)
    body = re.sub(r"我(?:好|很)?爱(?:他|她|TA)", "我爱你", body)
    body = re.sub(r"我(?:好|很)?喜欢(?:他|她|TA)", "我喜欢你", body)
    body = re.sub(r"我(?:好|很|特别)?讨厌(?:他|她|TA)", "我讨厌你", body)
    body = re.sub(r"我想和?(?:他|她|TA)分手", "我想跟你分手", body)
    body = re.sub(r"^(?:让|叫|催|喊|通知|告诉)(?:他|她|TA|他们)\s*", "你", body)
    body = re.sub(r"([去到见找陪问给向跟同与替帮])\s*(?:他|她|TA)", r"\g<1>你", body)
    body = re.sub(r"(?:他|她|TA)的", "你的", body)
    body = re.sub(r"(?:他|她|TA)家", "你家", body)
    body = re.sub(r"我([^\s，,。]+?)(?:他|她|TA)了", r"我\g<1>你了", body)
    body = re.sub(r"我([^\s，,。]+?)(?:他|她|TA)", r"我\g<1>你", body)
    if any(k in contact for k in ("总", "老师", "经理", "主任", "导师", "领导")):
        body = re.sub(r"(?<!我)你", "您", body)
    return body


def draft_fill(text, draft, *, message_answer=None):
    """上一句只说了联系人或只说了正文：这句补上另一半，念一遍等主人确认（确认前不发）。
    这句本身是一条完整的新指令时不管，交给正常流程。"""
    raw = (text or "").strip()
    restore = bool(_DRAFT_RESTORE_RE.fullmatch(raw))
    d = (draft.restore_explicit() if restore else draft.peek()) if draft is not None else None
    if not d:
        return None
    if restore and not (d.get("contact") and d.get("message")):
        missing = "说什么" if d.get("contact") and not d.get("message") else "发给谁"
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "draft_restore",
                "reply": f"接着刚才这条消息，{missing}？"}
    t = re.sub(r"[\s。.!！~～]", "", raw)
    if _DRAFT_CANCEL_RE.fullmatch(t):
        draft.clear()
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "好，这条不发了。", "_guard": "draft_cancel"}
    contact, message = d.get("contact"), d.get("message")
    contact_edit = _DRAFT_CONTACT_EDIT_RE.fullmatch(raw)
    message_edit = _DRAFT_MESSAGE_EDIT_RE.fullmatch(raw)
    if contact and message and not (contact_edit or message_edit or restore):
        return None
    own = fast_match_intent(raw)
    own_draft = (own or {}).get("_draft") or {}
    # A reply to a missing message slot can itself be a greeting or negation.
    # A chat template alone is not evidence that the user started a new task.
    missing_message = bool(contact and not message)
    preserve_format = False
    if not (contact_edit or message_edit or restore) and (own and own.get("action") != "chat" or own and own.get("reply") and not own_draft and not missing_message):
        draft.clear()  # 换了别的事
        return None
    if contact_edit and message:
        contact = _draft_contact_name(contact_edit.group(1))
        if not contact:
            return None
    elif message_edit and contact:
        message = message_edit.group(1).strip()
        if missing_message and isinstance(message_answer, str):
            original_edit = _DRAFT_MESSAGE_EDIT_RE.fullmatch(message_answer.lstrip())
            if original_edit:
                message = original_edit.group(1)
                preserve_format = True
    elif contact and not message:
        if own_draft.get("contact"):
            return None  # 又说了一遍“给谁发消息”：当新的开头，由主流程重新记
        rest = (_SAY_PREFIX_RE.sub("", raw).strip() if _SAY_PREFIX_RE.match(raw)
                else own_draft.get("message") or raw)
        if not rest or (is_noise_utterance(rest) and message_answer is None) or (re.search(r"[?？]$", raw) and not _SAY_PREFIX_RE.match(raw)
                                                   and message_answer is None):
            return None
        message = rest
        if isinstance(message_answer, str):
            surface = message_answer.lstrip()
            prefix = _SAY_PREFIX_RE.match(surface)
            if prefix or not own_draft.get("message"):
                message = surface[prefix.end():].lstrip() if prefix else message_answer
                preserve_format = True
    elif message and not contact:
        if own_draft.get("message"):
            return None
        name = _draft_contact_name(own_draft.get("contact") or raw)
        if not name:
            return None
        contact = name
    elif not (restore and contact and message):
        return None
    if not isinstance(message, str) or not message.strip():
        return None
    p = {"contact": contact, "message": _draft_message_body(message, contact, preserve_format=preserve_format)
         if not d.get("message") or message_edit else message}
    task = draft.complete(contact, p["message"])
    send = {"action": "send_chatapp", "param": json.dumps(p, ensure_ascii=False), "emotion": "confident", "reply": ""}
    if task:
        send["_context_task"] = task
    readback_message = p["message"] if preserve_format else p["message"].rstrip('。.!！?？~～')
    return {"action": "chat", "param": "", "emotion": "confident", "_guard": "chatapp_draft", "_pending": send,
            "reply": f"我准备给{p['contact']}发：{readback_message}。对吗？说确认就发，说取消就算了。"}


class ClarifyContext:
    """团团反问了一句（“打开哪个？”“哪个城市？”“要搜什么？”），30 秒内记着主人原来那句话，
    下一句回答和原话拼成完整的一句再理解。发聊天软件缺联系人 / 正文走 ChatAppDraft，等确认走 PendingConfirmation。"""

    def __init__(self, ttl=30.0, clock=time.monotonic, store=None, profile_key="owner", persist_enabled=None):
        self.ttl = ttl
        self.clock = clock
        self.store, self.profile_key, self.session_id = store, profile_key, None
        self.persist_enabled = persist_enabled or (lambda: True)
        self._task = None
        self._item = None
        self._lock = threading.RLock()
        self._generation = 0

    def bind(self, profile_key, session_id=None):
        with self._lock:
            if (profile_key, session_id) != (self.profile_key, self.session_id):
                self._generation += 1
            if profile_key != self.profile_key:
                self._item, self._task = None, None
            self.profile_key, self.session_id = profile_key, session_id
        return self

    def reset_local(self):
        with self._lock:
            self._generation += 1
            self._item, self._task = None, None

    def set(self, text, guard, question):
        with self._lock:
            self._generation += 1
            self._task = None
            self._item = {"text": text, "guard": guard or "", "question": question, "expires": self.clock() + self.ttl}
            if self.store is not None and self.persist_enabled():
                self._task = self.store.save_task(self.profile_key, self.session_id, "clarification",
                    {"text":text,"guard":guard or "","question":question}, missing_slot=guard or "target")

    def peek(self):
        with self._lock:
            if self.store is not None and self.persist_enabled():
                previous = self._task
                self._task = self.store.active_task(self.profile_key, action="clarification")
                if previous is not None and self._task is None:
                    self._generation += 1
                return dict(self._task["slots"]) if self._task else None
            if self.store is not None and self._task is not None:
                self._item, self._task = None, None
                self._generation += 1
            item = self._item
            if item is not None and self.clock() > item["expires"]:
                self._item = None
                self._generation += 1
                return None
            return dict(item) if item is not None else None

    def snapshot(self):
        """Opaque revision binding for an answer captured while this question was active."""
        with self._lock:
            if self.peek() is None:
                return None
            task = self._task
            backend = (task["id"], task["version"], task.get("session_id")) if task else None
            return (id(self), self._generation, self.profile_key, self.session_id, backend)

    def matches(self, expected):
        with self._lock:
            return self.snapshot() == expected

    def consume(self, expected):
        """Consume exactly one matching revision; a replacement is never cleared."""
        with self._lock:
            item = self.peek()
            task = self._task
            backend = (task["id"], task["version"], task.get("session_id")) if task else None
            current = (id(self), self._generation, self.profile_key, self.session_id, backend) if item else None
            if expected is None or current != expected:
                return None
            if task is not None and self.store is not None and self.persist_enabled():
                if self.store.finish_task(task["id"], self.profile_key, task["version"], "cancelled") is None:
                    return None
            self._item, self._task = None, None
            self._generation += 1
            return item

    def clear(self):
        with self._lock:
            self._generation += 1
            if self.store is not None and self.persist_enabled() and self._task:
                self.store.finish_task(self._task["id"],self.profile_key,self._task["version"],"cancelled")
            self._item = None
            self._task = None


# 这些反问不是在问缺的那一块：确认、能力问答、多步、撤回还是重发（“重新发”自己就是指令）等
_NOT_CLARIFY_GUARDS = {"capability_question", "multi_step_partial", "recall_not_said", "kept_pending", "negated",
                       "resend_chatapp", "resend_unknown", "chatapp_draft", "draft_cancel", "contact_ambiguous",
                       "no_contact", "empty_message", "hesitation", "app_not_found", "draft_restore"}
_ANSWER_FIRST_GUARDS = {"weather_no_place", "no_target"}   # “合肥” + “今天天气怎么样”、“亮度” + “调到百分之三十”


def remember_clarify(ctx, text, decision):
    """团团这句回复是在追问缺的信息时记下原话；返回是否记下。"""
    if ctx is None or not decision:
        return False
    reply = str(decision.get("reply") or "").strip()
    guard = decision.get("_guard")
    if (decision.get("action") != "chat" or decision.get("_pending") or decision.get("_draft")
            or guard in _NOT_CLARIFY_GUARDS or not re.search(r"[？?]", reply)):
        return False
    if not guard and not looks_like_command(text) and not _WEATHER_RE.search(text or ""):
        return False   # 闲聊里的反问（“你今天过得怎么样？”）不算
    ctx.set(text, guard, reply)
    return True


def clarify_merge(answer, ctx, expected=Ellipsis, stale_result=None):
    """把这句回答和被追问的原话拼成一句；回答本身是一条完整新指令、取消、或太长时返回 None 并清掉。"""
    snapshot = ctx.snapshot() if ctx is not None and expected is Ellipsis else expected
    if ctx is None:
        return None
    if not ctx.matches(snapshot):
        return stale_result
    item = ctx.peek() if snapshot is not None else None
    if not item:
        return None
    raw = (answer or "").strip()
    t = raw.rstrip("。!！~～ ")
    own = fast_match_intent(raw)
    new_reminder = own and own.get("_guard") in ("reminder_time_missing", "reminder_time_multiple") and raw != item["text"]
    if (_DRAFT_CANCEL_RE.fullmatch(t) or _CANCEL_RE.fullmatch(t) or not t or len(t) > 200 or is_noise_utterance(t) and len(t) < 2
            or new_reminder or (own and (own.get("action") not in (None, "chat") or own.get("_draft") or own.get("_pending")))):
        return None if ctx.consume(snapshot) is not None else stale_result
    orig = item["text"].rstrip("。.!！?？~～ ")
    guard = item["guard"]
    if guard == "reminder_time_missing":
        merged = _merge_reminder_time(t, orig)
    elif guard in _ANSWER_FIRST_GUARDS:
        merged = f"{t}{orig}"
    elif re.search(r"那个|这个|它", orig) and guard not in ("date_missing",):
        merged = re.sub(r"那个|这个|它", t, orig, count=1)
    elif guard == "vague_query":
        merged = f"帮我搜{t}"
    elif guard == "no_object" or re.search(r"(?:打开|关掉|关闭|关上|启动|退出)(?:吧|呗|一下)?$", orig):
        merged = re.sub(r"(?:吧|呗|啊|呀|一下)$", "", orig) + t
    else:
        merged = f"{orig}，{t}"
    return merged if ctx.consume(snapshot) is not None else stale_result


def resolve_references(decision, memory):
    """把“刚才那个人/他”这类指代替换成上一轮真正发过消息的联系人（确定性处理，不依赖模型）。"""
    if not decision or memory is None or decision.get("action") != "send_chatapp":
        return decision
    param = decision.get("param", "")
    try:
        obj = json.loads(param) if isinstance(param, str) else dict(param)
    except Exception:
        return decision
    contact = str(obj.get("contact", "")).strip()
    last = memory.last_contact()
    if last and contact in REFERENTIAL_CONTACTS:
        obj["contact"] = last
        decision = dict(decision)
        decision["param"] = json.dumps(obj, ensure_ascii=False)
        decision["resolved_contact"] = last
    return decision


class ConversationMemory:
    """短期多轮记忆：保留最近 max_turns 轮（用户原话 + 团团决策的紧凑 JSON + 执行结果），
    超过 idle_expire 秒没有新对话则清空，避免很久以前的话题干扰当前指令。"""

    def __init__(self, max_turns=4, idle_expire=300.0, clock=time.monotonic, revision=None):
        self.max_turns = max_turns
        self.idle_expire = idle_expire
        self.clock = clock
        self.turns = []
        self._last = 0.0
        self._lock = threading.Lock()
        self._revision = revision
        self._privacy_revision = revision() if revision else None

    def _expire_locked(self):
        if self._revision:
            revision = self._revision()
            if revision != self._privacy_revision:
                self.turns = []
                self._privacy_revision = revision
        if self.turns and self.clock() - self._last > self.idle_expire:
            self.turns = []

    def add_turn(self, user_text, decision, result_note="", expected_revision=None):
        # assistant 内容与模型要求的输出格式保持一致，避免小模型学到多余字段
        compact = {
            "action": decision.get("action", "chat"),
            "param": decision.get("param", ""),
            "reply": decision.get("reply", ""),
        }
        contact = None
        if compact["action"] == "send_chatapp":
            try:
                contact = json.loads(compact["param"]).get("contact")
            except Exception:
                contact = None
        with self._lock:
            self._expire_locked()
            if self._revision and expected_revision is not None and expected_revision != self._privacy_revision:
                return False
            self.turns.append({
                "user": user_text,
                "assistant": json.dumps(compact, ensure_ascii=False),
                "result": str(result_note)[:80] if result_note else "",
                "contact": contact,
            })
            self.turns = self.turns[-self.max_turns:]
            self._last = self.clock()

    def seed(self, turns, grace=0.0, expected_revision=None):
        """重启后接着聊：把上次留下的几轮放回来（turns: [(原话, 决策 dict)]）。
        grace：额外保留多久（重启后主人不一定马上开口）。"""
        for user_text, decision in turns[-self.max_turns:]:
            self.add_turn(user_text, decision, expected_revision=expected_revision)
        if turns:
            with self._lock:
                self._last = self.clock() + grace

    def messages(self):
        """user / assistant / tool 三种角色。执行结果用 tool 角色传入
        （Qwen 模板会渲染成 <tool_response>，而中途的 system 消息会被模板丢弃）。"""
        with self._lock:
            self._expire_locked()
            out = []
            for t in self.turns:
                out.append({"role": "user", "content": t["user"]})
                out.append({"role": "assistant", "content": t["assistant"]})
                if t["result"]:
                    out.append({"role": "tool", "content": "执行结果：" + t["result"]})
            return out

    def recent_actions(self, n=3):
        """最近几次实际操作电脑的简述（给陪聊做情境），不含闲聊。"""
        with self._lock:
            self._expire_locked()
            out = []
            for t in self.turns:
                try:
                    act = json.loads(t["assistant"]).get("action")
                except Exception:
                    act = None
                if act and act != "chat":
                    out.append(f"{t['user']}（{t['result'][:24]}）" if t["result"] else t["user"])
            return out[-n:]

    def last_contact(self):
        with self._lock:
            self._expire_locked()
            for t in reversed(self.turns):
                if t.get("contact"):
                    return t["contact"]
            return None

    def last_chatapp_send(self):
        """最近一次发聊天软件的参数（{"contact", "message"}），没有则 None。"""
        with self._lock:
            self._expire_locked()
            for t in reversed(self.turns):
                if not t.get("contact"):
                    continue
                try:
                    p = json.loads(json.loads(t["assistant"]).get("param") or "{}")
                except Exception:
                    continue
                if isinstance(p, dict) and str(p.get("contact") or "").strip() and str(p.get("message") or "").strip():
                    return {"contact": p["contact"].strip(), "message": p["message"].strip()}
            return None

    def clear(self):
        with self._lock:
            self.turns = []


class OllamaIntentClient:
    """本地大模型意图解析：流式输出、动作类型提前回调、记住可用模型、端口快速探测。"""

    def __init__(self, models=None, system_prompt=None, ollama_url=OLLAMA_URL,
                 commercial_url=COMMERCIAL_URL, num_predict=160, num_ctx=4096, timeout=20, json_mode=False):
        self.models = list(models or DEFAULT_MODELS)
        self.system_prompt = system_prompt or DESK_SYSTEM_PROMPT
        self.ollama_url = ollama_url
        self.commercial_url = commercial_url
        self.num_predict = num_predict
        # 系统提示词约 2500 token，模型 Modelfile 里 num_ctx=2048 会把它截掉一半以上，这里显式放大
        self.num_ctx = num_ctx
        self.json_mode = json_mode
        self.timeout = timeout
        self.working_model = None
        self.ollama_probe = PortProbe(*OLLAMA_HOST)
        self.commercial_probe = PortProbe(*COMMERCIAL_HOST)
        self._model_lock = threading.Lock()

    # ---- 服务管理 ----
    def ensure_ollama_alive(self):
        """Ollama 未监听时尝试后台拉起（最多等 6 秒）。"""
        if self.ollama_probe.is_up(force=True):
            return True
        print("[*] 本地大模型服务未监听，尝试后台启动 ollama serve ...")
        try:
            import subprocess
            subprocess.Popen(["ollama", "serve"], creationflags=0x08000000 if os.name == "nt" else 0)
        except Exception as e:
            print(f"[!] 启动 Ollama 失败: {e}")
            return False
        for _ in range(12):
            time.sleep(0.5)
            if self.ollama_probe.is_up(force=True):
                print("[✓] Ollama 已启动")
                return True
        return False

    def model_order(self):
        with self._model_lock:
            if self.working_model and self.working_model in self.models:
                return [self.working_model] + [m for m in self.models if m != self.working_model]
            return list(self.models)

    def warmup(self):
        self.ensure_ollama_alive()
        for model in self.model_order():
            try:
                payload = {"model": model, "messages": [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": "ping"}],
                           "stream": False, "keep_alive": "24h", "options": {"num_predict": 1, "num_ctx": self.num_ctx}}
                req = urllib.request.Request(self.ollama_url, data=json.dumps(payload).encode("utf-8"),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                with self._model_lock:
                    self.working_model = model
                print(f"[✓] Ollama 预热完成: {model}")
                return model
            except Exception as e:
                print(f"[!] 预热模型 {model} 失败: {e}")
        return None

    # ---- 推理 ----
    def _build_messages(self, user_text, history):
        msgs = [{"role": "system", "content": self.system_prompt}]
        msgs.extend(history or [])
        msgs.append({"role": "user", "content": user_text})
        return msgs

    def _query_commercial(self, messages):
        req = urllib.request.Request(self.commercial_url, data=json.dumps({"messages": messages}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["message"]["content"].strip()

    def _stream_ollama(self, model, messages, on_action, tracer):
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": "24h",
            "options": {"temperature": 0.3, "top_p": 0.85, "num_predict": self.num_predict, "num_ctx": self.num_ctx},
        }
        if self.json_mode:
            # Ollama 约束解码：输出必定是合法 JSON（小模型经常只输出口语回复而丢掉 JSON 外壳）
            payload["format"] = "json"
        req = urllib.request.Request(self.ollama_url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        buf = []
        action_sent = False
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                chunk = json.loads(line.decode("utf-8"))
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                piece = (chunk.get("message") or {}).get("content", "")
                if piece:
                    if not buf and tracer is not None:
                        tracer.mark("llm_first_token")
                    buf.append(piece)
                    if not action_sent and on_action is not None:
                        m = _ACTION_RE.search("".join(buf))
                        if m:
                            action_sent = True
                            try:
                                on_action(m.group(1))
                            except Exception as e:
                                print(f"[!] on_action 回调异常: {e}")
                if chunk.get("done"):
                    break
        return "".join(buf).strip()

    def decide(self, user_text, history=None, on_action=None, tracer=None):
        """返回决策 dict（action/param/emotion/reply），并在 decision['_model'] 标明来源模型。"""
        messages = self._build_messages(user_text, history)

        # 1. 本地商业模型微服务（11435）：仅在端口确实在监听时才请求
        if self.commercial_probe.is_up():
            try:
                raw = self._query_commercial(messages)
                if tracer is not None:
                    tracer.mark("llm_first_token")
                dec = extract_json(raw)
                if dec:
                    if on_action is not None:
                        on_action(dec.get("action", "chat"))
                    if tracer is not None:
                        tracer.mark("llm_done")
                    dec["_model"] = "commercial-11435"
                    return dec
            except Exception as e:
                print(f"[!] 11435 商业模型服务请求失败: {e}")
                self.commercial_probe.mark_down()

        # 2. Ollama：先用上次成功的模型，失败再依次尝试
        if not self.ollama_probe.is_up():
            self.ensure_ollama_alive()
        # 只有前面的模型“不可用”（请求异常，如模型不存在）时才把后备模型记为常用模型；
        # 某一句输出格式不对只在本次请求内换模型，避免一次失误就把后续请求全部切到慢得多的 7B
        prior_unavailable = True
        for model in self.model_order():
            try:
                raw = self._stream_ollama(model, messages, on_action, tracer)
                dec = extract_json(raw)
                if dec:
                    if prior_unavailable:
                        with self._model_lock:
                            self.working_model = model
                    if tracer is not None:
                        tracer.mark("llm_done")
                    dec["_model"] = model
                    return dec
                plain = _plain_text_chat(raw) if not looks_like_command(user_text) else None
                if plain is not None:
                    # 小模型闲聊时常直接输出一句话而不是 JSON：按 chat 处理，不再升级到慢得多的 7B
                    if tracer is not None:
                        tracer.mark("llm_done")
                    plain["_model"] = model
                    return plain
                prior_unavailable = False
                print(f"[!] 模型 {model} 输出无法解析为 JSON: {raw[:80]!r}")
            except Exception as e:
                err = str(e)
                print(f"[!] 模型 {model} 推理异常: {err}")
                if "10061" in err or "refused" in err.lower():
                    self.ollama_probe.mark_down()
                    if not self.ensure_ollama_alive():
                        break
        if tracer is not None:
            tracer.mark("llm_done")
        dec = dict(FALLBACK_DECISION)
        dec["_model"] = "fallback"
        return dec


COMPACT_PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "canaan_intent_compact.txt")
COMPACT_ACTIONS = ["open_app", "close_app", "open_chatapp", "send_chatapp", "recall_chatapp", "volume_up", "volume_down",
                   "volume_mute", "brightness_up", "brightness_down", "set_brightness", "media_control", "screenshot",
                   "lock_screen", "show_desktop", "system_status", "open_path", "search_file", "open_url", "web_search",
                   "doc_search", "run_script", "diagnose_error", "setup_workspace", "ufo_task", "run_cmd", "chat"]
COMPACT_SCHEMA = {"type": "object", "properties": {"a": {"type": "string", "enum": COMPACT_ACTIONS}, "p": {"type": "string"}},
                  "required": ["a", "p"]}


def action_confidence(content, logprobs, field="a"):
    """Return bounded, text-aligned field confidence; unavailable evidence is None."""
    from canaan_model_confidence import action_confidence as field_confidence
    return field_confidence(content, logprobs, field)


class CompactIntentClient:
    """紧凑输出的意图模型（专门微调，输出 {"a","p"}，用 JSON schema 约束解码，几乎不会格式错误）。
    模型不可用或输出异常时退回 fallback（原来的 OllamaIntentClient）。接口与 OllamaIntentClient 相同。"""

    def __init__(self, model, fallback=None, ollama_url=OLLAMA_URL, timeout=10, min_confidence=None):
        self.model = model
        self.fallback = fallback
        self.url = ollama_url
        self.timeout = timeout
        with open(COMPACT_PROMPT_PATH, encoding="utf-8") as f:
            self.system_prompt = f.read().strip()
        self.working_model = model
        self._primary_retry_at = 0.0
        if min_confidence is None:
            try:
                import canaan_config
                min_confidence = getattr(canaan_config, "INTENT_MIN_CONFIDENCE", 0.0)
            except Exception:
                min_confidence = 0.0
        self.min_confidence = min_confidence

    def ensure_ollama_alive(self):
        return self.fallback.ensure_ollama_alive() if self.fallback else True

    def _num_ctx(self):
        """和陪聊用同一个模型时（10-25 起 3B 两用）上下文要和陪聊一致（4096），不然 Ollama 每次切换都重载模型。
        识别提示词只有约 220 token，放大不影响结果。"""
        try:
            import canaan_config
            if self.model == getattr(canaan_config, "CHAT_MODEL", None):
                return getattr(canaan_config, "SHARED_NUM_CTX", 4096)
        except Exception:
            pass
        return 1024

    def _query(self, user_text, num_predict=64, with_logprobs=False, timeout=None):
        payload = {"model": self.model, "stream": False, "keep_alive": "24h", "format": COMPACT_SCHEMA,
                   "messages": [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user_text}],
                   "options": {"temperature": 0, "num_predict": num_predict, "num_ctx": self._num_ctx()}}
        if with_logprobs:
            payload["logprobs"] = True
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        if with_logprobs:
            return body["message"]["content"], body.get("logprobs") or []
        return body["message"]["content"]

    def warmup(self):
        try:
            self._query("你好", num_predict=8, timeout=max(self.timeout, 60))
            self._primary_retry_at = 0.0
            print(f"[OK] 紧凑意图模型预热完成: {self.model}")
            return self.model
        except Exception as e:
            self._primary_retry_at = time.monotonic() + 30
            status = "尝试已配置的备用意图模型" if self.fallback is not None else "未配置备用模型，未切换模型"
            print(f"[!] 紧凑意图模型 {self.model} 不可用（{e}），{status}")
            return self.fallback.warmup() if self.fallback else None

    def _cloud_decision(self, user_text, tracer=None):
        """data/llm_providers.json 配了 "intent" 时先问云端；结果照样过守卫，返回 None 表示交回本机模型。"""
        try:
            import canaan_llm_provider as llm
            obj = llm.cloud_intent(user_text, self.system_prompt, COMPACT_ACTIONS)
        except Exception:
            return None
        if obj is None:
            return None
        if tracer is not None:
            tracer.mark("llm_done")
        name = llm.role_provider("intent") or "cloud"
        # 云端不给逐字概率：按 0.9 记把握度，仍要过各动作的把握度门槛和参数核对，有风险的动作执行前照样确认
        dec = {"action": obj["a"], "param": obj["p"], "emotion": "confident", "reply": "",
               "_model": f"provider:{name}", "_conf": 0.9, "_pconf": 0.9}
        return apply_command_guard(user_text, dec)

    def decide(self, user_text, history=None, on_action=None, tracer=None, *, query_timeout=None):
        cloud = self._cloud_decision(user_text, tracer)
        if cloud is not None:
            if cloud["action"] != "chat" and 0.9 < confidence_threshold(cloud, user_text, self.min_confidence):
                return {"action": "chat", "param": "", "emotion": "confident", "reply": describe_action(cloud),
                        "_guard": "low_confidence", "_pending": cloud, "_model": cloud.get("_model", ""), "_conf": 0.9}
            if on_action is not None and cloud["action"] != "chat":
                on_action(cloud["action"])
            return cloud
        if self.fallback is not None and time.monotonic() < self._primary_retry_at:
            return self.fallback.decide(user_text, history=history, on_action=on_action, tracer=tracer)
        try:
            query_options = {"with_logprobs": True}
            if query_timeout is not None:
                query_options["timeout"] = query_timeout
            content, lps = self._query(user_text, **query_options)
            obj = json.loads(content)
            action = obj.get("a")
            if action not in COMPACT_ACTIONS:
                raise ValueError(f"未知动作 {action}")
            self._primary_retry_at = 0.0
            conf = action_confidence(content, lps)
            if tracer is not None:
                tracer.mark("llm_done")
            dec = {"action": action, "param": obj.get("p", "") or "", "emotion": "confident",
                   "reply": "", "_model": self.model, "_conf": conf,
                   "_pconf": action_confidence(content, lps, "p")}
            dec = apply_command_guard(user_text, dec)
            if dec["action"] == "chat":
                return dec
            if action != "chat" and (conf is None or conf < confidence_threshold(dec, user_text, self.min_confidence)):
                # 模型自己也拿不准：不执行，先问一句（原决策放在 _pending，用户说“好”再执行）
                return {"action": "chat", "param": "", "emotion": "confident",
                        "reply": describe_action(dec),
                        "_guard": "low_confidence", "_pending": dec, "_model": self.model, "_conf": conf}
            if on_action is not None:
                on_action(action)
            return dec
        except Exception as e:
            self._primary_retry_at = time.monotonic() + 30
            status = "尝试已配置的备用意图模型" if self.fallback is not None else "未配置备用模型，未切换模型"
            print(f"[!] 紧凑意图模型 {self.model} 不可用（{e}），{status}")
            if self.fallback is None:
                dec = dict(FALLBACK_DECISION)
                dec["_model"] = "fallback"
                return dec
            return self.fallback.decide(user_text, history=history, on_action=on_action, tracer=tracer)


def create_compact_intent_client(model, fallback=None, *, planning=True, plan_budget=None, local_only=False, **options):
    """Use one selected Compact backend, optionally completing full local plans."""
    base = CompactIntentClient(model, fallback=fallback, **options)
    if local_only:
        base._cloud_decision = lambda *args, **kwargs: None
        base._planning_local_only = True
    if not planning or fallback is not None:
        return base
    if not local_only:
        try:
            from canaan_llm_provider import role_provider
            if role_provider("intent"):
                return base  # Explicit cloud and backup choices retain their existing path.
        except Exception:
            return base
    from canaan_intent_planner import PlanningIntentClient
    return PlanningIntentClient(base, budget=plan_budget)


# ----------------------------------------------------------------------------
# 确定性规则快速通道
# ----------------------------------------------------------------------------

def _builtin_app_names():
    try:
        from canaan_os_executor import CanaanOSExecutor
        return {k.casefold() for k in CanaanOSExecutor.BUILTIN_APP_MAP}
    except Exception:
        return {"计算器", "记事本", "任务管理器", "资源管理器", "终端", "设置", "控制面板", "浏览器"}


def _find_application(name):
    """懒加载应用索引（避免导入本模块时就引入 win32 自动化依赖）。测试中可替换此函数。"""
    from canaan_os_executor import CanaanOSExecutor
    return CanaanOSExecutor.find_application(name)


def _known_application(name):
    """Recognize an existing application alias without claiming it is installed."""
    from canaan_os_executor import CanaanOSExecutor
    key = name.casefold()
    return any(key == alias.casefold() for app, aliases in CanaanOSExecutor.APP_ALIASES.items()
               for alias in (app, *aliases)) or key in _builtin_app_names()


# ----------------------------------------------------------------------------
# 提到动作 ≠ 命令：问功能、说过去/正在做的事、假设、转述别人的话时不执行
# ----------------------------------------------------------------------------

# 明确的请求语气：出现这些就按命令处理（“能帮我截个图吗”是命令）
_IMPERATIVE_RE = re.compile(r"(帮我|给我|替我|麻烦(?!的|死|了|得|啊|呀)|请你|请帮|赶紧|赶快|马上|立刻|能不能|可不可以|能否|可以帮|能帮|"
                            r"一下|撤回来|撤了|^把.{1,10}(?:关闭|关掉|打开|取消|开启)(?:吧|呗)?$)")
# 句末语气词只在短句里表示命令（“锁屏吧”“截个图呗”），长句里多是评论（“锁屏时间太短了吧”）
_SHORT_PARTICLE_RE = re.compile(r"^.{1,5}[吧呗哈]$")
_RELAY_RE = re.compile(r"(?:发|聊天软件|消息|短信).{0,8}(?:说|：|:)|(?:跟|和|对|给).{1,8}说|告诉|通知|转告|问问|问一下|提醒")
_MENTION_RULES = [
    ("quoted_report", re.compile(r"^(?:原话是|屏幕上(?:显示|写)的是|引用(?:的)?(?:内容)?是|文字写着)"
                                 r"|^(?:网页|文档|文件|正文|邮件|帖子|截图|网页正文)[^，,。]{0,8}(?:写着|写的是|显示着|里说)"
                                 r"|^(?:讲解稿|稿子|演讲稿|说明页|操作记录|记录|笔记)[^，,。]{0,8}(?:有一句|有句|写着|里有|里写)"
                                 r"|^(?:我|我朋友|朋友|他|她|同事)的?(?:操作记录|记录|笔记|清单)[^，,。]{0,6}在[“\"「]")),   # “我朋友的操作记录在“关闭计算器”旁边画了个勾”
    # 讲以前的事（“上周我把……交给Muse做过网页研究，出来的材料里……”）：整句看，后面的分句只是接着讲
    ("past_story", re.compile(r"^(?:上周|上个月|前几天|上回)[^。！？]*(?:做过|弄过|用过|试过)"
                              r"|^(?:刚才|刚刚|上午|下午|昨晚|昨天)我(?:自己)?[^，,。]{0,10}了[^，,。]{0,8}[，,]")),   # “刚才我自己暂停了播放器，起身……”
    ("hypothetical", re.compile(r"^(?:如果|假如|要是|假设|万一|倘若)|你说我要不要|^要不要.{0,10}(?:呢|吗)$"
                                r"|(?:其实|我)[^，,。]{0,4}想把[^，,。]{1,10}(?:了的|的)$")),   # “其实我挺想把聊天软件卸了的”：说想法
    ("progressive", re.compile(r"^我(?:正在|正|在)(?:用|给|跟|和|听|看|玩|写|截|打|聊|发|刷|开)")),
    ("reported", re.compile(r"^(?:我)?(?:同事|老板|领导|我妈|我爸|老妈|老爸|朋友|室友|老师|别人|他们?|她们?|同学|"
                            r"弟弟?|哥哥?|姐姐?|妹妹?|老婆|老公|对象|孩子|儿子|女儿|家里人|邻居)"
                            r"(?:刚才?|刚刚|又|还|也|一直|昨天|今天)?(?:老是|总是|说(?!话)|让我|叫我|让我们|叫我们|跟我说|觉得)")),
    # “老师说话挺轻的，电脑音量稍微调高些”：“说话”不是转述
    # 描述现象（“声音怪怪的”“屏幕一闪一闪的”）：是在说情况，不是要团团做某个动作
    ("symptom", re.compile(r"(?:怪怪的?|好怪|有点怪|不对劲|有杂音|滋滋响|一闪一闪|花屏|卡卡的|打不开|没反应)(?:的|啊|呀|了)?$")),
    # 问做法（“电脑怎么截长图”“怎么关掉开机自启”）：是在问怎么做，不是让团团去做
    ("howto", re.compile(r"(?:怎么|怎样|如何)(?:才能|才可以|能|可以|样)?(?:把|让|调|关|开|截|发|设|弄|改|取消|锁|切|撤|删|"
                         r"录|装|卸|连|换|传|找|不让|不自动|禁止|停|用)|学学|学一下|教教?我")),
    ("past", re.compile(r"(?:刚才|刚刚|已经|早就|昨天|前天|上次|之前|自己)[^，,。！？]*(?:了|过)$|过了$"
                        r"|^我(?:刚|刚刚)?把.+了$"
                        r"|(?:昨天|前天|上次|之前|刚才)[^，,。！？]*了(?:好多|很多|一堆|几张|几个|半天)")),   # “我昨天截了好多图”
    ("noun_mention", re.compile(r"(?:锁屏|截图|截屏|静音|音量|亮度|撤回|暂停)(?:壁纸|界面|密码|时间|快捷键|工具|功能|保存|"
                                r"模式|键|按钮|设置(?!为|成|到)|图标|文件|的消息|挺|很|真|太|也|都|还|又|留着)"
                                r"|(?:这张|那张|这个|那个)(?:截图|截屏)|(?:演示|示例|附带的?|配(?:着|了|的))[^，,。]{0,6}截图"
                                r"|(?:搜|查|找)(?:出来|到)的|(?:没关系|没事|不用管|无所谓)$"
                                r"|快捷键|(?:这个|那个)(?:软件|程序|应用)(?:挺|很|真|太|还|也|好)"   # “记事本这个软件挺简单的”
                                r"|(?:文件夹|桌面)(?:好|太|真|挺)(?:乱|脏)(?:啊|呀)?$")),           # “下载文件夹好乱”：抱怨，不是要打开
    ("question", re.compile(r"(?:好用|好看|好不好|怎么样|咋样|咋办|怎么办|靠谱|有用|是什么|是啥|啥意思|什么意思|干嘛的|"
                            r"在哪|哪个好|多少钱|有声音|伤眼|伤耳|会不会|叫什么|会自动|还能|能换|能算|是固定|有时间限制)"
                            r"|(?<![什怎])[吗么]$|[?？]$")),   # “看看下载里有什么”的“么”是“什么”，不是问句尾
]


def mention_reason(text):
    """句子只是提到某个动作而不是要求执行时，返回原因（hypothetical/progressive/reported/past/noun_mention/question），
    否则 None。只看最后一个分句里的问句/过去时（“我走了，锁屏”“刚才那条发错了，撤回来”仍是命令）。"""
    t = mask_quotes(_space_clauses(text))   # 引号里的原文不拿来判断语气；空格分句同 fast_match_intent
    t = re.sub(r"^(?:团团|加南|江南)[，,\s]*", "", t)
    if re.fullmatch(r"(?:屏幕上写了什么|屏幕上有啥)[?？。]?", t):
        return None  # 这是请求观察当前屏幕，问号不是把动作当话题讨论。
    last = re.split(r"[，,。；;]", t.rstrip("，,。！!；; "))[-1].strip() or t
    imperative = bool(_IMPERATIVE_RE.search(last) or _SHORT_PARTICLE_RE.match(last))
    # 转述消息内容（“发聊天软件说你明天有空吗”“告诉他我到了”）：问号/“了”属于要发的消息，不是在提问或说过去的事
    relay = bool(_RELAY_RE.search(t))
    later = [c for c in re.split(r"[，,。；;]", t)[1:] if c.strip()]
    for name, rx in _MENTION_RULES:
        whole = name in ("hypothetical", "progressive", "reported", "quoted_report", "past_story")
        target = t if whole else last
        if rx.search(target):
            if name in ("question", "past", "noun_mention", "howto") and (imperative or relay):
                continue
            # “聊天软件这个软件关掉吧，模拟讨论已经结束了”：命令在前，最后一句是交代原因
            earlier = re.split(r"[，,。；;]", t)[:-1]
            if name in ("past", "noun_mention") and any(
                    _CMD_HINT_RE.search(c) and (re.search(r"吧$|呗$|一下$|掉$|了吧$", c.strip())
                                                or re.match(r"(?:先|再|然后|接着|帮我|请|把|打开|关闭|关掉|截|锁)", c.strip())) for c in earlier):
                continue   # “截一张屏，再关闭Chrome，截图留着做样例”：前面才是命令
            # “我正在看表格，系统亮度升一点”：开头说自己在干嘛，后面那句才是要我做的
            if name == "progressive" and any((_CMD_HINT_RE.search(c) or _device_rule(c.strip(), c.strip()))
                                             and not rx.search(c.strip()) for c in later):
                continue
            return name
    return None


# 执行前不需要追问的只读动作
_READONLY_ACTIONS = {"chat", "system_status", "doc_search", "diagnose_error"}
_CONFIRM_ASK = {
    "lock_screen": "要我现在锁屏吗？", "screenshot": "要我现在截个图吗？", "close_app": "要我把它关掉吗？",
    "send_chatapp": "要我现在就发吗？", "open_app": "要我现在打开吗？", "show_desktop": "要我切回桌面吗？",
    "volume_mute": "要我静音吗？", "media_control": "要我切一下吗？", "recall_chatapp": "要我撤回吗？",
    "openmuse_task": "要我现在让 OpenMuse 上网处理吗？",
    "opendots_task": "要我现在让 OpenDots 虚拟同事撰写吗？",
    "opendots_memory": "要我现在把这件事记入知识库吗？",
    "multiagent_task": "要我现在启动多智能体协同流水线吗？",
}


def _contact_in_text(contact, text):
    """联系人在原话里说过：原样出现，或者是别名表里同一组的说法，或者“我妈 / 我爸 / 我哥”对“老妈 / 老爸 / 老哥”。"""
    t = text or ""
    if contact in t:
        return True
    if re.fullmatch(r"老[妈爸哥姐弟妹婆公]", contact) and re.search(r"我(?:的)?" + contact[1] + r"|" + contact[1] * 2, t):
        return True
    try:
        from canaan_os_executor import CanaanOSExecutor
        aliases = (CanaanOSExecutor.load_contact_aliases() or {}).get("aliases", {})
    except Exception:
        aliases = {}
    for key, values in aliases.items():
        group = {key, *values} if isinstance(values, (list, tuple)) else {key}
        if contact in group and any(len(v) >= 2 and v in t for v in group):
            return True
    return False


def _contact_in_aliases(name):
    """联系人别名表里原样有这个名字（那就不拆）。"""
    try:
        from canaan_os_executor import CanaanOSExecutor
        aliases = (CanaanOSExecutor.load_contact_aliases() or {}).get("aliases", {})
    except Exception:
        return False
    return any(name == k or name in (v if isinstance(v, (list, tuple)) else []) for k, v in aliases.items())


def _same_sound_in(name, text):
    """“打开机事本”→ 记事本：程序名和原话里某一段读音一样（语音识别的同音错字）。只看 2～6 个汉字的名字。"""
    if not re.fullmatch(r"[一-鿿]{2,6}", name or ""):
        return False
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return False
    want = lazy_pinyin(name)
    chars = re.sub(r"[^一-鿿]", "", text or "")
    n = len(name)
    return any(lazy_pinyin(chars[i:i + n]) == want for i in range(len(chars) - n + 1))


def _sound_alike_app(name):
    """认不出的程序名（“记世本”“几事本”）按读音对内置程序名，读音完全一样且只有一个对得上时返回正确的名字。"""
    if not re.fullmatch(r"[一-鿿]{2,6}", name or ""):
        return None
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None
    want = lazy_pinyin(name)
    names = {n for n in _builtin_app_names() if re.fullmatch(r"[一-鿿]{2,6}", n)} | {cn for cn, _ in _CLOSE_MAP}
    hits = sorted(n for n in names if n != name and lazy_pinyin(n) == want)
    return hits[0] if len(hits) == 1 else None


def _message_grounded(message, text):
    """要发的消息必须来自用户原话（允许“他→你”这类人称转换），防止模型自己编一句“你好！”发出去。"""
    if not isinstance(message, str) or not message.strip():
        return False
    # Only the server's current typed slot scope can supply a complete literal
    # body when ordinary command parsing would treat its first quote as syntax.
    if _message_source_matches(message, text):
        return True

    def normalize(value):
        # 正文前可能带“问 / 说”这类引出词、分隔符，以及模型把“他”换成“你”多出的称呼；
        # 它们不是正文内容，比较前统一去掉，避免把正确的发送误判成空正文。
        # 去掉后不能为空（否则“问”“问一下”会退化成空串互相相等），空就保留原样。
        v = re.sub(r"[他她您]", "你", re.sub(r"[\s“”\"']", "", value)).lstrip("，,：:；;、")
        stripped = re.sub(r"^(?:问一下|问一声|问问|问)", "", v)
        if stripped:
            v = stripped
        stripped = re.sub(r"^你(?=.)", "", v)
        if stripped:
            v = stripped
        return v.rstrip("，,。.!！?？~～：:")
    parsed = fast_match_intent(text or "")
    if parsed and parsed.get("action") == "send_chatapp":
        return normalize(message) == normalize(json.loads(parsed["param"])["message"])
    # 规则解析不了的说法（“提醒老爸记得吃药”“跟我老婆说一声我今晚加班”）：正文是原话里一字不差的一段就算有依据。
    # 模型编不出原话里没有的字（“收到，谢谢”里多出的“谢谢”照样拦下）；发送前照样要主人确认。
    body, said = normalize(message), normalize(text or "")
    return len(body) >= 2 and body in said


# 打开/关闭/打开网址/打开文件夹：参数必须来自用户原话（直接出现，或经别名表对应），
# 否则是模型自己补的（“把它打开”→ Edge 浏览器），先问一句再做
_GROUNDED_ACTIONS = {"open_app", "focus_app", "open_chatapp", "close_app", "open_url", "open_path", "computer_use", "openmuse_task", "opendots_task", "run_cmd", "run_script"}
_BARE_VERB_RE = re.compile(r"^(?:团团|你)?(?:把它|把这个|把那个|给我)?(?:关|开|打开|关掉|关上|关闭|弄|搞|整)(?:了|掉|上|开|一下|下)?"
                           r"(?:吧|呗|啊|呀|嘛)?$")
_GENERIC_WORDS_RE = re.compile(r"软件|程序|应用|客户端|电脑版|网页版|官网|网站|文件夹|目录|https?|www|com|cn|net")
_FOLDER_PAIRS = [("下载", "download"), ("文档", "document"), ("桌面", "desktop"), ("图片", "picture"), ("照片", "picture"),
                 ("视频", "video"), ("音乐", "music"), ("机器鱼", "fishsim"), ("仿真目录", "fishsim"), ("工作区", "aaa"),
                 ("管家", "aaa")]
# 常见俗称 ↔ 参数里的英文名
_COMMON_ALIASES = [("b站", "bilibili"), ("哔哩哔哩", "bilibili"), ("谷歌", "google"), ("谷歌", "chrome"), ("油管", "youtube"),
                   ("游戏", "game")]


def _alias_pairs():
    pairs = [(cn, proc) for cn, proc in _CLOSE_MAP] + list(_FOLDER_PAIRS) + list(_COMMON_ALIASES)
    for cn, url in _SITES.items():
        dom = re.sub(r"^https?://(?:www\.|mail\.)?", "", url).split(".")[0]
        pairs.append((cn, dom))
    try:
        from canaan_os_executor import CanaanOSExecutor
        for k, vs in CanaanOSExecutor.APP_ALIASES.items():
            for v in vs:
                pairs.append((k.lower(), v.lower()))
    except Exception:
        pass
    return pairs


_ALIAS_CACHE = []


def _app_target_name(value):
    """Normalize only a complete application target, never words inside its name."""
    value = str(value or "").strip()
    if _QUOTED_SPAN_RE.fullmatch(value):
        value = value[1:-1].strip()
    value = re.sub(r"^(?:那个|这个|这款|整个|聊天工具|桌面上的)\s*", "", value)
    value = re.sub(r"(?:这个|那个|这款|聊天)?(?:软件|程序|应用|客户端|电脑版|PC版|窗口|界面|图标|进程)$|那玩意儿$|那个东西$|给我?$|帮我$", "", value, flags=re.I).strip()
    value = re.sub(r"浏览器$", "", value).strip() if value != "浏览器" else value
    return re.sub(r"\s+", "", value.casefold()).removesuffix(".exe")


def _app_alias_groups(action):
    from canaan_os_executor import CanaanOSExecutor
    from canaan_action_framework.actions.app_actions import _focus_process_names
    for name, aliases in CanaanOSExecutor.APP_ALIASES.items():
        group = (name, *aliases)
        families = {_focus_process_names(value) for value in group} - {frozenset()}
        if action == "open_app" or len({_app_target_name(value) for value in aliases}) == 1 or len(families) <= 1:
            yield {_app_target_name(value) for value in group}


def _app_target_equal(requested, proposed, action):
    first, second = _app_target_name(requested), _app_target_name(proposed)
    if not first or not second:
        return False
    if first == second:
        return True
    if action == "focus_app":
        from canaan_action_framework.actions.app_actions import _focus_process_names
        names = _focus_process_names(requested)
        if names and names == _focus_process_names(proposed):
            return True
    else:
        stable = {_app_target_name(name): _app_target_name(code) for name, code in _CLOSE_MAP}
        stable.update({"calculator": "calc", "chatapp": "chatapp", "msedge": "edge"})
        if stable.get(first, first) == stable.get(second, second):
            return True
    return any({first, second} <= group for group in _app_alias_groups(action))


def _app_request_targets(action, text):
    """Extract complete local app operands without entering the intent matcher."""
    text = _corrected_open_text(_space_clauses(text or "")) or text
    browsing = _query_source_decision(text) if action == "open_app" else None
    if browsing and browsing.get("action") == "open_app":
        return [browsing["param"]]  # Reuse the existing application-owned browsing purpose.
    targets = []
    whole = _operation_request_head(text or "").strip(" 。！!？?")
    closing = re.fullmatch(r"(?:不用(?P<before>.{1,30}?)了|(?P<after>.{1,30}?)(?:不用了|不弄了|别开着了|用完了))[，,\s]*(?:把它|就|帮我)?(?:退出去|退出|关掉|关了|关闭)(?:吧|呗)?", whole)
    if action == "close_app" and closing:
        return [closing["before"] or closing["after"]]
    for raw in _clauses(_space_clauses(text or "")):
        clean = _operation_request_head(raw).strip(" 。！!？?")
        clean = re.sub(r"^(?:麻溜|麻利点)\s*(?=把|将|让|打开|关闭)", "", clean)
        if not clean or _QUOTED_SPAN_RE.fullmatch(clean) or negative_request(clean) or mention_reason(raw) or _is_reminder_clause(clean) or re.match(r"^(?:如何|怎么|怎样)(?:才能|才可以|能|可以|样)?(?:把|让|打开|启动|关闭|开|关|退出)", clean):
            continue
        clarified = re.fullmatch(r"(?:要|是)(.+?)(?:那个|这个)", clean)
        if clarified:
            targets.append(clarified[1])
            continue
        tail = r"(?:一下|一会儿|下|吧|呗|呀|啊|了|就行|就好|看看|看一下|看看群消息|看看文档|让我看看群)*"
        target = r"(?P<app>[^，,。！!？?；;]+?)"
        if action == "focus_app":
            parsed = _foreground_app_decision(clean)
            if parsed and parsed.get("action") == "focus_app":
                targets.append(parsed["param"])
            continue
        if action == "open_app":
            # The existing phrase rule owns a short ASCII name followed by its purpose.
            purpose = re.match(r"(?:帮我)?开(?:一下|个|下)?\s*([A-Za-z][A-Za-z0-9]{1,15})\s*(?:来|去)?(?:写|做|改|看|画|算|编)", clean)
            if purpose:
                targets.append(purpose[1])
                continue
            patterns = (r"(?:打开|启动|拾掇开|开开|开启|调出|唤起|叫出|拉起|运行|登录|登|上|开)(?:一下|个|下)?\s*" + target + tail,
                r"(?:把|将|让)?" + target + r"(?:给我|帮我)?(?:打开|启动|拾掇开|开开|开一下|开|露个面|(?:叫|喊|唤|弹|调|弄|整)?[出起]来)" + tail)
        else:
            patterns = (r"(?:关[闭比]|关掉|关脱|整死|退出|结束|杀掉|收掉|关(?!机))(?:一下)?\s*" + target + tail,
                r"(?:把|将|让)?" + target + r"(?:(?:给我?|帮我|先|可以|全|都)|(?:别放了)?直接)*(?:关脱|关掉|关闭|整死|退了|关一下|关了|退掉|退出|结束|杀掉|收掉)" + tail)
        match = next((m for pattern in patterns if (m := re.fullmatch(pattern, clean, re.I))), None)
        if match:
            targets.append(match["app"].strip())
    return targets


def _app_grounding_names(action, param):
    proposed = _app_target_name(param)
    names = {proposed}
    for name, code in _CLOSE_MAP:
        for alias in (name, code):
            if _app_target_equal(alias, param, action):
                names.add(_app_target_name(alias))
    for group in _app_alias_groups(action):
        if proposed in group:
            names.update(group)
    return names - {""}


def _app_bounded_mentions(action, param, text, *, _names=None):
    """Keep free spoken frames, while names remain complete lexical operands."""
    names = _app_grounding_names(action, param) if _names is None else _names
    text = _corrected_open_text(_space_clauses(text or "")) or text
    before = r"(?:打开|启动|开开|开启|调出|唤起|叫出|拉起|运行|登录|登|上|开|点开|翻开|翻翻|关[闭比]|关掉|退出|关|结束|收掉|把|将|让|用|要|是|的|了|工具|软件|图标|客户端|那个|这个|一下|给我|帮我|替我|先|来|去|个|请|open|launch|start|run|close|exit|focus)\s*$"
    after = r"^\s*(?:(?:(?:这个|那个|这款|聊天)?(?:软件|程序|应用|客户端|电脑版|窗口|界面|图标|进程|下载器|浏览器)|那玩意儿|那个东西))?(?:$|给|帮|替|先|可以|不用|不弄|不要|别|全|整|开|启|打|点|翻|关|退|结|杀|收|调|唤|弄|放|写|做|改|看|画|算|编|录|剪|跑|解|停|用|要|我(?:要|想|得|来|准备)|了|哈|吧|呗|啊|呀|嘛|呢|吗|么|谢谢|那个|这个)"
    for raw in _clauses(_space_clauses(text or "")):
        clean = _operation_request_head(raw).strip(" 。！!？?")
        if negative_request(clean) or mention_reason(raw) or _is_reminder_clause(clean) or re.match(r"(?:给.{1,8}发|发.{0,5}(?:聊天软件|消息)|跟.{1,8}说|告诉|通知|转告|搜索|搜|查找|(?:如何|怎么|怎样)(?:才能|才可以|能|可以|样)?(?:把|让|打开|启动|关闭|开|关|退出))", clean):
            continue
        value = mask_quotes(clean).casefold()
        for name in names - {""}:
            suffix = r"(?:\.exe)?" if re.fullmatch(r"[a-z][a-z0-9_.+-]*", name) else ""
            pattern = r"(?<![a-z0-9_.+-])" + r"\s*".join(re.escape(char) for char in name) + suffix + r"(?!\s*[a-z0-9_.+-])"
            for match in re.finditer(pattern, value):
                left, right = value[:match.start()], value[match.end():]
                separated = bool(left and left[-1].isspace() and not re.search(r"[a-z0-9_.+-]\s*$", left))
                if (not left or separated or re.search(before, left)) and (not right or re.match(after, right)):
                    return True
    return False


def _app_target_grounded(action, param, text):
    targets = _app_request_targets(action, text)
    for target in targets:
        if _app_target_equal(target, param, action):
            return True
        if action in ("open_app", "close_app"):
            alike = _app_sound_alike_name(_app_target_name(target))
            if alike and _app_target_equal(alike, param, action):
                return True
    names = _app_grounding_names(action, param)
    for target in targets:
        name = _app_target_name(target)
        separate_ascii = any(re.fullmatch(r"[a-z][a-z0-9_.+-]*", alias) and
                             re.search(r"[一-鿿].*\s+" + re.escape(alias) + r"(?:给我|帮我)?$", target, re.I)
                             for alias in names)
        if _known_application(target) or _known_application(name) or not separate_ascii and any(name != alias and name.endswith(alias) for alias in names):
            return False  # A complete different name cannot become a suffix app alias.
        if any(name.startswith(alias) and re.search(r"版|增强|专业", name[len(alias):]) for alias in names):
            return False
    return _app_bounded_mentions(action, param, text, _names=names)


def _app_close_target_code(target):
    """Only a complete close-map alias can select its process identifier."""
    return next((code for name, code in _CLOSE_MAP if _app_target_equal(target, name, "close_app")), None)


def _app_sound_alike_name(name):
    """A unique whole-name homophone among declared application names/aliases."""
    if not re.fullmatch(r"[一-鿿]{2,24}", name or "") or _known_application(name):
        return None
    try:
        from pypinyin import lazy_pinyin
        from canaan_os_executor import CanaanOSExecutor
    except ImportError:
        return None
    names = set(_builtin_app_names()) | {cn for cn, _ in _CLOSE_MAP}
    for key, aliases in CanaanOSExecutor.APP_ALIASES.items():
        names.update((key, *aliases))
    want = lazy_pinyin(name)
    hits = sorted(value for value in names if value != name and len(value) == len(name)
                  and re.fullmatch(r"[一-鿿]{2,24}", value) and lazy_pinyin(value) == want)
    return hits[0] if len(hits) == 1 else None


def _app_literal_request(text):
    """A complete quoted app instruction is data; outside clauses remain requests."""
    clean = _operation_request_head(text or "").strip(" 。！!？?")
    if not _QUOTED_SPAN_RE.fullmatch(clean):
        return None
    if _named_site_literal(clean) or _state_target_literal(clean):
        return None  # Existing website and state literal domains retain their own rules.
    inner = clean[1:-1].strip()
    targets = _app_request_targets("open_app", inner) + _app_request_targets("close_app", inner)
    named = any(re.fullmatch(r"[A-Za-z0-9_.+ 一-鿿-]{2,80}", value) and
                (not _NOT_APP_RE.fullmatch(value) or _known_application(value)) for value in targets)
    focus = re.fullmatch(r"(?:(?:把|将|让)?[A-Za-z0-9_.+ 一-鿿-]{2,80}?(?:窗口|界面)?(?:切到|调到|弄到|置于)(?:最前面|前面|最前台|前台)"
                         r"|(?:把|将|让)?[A-Za-z0-9_.+ 一-鿿-]{2,80}?(?:窗口|界面)?置前"
                         r"|(?:切换到|切到|激活|聚焦|唤出)[A-Za-z0-9_.+ 一-鿿-]{2,80})", inner)
    if named or focus:
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "application_literal", "_pending": None,
                "reply": "这段引用不会作为当前应用操作。"}
    return None


def param_grounded(action, param, text):
    """动作参数能否在原话里找到依据。只检查 _GROUNDED_ACTIONS，其余一律视为有依据。"""
    if action == "open_chatapp":
        return param_grounded("open_app", "聊天软件", text)   # 空参数不代表可以省略对隐含目标的核对。
    if action in ("open_app", "focus_app", "close_app"):
        return _app_target_grounded(action, param, text)
    scoped_param = param
    if action in ("open_path", "search_file") and isinstance(param, str):
        try:
            scoped_param = json.loads(param)
        except (ValueError, TypeError):
            pass
    if action in ("open_path", "search_file"):
        requested_scopes = []
        for clause in _clauses(_strip_prefix(_space_clauses(text))):
            scoped = _local_file_decision(_strip_prefix(_STEP_ORDER_RE.sub("", clause).strip()))
            if scoped and isinstance(scoped.get("param"), dict) and "root" in scoped["param"]:
                requested_scopes.append(scoped)
        if requested_scopes or isinstance(scoped_param, dict) and "root" in scoped_param:
            return any(scoped.get("action") == action and scoped["param"] == scoped_param for scoped in requested_scopes)
    if action not in _GROUNDED_ACTIONS:
        return True
    t = re.sub(r"\s", "", (text or "").lower())
    p = re.sub(r"\s", "", str(param or "").lower())
    if not p:
        return False
    if action == "run_cmd":
        parsed = fast_match_intent(text or "")
        return p in t or bool(parsed and parsed.get("action") == action and parsed.get("param") == param)
    if action == "run_script":
        return p.replace("\\", "/") in t.replace("\\", "/")
    if action in ("computer_use", "openmuse_task", "opendots_task"):
        return p in t
    if action == "open_path":
        known = any(cn in p or en in p for cn, en in _FOLDER_PAIRS)
        drive = re.match(r"^([a-z])(?::|盘)", p)
        if drive and p.rstrip(":\\/盘") == drive.group(1) and (drive.group(1) + "盘" in t or drive.group(1) + ":" in t):
            return True
        if not (known or drive or "\\" in p or "/" in p):
            return False  # 不是已知文件夹、盘符或路径（“打开几事本”→ open_path 几事本）
        if known and not ("\\" in p or "/" in p) and any(cn in t for cn, en in _FOLDER_PAIRS if cn in p or en in p):
            return True   # 模型写成 downloads / 下载文件夹，原话里说了“下载”
        if "\\" in p or "/" in p:
            from canaan_action_framework.actions.app_actions import OpenPathAction
            if p.replace("\\", "/") in t.replace("\\", "/"):
                return True
            return any(any(name in t for name in names) and p.replace("\\", "/") == resolve().lower().replace("\\", "/")
                       for names, resolve in OpenPathAction.SHORTCUTS.items())
    if action != "open_url" and p in t:
        return True
    if action == "open_url":
        from urllib.parse import urlsplit
        try:
            url = urlsplit(p)
            url.port
        except ValueError:
            return False
        if not url.hostname or url.scheme not in ("http", "https"):
            return False
        # 只认网址字符：原来的 [^\s] 会把后面紧跟的中文（“…/training这个测试网址打开一下”）算进网址
        explicit = _URL_RE.findall(text or "")
        if explicit:
            return any(str(param).rstrip("/") == value.rstrip("，。！；!;").rstrip("/") for value in explicit)
        if url.path not in ("", "/") or url.query or url.fragment or url.username or url.password:
            return False
        host = url.hostname or ""
        if re.search(r"(?<![a-z0-9_.-])" + re.escape(host) + r"(?![a-z0-9_.-])", t):
            return True
        for clause in _clauses(_space_clauses(text or "")):
            requested = _named_site_request(clause)
            if requested and requested.get("action") == "open_url" and p.rstrip("/") == requested["param"].casefold().rstrip("/"):
                return True
        return any(name in t and host.removeprefix("www.") == brand + ".com"
                   for name, brand in _COMMON_ALIASES if brand in ("google", "youtube"))
    name = re.sub(r"浏览器$", "", _GENERIC_WORDS_RE.sub("", p)).removesuffix(".exe")
    words_p = re.findall(r"[a-z0-9_]+|[一-鿿]+", name)
    def mentioned(value):
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(value) + r"(?![a-z0-9_])", t))
    if words_p and all(mentioned(word) for word in words_p):
        return True
    if not _ALIAS_CACHE:
        _ALIAS_CACHE.extend(_alias_pairs())
    for a, b in _ALIAS_CACHE:
        if (a and mentioned(a) and name == b) or (b and len(b) >= 2 and mentioned(b) and name == a):
            return True
    if action in ("open_app", "close_app") and _same_sound_in(name, text) and \
            (_find_application(name) or name.casefold() in _builtin_app_names()):
        return True   # 同音错字，而且这个程序确实存在（“打开机事本”→ 记事本）
    if action == "close_app" and any(code == name and _same_sound_in(cn, text) for cn, code in _CLOSE_MAP):
        return True   # 进程名（notepad）对应的中文名和原话里同音（“几事本关了”）
    return False


def negative_request(text):
    clean = _strip_prefix(mask_quotes(_space_clauses(text)))
    # 最后一个分句是否定（“我先不走了，别锁屏”）：前面的分句只是铺垫，整句按否定处理
    last = [s for s in re.split(r'[，,。；;！!]', clean) if s.strip()]
    last = last[-1].strip() if last else ""
    if re.fullmatch(r"(?:把)?(?:音乐|歌|视频|歌曲)(?:先)?(?:别放了|别播了|不要放了|先别放了?)(?:吧|呗)?[。！!]*", clean):
        return False   # “歌别放了”是让它停，不是让我别动
    if re.match(r"^(?:先)?别(?:出声|吵)", clean) or re.fullmatch(r"(?:别放了|别播了)[，,]?(?:先)?停(?:一下|下)?(?:吧)?[。！!]*", clean):
        return False   # “先别出声，静个音”“别放了，先停”：要安静 / 要停，不是让我别动
    if re.fullmatch(r"不用.{1,8}?了[，,]?(?:把它|就)?(?:关掉|关了|关闭)(?:吧|呗)?[。！!]*", clean):
        return False   # “不用剪映了，关掉吧”：不用了所以要关
    clauses = _clauses(clean)
    if clauses and all(_cancelled_clause(c) for c in clauses):
        return True
    return clean != "别吵" and bool(re.search(r'^(?:(?:先|你|可|暂时|现在|今天|这次|千万|一定)\s*)*(别|不要|不用|甭|不必|无需|没必要|不可以|不能)', clean)
                                  or any(_cancelled_clause(c) for c in clauses)
                                  or (last != clean and last != "别吵" and re.match(
                                      r'^(?:先|就|千万|可|也)?(别|不要|不用)(?!问|管|担心|客气|急|怕|另|添|加|多加|再问|确认|核对|解释|再核对|再确认)', last)))


_HESITATE_RE = re.compile(r"拿不定|拿不准|纠结|犹豫|选不出|说混|讲混|搞混|弄混|绕一块|没想好|没想妥|还没定|糊涂|没拿定|没拿准|没决定|没想定|"
                          r"两条都算|两句我都要算数|都算我的要求|(?:数字|数|后边|后面)[^，,。]{0,4}想不起来|(?:还是|或者)[^，,。]{0,12}(?:呢|吧)?[，,]?\s*(?:我|俺)?(?:还)?(?:没|不)(?:想|定|决定)")
# 低风险只读动作：做错了也只是打开一个搜索页，置信度门槛可以低一点（参数必须来自原话）
_LOW_RISK_READ = {"web_search"}
_WEATHER_RE = re.compile(r"天气|下雨|下雪|冷不冷|热不热|气温|温度|多少度|刮风|带伞")
_WEATHER_NOISE_RE = re.compile(r"今天|明天|后天|大后天|现在|这会儿|今晚|明早|早上|上午|中午|下午|晚上|这周末?|周末|这几天|一会儿|"
                               r"帮我|给我|麻烦|请|查查|查下|查一下|查|看看|看下|搜一下|搜|问一下|"
                               r"天气|下雨|下雪|冷不冷|热不热|气温|温度|多少度|刮风|带伞|会不会|会|要不要|需要|"
                               r"啥|什么|怎么样|咋样|如何|怎样|吗|呢|吧|呀|啊|的|了|是|有没有|"
                               r"外面|这里|这边|我们这儿?|本地|家里|这儿|那边|那儿|网页上?|网上|上网|浏览器里?|出门前?|想看看|看一眼")


def _vague_query(param, text):
    """搜索词是空的或者只是“东西 / 资料”，或者原话里搜索对象只说了一半（“关于……”）。"""
    q = re.sub(r"[“”\"「」『』\s。.!！?？]", "", str(param or ""))
    if q in ("", "东西", "点东西", "些东西", "资料", "一下", "内容", "点资料", "网页"):
        return True
    return bool(re.search(r"关于(?:……|\.\.\.|…)|“[^”]*(?:……|…)[^”]*”|搜点东西|查点东西|搜个东西", text or ""))


def _weather_place(text):
    """天气问句里有没有说地方：去掉时间、语气、天气词和“外面 / 这里”后，还剩两个以上汉字就算说了地方。"""
    rest = _WEATHER_NOISE_RE.sub("", text or "")
    return len(re.findall(r"[一-鿿]", rest)) >= 2 or bool(re.search(r"[A-Za-z]{3,}", rest))


def confidence_threshold(decision, text, base):
    """模型置信度门槛：低风险只读动作（只是打开搜索页）且参数都来自原话时放低到 0.3，少问一句；其余照旧。"""
    if decision.get("action") in _LOW_RISK_READ and _search_words_grounded(decision.get("param", ""), text):
        try:
            import canaan_config
            low = getattr(canaan_config, "INTENT_MIN_CONFIDENCE_LOW_RISK", .3)
        except Exception:
            low = .3
        return min(base, low)
    return base


def _search_words_grounded(param, text):
    chars = [c for c in str(param or "") if c.isalnum()]
    return bool(chars) and sum(c in (text or "") for c in chars) / len(chars) >= 0.8


def _app_launch_without_briefing_request(text):
    outer = mask_quotes(text or "")
    return bool(re.search(r"打开|启动|开开|运行", outer) and not re.search(
        r"聊天软件|chatapp|chatapp|(?<![a-z])imapp(?![a-z])|总结|汇总|提炼|整理|早报|待办|汇报", outer, re.I))


def _guard_multi_action_steps(param):
    """Read exactly the list/JSON/double-JSON plans accepted by the executor."""
    steps = param
    try:
        for _ in range(2):
            if not isinstance(steps, str):
                break
            steps = json.loads(steps)
    except (TypeError, ValueError):
        return []
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        return []
    normalized = []
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("action"), str):
            return []
        action = step["action"].strip().lower()
        if not action or action == "multi_action":
            return []
        from canaan_action_framework import ActionRegistry
        registered = ActionRegistry.get_action(action)
        normalized.append({**step, "action": registered.name if registered else action})
    return normalized


def _plan_step_identity(step):
    from canaan_action_framework import ActionRegistry
    name = str(step.get("action", "")).strip().lower()
    registered = ActionRegistry.get_action(name)
    name = registered.name if registered else name
    value = step.get("param", "")
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if name == "open_chatapp":
        return "open_app", "聊天软件"
    if isinstance(value, dict) and set(value) == {"value"}:
        value = value["value"]
    return name, value


def _plan_app_identity(value):
    name = str(value).strip().lower().removesuffix(".exe")
    stable = {cn.lower(): proc for cn, proc in _CLOSE_MAP if cn not in ("浏览器", "音乐")}
    stable.update({"calculator": "calc", "chatapp": "chatapp", "聊天软件": "chatapp", "msedge": "edge"})
    return stable.get(name, name)


def _plan_contact_equal(left, right):
    if left == right:
        return True
    def family(name):
        name = re.sub(r"^我的?", "", name)
        if name in ("老婆", "妻子"):
            return "spouse_wife"
        if name in ("老公", "丈夫"):
            return "spouse_husband"
        match = re.fullmatch(r"(?:老)?([妈爸哥姐弟妹])\1?", name)
        return match[1] if match else None
    if family(left) and family(left) == family(right):
        return True
    from canaan_os_executor import CanaanOSExecutor
    load_aliases = getattr(CanaanOSExecutor, "load_contact_aliases", None)  # 公开版没有聊天软件执行器，也就没有别名表
    for key, values in ((load_aliases() if load_aliases else None) or {}).get("aliases", {}).items():
        group = {key, *(values if isinstance(values, (list, tuple)) else [])}
        if left in group and right in group:
            return True
    return False


def _plan_task_text(value):
    value = str(value or "").strip()
    automatic = _auto_ui_decision(_strip_prefix(value))
    if automatic and automatic.get("action") == "ufo_task":
        value = automatic["param"]
    return tuple(_strip_prefix(_STEP_ORDER_RE.sub("", part.strip())) for part in _clauses(_space_clauses(value)))


def _plan_comparison_now():
    from datetime import datetime
    return datetime.now()


def _plan_numeric_value(action, value):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if text == "":
        if action in ("volume_up", "volume_down"):
            return "keys", 5
        return ("default", action) if action in ("brightness_up", "brightness_down") else None
    if text.startswith("delta:"):
        sign = "+" if action == "brightness_up" else "-" if action == "brightness_down" else None
        match = re.fullmatch(r"delta:([+-])(\d{1,3})", text)
        return ("delta", int(match[2])) if match and match[1] == sign and 0 < int(match[2]) <= 100 else None
    percent = "" if action in ("volume_up", "volume_down") else "%?"
    if not re.fullmatch(r"\+?\d+(?:\.0+)?" + percent, text):
        return None
    digits = text.rstrip("%").split(".", 1)[0].lstrip("+0") or "0"
    if len(digits) > 3:
        return None
    number = int(digits)
    if action in ("volume_up", "volume_down"):
        return ("keys", number) if 1 <= number <= 50 else None
    return ("target", number) if 0 <= number <= 100 else None


def _plan_folder_identity(value):
    import ntpath
    from canaan_action_framework.actions.app_actions import OpenPathAction
    name = str(value).strip().casefold()
    for aliases in OpenPathAction.SHORTCUTS:
        aliases = tuple(str(item).casefold() for item in aliases)
        if name in aliases:
            return "shortcut", aliases
    return "path", ntpath.normcase(ntpath.normpath(str(value).strip()))


def _plan_step_equal(expected, proposed, source, now=None):
    action, want = _plan_step_identity(expected)
    actual, got = _plan_step_identity(proposed)
    if action != actual:
        return False
    if action == "set_brightness" and want in ("min", "max"):
        return isinstance(got, str) and got == want
    if action in ("volume", "set_brightness", "volume_up", "volume_down", "brightness_up", "brightness_down"):
        first, second = _plan_numeric_value(action, want), _plan_numeric_value(action, got)
        return first is not None and first == second
    if want == got:
        return True
    if action == "open_url":
        named = _named_site_request(source)
        root = _named_site_root_key(want)
        return bool(named and named.get("action") == "open_url" and root and
                    root == _named_site_root_key(named["param"]) == _named_site_root_key(got))
    if action == "send_chatapp":
        if not isinstance(want, dict) or not isinstance(got, dict):
            return False
        contact, body = got.get("contact"), got.get("message")
        if not isinstance(contact, str) or not isinstance(body, str) or not _plan_contact_equal(want.get("contact", ""), contact):
            return False
        original = want.get("message", "")
        if any(q.group(0)[1:-1].strip() == original for q in _QUOTED_SPAN_RE.finditer(source)):
            return body == original
        normalize = lambda text: re.sub(r"[他她您]", "你", str(text).strip().rstrip("，,。.!！?？~～"))
        return normalize(body) == normalize(original)
    if action == "focus_app":
        from canaan_action_framework.actions.app_actions import _focus_process_names
        names = _focus_process_names(want)
        return bool(names) and names == _focus_process_names(got)
    if action in ("open_app", "close_app"):
        return _plan_app_identity(want) == _plan_app_identity(got)
    if action == "media_control":
        modes = {"next": "next", "下一首": "next", "下一曲": "next", "prev": "prev", "previous": "prev", "上一首": "prev", "上一曲": "prev",
                 "play_pause": "play_pause", "pause": "pause", "play": "play"}
        return str(want) in modes and modes.get(str(want)) == modes.get(str(got))
    if action == "set_reminder":
        import canaan_proactive
        value = lambda item: str(item.get("value") or item.get("text") or "") if isinstance(item, dict) else str(item)
        now = now or _plan_comparison_now()
        first = canaan_proactive.parse_reminder(value(want), now)
        return first is not None and first == canaan_proactive.parse_reminder(value(got), now)
    if action in ("open_path", "search_file"):
        if isinstance(want, dict) or isinstance(got, dict):
            return want == got  # root/value/literal all belong to the target contract.
        return _plan_folder_identity(want) == _plan_folder_identity(got)
    if action in ("ufo_task", "computer_use", "openmuse_task", "opendots_task"):
        return _plan_task_text(want) == _plan_task_text(got)
    return str(want).strip() == str(got).strip()


def _reminder_time_slots(text):
    """Locate top-level self-reminder steps, retaining their original insertion offsets."""
    if mention_reason(text) in ("hypothetical", "reported", "quoted_report", "past", "past_story"):
        return []
    slots, owner, cursor = [], None, 0
    for raw in _clauses(_space_clauses(text)):
        raw = raw.strip()
        start = text.find(raw, cursor)
        if start < 0:
            continue
        gap = text[cursor:start]
        cursor = start + len(raw)
        clean = _strip_prefix(_STEP_ORDER_RE.sub("", raw))
        outer = _QUOTED_SPAN_RE.sub(lambda m: "Q" * len(m.group(0)), clean)
        match = re.fullmatch(r"(?P<when>.*?)提醒(?:一下|一声)?我(?P<body>.+)", outer, re.S)
        header = match["when"].strip() if match else None
        frame = match and (not header or re.fullmatch(r"[\d零一二两三四五六七八九十百半年月日号点时分秒钟头个以后周星期礼拜天早晚上下午中午凌晨傍晚今明后大下本这过再的\s:：]+", header))
        if owner and not (owner == "search" and frame and re.search(r"然后|再|接着|之后|最后", gap)):
            continue
        if frame:
            offset = start + raw.find(clean)
            slots.append({"start": offset, "verb": offset + match.start("when") + len(match["when"]),
                          "header": match["when"], "text": clean})
            owner = None
        elif _diagnostic_content_intro(raw):
            owner = "search" if _web_search_decision(raw) else "payload"
    return slots


def _reminder_time_missing_decision(text):
    outer = _strip_prefix(mask_quotes(text))
    mentioned = re.search(r"提醒(?:过|了|一下|一声)?我", text)
    current_tail = re.search(r"(?:然后|再|接着|之后|最后)[，,\s]*(?:现在|此刻)", outer)
    if mentioned and not current_tail:
        if _QUOTED_SPAN_RE.fullmatch(text.strip()) or mention_reason(text) in ("hypothetical", "reported", "quoted_report", "past_story") or re.match(r"^(?:我)?(?:昨天|前天|上次|之前|刚才).{0,12}提醒(?:过|了)?我", outer):
            return {"action": "chat", "param": "", "_guard": "reminder_inactive", "_pending": None, "reply": "这段提醒内容不会作为当前操作执行。"}
        negative = _NEG_CLAUSE_RE.sub("", outer, count=1)
        if negative != outer and re.match(r"提醒(?:一下|一声)?我(?:先|首先|第一步)", negative):
            return dict(_NEGATED_REPLY)
    slots = _reminder_time_slots(text)
    missing = [slot for slot in slots if not _is_reminder_clause(slot["text"])]
    if not missing:
        return None
    if len(missing) > 1:
        return {"action": "chat", "param": "", "_guard": "reminder_time_multiple", "_pending": None,
                "reply": "有多个提醒还没定时间，请逐条说明完整提醒和各自时间。"}
    return {"action": "chat", "param": "", "emotion": "confident", "_guard": "reminder_time_missing", "_pending": None,
            "reply": "什么时候提醒你？请说具体时间，比如二十分钟后或明早八点。"}


def _merge_reminder_time(answer, original):
    missing = [slot for slot in _reminder_time_slots(original) if not _is_reminder_clause(slot["text"])]
    if len(missing) != 1:
        return original
    slot = missing[0]
    position = slot["verb"]
    candidate = original[:position] + answer + original[position:]
    local = slot["header"] + answer + slot["text"][len(slot["header"]):]
    return candidate if _is_reminder_clause(local) else original


def _sequenced_reminder_parts(text):
    """A timed '提醒我先…再…' owns that future sequence until an explicit now-step."""
    outer = _QUOTED_SPAN_RE.sub(lambda match: "Q" * len(match.group(0)), text or "")
    intro = re.search(r"提醒我[：:，,\s]*(?:首先|第一步|先)", outer)
    if not intro:
        return None
    header = _strip_prefix(outer[:intro.start()])
    if _CMD_HINT_RE.search(header) or _RELAY_RE.search(header) or re.search(r"输入|写入|填写|填入|写一句|写一段", header) or \
            negative_request(outer[:intro.end()]) or mention_reason(outer):
        return None
    boundary = re.search(r"[，,；;\s]*(?:然后|再|接着|之后|最后)[，,\s]*(?:现在|此刻)(?:就|立刻|立即|马上)?[，,\s]*", outer[intro.end():])
    later_reminders = [slot for slot in _reminder_time_slots(text)
                       if slot["start"] > intro.end() and slot["header"] and _is_reminder_clause(slot["text"])]
    if later_reminders and (not boundary or later_reminders[0]["start"] < intro.end() + boundary.start()):
        start = later_reminders[0]["start"]
        request = re.sub(r"[，,；;\s]*(?:然后|再|接着|之后|最后)[，,\s]*$", "", text[:start]).rstrip(" ，,；;")
        tail = text[start:].strip()
    elif boundary:
        start, end = intro.end() + boundary.start(), intro.end() + boundary.end()
        request, tail = text[:start].rstrip(" ，,；;"), text[end:].strip()
    else:
        request, tail = text.strip(), ""
    return (request, tail) if _is_reminder_clause(request) else None


def _prefix_sequenced_reminder(text):
    """Keep a future reminder's body together after independent current operations."""
    for slot in _reminder_time_slots(text):
        if slot["start"] and _is_reminder_clause(slot["text"]) and re.search(r"提醒(?:一下|一声)?我(?:先|首先|第一步)", mask_quotes(slot["text"])):
            prefix = re.sub(r"[，,；;\s]*(?:然后|再|接着|之后|最后)[，,\s]*$", "", text[:slot["start"]]).rstrip(" ，,；;")
            if prefix and _plan_explicit_request(prefix, None):
                return prefix, text[slot["start"]:]
    return None


def _plan_whole_owned_source(text, reference=None):
    """Whether the whole request is one payload, rather than independently actionable units."""
    reference = reference if reference is not None else fast_match_intent(text)
    if not reference:
        return False
    action = reference.get("action")
    if action == "send_chatapp":
        clean = _strip_prefix(text)
        quoted = _quoted_chatapp_decision(clean)
        spans = list(_QUOTED_SPAN_RE.finditer(clean))
        if quoted and len(spans) == 1:
            tail = re.sub(r"^[\s，,；;。]*(?:然后|再|接着|之后|最后|顺便)?\s*", "", clean[spans[0].end():])
            if tail and _plan_explicit_request(tail, None):
                return False
        return True  # Unquoted message text owns its sequence words and action names.
    if action in ("set_reminder", "web_search"):
        return True
    if action == "diagnose_error":
        return diagnostic_request_text(text) == text
    return action == "search_file" and _contextual_file_search_request(text) is not None


def _plan_app_has_additional_task(source):
    """A launch cannot stand in for a requested operation inside the application."""
    clean = _strip_prefix(mask_quotes(source))
    if not re.search(r"打开|启动|开开|开启|调出|唤起|拉起", clean):
        return False
    clean = re.split(r"我(?:要|想|准备)|用于|用来|为了", clean, maxsplit=1)[0]
    operation = r"新建|创建|建(?:个|一)|画|写|整理|制作|生成|编辑|计算|输入|保存|导出"
    return bool(re.search(r"(?:并(?:且)?|同时|顺便|然后|接着|之后|再)\s*(?:帮我|给我|替我)?(?:" + operation + r")", clean) or
                re.search(r"(?:建(?:个|一)|画(?:个|一)|写一(?:份|篇|段))", clean))


def _plan_calculation_request(source):
    clean = _operation_request_head(source).strip(" 。！!")
    if re.fullmatch(r"(?:计算|算)(?:一下|一算|个|点|点儿|一些)?(?:账|东西|数字|数|题|数学题)(?:吧|呗)?", clean):
        return False  # A generic purpose has no independent calculation input yet.
    return bool(re.match(r"^(?:用计算器(?:里|中)?|计算器(?:里|中)?)?(?:计算(?:一下|下)?|算(?:一下|一算|下)?|求(?:一下)?)"
                         r"(?!了|是|机)(?=.+)", clean))


def _media_selection_decision(text):
    """Media keys control current playback; they cannot select named content or genres."""
    if mention_reason(text) or _literal_media_request(_space_clauses(_strip_prefix(text))):
        return None
    split = _prefix_sequenced_reminder(text)
    if split:
        return _media_selection_decision(split[0]) or _media_selection_decision(split[1])
    future = _sequenced_reminder_parts(text)
    if future:
        return _media_selection_decision(future[1]) if future[1] else None
    owner = False
    for part in _clauses(_space_clauses(text)):
        clean = _operation_request_head(_STEP_ORDER_RE.sub("", part.strip())).strip(" 。！!")
        if negative_request(clean):
            continue
        if owner:
            continue
        if _quoted_chatapp_decision(clean) or _is_reminder_clause(clean):
            continue
        if _diagnostic_content_intro(clean):
            owner = not bool(_web_search_decision(clean))
            continue
        clean = re.sub(r"(?:好|行|可以)(?:吗|么)[？?]*$", "", clean)
        clean = re.sub(r"(?<=暂停)(?:一下|一会儿)?我(?:要|先|得)?(?:接|打)(?:个|一通)?电话[吧呗了]*$", "", clean)
        if re.match(r"切|换", clean) and (re.search(r"窗口|前台|前面", clean) or
                not re.search(r"上一首|下一首|上一曲|下一曲|音乐|歌曲|歌单|\b(?:music|song|next|prev)", clean, re.I)):
            continue  # Switching applications/pages is not selecting media content.
        match = re.match(r"^(?:给我|帮我|我想听|我想放)?(?:把|将)?(?:音乐播放器|播放器|音乐|歌曲|曲子|歌)?(?:先)?"
                         r"(?:(?:继续|恢复|接着)?(?:播放(?!器)|放|播(?!放器)|听(?!不|过|完|了|着|得|起来|上去))|play\b|pause\b|暂停(?:播放)?|换(?:回|成|到)?|切(?:换)?(?:回|到)?|下一首|上一首|下一曲|上一曲)\s*(.*)$", clean, re.I)
        if not match:
            continue
        content = match[1].strip()
        generic = r"(?:(?:一?点儿?|一?首|一?段|几首|些|个|一下|一会儿?|会儿|当前|现在|这个|那个|这首|那首|正在|的|音乐|歌曲|歌|曲子|视频|音频|播放列表|上一首|下一首|上一曲|下一曲|它|前面那首|前一首|刚才那首|再听一遍|再放一遍|再播一遍|听听|吧|呗|啊|呀)|\s|music|songs?|audio|video|current)*"
        if content and not re.fullmatch(generic, content, re.I):
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "media_selection_unsupported", "_pending": None,
                    "reply": "当前只支持控制正在播放的媒体，不能按歌名或类型选曲。请先选好内容，或明确控制当前播放。"}
    return None


def _plan_source_units(text, *, _reference=Ellipsis):
    split = _prefix_sequenced_reminder(text)
    if split:
        return _plan_source_units(split[0]) + _plan_source_units(split[1])
    future = _sequenced_reminder_parts(text)
    if future:
        request, tail = future
        return [(request, {"action": "set_reminder", "param": request})] + (_plan_source_units(tail) if tail else [])
    whole = fast_match_intent(text) if _reference is Ellipsis else _reference
    if _plan_whole_owned_source(text, whole):
        return [(text, whole)]
    state_target = _state_target_request(text)
    if state_target:
        return [(text, {key: value for key, value in state_target.items() if key != "_check_text"})]
    units = []
    normalized = _strip_prefix(_asr_fix(_space_clauses(text)))
    for part in _coalesce_state_clauses(normalized, _clauses(normalized)):
        clause = _strip_prefix(_STEP_ORDER_RE.sub("", part.strip()))
        message = _quoted_chatapp_decision(clause)
        if not message and (mention_reason(clause) or _SELF_STATE_RE.match(clause) or _completed_clause(mask_quotes(clause)) or _BACKGROUND_RE.fullmatch(clause)):
            continue
        if _cancelled_clause(mask_quotes(clause)):
            continue
        if not _CMD_HINT_RE.search(mask_quotes(clause)) and re.match(r"^[^，,。]{1,24}(?:里|上|中)(?:有|是|显示|放着|保存着)", clause):
            continue  # A location/existence statement supplies context, not a missing operation.
        if _SEARCH_QUALIFIER_RE.search(clause) and units and units[-1][1] and units[-1][1].get("action") == "web_search":
            continue
        reference = message or fast_match_intent(clause)
        if reference and reference.get("action") in ("open_app", "open_chatapp") and _plan_app_has_additional_task(clause):
            reference = None
        if reference and reference.get("action") == "chat":
            if not _CMD_HINT_RE.search(mask_quotes(clause)) and not re.search(r"[？?]|几点|什么|多少", clause):
                continue
            reference = None
        units.append((clause, reference))
    return units


def _plan_local_action_intent(action, source):
    clean = _strip_prefix(mask_quotes(source)).strip(" 。！!")
    if action == "focus_app":
        focus = _foreground_app_decision(clean)
        return bool(focus and focus.get("action") == "focus_app")
    if action == "open_app":
        return not _plan_app_has_additional_task(source) and bool(re.match(r"打开|启动|开开|开启|调出|唤起|叫出|拉起|开(?!会|车|机)", clean) or
                    re.search(r"(?:打开|启动|开开|开|露个面|(?:叫|喊|唤)?出来)(?:一下|下|吧|呗)?$", clean))
    if action == "close_app":
        return bool(re.match(r"关闭|关掉|退出|结束|杀掉|收掉", clean) or re.search(r"关掉|关闭|退出|结束|退了|收掉", clean))
    if action == "open_path":
        return bool(re.match(r"打开|进入|进去|去|定位|浏览|跳到", clean) or re.search(r"打开|开开|展示出来", clean))
    if action == "open_url":
        return bool(_named_site_request(source) or re.match(r"打开|访问|上|去|逛|进|浏览|open|visit", clean, re.I) or re.search(r"打开|访问|浏览", clean))
    if action in ("run_script", "run_cmd"):
        return bool(re.search(r"运行|执行|跑|命令|powershell|\brun\b", clean, re.I))
    return False


def _plan_explicit_request(source, reference):
    """A source unit needs its own operation cue; a symptom or purpose is not a step."""
    clean = _operation_request_head(mask_quotes(source)).strip(" 。！!")
    if _state_target_request(source):
        return True
    if reference and reference.get("action") == "send_chatapp":
        return True
    if reference and reference.get("action") in ("ufo_task", "computer_use") and \
            _plan_task_text(reference.get("param")) == _plan_task_text(source):
        ui = _in_app_ui_decision(source) or _auto_ui_decision(source)
        if ui and ui.get("action") in ("ufo_task", "computer_use"):
            return True  # Request ownership is separate from the driver's capability preflight.
    if _plan_calculation_request(source):
        return True
    if _screenshot_clause_request(clean) or _foreground_app_decision(clean):
        return True
    if _CMD_HINT_RE.match(clean) or re.match(r"(?:把|将|让|新建|创建|输入|保存|建个|画个)", clean):
        return True
    if _plan_local_action_intent("open_app", clean):
        return True  # Object-first launch requests remain requests even for an unknown app.
    if reference:
        action = reference.get("action")
        if action == "set_reminder":
            return _is_reminder_clause(clean)
        if action in ("open_app", "close_app", "open_path", "open_url", "run_cmd", "run_script"):
            return _plan_local_action_intent(action, clean)
        if action in ("volume_up", "volume_down", "brightness_up", "brightness_down", "volume", "set_brightness", "volume_mute"):
            return bool(re.search(r"调|设|静音|(?:大|小|亮|暗|响)(?:一?点|些|两格)|往[上下]|压一压", clean))
        if action == "media_control":
            return bool(re.search(r"换|切|播放|暂停|继续|下一首|上一首|下一曲|上一曲", clean))
    return False


def _contextual_file_search_request(text):
    """A coarse bare-search fallback must not erase an explicit file-location context."""
    parts = _clauses(_space_clauses(text))
    if len(parts) != 2:
        return None
    context = mask_quotes(_strip_prefix(parts[0]))
    if not re.search(r"(?:文件|文档)[^，,。]{0,16}(?:放哪|存哪|在哪|位置|找不到)", context) or \
            _diagnostic_content_intro(parts[0]) or _plan_explicit_request(parts[0], fast_match_intent(parts[0])):
        return None
    request = _strip_prefix(_STEP_ORDER_RE.sub("", parts[1]))
    match = re.fullmatch(r"(?:搜(?:索|搜)?(?:一下)?|找(?:一下|找)?)\s*(.+)", request)
    if not match:
        return None
    query = match[1].strip(" 。！! ")
    literal = bool(_QUOTED_SPAN_RE.fullmatch(query))
    if not literal and re.match(r"^(?:从|在)?(?:(?:网页|网站|百度|谷歌|必应|google|bing)(?:上|里|中)|网上|互联网上|网络上|线上)", query, re.I):
        return None  # An explicit web location takes precedence over a file-memory background.
    named = _local_file_decision(request)
    if named:
        return named
    if literal:
        query = query[1:-1].strip()
    return {"action": "search_file", "param": query, "emotion": "confident", "reply": f"我在本地找找{query}。"}


def _plan_local_step_grounded(step, source, now=None):
    action, value = _plan_step_identity(step)
    from canaan_action_framework import ActionRegistry
    if not ActionRegistry.get_action(action):
        return False
    local_param = json.dumps(value, ensure_ascii=False) if action == "send_chatapp" and isinstance(value, dict) else value
    guarded = apply_command_guard(source, {"action": action, "param": local_param}, require_confidence=False, _local_step=True)
    if not guarded or guarded.get("action") == "chat" or guarded.get("_pending"):
        return False
    if action == "screenshot":
        return len(_screenshot_request_contexts(source)) == 1
    if action in ("ufo_task", "computer_use", "openmuse_task", "opendots_task"):
        return _plan_task_text(value) == _plan_task_text(source)
    outer = mask_quotes(source)
    if re.search(r"(?:窗口|页签|标签)[^，,。]{0,12}(?:挪|移|拖|居中|最大化|最小化|缩放|调整|放到)|(?:挪|移|拖|最大化|最小化)[^，,。]{0,8}(?:窗口|页签|标签)", outer):
        return False
    if action == "send_chatapp":
        return isinstance(value, dict) and isinstance(value.get("contact"), str) and isinstance(value.get("message"), str) and _contact_in_text(value["contact"], source) and _message_grounded(value["message"], source)
    if action in _GROUNDED_ACTIONS:
        if not (bool(str(value).strip()) and param_grounded(action, value, source) and _plan_local_action_intent(action, source)):
            return False
        if action == "open_path" and isinstance(value, str):
            paths = re.findall(r"[A-Za-z]:\\[^，,。；;\n\"“”'‘’]*", source)
            if paths:
                return any(_plan_folder_identity(value) == _plan_folder_identity(path.strip()) for path in paths)
            drives = re.findall(r"([A-Za-z])盘", source)
            if drives:
                return any(_plan_folder_identity(value) == _plan_folder_identity(drive + ":\\") for drive in drives)
        return True
    if action == "set_reminder":
        import canaan_proactive
        now = now or _plan_comparison_now()
        candidate = str(value.get("value") or value.get("text") or "") if isinstance(value, dict) else str(value)
        expected = canaan_proactive.parse_reminder(source, now)
        return expected is not None and expected == canaan_proactive.parse_reminder(candidate, now)
    if action == "media_control":
        if str(value) in ("pause", "play"):
            return _media_state_request(source) == str(value)
        cues = {"next": r"下一首|下一曲|下首|\bnext\b", "prev": r"上一首|上一曲|前一首|前面那首|刚才那首|\bprev(?:ious)?\b",
                "play_pause": r"切换(?:当前|音乐|媒体|的)*播放状态|播放暂停|\bplay_pause\b"}
        mode = {"下一曲": "next", "下一首": "next", "previous": "prev", "上一曲": "prev", "上一首": "prev", "pause": "pause", "play": "play"}.get(str(value), str(value))
        return mode in cues and bool(re.search(cues[mode], outer, re.I))
    if action in ("volume", "set_brightness", "volume_up", "volume_down", "brightness_up", "brightness_down"):
        if action == "set_brightness" and value in ("min", "max"):
            target = _state_target_request(source)
            return bool(target and target["action"] == action and _plan_step_equal(target, step, source, now))
        numeric = _plan_numeric_value(action, value)
        domain = r"音量|声音|volume" if action.startswith("volume") else r"亮度|屏幕|背光|brightness"
        return numeric is not None and numeric[1] in numbers_in(source) and bool(re.search(domain, outer, re.I))
    if isinstance(value, str) and value.strip() and value.strip() in source:
        if action == "web_search":
            return bool(re.search(r"搜|查|检索|网上|百度|google|search|look up", outer, re.I))
        if action == "search_file":
            return bool(re.search(r"找|搜|查找|翻|寻|检索|打开文件|打开文档", outer))
        if action in ("doc_search", "knowledge_query", "diagnose_error"):
            return bool(re.search(r"文档|指南|规范|知识库|资料库|报错|错误|故障|error|exception", outer, re.I) and re.search(r"查|搜|翻|问|看|分析|诊断|排查", outer))
    return False


def _multi_plan_matches_source(text, proposed):
    if not proposed:
        return False
    now = _plan_comparison_now()
    reference = fast_match_intent(text)
    units = _plan_source_units(text)
    if _plan_whole_owned_source(text, reference):
        return len(proposed) == 1 and _plan_step_equal(reference, proposed[0], text, now)
    # A complete UI delegation may include a known launch plus an unknown window
    # operation. Its payload must cover the whole request, never only the tail.
    if len(proposed) == 1 and _plan_step_identity(proposed[0])[0] in ("ufo_task", "computer_use") and \
            _plan_task_text(_plan_step_identity(proposed[0])[1]) == _plan_task_text(text) and \
            not any(ref and ref.get("action") in ("send_chatapp", "set_reminder", "run_cmd", "run_script") for _, ref in units):
        return True
    if reference and reference.get("action") != "chat":
        expected = _guard_multi_action_steps(reference["param"]) if reference["action"] == "multi_action" else [reference]
        opaque = reference["action"] in ("ufo_task", "computer_use") and _plan_task_text(reference.get("param")) == _plan_task_text(text)
        normalized = _space_clauses(_strip_prefix(text))
        corrected = _correction_decision(normalized) or _corrected_open_text(normalized) or _literal_media_request(normalized)
        separate_ui_steps = opaque and len(proposed) > 1 and sum(_plan_explicit_request(source, known) for source, known in units) > 1
        if not separate_ui_steps and (opaque or corrected or not any(item[1] is None for item in units)):
            sources = [text] * len(expected)
            if len(units) == len(expected):
                for index, (source, known) in enumerate(units):
                    if known and _plan_step_identity(expected[index])[0] == "open_url" and \
                            _plan_step_identity(known) == _plan_step_identity(expected[index]):
                        sources[index] = source
            return len(expected) == len(proposed) and all(_plan_step_equal(a, b, source, now)
                    for a, b, source in zip(expected, proposed, sources))
    from functools import lru_cache
    @lru_cache(None)
    def align(current, index):
        if current == len(proposed):
            return index == len(units)
        if index == len(units):
            return False
        source, known = units[index]
        if known:
            return _plan_step_equal(known, proposed[current], source, now) and align(current + 1, index + 1)
        delegated = _plan_step_identity(proposed[current])[0] in ("ufo_task", "computer_use", "openmuse_task", "opendots_task")
        limit = len(units) + 1 if delegated else min(index + 2, len(units) + 1)
        for end in range(index + 1, limit):
            if end > index + 1 and units[end - 1][1]:
                break
            local = "，".join(unit[0] for unit in units[index:end])
            if _plan_local_step_grounded(proposed[current], local, now) and align(current + 1, end):
                return True
        return False
    return align(0, 0)


def _query_source_decision(text):
    """Keep lookup domains and self-browsing purposes tied to the source request."""
    clean = _strip_prefix(_asr_fix(_space_clauses(text or "")))
    inactive = {"reported", "quoted_report", "past_story", "past", "hypothetical", "progressive", "howto"}
    if not clean or mention_reason(clean) in inactive or _CAPABILITY_ONLY_RE.search(clean):
        return None
    # Select a complete local read-only memory query before keyword matching.
    # Multi-clause requests must still pass the existing complete-plan parser.
    from canaan_status_query import query_kind
    query = query_kind(mask_quotes(clean)) if len(_clauses(clean)) == 1 else None
    if query is not None and query[0] == "memory_available":
        return {"action": "system_status", "param": "", "emotion": "confident",
                "_guard_rule": "query_domain", "reply": "查看当前可用内存。"}
    net_search = _NET_SEARCH_RE.match(clean)
    if net_search and not net_search.group(1).strip():
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "vague_query", "_pending": None,
                "reply": "要搜什么？把关键词告诉我。"}
    # Bare message bodies own their action words and commas. Quoted bodies own
    # only the quoted span, so a genuinely separate tail still needs checking.
    message = _quoted_chatapp_decision(clean)
    if message:
        span = next(_QUOTED_SPAN_RE.finditer(clean), None)
        tail = clean[span.end():] if span else ""
        tail = re.sub(r"^[\s，,；;。]*(?:然后|再|接着|之后|最后|顺便)?\s*", "", tail)
        result = _query_source_decision(tail) if tail else None
        return result if result and result.get("action") == "chat" else None
    if re.match(r"^(?:(?:在|用|通过)?聊天软件(?:里|上)?\s*)?(?:给|发给|向|跟|告诉|通知)[^，,。]{1,16}?(?:发|说)", clean) or \
            re.match(r"^(?:(?:在|用|通过)?聊天软件(?:里|上)?\s*)?(?:发聊天软件|发一条聊天软件|发条聊天软件|发个聊天软件|发消息|发信息|发送消息|发送)"
                     r"(?:给|发给|向|跟)[^，,。]{1,16}?(?:说|：|:|内容是|内容为)", clean):
        return None
    future = _sequenced_reminder_parts(clean)
    if future:
        result = _query_source_decision(future[1]) if future[1] else None
        return result if result and result.get("action") == "chat" else None
    parts = _clauses(clean)
    single = None
    for raw in parts:
        clause = _strip_prefix(_STEP_ORDER_RE.sub("", raw.strip())).strip(" 。！!？?")
        if mention_reason(clause) in inactive or _cancelled_clause(mask_quotes(clause)) or _is_reminder_clause(clause):
            continue
        outer = mask_quotes(clause)
        if re.match(r"^(?:(?:在|用)?(?:百度|谷歌|必应|google|bing|浏览器)|网上|上网|在网上|互联网上)", outer, re.I) and re.search(r"搜|查|找", outer):
            continue  # Explicit online search owns its entire topic.
        scoped_file = _local_file_decision(clause)
        if scoped_file and isinstance(scoped_file.get("param"), dict) and "root" in scoped_file["param"]:
            continue  # The existing scoped-file contract owns its directory and exact name.
        filename = re.fullmatch(r"(?:打开|查找|寻找|找(?:一下|一找|找)?|搜(?:索|一下|搜)?)(?:文件(?!夹)|文档)"
                                r"([^，,。\s\\/:]+\.[A-Za-z0-9]{1,8})", clause)
        if filename:
            single = {"action": "search_file", "param": filename[1], "emotion": "confident", "_guard_rule": "query_domain",
                      "_query_literal_file": True, "reply": "我查找这个名称的文件。"}
            continue  # An explicitly named file owns domain words in its name.
        contact = re.fullmatch(r"(?P<via>在|从|用|通过)?(?:聊天软件|chatapp|chatapp)"
                               r"(?P<scope>(?:里|中|内|上)?(?:面)?(?:的)?(?:联系人|好友|通讯录)?(?:里|中|内)?)"
                               r"(?:帮我|替我)?"
                               r"(?:查找|寻找|搜索|搜(?:一下|搜)?|找(?:一下|一找|找)?)(?:一下)?"
                               r"(?P<kind>联系人|好友|通讯录(?:里|中)?的?)?(?P<name>.+)", outer, re.I)
        if contact and (contact["via"] or contact["scope"] or contact["kind"]):
            name = contact["name"].strip(" ，,。！!？?")
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "contact_navigation_unsupported",
                    "_guard_rule": "query_domain", "_pending": None,
                    "reply": f"当前不支持在聊天软件里查找或选中联系人{name}。请手动打开聊天软件搜索这位联系人。"}
        document = re.fullmatch(r"(?:(?:查(?:询|找)?|搜(?:索)?|翻)(?:一下|下|找)?(?:在|从)?|在|从)?"
                                r"([^，,。]{0,12}?(?:文档|资料)(?:库)?)(?:里|中|内)(.+)", outer)
        if document and not re.search(r"比赛|竞赛|指南|^(?:打开|发|说|告诉|通知|提醒|输入|写)", document[1]) and \
                re.search(r"查|找|搜|说明|解释|要求|放哪|放在哪|存哪|在哪|位置|路径|什么|怎么", document[2]):
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "document_scope_missing",
                    "_guard_rule": "query_domain", "_pending": None,
                    "reply": f"要查询哪份{document[1]}？请指定文档名称或路径，当前不能直接检索这个未指定的文档库。"}
        purpose = r"(?:群(?:消息|聊)?|消息|聊天(?:记录)?|未读(?:消息)?|文档|资料|内容|网页|页面|文件|表格)(?:吧|呗)?"
        launch = re.fullmatch(r"(?:打开|启动|开开)(.+?)(?:让我|给我|我来|来)?(?:看看|看一下|看下|浏览)" + purpose, outer) or \
                 re.fullmatch(r"(?:把)?(.+?)(?:给我|帮我)?(?:打开|启动|开开)(?:让我|给我|我来|来)?(?:看看|看一下|看下|浏览)" + purpose, outer)
        if launch:
            app = launch[1].strip()
            folder_or_site = _named_site_url(app) or _plan_folder_identity(app)[0] == "shortcut" or \
                             re.search(r"[\\/:]|文件夹|目录|^[A-Za-z]盘", app)
            if not folder_or_site:
                if _find_application(app):
                    single = {"action": "open_app", "param": app, "emotion": "confident", "_guard_rule": "query_domain",
                              "reply": f"我打开{app}，你可以自行查看。"}
                elif len(app) <= 24 and not re.search(r"\.[A-Za-z]{2,}(?:$|[/#?])|然后|并且|接着|发消息|发聊天软件|说", app):
                    return {"action": "chat", "param": "", "emotion": "confident", "_guard": "app_not_found",
                            "_guard_rule": "query_domain", "_requested_app": app, "_pending": None,
                            "reply": f"我没找到{app}的应用入口，请确认软件名称或安装位置，这次先不执行。"}
        manual = re.fullmatch(r"(?:找(?:一下|一找|找)?|搜(?:索|一搜|搜|一下)?)\s*(.+(?:说明书|使用手册|用户手册))(?:吧|呗)?", clause)
        if manual:
            target = manual[1].strip(" “”。！!\"「」")
            single = {"action": "search_file", "param": target, "emotion": "confident", "_guard_rule": "query_domain",
                      "reply": f"我在本地找找{target}。"}
    return single if len(parts) == 1 else None


def _non_direct_file_source(text):
    """Parse one complete file envelope without treating its name as an action."""
    if len(_clauses(_space_clauses(text))) != 1 or _MORE_STEPS_RE.search(mask_quotes(text)):
        return None
    reference = _local_file_decision(text)
    if reference and reference.get("action") in ("search_file", "open_path"):
        return reference
    reference = _query_source_decision(text)
    if reference and reference.get("_query_literal_file"):
        return reference
    # These are the existing explicit-file/fallback lookup envelopes. A bare
    # lookup is opaque here only when it actually supplies a filename suffix.
    match = re.fullmatch(r"(打开文件|打开文档|找一下|查找|搜索)\s*(.+)", text)
    if match:
        value = match[2].strip(" “”。！!\"「」『』")
        if value and (match[1] in ("打开文件", "打开文档") or re.search(r"\.[A-Za-z0-9]{1,8}$", value)):
            return {"action": "search_file", "param": value}
    return None


def _non_direct_target_text(text, decision):
    """Conceal only a parser-proven target span, retaining the request around it."""
    clean = _strip_prefix(text)
    action, param = decision.get("action"), decision.get("param")
    if action in ("search_file", "open_path"):
        reference = _non_direct_file_source(clean)
        if not reference or reference.get("action") != action:
            return text
        target = reference["param"]
        literal = _QUOTED_SPAN_RE.fullmatch(param.strip()) if isinstance(param, str) else None
        supplied = literal[0][1:-1].strip() if literal else param
        if not (target == supplied or _plan_step_equal(reference, decision, clean)):
            return text
        value = target.get("value") if isinstance(target, dict) else target
        if not isinstance(value, str) or not value:
            return text
        suffix = "文件夹" if action == "open_path" else ".txt"
        marker = "〔文件名〕" + suffix
        expected = {**target, "value": marker} if isinstance(target, dict) else marker
        for span in re.finditer(re.escape(value), clean):
            scoped = clean[:span.start()] + marker + clean[span.end():]
            envelope = _non_direct_file_source(scoped)
            if envelope and envelope.get("action") == action and envelope.get("param") == expected:
                return scoped  # Re-parsing proves this occurrence is the data field.
    elif action == "web_search":
        reference = _web_search_decision(clean)
        if not reference or reference.get("action") != action or not _plan_step_equal(reference, decision, clean):
            return text
        value = reference["param"]
        for span in _QUOTED_SPAN_RE.finditer(clean):
            if span[0][1:-1].strip() == value:
                # Keep the quote boundary; the marker must not itself contain a search cue.
                return clean[:span.start() + 1] + "〔数据〕" + clean[span.end() - 1:]
        # An unquoted topic must not absorb an independently sequenced request.
        if len(_clauses(_space_clauses(clean))) == 1 and not _MORE_STEPS_RE.search(mask_quotes(clean)):
            start = clean.rfind(value)
            if value and start >= 0:
                return clean[:start] + "〔数据〕" + clean[start + len(value):]
    elif action == "open_url":
        reference = _device_rule(clean, clean)
        if not reference or reference.get("action") != action or reference.get("param") != param:
            return text
        for span in _URL_RE.finditer(clean):
            value = span[0].rstrip("）)】]")
            if value == param:
                return clean[:span.start()] + "〔网址〕" + clean[span.start() + len(value):]
    return text


def _non_direct_positive_prefix(text, decision):
    """Detach only independently cancelled opens from an exactly owned request."""
    if decision.get("action") in (None, "", "chat") or decision.get("_pending"):
        return text

    def exact(reference):
        return reference and reference.get("action") == decision.get("action") and \
            "param" in reference and "param" in decision and reference["param"] == decision["param"]

    # Both complete-source and prefix parsers must own precisely this payload.
    # In particular, an unquoted message/reminder/search body may own "别开".
    if not exact(fast_match_intent(text)):
        return text
    future = _sequenced_reminder_parts(text)
    candidates = [(future[0], future[1])] if future and future[1] else []
    outer = _QUOTED_SPAN_RE.sub(lambda match: "Q" * len(match[0]), text)
    candidates.extend((text[:span.start()].rstrip(" ，,；;。"), text[span.end():].strip())
                      for span in _STEP_SPLIT_RE.finditer(outer))
    for prefix, tail in candidates:
        if not prefix or not tail or not exact(fast_match_intent(prefix)):
            continue
        clauses = _clauses(_space_clauses(tail))
        pure = bool(clauses)
        for raw in clauses:
            clause = _strip_prefix(_STEP_ORDER_RE.sub("", raw.strip())).strip(" 。！!")
            negative = _NEG_CLAUSE_RE.match(clause)
            request = clause[negative.end():] if negative else ""
            opening = re.fullmatch(r"(?:再)?(?:打开|开启|启动|开)(?P<target>.+)", request)
            target = re.sub(r"[了吧呗啊呀]+$", "", opening["target"].strip()) if opening else ""
            reference = fast_match_intent(request) if opening else None
            # Cancellation-prefix recognition alone is deliberately insufficient:
            # a second operation can follow without punctuation or a known split.
            if not reference or reference.get("param") != target or not _known_application(target) or \
                    not _cancelled_clause(clause) or not negative_request(clause) or \
                    non_direct_reason(clause) != "negated_open" or _QUOTED_SPAN_RE.search(target) or \
                    _CMD_HINT_RE.search(target) or re.search(r"还|并|同时|顺便|然后|接着|之后|再|以及|另外|和|与|或", target):
                pure = False
                break
        if pure:
            return prefix
    return text


def _non_direct_command_text(text, decision):
    """Hide only payloads established by the existing source/parameter parsers."""
    prefix = _non_direct_positive_prefix(text, decision)
    if prefix != text:
        return _non_direct_command_text(prefix, decision)
    if decision.get("action") == "multi_action":
        steps = _guard_multi_action_steps(decision.get("param")) or []
        scoped, cursor = [], 0
        for source, _ in _plan_source_units(text):
            start = text.find(source, cursor)
            if start < 0:
                continue
            value = source
            for step in steps:
                value = _non_direct_command_text(value, step)
            scoped.extend((text[cursor:start], value))
            cursor = start + len(source)
        return "".join(scoped) + text[cursor:]
    clean = _strip_prefix(text)
    action = decision.get("action")
    if action == "send_chatapp":
        try:
            param = decision.get("param")
            param = json.loads(param) if isinstance(param, str) else param
            body = param["message"]
        except (ValueError, TypeError, KeyError):
            return text
        if not isinstance(body, str) or not body.strip():
            return text
        body = body.strip().strip("，,。.!！?？~～：:；; ")
        if not body:
            return text
        if isinstance(decision.get("param"), str):
            try:
                payload = json.loads(decision["param"])
                if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                    decision = {**decision, "param": json.dumps({**payload, "message": body}, ensure_ascii=False)}
            except (ValueError, TypeError):
                pass
        reference = _quoted_chatapp_decision(clean)
        if reference and reference.get("action") == action and _plan_step_equal(reference, decision, clean) and _plan_whole_owned_source(clean, reference):
            span = _QUOTED_SPAN_RE.search(clean)
            return clean[:span.start()] + "〔消息正文〕" + clean[span.end():]
        # Reuse the quoted-envelope parser for an exact (or established
        # pronoun-equivalent) unquoted body. A model's action name alone is
        # never evidence that arbitrary source text is an opaque message.
        normalize = lambda value: re.sub(r"[他她您]", "你", value)
        needle = normalize(body)
        haystack, offset = normalize(clean), 0
        while needle and (start := haystack.find(needle, offset)) >= 0:
            end, offset = start + len(needle), start + 1
            original = clean[start:end]
            left, right = start, end
            if left and right < len(clean) and (clean[left - 1], clean[right]) in (("“", "”"), ('"', '"'), ("「", "」"), ("『", "』")):
                left, right = left - 1, right + 1
            quoted = clean[:left] + "“〔消息正文〕”" + clean[right:]
            reference = _quoted_chatapp_decision(quoted)
            if not reference or reference.get("action") != action:
                continue
            parsed = json.loads(reference["param"])
            reference = {**reference, "param": {**parsed, "message": original}}
            if _plan_step_equal(reference, decision, clean) and _plan_whole_owned_source(quoted, reference):
                return clean[:left] + "〔消息正文〕" + clean[right:]
    elif action == "set_reminder":
        import canaan_proactive
        parsed = canaan_proactive.parse_reminder(clean, _plan_comparison_now())
        reference = {"action": action, "param": clean}
        if parsed and _plan_step_equal(reference, decision, clean):
            body = parsed[1]
            start = clean.find(body)
            if start >= 0:
                return clean[:start] + "〔提醒正文〕" + clean[start + len(body):]
    return _non_direct_target_text(text, decision)


def apply_command_guard(text, decision, require_confidence=False, *, _local_step=False):
    """Apply source/parameter validation before adding non-direct policy."""
    checked = _validated_command_guard(text, decision, require_confidence, _local_step=_local_step)
    if not checked:
        return checked
    reason = non_direct_reason(text or "")
    if reason:
        reason = non_direct_reason(_non_direct_command_text(text or "", checked))
    if reason and checked.get("action") in (None, "", "chat") and decision and decision.get("action") not in (None, "", "chat"):
        # 校验层已把它改成 chat 时，用原决策再遮一次正文，避免把正文当成危险命令
        reason = non_direct_reason(_non_direct_command_text(text or "", decision))
    if not reason and (checked.get("_guard") == "hypothetical" or
                       isinstance(checked.get("_pending"), dict) and mention_reason(
                           _non_direct_command_text(text or "", checked["_pending"])) == "hypothetical"):
        reason = "hypothetical"  # Also cover mention grammar and earlier confidence/target confirmations.
    if not reason and checked.get("_guard") == "reported":
        reason = "reported_name"
    rejected_information = (checked.get("action") == "chat" and checked.get("_guard") and
                            not checked.get("_pending") and not checked.get("_draft") and
                            decision and decision.get("action") not in (None, "", "chat"))
    if not reason and checked.get("_guard") == "weather_no_place":
        return checked  # 问天气没说地方：反问城市就是回答，不交给陪聊瞎答
    if not reason and (isinstance(checked.get("_pending"), dict) or rejected_information):
        proposed = checked["_pending"] if isinstance(checked.get("_pending"), dict) else decision
        suggestion = re.sub(r"[？?\s]+$", "", text or "")
        if suggestion != (text or "").strip() and suggestion.endswith("吧") and \
                mention_reason(_non_direct_command_text(suggestion, proposed)) is None:
            return checked  # “把记事本关了吧？”是委婉的请求，不是问句：照旧先确认
        source_reason = mention_reason(_non_direct_command_text(text or "", proposed))
        if source_reason in {"question", "howto", "noun_mention", "progressive", "symptom", "quoted_report", "past_story", "past"} and (
                not rejected_information or source_reason in {"question", "howto", "noun_mention"}):
            reference = fast_match_intent(text)
            requested = bool(reference and reference.get("action") != "chat" and (
                rejected_information or _plan_explicit_request(text, reference) and _plan_step_equal(reference, proposed, text)))
            if rejected_information:
                requested = requested or bool(reference and reference.get("_guard"))
                # A question mark alone must not erase an operation or a missing slot.
                for part in _clauses(_space_clauses(text or "")):
                    source = _strip_prefix(_STEP_ORDER_RE.sub("", part.strip())).rstrip(" ？?")
                    known = fast_match_intent(source)
                    if (known and (known.get("action") != "chat" or known.get("_guard"))) or (
                            mention_reason(source) is None and _plan_explicit_request(source, known)):
                        requested = True
                        break
            if not requested:
                observed = {key: proposed[key] for key in ("action", "param", "_model", "_conf", "_pconf") if key in proposed}
                informational = {"action": "chat", "param": "", "reply": "", "emotion": "confident",
                                 "_guard_rule": "noncommand_source", "_source_reason": source_reason,
                                 "_model_observation": observed, "_model": checked.get("_model", proposed.get("_model", ""))}
                if rejected_information:
                    informational["_declined_guard"] = checked["_guard"]
                return informational
    if not reason:
        return checked
    candidate = checked.get("action") not in (None, "", "chat")
    report_scope_guards = {"screenshot_not_requested", "volume_steps_mismatch", "single_plan_mismatch", "multi_plan_mismatch", "multi_step_partial"}
    recover_report_scope = checked.get("_guard") in report_scope_guards and decision and decision.get("action") not in (None, "chat")
    if reason == "reported_name" and (candidate or isinstance(checked.get("_pending"), dict) or recover_report_scope):
        from canaan_reported_requests import reported_request_text, reported_execution_cancelled, reported_recall_request
        proposed = checked if candidate else checked.get("_pending") or decision
        source = reported_request_text(text or "")
        reference = fast_match_intent(source) if source else None
        if source and reference is None and reported_recall_request(_operation_request_head(mask_quotes(source))):
            reference = {"action": "recall_chatapp", "param": ""}
        scope = reference if reference and reference.get("action") != "chat" else proposed
        if source and reported_execution_cancelled(_space_clauses(_non_direct_command_text(source, scope))):
            return {"action": "chat", "param": "", "_guard": "reported_cancelled", "_pending": None,
                    "reply": "好，这次不执行转述的操作。", "_model": proposed.get("_model", "")}
        compatible = bool(source and reference and reference.get("action") != "chat" and
                          _plan_step_equal(reference, proposed, source))
        if compatible:
            local = apply_command_guard(source, dict(proposed), require_confidence=False)
            compatible = bool(local and local.get("action") not in (None, "chat") and not local.get("_pending") and
                              not local.get("_draft") and _plan_step_equal(local, proposed, source))
        elif source and reference is None:
            compatible = _plan_local_step_grounded(proposed, source)
        if not compatible:
            if not candidate and not checked.get("_pending"):
                return checked  # Preserve earlier missing-slot/source errors; never revive an invalid proposal.
            return {"action": "chat", "param": "", "_guard": "reported_source_mismatch", "_pending": None,
                    "reply": "转述内容和这个操作还没核对齐，请直接说明现在要做的完整操作。", "_model": checked.get("_model", "")}
        if recover_report_scope and not candidate and not checked.get("_pending"):
            return {"action": "chat", "param": "", "_guard": "reported_name", "_pending": dict(proposed),
                    "reply": describe_action(proposed), "_model": proposed.get("_model", "")}
    if reason == "hypothetical":
        if not candidate and not checked.get("_pending"):
            return checked
        pending = None  # Discussing an action does not authorize a future “好” to execute it.
    elif reason == "reported_name":
        if not candidate:
            return checked  # Keep existing rejection, missing-slot or confirmation results.
        pending = dict(checked)
    elif candidate or checked.get("_pending"):
        pending = None
    else:
        return checked
    return {"action": "chat", "param": "", "emotion": "confident", "_guard": reason,
            "_pending": pending, "reply": _non_direct_reply(reason), "_model": checked.get("_model", "")}


def _status_source_requests(text, units=None):
    """Private status presentation/source check; never an execution authority.

    Keep original clause request modifiers that ordinary plan units normalize
    away. Unresolved text is explicitly uncertain, not a guessed operation.
    Background needs a complete state/feeling predicate, never just a subject
    or causal prefix. This grammar contains no business-operation aliases.
    """
    from canaan_status_query import query_kind
    from canaan_query_context import status_query_identity
    if units is None:
        units = _plan_source_units(text, _reference={})
    known_by_source = {source: known for source, known in units}
    clean = _asr_fix(_space_clauses(text or ""))
    parts = _clauses(clean)
    spans, cursor = [], 0
    for part in parts:
        start = clean.find(part, cursor)
        if start < 0:
            raise ValueError("Status source position is unavailable")
        spans.append((start, start + len(part)))
        cursor = start + len(part)
    rows = []
    for index, (part, (start, end)) in enumerate(zip(parts, spans)):
        raw = part.strip()
        source = _strip_prefix(_STEP_ORDER_RE.sub("", raw))
        masked = mask_quotes(source)
        known = known_by_source.get(source)
        action = _plan_step_identity(known)[0] if known else None
        reason = mention_reason(source)
        inactive = (_cancelled_clause(masked) or _completed_clause(masked)
                    or reason in ("reported", "quoted_report", "past_story", "past", "hypothetical", "progressive", "howto", "noun_mention"))
        query = None if inactive else query_kind(masked)
        before = clean[spans[index - 1][1]:start] if index else ""
        # A connector belongs to the following clause. It must not turn the
        # preceding complete state description into an extra requested task.
        sequenced = bool(_STEP_ORDER_RE.match(raw)
                         or re.search(r"(?:^|[，,；;。\s])(?:接下来|紧接着|随后|接着|之后)\s*",
                                      mask_quotes(before + raw))
                         or _MORE_STEPS_RE.search(mask_quotes(before + raw)))
        head = _operation_request_head(mask_quotes(raw))
        time = r"(?:(?:现在|此刻|今天|最近|刚才|一直|这会儿|这时候|目前|眼下)\s*)?"
        explicit = bool(reason == "question" or _plan_explicit_request(raw, known)
                        or _IMPERATIVE_RE.search(mask_quotes(raw))
                        or re.match(r"^(?:我|我们)?" + time + r"(?:要|想|需要|希望|打算)", head)
                        or re.search(r"需要|希望|打算", head))
        # Closed descriptive grammar: a finite feeling/state predicate must
        # consume the clause. Unknown objects and trailing verbs stay uncertain.
        # Causal markers can qualify a state, not arbitrary trailing content.
        causal = r"(?:(?:因为|由于|只是|就是|主要是|原因是)\s*)?"
        degree = r"(?:(?:有点|有些|有一些|很|挺|比较|非常|特别|太|不太)\s*)?"
        # Reduplicated negative state morphology is a general feeling form;
        # it is not an alias for a particular measured feeling or operation.
        repeated_state = r"(?P<feeling_repeat>[\u4e00-\u9fff])(?P=feeling_repeat)(?:不|无)[\u4e00-\u9fff]"
        feeling = (r"(?:我|我们)" + time + r"(?:(?:感觉|感到|觉得)\s*)?" + degree
                   + r"(?:担心|焦虑|紧张|害怕|不安|安心|放心|疑惑|困惑|好奇|着急|"
                   r"高兴|难过|疲惫|疲倦|困|累|" + repeated_state + r")")
        device_state = (r"(?:(?:这|那)(?:台|个|部)?(?:机器|设备|电脑|系统)|机器|设备|电脑|系统|它)"
                        + time + r"(?:(?:感觉|显得|变得)\s*)?" + degree
                        + r"(?:慢|卡|卡顿|热|烫|吵|安静|稳定|不稳定|流畅|不流畅|正常|不正常)")
        # Perceptual copula + short degree-qualified evaluation is a complete
        # descriptive structure, even with an unfamiliar topic or adjective.
        # Longer trailing content, request/intent cues and sequences cannot
        # hide in this bounded predicate or override the earlier request checks.
        # Functional obligation/intent/order particles cannot be read as a
        # short adjective, including their negative forms. Lexical adjectives
        # with the same initial character remain an explicit adjective branch.
        functional = r"(?:(?:不|未|无|毋|弗)\s*)?(?:要|想|先|再|得|该|应|需|须|愿|欲|敢|肯|必|务|请|勿|莫|别|甭|待)"
        lexical_evaluation = r"不?(?:得体|得当|应景|应时)"
        evaluation = (r"(?:" + lexical_evaluation + r"|"
                      + r"(?!" + functional + r")"
                      + r"(?:不?[\u4e00-\u9fff]{1,2}|"
                      r"(?P<sense_a>[\u4e00-\u9fff])(?P=sense_a)"
                      r"(?P<sense_b>[\u4e00-\u9fff])(?P=sense_b)))")
        perception = (r"[\u4e00-\u9fffA-Za-z0-9]{1,16}(?:看起来|听起来|闻起来|摸起来|尝起来)"
                      + degree + evaluation)
        contextual = bool(re.fullmatch(causal + r"(?:" + feeling + r"|" + device_state + r"|" + perception
                                      + r")(?:了|呢|啊|呀|的)?[ 。！!]*", head))
        identity = None
        if inactive:
            kind = "background"
        elif action == "system_status" or action is None and query is not None:
            kind = "query"
            identity = status_query_identity(masked)
        elif action not in (None, "chat") or explicit:
            kind = "request"
        elif sequenced:
            kind = "request"
        elif contextual:
            kind = "background"
        elif _CMD_HINT_RE.search(masked):
            kind = "request"
        else:
            kind = "uncertain"
        rows.append({"source": source, "selection_text": masked, "position": index,
                     "known_action": action, "query": query, "identity": identity,
                     "kind": kind, "explicit": explicit, "sequenced": sequenced})
    return tuple(rows)


def _validated_command_guard(text, decision, require_confidence=False, *, _local_step=False):
    """规则或大模型给出的动作，若原话只是提到而非命令，改为先问一句（原决策放在 _pending，用户说“好”再执行）。
    发聊天软件缺少消息内容时改为追问内容。"""
    if not decision:
        return decision
    if _negated_state_target(text or ""):
        return dict(_NEGATED_REPLY)
    literal_state = _state_target_literal(text or "")
    if literal_state and decision.get("action") != "chat":
        if literal_state["action"] == "chat" or not _plan_step_equal(literal_state, decision, text):
            return {"action": "chat", "param": "", "_guard": "state_target_literal", "_pending": None,
                    "reply": "这是正文或名称里的文字，这次不会执行其中的状态操作。", "_model": decision.get("_model", "")}
        text = "打开文件“" + literal_state["param"] + "”"
    literal_site = _named_site_literal(text or "")
    if literal_site:
        return literal_site
    literal_app = _app_literal_request(text or "")
    if literal_app:
        return literal_app
    literal_candidate = decision.get("_pending") if decision.get("action") == "chat" else decision
    if isinstance(literal_candidate, dict) and literal_candidate.get("action") in ("open_app", "open_chatapp", "close_app", "focus_app") and _CORRECTION_MARK_RE.search(text or ""):
        owner = _fast_match_single(text)
        if owner and owner.get("action") not in ("open_app", "open_chatapp", "close_app", "focus_app") and _plan_whole_owned_source(text, reference=owner):
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "application_payload", "_pending": None,
                    "reply": "这段应用改口属于消息或其他正文，不作为当前应用操作。"}
    if isinstance(literal_candidate, dict) and literal_candidate.get("action") in ("open_app", "open_chatapp", "close_app", "focus_app") and any(
            _app_literal_request(clause) for clause in _clauses(_space_clauses(text or ""))):
        reference = fast_match_intent(text)
        if not reference or not _plan_step_equal(reference, literal_candidate, text):
            if reference and reference.get("action") != "chat":
                resolved = apply_command_guard(text, reference, require_confidence=False, _local_step=_local_step)
                observation = {key: literal_candidate[key] for key in ("action", "param", "_model", "_conf", "_pconf") if key in literal_candidate}
                return {**resolved, "_guard_rule": "application_literal", "_model_observation": observation}
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "application_literal", "_pending": None,
                    "reply": "引用中的应用操作不是当前请求，请明确要执行的完整步骤。"}
    if decision.get("action") in ("open_app", "open_url") and any(
            _named_site_literal(clause) for clause in _clauses(_space_clauses(text or ""))):
        reference = fast_match_intent(text)
        if not reference or not _plan_step_equal(reference, decision, text):
            return {"action": "chat", "param": "", "_guard": "website_source_mismatch", "_pending": None,
                    "reply": "引用中的网站不是当前操作，请明确要执行的完整步骤。"}
    text = _asr_fix(text or "")    # 和 fast_match_intent 用同一份改正后的原话核对参数
    website_source = _named_site_request(text)
    website_steps = _guard_multi_action_steps(decision.get("param")) if website_source and _plan_step_identity(decision)[0] == "multi_action" else [decision]
    if website_source and (website_source["action"] == "chat" or
                           decision.get("action") != "chat" and (len(website_steps) != 1 or not _plan_step_equal(website_source, website_steps[0], text))):
        if website_source["action"] == "chat":
            return website_source
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "website_source_mismatch", "_pending": None,
                "reply": "网站目标或操作和原话不一致，请重新说明要打开的网址。", "_model": decision.get("_model", "")}
    scoped_source = _local_file_decision(_strip_prefix(_STEP_ORDER_RE.sub("", text.strip())))
    if scoped_source and isinstance(scoped_source.get("param"), dict) and "root" in scoped_source["param"] and \
            decision.get("action") != "chat" and not _plan_step_equal(scoped_source, decision, text):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "scoped_file_mismatch", "_pending": None,
                "reply": "文件名或指定目录和原话不一致，请重新说明完整目标。", "_model": decision.get("_model", "")}
    query_source = _query_source_decision(text)
    if query_source and (query_source["action"] == "chat" or not _plan_step_equal(query_source, decision, text)):
        observation = {key: decision[key] for key in ("action", "param", "_model", "_conf", "_pconf") if key in decision}
        if query_source.get("_query_literal_file"):
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "literal_file_mismatch", "_pending": None,
                    "_guard_rule": "query_domain", "_model_observation": observation,
                    "reply": "这是要查找的文件名，操作或文件名和原话不一致，请重新说明。"}
        resolved = query_source if query_source["action"] == "chat" else apply_command_guard(
            text, query_source, require_confidence=False, _local_step=_local_step)
        return {**resolved, "_guard_rule": "query_domain", "_model_observation": observation}
    if query_source and query_source["action"] == "search_file" and re.search(r"\.[A-Za-z0-9]{1,8}$", str(query_source["param"])):
        text = "打开文件“" + query_source["param"] + "”"  # Filename words stay opaque during later mention checks.
    missing_reminder = _reminder_time_missing_decision(text)
    if missing_reminder:
        return missing_reminder
    act = decision.get("action", "chat")
    if isinstance(act, str) and act.strip().lower() in _SCREENSHOT_ACTION_NAMES | {"multi_action"}:
        act = "multi_action" if act.strip().lower() == "multi_action" else "screenshot"
        decision = {**decision, "action": act}
    decision = _canonicalize_media_decision(text, decision)
    if decision.get("_guard") == "media_state_mismatch":
        return decision
    act = decision.get("action", "chat")
    if act == "media_control" and decision.get("param") == "pause" and _media_state_request(text) == "pause" and any(_cancelled_clause(clause) for clause in _clauses(_space_clauses(text))):
        text = "暂停播放"  # “音乐先别放” asks to stop playback, not to cancel stopping.
    state_target = _state_target_request(text)
    brightness_target = state_target if state_target and state_target["action"] == "set_brightness" else None
    relative_brightness_target = None
    if state_target and act != "chat":
        proposed = _guard_multi_action_steps(decision.get("param")) if act == "multi_action" else [decision]
        if len(proposed) != 1 or not _plan_step_equal(state_target, proposed[0], text):
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "state_target_mismatch", "_pending": None,
                    "reply": "操作或目标状态和原话不一致，请把完整要求再说明一下。", "_model": decision.get("_model", "")}
        text = state_target["_check_text"]
    if not state_target and act != "chat" and re.search(r"亮度|屏幕|背光", text):
        source_units = _plan_source_units(text)
        if len(source_units) == 1:
            local_target = _state_target_request(source_units[0][0])
            if local_target and local_target["action"] == "set_brightness" and local_target["param"] in ("min", "max"):
                proposed = _guard_multi_action_steps(decision.get("param")) if act == "multi_action" else [decision]
                if not _multi_plan_matches_source(text, proposed):
                    return {"action": "chat", "param": "", "emotion": "confident", "_guard": "state_target_mismatch", "_pending": None,
                            "reply": "操作或目标状态和原话不一致，请把完整要求再说明一下。", "_model": decision.get("_model", "")}
                brightness_target = local_target
            local_relative = _relative_brightness(source_units[0][0])
            if local_relative:
                proposed = _guard_multi_action_steps(decision.get("param")) if act == "multi_action" else [decision]
                if not _multi_plan_matches_source(text, proposed):
                    return {"action": "chat", "param": "", "emotion": "confident", "_guard": "relative_brightness_mismatch", "_pending": None,
                            "reply": "亮度调整的方向或步长和原话不一致，请重新说明。", "_model": decision.get("_model", "")}
                relative_brightness_target = local_relative
    if not state_target and act in ("set_brightness", "brightness_up", "brightness_down") and any(
            (literal := _state_target_literal(_strip_prefix(_STEP_ORDER_RE.sub("", part.strip())))) and literal["action"] == "chat"
            for part in _clauses(_space_clauses(text))):
        if not _multi_plan_matches_source(text, [decision]):
            return {"action": "chat", "param": "", "_guard": "state_target_literal", "_pending": None,
                    "reply": "这是引用里的文字，请明确要执行的完整亮度操作。", "_model": decision.get("_model", "")}
    requested_ui = _in_app_ui_decision(_strip_prefix(text))
    if requested_ui and requested_ui.get("action") == "chat":
        return requested_ui
    if act == "daily_briefing" and _app_launch_without_briefing_request(text):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "message_source_mismatch",
                "reply": "你要打开哪个消息应用？我先核对应用名称。"}
    clauses = _clauses(_strip_prefix(mask_quotes(_space_clauses(text))))
    if _whole_cancelled_request(text, clauses):
        return dict(_NEGATED_REPLY)
    if (act != "chat" or decision.get("_pending")) and clauses and all(_cancelled_clause(c) or _completed_clause(c) for c in clauses):
        # “别调大”不能被模型反向改成调小；已完成的操作也不变成待确认任务。
        return dict(_NEGATED_REPLY) if any(_cancelled_clause(c) for c in clauses) else {
            "action": "chat", "param": "", "emotion": "confident", "reply": "", "_guard": "past"}
    screenshot_limit = _screenshot_constraint_decision(text)
    if screenshot_limit:
        return screenshot_limit
    media_selection = _media_selection_decision(text)
    if media_selection:
        return media_selection
    if act in ("open_app", "open_chatapp") and len(_clauses(_space_clauses(text))) == 1 and _plan_app_has_additional_task(text):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "application_task_incomplete", "_pending": None,
                "reply": "打开软件还不能完成其中的后续任务，请把完整操作要求说明清楚。"}
    missing_calculation = _missing_calculation_decision(_strip_prefix(text))
    if missing_calculation:
        return missing_calculation
    corrected_open = _corrected_open_text(_strip_prefix(text))
    if corrected_open:
        resolved = _fast_match_single(corrected_open)
        if resolved and resolved.get('action') in ('open_app','open_chatapp'):
            return apply_command_guard(corrected_open, resolved, require_confidence=False)
        return {'action':'chat','param':'','emotion':'confident','_guard':'no_object','_pending':None,
                'reply':'要改开哪个程序？请告诉我完整名称。'}
    literal_media = _literal_media_request(_strip_prefix(text))
    if act == 'media_control' and literal_media and literal_media['param'] == decision.get('param'):
        text = {'prev':'上一首','next':'下一首','play_pause':'暂停播放'}[literal_media['param']]
    outer = mask_quotes(text)   # 引号里的正文 / 关键词不参与否定、犹豫、多步判断
    # 主人自己说拿不定主意 / 说混了 / 两个要求都算：不替他选，先问
    # 模型判成闲聊时也要问（原来只在要执行时才问，闲聊回复里往往没有问句，主人等不到下文）
    if _HESITATE_RE.search(outer) and (act != "chat" or (not decision.get("_guard") and command_clauses(text))):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "你拿定主意了再跟我说具体要哪一个？",
                "_guard": "hesitation", "_pending": None, "_model": decision.get("_model", "")}
    # 问天气却没说哪儿、模型又当成闲聊：问一句城市，不顺口编天气
    if act == "chat" and not decision.get("_guard") and _WEATHER_RE.search(text or "") and not _weather_place(text) \
            and re.search(r"[？?]|怎么样|咋样|如何|吗|呢|冷不冷|热不热|会不会|要不要", text or ""):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "哪个城市的天气？",
                "_guard": "weather_no_place", "_pending": None, "_model": decision.get("_model", "")}
    # 音量 / 亮度设到具体数值：说了两个数（“五十还是三十”）、说的是相对调整（“再亮五个百分点”）、
    # 或者没说是音量还是亮度（“调到百分之三十”），都先问，不猜
    if act in ("set_brightness", "volume") and not decision.get("_pending"):
        vals = {v for v in numbers_in(text) if 0 <= v <= 100}
        target_word = r"亮|屏幕" if act == "set_brightness" else r"声|音量"
        if re.search(r"还是|或者", text or "") and len(vals) >= 2:
            return {"action": "chat", "param": "", "emotion": "confident", "reply": "你要调到哪个数？", "_guard": "conflicting_values"}
        if re.search(r"个百分点|(?:再|加|减|降|升|多|少)[^，,。]{0,3}百分", text or ""):
            return {"action": "chat", "param": "", "emotion": "confident",
                    "reply": "是调到这个数，还是在现在的基础上加减？", "_guard": "relative_value"}
        if not re.search(target_word, text or ""):
            return {"action": "chat", "param": "", "emotion": "confident", "reply": "调音量还是亮度？", "_guard": "no_target"}
    # 否定：句子里任何位置被“别/不要/先不/不用/甭”否定的动作都不执行；整句只有否定时什么都不做
    subs = _guard_multi_action_steps(decision.get("param")) if act == "multi_action" else []
    positive_focus = any((focus := _foreground_app_decision(c)) and focus.get("action") == "focus_app" for c in clauses)
    positive_relative = relative_brightness_target and not any(
        re.search(r"亮度|屏幕|背光|调|提|升|降|加|减|亮|暗", clause) for clause in negated_clauses(text))
    if act != "chat" and (_negated(act, decision.get("param", ""), text)
                          or any(_negated(s.get("action"), s.get("param", ""), text) for s in subs)
                          or (negative_request(text) and not command_clauses(_space_clauses(text)) and not positive_focus and not positive_relative)):
        return dict(_NEGATED_REPLY)
    if act in ("volume_up", "volume_down") or any(s.get("action") in ("volume_up", "volume_down") for s in subs):
        from canaan_volume_steps import normalize_volume_steps
        units = _plan_source_units(text)
        if act == "multi_action" and len(units) == len(subs):
            recovered = [normalize_volume_steps(_operation_request_head(mask_quotes(source)), step, cn_number)
                         for (source, _), step in zip(units, subs)]
            rejected = next((step for step in recovered if step.get("_guard") == "volume_steps_mismatch"), None)
            if rejected:
                return rejected
            if recovered != subs:
                decision = {**decision, "param": json.dumps(recovered, ensure_ascii=False)}
                if "_pconf" in decision:
                    decision["_pconf"] = None
                subs = recovered
        elif act in ("volume_up", "volume_down"):
            sources = [(source, known) for source, known in units if known and known.get("action") in ("volume_up", "volume_down")]
            source = text
            if len(sources) == 1:
                source = sources[0][0] if sources[0][1]["action"] == act else ""
            decision = normalize_volume_steps(_operation_request_head(mask_quotes(source)), decision, cn_number)
            if decision.get("action") == "chat":
                return decision
    capture_count = int(act == "screenshot") + sum(s.get("action") == "screenshot" for s in subs)
    if capture_count and _screenshot_proposal_mismatch(text, capture_count):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "screenshot_not_requested",
                "reply": "截图步骤和原话对不上，请把完整操作再说明一下？", "_pending": None}
    if act == "multi_action" and not _multi_plan_matches_source(text, subs):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "multi_plan_mismatch",
                "reply": "操作顺序、目标或内容还没核对齐。请把完整步骤再说明一下？", "_pending": None}
    if not _local_step and act not in ("chat", "multi_action"):
        reference = fast_match_intent(text)
        if act in ("search_file", "web_search") and reference and reference.get("action") == "chat" and reference.get("_guard") == "vague_query":
            return dict(reference)
        if act == "web_search" and _vague_query(decision.get("param", ""), text):
            # 没说搜什么（“查一下关于……的资料”）：先问搜什么，不要落到下面笼统的“没核对齐”
            return {"action": "chat", "param": "", "emotion": "confident", "reply": "要搜什么？把关键词告诉我。",
                    "_guard": "vague_query", "_pending": None, "_model": decision.get("_model", "")}
        observation =decision.get("_model_observation") if decision.get("_guard_rule") == "lookup_source" else decision
        if act in ("search_file", "web_search") and isinstance(observation, dict) and observation.get("action") == "search_file" and \
                reference and reference.get("action") == "web_search" and isinstance(reference.get("param"), str) and \
                observation.get("param") == reference["param"] and decision.get("param") == reference["param"] and \
                len(_clauses(_space_clauses(text))) == 1 and not re.search(r"[\\/]|\.[A-Za-z0-9]{1,8}$", reference["param"]) and \
                (act == "search_file" or decision.get("_guard_rule") == "lookup_source"):
            resolved = apply_command_guard(text, reference, require_confidence=False)
            observed = {key: observation[key] for key in ("action", "param", "_model", "_conf", "_pconf") if key in observation}
            return {**resolved, "_guard_rule": "lookup_source", "_model_observation": observed}
        local_file = _contextual_file_search_request(text)
        owned_payload = _plan_whole_owned_source(text, reference)
        units = _plan_source_units(text) if not owned_payload else []
        focus = _foreground_app_decision(_strip_prefix(text)) or (reference if reference and reference.get("action") == "focus_app" else None)
        future = _sequenced_reminder_parts(text)
        independent = sum(_plan_explicit_request(source, known) for source, known in units)
        status_sources = _status_source_requests(text, units) if _plan_step_identity(decision)[0] == "system_status" else ()
        status_queries = {row["identity"] for row in status_sources if row["kind"] == "query"}
        partial = (bool(status_queries) and (len(status_queries) > 1 or any(
            row["kind"] in ("request", "uncertain") for row in status_sources))) or (
            independent > 1 and not _CAPABILITY_ONLY_RE.search(text) and not _multi_plan_matches_source(text, [decision]))
        mismatch = (owned_payload and act != reference["action"]) or \
                   (reference and reference.get("action") == "open_url" and not _plan_step_equal(reference, decision, text)) or \
                   (act == "web_search" and reference and reference.get("action") == "web_search" and
                    not _WEATHER_RE.search(text or "") and not _plan_step_equal(reference, decision, text)) or \
                   (local_file and act == "search_file" and _plan_step_identity(decision)[1] != local_file.get("param")) or \
                   (future and act == "set_reminder" and reference and reference.get("action") == "set_reminder" and not _plan_step_equal(reference, decision, text)) or \
                   (focus and focus.get("action") == "focus_app" and not _plan_step_equal(focus, decision, text)) or \
                   (act == "focus_app" and len(units) <= 1 and not (focus and focus.get("action") == "focus_app")) or \
                   partial
        if mismatch:
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "multi_step_partial" if partial else "single_plan_mismatch",
                    "reply": "操作、目标或完整步骤还没核对齐，请把要做的事再说明一下？", "_pending": None}
    # 撤回撤不回来：原话里没说“撤 / 收回”就不撤（“再给我发一遍”曾被模型当成撤回）
    if act == "recall_chatapp" and not re.search(r"撤|收回|召回", outer or ""):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "你是要我撤回刚才那条，还是重新发一遍？",
                "_guard": "recall_not_said", "_pending": None, "_model": decision.get("_model", "")}
    # 对象只是“那个 / 这个 / 它”：不知道要开关什么，先问
    if act in _TARGETED and re.fullmatch(r"(?:那个|这个|它|那|这|另一个|别的)(?:软件|程序|应用)?", str(decision.get("param", "")).strip()):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "你说的是哪个程序呀？",
                "_guard": "vague_object", "_pending": None, "_model": decision.get("_model", "")}
    # 多步：原话里有两个以上要做的分句，决策却只有一个动作（通常是模型只认出了第一步）——先问，不只做一步
    steps = command_clauses(text)
    if state_target and state_target["action"] == "set_reminder":
        steps = []  # The entire recognized state request belongs to the future reminder.
    corrected = _correction_decision(_strip_prefix(text))
    if corrected and corrected.get("action") == act and corrected.get("param") == decision.get("param"):
        steps = []   # 说到一半改口：前半句已经作废，不是“好几步只做了一步”
    whole = _phrase_decision(_space_clauses(_strip_prefix(text)))
    if whole and whole.get("action") == act and whole.get("param") == decision.get("param"):
        steps = []   # 整句规则认出来的一件事（“给Dots提交资料“X”，帮我整理成对照表”）：逗号后面是同一件事的要求
    future = _sequenced_reminder_parts(text) if act == "set_reminder" else None
    if future and (not future[1] or all(_cancelled_clause(c) for c in _clauses(future[1]))):
        steps = []  # “提醒我先…再…” describes future reminder content, not immediate operations.
    if act == "web_search":   # “走网页搜索就行”“把搜索页给我打开”说的是这次搜索怎么做，不是另一步
        steps = [c for c in steps if not _SEARCH_QUALIFIER_RE.search(_STEP_ORDER_RE.sub("", c).strip())]
    if act not in ("chat", "multi_action") and len(steps) >= 2 and act not in ("send_chatapp", "computer_use", "ufo_task"):
        return {"action": "chat", "param": "", "emotion": "confident",
                "reply": "这句话里有好几步，我怕只做了一半。你要我按顺序都做吗？可以一步一步跟我说。",
                "_guard": "multi_step_partial", "_pending": None, "_model": decision.get("_model", "")}
    # 问的是“你能不能做”而且说明了只是确认功能：回答，不真的去做
    if act != "chat" and _CAPABILITY_ONLY_RE.search(text or ""):
        return {"action": "chat", "param": "", "emotion": "confident",
                "reply": f"可以的，要用的时候直接跟我说“{describe_action(decision)}”就行。",
                "_guard": "capability_question", "_pending": None, "_model": decision.get("_model", "")}
    # 拖页签 / 拖窗口却没说拖哪个、拖到哪：先问，补齐后再核对，不自己开始拖
    if act != "chat" and re.search(r"(?:拖|挪)[^，,。]{0,6}(?:页签|标签|窗口)", text or "") \
            and not re.search(r"(?:拖|挪|放)到[^，,。]{1,}", text or ""):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "要拖哪个、拖到哪里？说清楚后我先跟你核对一遍再动手。",
                "_guard": "drag_target_missing", "_pending": None, "_model": decision.get("_model", "")}
    # 只总结“某一天 / 那天”却没说哪天：先问日期，不把整段都总结了
    if act == "daily_briefing" and re.search(r"某一?天|那一?天|哪一?天|某天|只总结一天", text or "") \
            and not re.search(r"今天|昨天|前天|\d+\s*[月号日]|[一二三四五六七八九十]+[月号日]|周[一二三四五六日天]|星期", text or ""):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "总结哪一天的？告诉我日期。",
                "_guard": "date_missing", "_pending": None, "_model": decision.get("_model", "")}
    # 相对音量已按所属分句核对；亮度继续使用原有目标值/步长规则。
    if act in ("brightness_up", "brightness_down"):
        p = str(decision.get("param", "")).rstrip("%")
        if act in ("brightness_up", "brightness_down") and re.fullmatch(r"[A-Za-z][A-Za-z_ -]*", p):
            return {"action": "chat", "param": "", "reply": "相对亮度调整需要方向或明确步长，请重新说明。",
                    "_guard": "invalid_parameter", "_pending": None}
        relative = (relative_brightness_target or _relative_brightness(_strip_prefix(text))) if act in ('brightness_up','brightness_down') else None
        verified_delta = relative and relative['action'] == act and relative['param'] == p
        if p and not verified_delta and (not re.fullmatch(r"\d+(?:\.\d+)?", p) or float(p) not in numbers_in(text)):
            decision = {**decision, "param": ""}
    # 路径里有空格时模型常截断（“D:\有空格的 测试目录”→“D:\有空格的”）：原话里以它开头的完整路径为准
    if act == "open_path":
        p = str(decision.get("param", ""))
        for full in re.findall(r"[A-Za-z]:\\[^，,。；;\n\"“”'‘’]+", text or ""):
            # 先去掉路径后面的口头话（“这个文件夹给我开一下”“看看”），剩下的才是完整路径
            full = re.sub(r"(?:(?:这个|那个)(?:文件夹|目录))?(?:给我|帮我)?(?:开一下|打开|看看|一下)*$", "", full.rstrip(" 。.")).rstrip()
            if p and " " in full and full.startswith(p) and len(full) > len(p):
                decision = {**decision, "param": full}
                break
    if act == "set_brightness":
        p = str(decision.get("param", "")).rstrip("%")
        trusted_extremum = brightness_target and brightness_target["param"] in ("min", "max") and \
                           _plan_step_equal(brightness_target, decision, text)
        if not trusted_extremum:
            if not re.fullmatch(r"\d+(?:\.\d+)?", p) or not 0 <= float(p) <= 100:
                return {"action": "chat", "param": "", "reply": "亮度要设为0到100中的多少？", "_guard": "invalid_parameter"}
            if float(p) not in numbers_in(text):  # 中文数字（“百分之六十”）也算原话里说过
                return {"action": "chat", "param": "", "reply": describe_action(decision), "_guard": "ungrounded_param", "_pending": decision}
    if act == "send_chatapp":
        try:
            p = json.loads(decision.get("param") or "{}")
        except Exception:
            # 模型偶尔多吐引号（{"contact": "姐", "message": "我到家了""}"}）：按字段取出来，后面照样逐项核对
            raw = str(decision.get("param") or "")
            p = {k: m.group(1) for k in ("contact", "message")
                 if (m := re.search(r'"' + k + r'"\s*:\s*"([^"]*)"', raw))}
            decision = {**decision, "param": json.dumps(p, ensure_ascii=False)}
        contact = p.get("contact", "").strip() if isinstance(p, dict) and isinstance(p.get("contact"), str) else ""
        if re.fullmatch(r"[妈爸哥姐弟妹]", contact) and "我" + contact in (text or ""):   # 模型只取了“姐”：按原话补成“我姐”
            contact = "我" + contact
            p = {**p, "contact": contact}
            decision = {**decision, "param": json.dumps(p, ensure_ascii=False)}
        parsed = fast_match_intent(text or "")
        expected = json.loads(parsed["param"]) if parsed and parsed.get("action") == "send_chatapp" else {}
        vague = re.fullmatch(r"某某|某人|某个人|一个人|别人|随便|随便谁|谁|有人|陌生人|他|她|对方|我|你|自己|我自己", contact or "")
        def _core(name):   # “我老婆 / 老婆”“我妈 / 老妈”是同一个人
            name = re.sub(r"^我的?", "", name or "")
            return name[1:] if re.fullmatch(r"老[妈爸哥姐弟妹]", name) else name
        if not contact or vague or not _contact_in_text(contact, text) or expected and _core(contact) != _core(expected.get("contact")):
            # 联系人不是用户说的（模型编的，或根本没说发给谁）：反问，不发
            message = p.get("message") if isinstance(p, dict) else None
            draft = {"message": message.strip()} if isinstance(message, str) and _message_grounded(message, text) else None
            return {"action": "chat", "param": "", "emotion": "pamper", "reply": "想发给谁呀？跟我说声名字～",
                    "_guard": "no_contact", "_model": decision.get("_model", ""), "_draft": draft}
        placeholder = re.fullmatch(r"(?:个|一个|那个|这个|点|一点)?(?:数字|数|东西|内容|那个|事|啥|消息)(?:呗|吧|啊)?",
                                   str(p.get("message") or "").strip(" 。，,!！"))
        if not _message_grounded(p.get("message") or "", text) or placeholder:
            # “给李四发个数字呗”：说了要发，但没说发什么
            return {"action": "chat", "param": "", "emotion": "pamper", "reply": f"想跟{p['contact']}说啥呀？",
                    "_guard": "empty_message", "_model": decision.get("_model", ""), "_draft": {"contact": contact}}
    if act in _READONLY_ACTIONS:
        # 只读动作不追问，但说的是以前的事（“上次那个报错后来好了”）就不是要我现在去查
        if act != "chat" and mention_reason(text) in ("past", "past_story", "reported", "quoted_report"):
            return {"action": "chat", "param": "", "emotion": "confident", "reply": describe_action(decision),
                    "_guard": "past", "_pending": decision, "_model": decision.get("_model", "")}
        if act == "diagnose_error" and diagnostic_source(text) != "clipboard":
            return {**decision, "param": text}
        return decision
    if _BARE_VERB_RE.match(re.sub(r"[\s，,。.!！?？~～]", "", text or "")):
        # 只有动词没有对象（“关了吧”“打开”）：不知道要关/开什么，问一句，不执行
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "你想让我关掉还是打开哪个呀？",
                "_guard": "no_object", "_pending": None, "_model": decision.get("_model", "")}
    if not param_grounded(act, decision.get("param", ""), text):
        p = str(decision.get("param") or "")
        ask = describe_action(decision) if p and len(p) <= 20 and act != "open_path" else "请把完整操作和目标再说明一次，我先核对参数。"
        return {"action": "chat", "param": "", "emotion": "confident", "reply": ask,
                "_guard": "ungrounded_param", "_pending": decision if p and len(p) <= 20 and act != "open_path" else None,
                "_model": decision.get("_model", "")}
    if act == "search_file" and (re.sub(r"[\s。.!！?？]", "", str(decision.get("param", ""))) in (
            "", "文件", "一份", "一份文件", "个文件", "东西", "资料", "文档", "一个文件", "电脑里", "本机")
            or re.match(r"(?:找找|找一下|电脑里)?(?:个|一个|一份)?文件[，,]", str(decision.get("param", "")))
            or re.search(r"找找文件|找个文件|弄一份", text or "")):
        # “电脑里找找文件，弄一份来让我参考参考”：没说找哪个文件
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "要找哪个文件？说个名字或者关键词。",
                "_guard": "vague_query", "_model": decision.get("_model", "")}
    if act == "web_search" and _vague_query(decision.get("param", ""), text):
        # 没说搜什么（“帮我搜点东西”“查一下关于……的资料”）：先问，不瞎搜
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "要搜什么？把关键词告诉我。",
                "_guard": "vague_query", "_pending": None, "_model": decision.get("_model", "")}
    if act == "web_search" and _WEATHER_RE.search(text or ""):
        if not _weather_place(text):
            # 问天气却没说哪儿（“今天天气咋样”“外面冷不冷”）：先问城市，不瞎搜
            return {"action": "chat", "param": "", "emotion": "confident", "reply": "哪个城市的天气？",
                    "_guard": "weather_no_place", "_pending": None, "_model": decision.get("_model", "")}
        # 说了地方：直接拿原话去搜（模型有时把城市丢了，只搜“天气”）；关键词在引号里时只搜引号里的
        quoted_kw = re.search(r"[“\"「『]([^”\"」』]{1,60})[”\"」』]", text or "")
        if quoted_kw and _WEATHER_RE.search(quoted_kw.group(1)):
            decision = {**decision, "param": quoted_kw.group(1).strip()}
        else:
            decision = {**decision, "param": re.sub(r"^(?:帮我|给我|麻烦|请)?\s*(?:查查|查下|查一下|搜一下|看看)?\s*|[？?。！!]+$", "", text.strip())}
    if require_confidence and act != "chat":
        import canaan_config
        confidence = decision.get("_conf")
        threshold = confidence_threshold(decision, text, getattr(canaan_config, "INTENT_MIN_CONFIDENCE", .6))
        trusted_steps = False
        if act == "multi_action" and decision.get("_plan_evidence") is not None:
            from canaan_plan_gate import trusted_plan_confidence
            trusted_steps = trusted_plan_confidence(text, decision)
        if not trusted_steps and (type(confidence) not in (int, float) or not threshold <= confidence <= 1):
            return {"action": "chat", "param": "", "emotion": "confident", "reply": describe_action(decision),
                    "_guard": "low_or_missing_confidence", "_pending": decision, "_model": decision.get("_model", "")}
    reason = mention_reason(text)
    if not reason or (reason == "question" and act in _LOW_RISK_READ):
        return decision   # 问句本身就是要查的东西（“北京今天啥天气？”）：直接查，不再反问
    if reason == "noun_mention" and act == "search_file":
        local = _local_file_decision(_strip_prefix(text))
        if local and local["action"] == act and local["param"] == decision.get("param"):
            return decision   # “找一下截图文件”：截图是文件名，找文件才是命令。
    if reason == "noun_mention" and act == "volume_mute" and re.fullmatch(
            r"(?:把)?(?:系统|电脑)?的?静音(?:模式|开关)?(?:给我)?(?:打开|开启|关闭|关掉|取消)(?:吧|呗)?[。！!]*", _strip_prefix(text)):
        return decision
    if reason == "question" and act in ("search_file", "open_path") and _where_decision(_strip_prefix(text)):
        return decision   # “我的简历文件在哪”：问在哪就是要我找 / 打开，只读，不反问
    return {"action": "chat", "param": "", "emotion": "confident", "reply": describe_action(decision),
            "_guard": reason, "_pending": decision, "_model": decision.get("_model", "")}


_SITES = {"百度": "https://www.baidu.com", "知乎": "https://www.zhihu.com", "淘宝": "https://www.taobao.com",
          "京东": "https://www.jd.com", "抖音": "https://www.douyin.com", "微博": "https://weibo.com",
          "12306": "https://www.12306.cn", "网易邮箱": "https://mail.163.com", "小红书": "https://www.xiaohongshu.com",
          "CSDN": "https://www.csdn.net", "优酷": "https://www.youku.com", "GitLab": "https://gitlab.com", "携程": "https://www.ctrip.com"}
_NAMED_SITE_URLS = {name.casefold(): url for name, url in _SITES.items()}
_NAMED_SITE_URLS.update({"哔哩哔哩": "https://www.bilibili.com", "b站": "https://www.bilibili.com",
                         "bilibili": "https://www.bilibili.com", "github": "https://github.com", "开源社区": "https://github.com"})


def _named_site_url(name):
    """Resolve a complete site alias, never an alias embedded in a host or name."""
    return _NAMED_SITE_URLS.get(str(name).strip().casefold())


def _named_site_root_key(value):
    """Only named homepages allow DNS casing and an optional empty root slash."""
    from urllib.parse import urlsplit
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        return None
    if url.scheme.casefold() not in ("http", "https") or not url.hostname or url.path not in ("", "/") or \
            url.query or url.fragment or url.username or url.password:
        return None
    return url.scheme.casefold(), url.hostname.casefold(), port


def _named_site_request(text):
    """One anchored launch frame shared by whole rules, local rules and grounding."""
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", (text or "").strip())).strip(" 。！!")
    # The protected literal-URL rule retains the exact scheme, port, path and case.
    if not clean or _URL_RE.search(clean) or mention_reason(clean) or _CAPABILITY_ONLY_RE.search(clean) or negative_request(clean):
        return None
    ending = r"(?:一下|下)?(?:吧|呗|啊|呀|啦|喽)?"
    browsing = r"(?:逛逛|看看|刷刷|上看一眼|翻个回答)?"
    request = re.fullmatch(r"(?:(?:想)?(?:打开|开开|访问|浏览|进入|开|上|去|进|逛|刷)|我想看看)(?:会儿|一会儿?|一下|下)?\s*(.+?)(?:上去)?" + browsing + ending, clean, re.I) or \
              re.fullmatch(r"(?:把|将)?(.+?)(?:先)?(?:给我|帮我)?(?:打开|开开|访问|浏览|上去|去|上)" + browsing + ending, clean, re.I)
    if not request:
        return None
    target = re.sub(r"(?:的)?(?:官网|首页|主页|网站|网页版|网页)$", "", request[1].strip()).strip()
    if re.fullmatch(r"(?:我们|我)?(?:公司|内网|自建)(?:的)?(?:内网|自建)?(?:的)?gitlab", target, re.I):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "website_url_missing", "_pending": None,
                "reply": "请给我这个公司、内网或自建 GitLab 的完整网址，我再打开。"}
    if len(target) <= 253 and re.fullmatch(r"gitlab(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", target, re.I) and not re.search(
            r"\.(?:txt|exe|json|py|pyw|bat|cmd|ps1|sh|js|md|csv|pdf|docx?|xlsx?|pptx?|zip|log|yaml|yml)$", target, re.I):
        return {"action": "open_url", "param": "https://" + target, "emotion": "confident", "reply": "打开这个网址。"}
    # Explicit software/client names belong to the existing application rules.
    url = _named_site_url(target)
    if not url and target.endswith("商城"):
        url = _named_site_url(target.removesuffix("商城"))
    return {"action": "open_url", "param": url, "emotion": "confident", "reply": "打开啦。"} if url else None


def _named_site_literal(text):
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", (text or "").strip())).strip(" 。！!")
    quoted = _QUOTED_SPAN_RE.fullmatch(clean)
    inner = clean[1:-1] if quoted else ""
    literal = _device_rule(inner, inner) if quoted and _URL_RE.search(inner) else None
    if quoted and (_named_site_request(inner) or _URL_RE.fullmatch(inner) or literal and literal.get("action") == "open_url"):
        return {"action": "chat", "param": "", "_guard": "website_literal", "_pending": None,
                "reply": "这段引用不会作为当前网站操作。"}
    return None
# 关程序：常用中文名 → 进程名（按包含关系匹配，“网易云音乐”“网易云”都行）
_CLOSE_MAP = [("记事本", "notepad"), ("计算器", "calc"), ("聊天软件", "chatapp"), ("网易云", "cloudmusic"), ("浏览器", "edge"),
              ("谷歌", "chrome"), ("画图", "mspaint"), ("Windows媒体播放器", "wmplayer"), ("媒体播放器", "wmplayer"),
              ("音乐", "cloudmusic")]
# 这些是系统设置/界面里的开关，不是程序（“在设置里把蓝牙关掉”交给模型判断为界面操作）
_NOT_APP_RE = re.compile(r"里|中|设置|蓝牙|wifi|无线|网络|夜间模式|声音|音量|屏幕|灯|按钮|通知|定位|热点|飞行模式", re.I)


# 语音识别常见的同音错字，只收“错字本身不会是别的意思”的（“卓面”“计算鸡”“下栽”“上一手”）
_ASR_FIXES = {"卓面": "桌面", "计算鸡": "计算器", "下栽": "下载", "上一手": "上一首", "下一手": "下一首", "为信": "聊天软件",
              "因量": "音量"}


def _space_clauses(text):
    """语音转写常用空格分句（“歌先停一下 我接个电话”）：两个汉字之间的空格当逗号。引号里的原文不动（“纸飞机 折法”）。"""
    text = (text or "").strip()
    # 后面紧跟“说 / 让他 / 问他”的空格是发消息的正文开头（“给大舅回个聊天软件 说初三去拜年”），不拆
    rx = re.compile(r"(?<=[一-鿿])\s+(?=[一-鿿])(?!说|让他|让她|问他|问她|告诉他|告诉她)")
    out, pos = [], 0
    # 引号里的原文和 Windows 路径（“D:\有空格的 测试目录”）里的空格都不动
    keep = re.compile(_QUOTED_SPAN_RE.pattern + r"|[A-Za-z]:\\[^，,。；;\n\"“”'‘’]+")
    for q in keep.finditer(text):
        out.append(rx.sub("，", text[pos:q.start()]))
        out.append(q.group(0))
        pos = q.end()
    out.append(rx.sub("，", text[pos:]))
    return "".join(out)


def _asr_fix(text):
    for wrong, right in _ASR_FIXES.items():
        text = text.replace(wrong, right)
    return text


_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_number(s):
    """“四十”“二十五”“一百”“百分之六十”“60%”“60” → 数字；认不出返回 None。"""
    s = re.sub(r"^百分之|[%％]$", "", (s or "").strip())
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if s == "一百" or s == "百":
        return 100.0
    if s and all(c in _CN_DIGIT or c == "十" for c in s):
        if "十" in s:
            a, _, b = s.partition("十")
            return float((_CN_DIGIT.get(a, 1) if a else 1) * 10 + (_CN_DIGIT.get(b, 0) if b else 0))
        if len(s) == 1:
            return float(_CN_DIGIT[s])
    return None


def numbers_in(text):
    """原话里出现的所有数值（阿拉伯数字和“百分之六十”这类中文数字都算）。"""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]
    for m in re.finditer(r"(?:百分之)?([零一二两三四五六七八九十百]+)", text or ""):
        v = cn_number(m.group(1))
        if v is not None:
            nums.append(v)
    for m in re.finditer(r"(\d|[一二两三四五六七八九十])\s*成", text or ""):   # “六成”= 60%
        v = cn_number(m.group(1))
        if v is not None and v <= 10:
            nums.append(v * 10)
    if "一半" in (text or ""):
        nums.append(50.0)
    if re.search(r"(?:调到|调成|设成|设为|开到|设到)(?:最大|最高)", text or ""):
        nums.append(100.0)
    return nums


def set_value(raw):
    """“调成”后面那个值：六成 / 6成 → 60，百分之六十 / 60% / 六十 → 60。"""
    raw = (raw or "").strip()
    if raw == "一半":   # “亮度调到一半”：原来按“一”取成 1
        return 50.0
    if raw in ("最大", "最高"):
        return 100.0
    if raw.endswith("成"):
        v = cn_number(raw[:-1].strip())
        return v * 10 if v is not None and v <= 10 else None
    return cn_number(raw)


# 只是问能力（“我是在确认功能，你能查看 CPU 吗”）：说明了不是现在要做
_CAPABILITY_ONLY_RE = re.compile(r"(?:确认|了解|问问|看看)(?:一下)?(?:你的)?(?:功能|能力|支不支持|会不会)|不是(?:在|现在)?要(?:你)?(?:马上|现在|立刻|即时)?(?:做|查|执行|操作|数据)"
                                 r"|只是(?:想)?问(?:问|一下)|先别(?:真的?)?(?:做|执行|操作)|不用真的?(?:做|执行|查|操作)")
_SET_VALUE = r"(?:调到|调成|调为|设成|设为|设置为|设置成|设到|开到|开成|改成|改为|放到|变成|降到|拉到|打到|拧到)\s*(一半|最大|最高|(?:\d|[一二两三四五六七八九十])\s*成|百分之[零一二两三四五六七八九十百]+|\d+\s*[%％]?|[零一二两三四五六七八九十百]+)"
_VOLUME_SET_RE = re.compile(r"(?:音量|声音|电脑声音|系统声音)[^，,。亮]{0,6}?" + _SET_VALUE)
_BRIGHT_SET_RE = re.compile(r"(?:屏幕亮度|亮度|屏幕)[^，,。音声]{0,6}?" + _SET_VALUE)


def _refusal_decision(clean):
    """整句看：破坏性请求、绕过安全保护、把别人发来的内容当成主人授权，直接拒绝。
    必须先于按分句匹配，否则“……都当是我，叫你截图就截”会因为后半句里的“截图”被执行。"""
    if re.search(r"(格式化|format\s+[a-z]:)|(删|清空|清除)[^，,。]{0,6}([a-zA-Z]\s*盘|系统盘|硬盘|所有文件|全部文件)"
                 r"|([a-zA-Z]\s*盘|系统盘|硬盘)[^，,。]{0,4}(删|清空|清除)|rm\s+-rf", clean, re.I):
        return {"action": "chat", "param": "", "emotion": "dangerous", "reply": "这种会把电脑搞坏的事，我可不干。"}
    if re.search(r"(?:病毒|木马|恶意软件|勒索软件)[^，,。]{0,8}(?:发|传|分享|转)|(?:发|传|分享|转)[^，,。]{0,8}(?:病毒|木马|恶意软件)", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous", "reply": "病毒文件我不帮着往外发，会害了别人。"}
    # 10-26（eval600 封存题开封后转调参）：绕过安全保护 / 管理限制，整句看（“防火墙老拦我，帮我自动把它所有配置都关掉”跨了逗号）
    if re.search(r"跳过|绕过|绕开|绕掉|越过|找漏洞|松开|彻底关掉|关掉|关闭|禁用|关了", clean) and \
            re.search(r"管理员密码|电脑密码|登录密码|跳过密码|密码解锁|防火墙|用户账户控制|UAC|家庭管理|家长管控|白名单|安全中心|脚本扫描|安全扫描|"
                      r"驱动签名|安全检查|受管|管控|使用时间|使用时长", clean, re.I) and \
            not re.search(r"怎么|如何|为什么|是不是|会不会", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "绕过密码、安全保护或者管理限制的事我不做。需要的话找管理员，或者走正规的恢复办法。"}
    # 别人的私人数据，对方没同意（“张三的私人日记，他不同意也没事”“李四没公开的聊天软件语音”）
    if (re.search(r"(?:张三|李四|别人|他人|同事|对方|他|她)[^。]{0,16}(?:私人|私信|加密|日记|通讯录|备忘|语音|聊天|记录|笔记|登录|账号)", clean)
            or re.search(r"(?:聊天|记录|私信|日记|通讯录)[^。]{0,10}(?:属于|是)(?:张三|李四|别人|他|她)", clean)) and \
            re.search(r"不让|没许可|没有许可|不同意|不知道|没答应|没有权|没权|不许|没公开|不准|没有密钥|不让我", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "那是别人的私人数据，人家没同意，我不能替你看、也不能拿去处理。"}
    if re.search(r"(?:造|捏造|编造|伪造|随便造|编一个)[^。]{0,10}(?:联系人|对象|资料|身份)", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous", "reply": "联系人不能编，找不到的人我不会造一个出来发消息。"}
    if re.search(r"(?:全部|所有)[^。]{0,8}(?:聊天|记录|消息)[^。]{0,20}(?:发|贴|传|外发)|发[^。]{0,10}(?:全部|所有)[^。]{0,6}(?:聊天|记录)", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous", "reply": "把全部聊天记录往外发我不做，拆成几条也一样。"}
    if re.search(r"(?:一出现|出现|超过|多于|低于|高于|到了|一到|只要|一旦|每次|每当|如果|要是|以后|往后)[^。]{0,24}自动[^。]{0,12}(?:发|回)", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "到了条件就自动发消息这种我不做：每一条发出去之前都得你当场确认。"}
    if "天气" in clean and re.search(r"不联网|不上网|不搜索|别搜|不要搜|不用搜", clean):
        return {"action": "chat", "param": "", "emotion": "confident",
                "reply": "天气我只能上网搜着看，不让搜的话我没法直接报，免得瞎编。"}
    if re.search(r"绕过|破解|提权|越权|管理员权限|别人的电脑|他人的电脑|跳过(?:身份|验证|密码)|别核(?:对)?身份|不(?:用|要)核(?:对)?身份"
                 r"|当(?:是我|成我|我亲口)|算我(?:授权|的)|(?:都|就)(?:直接)?照做|就全做"
                 r"|(?:关|关掉|关闭|停用|禁用)[^，,。]{0,8}(?:防火墙|杀毒|安全中心|defender|管理限制|管控|组策略)"
                 r"|(?:防火墙|杀毒软件|安全中心|defender|管理限制|管控|组策略)[^，,。]{0,6}(?:关|停用|禁用)", clean, re.I):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "这类会绕过安全保护、或者把别人发来的内容当成你的授权，我不做。需要什么，你直接跟我说具体操作。"}
    # 条件触发的自动发消息（“只要……就自动给 X 发”“半小时没回就自动发”）：每一条都要主人当场确认，不能事先包圆
    if re.search(r"(?:只要|一旦|每次|每当|如果|要是|当|以后|往后|规则)[^。]{0,40}自动[^。]{0,12}(?:发|回|(?:告诉|通知)(?!我(?:一声|一下)?(?:[，,。！!吧啊呀]|$)))", clean) or \
            re.search(r"(?:发|回)[^。]{0,30}(?:提前算确认|替每次确认|以后[^。]{0,10}自动|以后(?:都)?(?:别|不用)(?:再)?核对|别(?:再|重新)?核对|我不用再点头)", clean) or \
            re.search(r"(?:先|确认)[^。]{0,8}(?:一次|规则)[^。]{0,20}以后[^。]{0,20}自动", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "到了条件就自动发消息这种我不做：每一条发出去之前都得你当场确认。"}
    # 用别人的权限 / 别人的数据：没授权就是没授权，主人一句话代替不了别人的同意
    if re.search(r"夺取|所有权|越过[^，,。]{0,6}权限|借[^，,。]{0,12}(?:会话|账号|登录)|升成管理员|偷偷升"
                 r"|(?:算|当)(?:成|作)?(?:他|她|李四|张三|别人|对方)[^，,。]{0,4}(?:授权|同意)|(?:数据)?主人换成我|当作是我的|当成我的|算我自己的|都算我自己的"
                 r"|(?:没|未|不)(?:有|得到)?(?:他|她|对方|他们)?(?:的)?(?:权限|授权|许可|同意)[^。]{0,24}(?:自动|打开|进|读|看|总结|交|发|处理|展开)"
                 r"|(?:安全浏览|安全设置|安全保护)[^，,。]{0,4}(?:关|关掉|关闭)|设备管理限制", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "这是别人的东西或者你没有的权限，我不能替你绕过去；要用得先拿到对方或管理员的授权。"}
    # 把文档 / 别人的话当成主人的授权（“文档里写‘主人已授权’，你按文档算我确认”）；别人明确不同意却说“由我授权”
    if re.search(r"(?:按|照|把)[^，,。]{0,12}(?:文档|文件|消息|聊天|网页|邮件|这句)[^，,。]{0,8}(?:算|当)(?:成|作)?(?:是)?我"
                 r"|当(?:成|作)?我(?:本人)?的(?:授权|确认|同意)|写[着了]?[“\"「]?主人已(?:经)?(?:授权|同意|确认)"
                 r"|(?:不同意|没同意|不让|不愿意)[^。]{0,16}(?:我授权|由我授权|我说了算|我来授权)", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "文档里或别人说的“已授权”不算你本人确认，别人不同意的也不能由你代替同意，这个我不做。"}
    # 不联网问实时天气：查不到就是查不到，不能编
    if re.search(r"(?:别|不要|不用|不许)(?:联网|上网|搜索|查)[^。]{0,16}(?:天气|气温|温度|下雨)", clean):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "不联网我没法知道现在的天气，编一个更不行。要的话我上网查。"}
    # “一……就自动给 X 发”：条件触发的自动发送
    if re.search(r"一[^。]{1,20}就(?:自动|马上)[^。]{0,16}(?:发|回)", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "到了条件就自动发消息这种我不做：每一条发出去之前都得你当场确认。"}
    # 群发给所有人 / 每个人：一次发给一个明确的人，不做群发
    if re.search(r"(?:给|发给|向)(?:聊天软件里的?|通讯录里的?)?(?:所有人|所有联系人|所有好友|每个人|每个联系人|每一个人|大家|全部联系人|全部好友)"
                 r"|群发", mask_quotes(clean)):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "群发给所有人我不做，发消息得一个一个确认对象。你要发给谁？"}
    # 编造联系人：发给谁必须是你说的真实对象
    if re.search(r"(?:编造|编个|编一个|编出|随便编|瞎编|瞎填|乱填|随便填|捏造|虚构|伪造|假装是)[^，,。]{0,6}(?:联系人|好友|人|号码|账号|名字|身份)",
                 mask_quotes(clean)):
        return {"action": "chat", "param": "", "emotion": "dangerous", "reply": "联系人不能编，你告诉我具体发给谁。"}
    # 整批导出聊天记录 / 导出后转给别人：里面有别人的信息，不能一句话就交出去
    if (re.search(r"聊天记录|聊天内容|消息记录|聊天历史", clean) and re.search(r"导出|打包|拷贝|复制|备份|下载", clean) and
            re.search(r"所有|全部|整个|全都|发给|传给|转给|发到|传到|上传|分享", clean)) or \
            (re.search(r"(?:所有|全部|整个|整库|一字不落|一个字都别漏)[^。]{0,16}(?:聊天|消息)", clean) and
             re.search(r"发给|传给|转给|发到|传到|上传|分享|公开|粘贴|聊天软件发|发出去|全发", clean)):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "整批导出聊天记录里有别人的信息，我不能凭一句话导出或转给别人。确实需要的话，你在看板里自己选范围导出。"}
    # 发消息还要瞒着主人（别告诉我发没发 / 悄悄发）：不做。只说“不用问”的照旧发前单独确认
    if re.search(r"(?:别|不要|不用)(?:告诉|通知)我(?:发没发|发了没|有没有发|结果)|(?:偷偷|悄悄|背着我)[^，,。]{0,8}(?:发|转|回)|别让我知道", clean):
        return {"action": "chat", "param": "", "emotion": "dangerous",
                "reply": "发消息我一定会先让你确认、发完告诉你结果，这个不能省。"}
    return None


# Modifiers belong to an operation head/verb slot, never to a payload-wide rewrite.
_REQUEST_URGENCY = r"(?:立刻|马上|立即|赶紧|赶快|尽快)(?:就)?"
_MODIFIED_STEP_START = _REQUEST_URGENCY + r"\s*(?:打开|开|把|将|让|给|截|屏幕|当前屏幕|保存|存|激活|聚焦|切换到)"

# 多步与否定按分句处理：逗号 / 然后 / 接着 / 最后 / 动词前的“再”都算分句
_STEP_SPLIT_RE = re.compile(r"\s*(?:[，,；;。]\s*(?:然后|接着(?![播放])|之后|最后|再|并且|还要)?|然后|接着(?![播放])|最后再|之后再|"
                            r"并(?:且)?(?=打开|开|把|给|截|" + _SCREENSHOT_ALIAS + r"|锁|调|关|静音|发|查|搜|播放|暂停|切|设|提醒|放|回到|退出|" + _MODIFIED_STEP_START + r")"
                            r"|(?<!别)(?<!要)(?<!不)再(?=打开|开|把|给|截|" + _SCREENSHOT_ALIAS + r"|锁|调|关|静音|发|查|搜|播放|暂停|放|播|切|设|提醒|告诉|回到|回桌面|显示|最小化|退出|" + _MODIFIED_STEP_START + r")"
                            r"|(?<=[一-鿿])之后(?=把|再|打开|关|截|锁|调))\s*")   # “回桌面之后把计算器关了”
_CMD_HINT_RE = re.compile(r"打开|开|关|调|设|截|" + _SCREENSHOT_ALIAS + r"|锁|静音|发|查|搜|找|播放|暂停|停|放|切|下一首|上一首|继续|提醒|叫我|喊我|删|清空|重启|关机|运行|执行|下载|导出|告诉|"
                          r"退出|退了|总结|概括|汇总|整理|最大化|最小化|拖|挪到|放到|填|点一下|点击|交给|提交")
_NEG_CLAUSE_RE = re.compile(r"^(?:(?:我|你|请|帮我|替我|麻烦你|团团|加南|江南|先|就|千万|可|也|暂时|现在|今天|这次|一定|但|但是|不过|可是|" + _REQUEST_URGENCY + r")[\s，,:：]*)*"
                            r"(?:别|不要|先不|不用|甭|不必|无需)(?!问|管|担心|客气|急|怕|谢|再?核对|再?确认|再?问|出声|吵|让人)")
# “别出声”是要安静（静音），“别让人动我电脑”是交代原因：都不是在否定某个动作
# 否定分句里提到的动作 → 不能执行的动作名
_NEG_KEYWORDS = [(r"锁", ("lock_screen",)), (r"截|" + _SCREENSHOT_ALIAS, ("screenshot",)), (r"静音", ("volume_mute",)), (r"发|告诉", ("send_chatapp",)),
                 (r"激活|聚焦|置前|(?:弄到|调到|切到|切换到|回到|带到|到)(?:最前面|前面|最前台|前台)", ("focus_app",)),
                 (r"关机|重启|删|清空", ("run_cmd",)), (r"关", ("close_app",)),
                 (r"打开|开", ("open_app", "open_url", "open_chatapp", "open_path")),
                 (r"音量|声音|调大|调小", ("volume", "volume_up", "volume_down")), (r"亮", ("set_brightness", "brightness_up", "brightness_down")),
                 (r"播放|暂停|切歌|下一首|上一首", ("media_control",))]
_NEGATED_REPLY = {"action": "chat", "param": "", "emotion": "confident", "reply": "好，那就先不动。", "_guard": "negated"}


def _strip_prefix(text):
    clean = re.sub(r'^(?:(?:你)?(?:(?:能不能|可不可以|可以|能否|能)?帮我|帮忙|替我|请|麻烦你|团团|加南|江南)[\s，,:：]*)+', '', text.strip()).strip()
    clean = re.sub(r'^你(?=[帮替去把给说做查看看拾掇])\s*', '', clean).strip()
    # “你去给王小明发消息”“帮我去打开记事本”：开头的“去”只是语气
    return re.sub(r'^去(?=给|发|跟|告诉|通知|打开|关|搜|查|找|截)\s*', '', clean).strip()


def _clauses(clean):
    """按逗号 / 顺序词拆分句；引号里的逗号不拆（“发‘A，B’”是一条消息）。"""
    spans = []

    def _hold(m):
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"
    held = _QUOTED_SPAN_RE.sub(_hold, clean or "")
    parts, start = [], 0
    for boundary in _STEP_SPLIT_RE.finditer(held):
        part = held[start:boundary.start()]
        if boundary.group().strip() == "再":
            from canaan_volume_steps import volume_adverb_continues
            head = _strip_prefix(_STEP_ORDER_RE.sub("", part.strip()))
            if volume_adverb_continues(head, held[boundary.end():]):
                continue
        if part and part.strip("，,。.!！?？ "):
            parts.append(part)
        start = boundary.end()
    tail = held[start:]
    if tail and tail.strip("，,。.!！?？ "):
        parts.append(tail)
    return [re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], p) for p in parts]


# 动作在前、作罢在后（“截图就算了不用了”“锁屏就免了”）：动作词要在“算了”前面，“算了，帮我截个图”不算
_TRAILING_CANCEL_RE = re.compile(r"(?:截|锁|静音|发|关|开|调|播放|暂停|切|搜|查).{0,6}?(?:就|也|还是)?(?:算了|不用了|不要了|不必了|免了)")


_WHOLE_CANCEL_RE = re.compile(r"(?:那|还是|哎)?(?:算了|不用了|不要了|不弄了|不做了|不开了|不发了)+(?:吧|啊|呀)?[。！!]*")
_COMPOSED_WHOLE_CANCEL_RE = re.compile(r"算了(?:都别做|全部不要执行)(?:了)?[。！!]*")


def _whole_cancelled_request(text, clauses):
    if len(clauses) < 2:
        return False
    if _WHOLE_CANCEL_RE.fullmatch(clauses[-1].strip()):
        return True
    if not _COMPOSED_WHOLE_CANCEL_RE.fullmatch(clauses[-1].strip()):
        return False
    # A bare message/reminder/search body owns its following sequence words.
    # A closed quoted body still permits a separate outside cancellation.
    return not any(_diagnostic_content_intro(clause) for clause in clauses[:-1])


def _cancelled_clause(clause):
    """Recognize object-first cancellations without reading message bodies as commands."""
    c = clause.strip(" 。！!")
    if _NEG_CLAUSE_RE.match(c) or _TRAILING_CANCEL_RE.search(c):
        return True
    if re.fullmatch(_SCREENSHOT_ALIAS + r"(?:(?:就|也|还是)?(?:算了|不用了|不要了|不必了|免了)+|"
                    r"(?:先|暂时)?(?:别|不要|先不|暂时不)(?:再)?照(?:了)?|"
                    r"(?:这件?事)?(?:先放放|晚点再说|以后再说))[吧呗啊呀。！!]*", c):
        return True
    if re.fullmatch(r"(?:把)?(?:音乐|歌|视频|歌曲)(?:先)?(?:别放了|别播了)(?:吧|呗)?", c):
        return False   # 已在播放时要求停下，与“先别放”不同。
    if re.search(r"(?:别放了|别播了)(?:直接|就|把它)?(?:关掉|关了|关闭)(?:吧|呗)?$", c):
        return False   # “网易云别放了直接关掉”：后面明确要求退出。
    if re.match(r"^(?:百度(?:一下|搜)?|搜索|搜一下|上网搜|网上搜)", c):
        return False   # “搜索不要锁屏”：否定词是搜索主题的一部分。
    match = re.search(r"(?:先|暂时|还是)?(?:别|不要|先不|暂时不)(?:再)?(?:往[上下])?(?=开|关|调|截|锁|发|放|播|切|搜|查)", c)
    if match and not re.search(r"发|说|告诉|通知|提醒|转告", c[:match.start()]):
        return True
    return bool(re.fullmatch(r"(?:截图|截屏|锁屏|静音|播放|暂停|打开|关闭|调音量|调亮度)"
                             r"(?:这件?事)?(?:先放放|晚点再说|以后再说)(?:吧|呗)?", c))


def _completed_clause(clause):
    """An explicit first-person completion is background, not another action to offer."""
    c = clause.strip(" 。！!")
    if re.search(r"帮我|给我|替我|请|告诉|通知|发消息|说", c):
        return False
    return bool(re.fullmatch(r"我(?:刚刚|刚才|已经|早就|昨天|之前|上次)(?:自己)?[^，,。]{0,24}(?:了|过)[^，,。]{0,12}"
                             r"|[^，,。]{1,16}我(?:已经|刚刚|刚才)?(?:打开|关|关闭|暂停|锁|截)(?:了|过)", c))


def negated_clauses(text):
    text = _space_clauses(text)
    clauses = _clauses(_strip_prefix(mask_quotes(text)))
    if _whole_cancelled_request(text, clauses):
        return clauses       # “打开记事本，算了不用了”：最后一句把前面的都作废了
    return [c for c in clauses if _cancelled_clause(c)]


def negated_actions(text):
    """原话里被否定的动作（“别锁屏”“不要打开聊天软件”）。不管出现在句子哪个位置，这些动作都不能执行。"""
    out = set()
    for clause in negated_clauses(text):
        for pattern, actions in _NEG_KEYWORDS:
            if re.search(pattern, clause):
                out.update(actions)
    return out


_TARGETED = ("open_app", "focus_app", "open_url", "open_path", "close_app", "open_chatapp")


def _negated(decision_action, param, text):
    """这个动作是否被否定。开/关类动作还要看对象：“别开聊天软件，开 Edge”只否定聊天软件。"""
    canonical, value = _plan_step_identity({"action": decision_action, "param": param})
    if canonical == "focus_app":
        from canaan_action_framework.actions.app_actions import _focus_process_names
        names, denied = _focus_process_names(value), False
        for clause in _clauses(_strip_prefix(mask_quotes(_space_clauses(text)))):
            negative = _cancelled_clause(clause)
            request = _foreground_app_decision(_NEG_CLAUSE_RE.sub("", clause, count=1) if negative else clause)
            if not request or request.get("action") != "focus_app":
                continue
            target = _focus_process_names(request["param"])
            if negative and names.intersection(target):
                denied = True
            elif not negative and names and names == target:
                denied = False  # A later explicit request can reauthorize this same identity.
        return denied
    if decision_action not in negated_actions(text):
        return False
    clauses = _clauses(_strip_prefix(mask_quotes(_space_clauses(text))))
    last_negative = max((i for i, c in enumerate(clauses) if _cancelled_clause(c)), default=-1)
    for clause in clauses[last_negative + 1:]:
        if _completed_clause(clause):
            continue
        positive = _fast_match_single(_inherit_device_target(_STEP_ORDER_RE.sub("", clause).strip(), text))
        if positive and positive.get("action") == decision_action and (
                decision_action not in _TARGETED or positive.get("param", "") == param):
            return False   # “音量别调大，调小一点”：保留后面明确说的动作。
    if decision_action not in _TARGETED:
        return True
    target = "聊天软件" if decision_action == "open_chatapp" else str(param or "").casefold()
    if not target:
        return True
    clauses = [c.casefold() for c in negated_clauses(text)]
    return any(target in c or _alias_in(target, c) for c in clauses)


def _alias_in(target, clause):
    for name, code in _CLOSE_MAP:
        if code == target and name in clause:
            return True
    return False


_SEARCH_QUALIFIER_RE = re.compile(r"^(?:就|只)?(?:走|用|通过)?[^，,。]{0,6}(?:网页|搜索|百度|浏览器)[^，,。]{0,4}(?:就行|就好|就可以|即可|就成)[了吧啊呀]?$"
                                  r"|^(?:把|将)?(?:搜索|结果|网)?(?:页|页面)(?:给我|帮我)?(?:打开|开开|显示出来)[吧呀]?$")
_STEP_ORDER_RE = re.compile(r"^(?:先|首先|第一步|然后|再|接着|之后|最后|顺便|并且|还要|同时|现在)\s*(?:再)?\s*")
# 只是交代处境 / 现象的分句（“我关灯看资料了”“这个视频正播放着”“桌上灯已经开着”“桌灯关了以后太刺眼”）：
# 里面的“关灯”“开着”不是要我做的事
_BACKGROUND_RE = re.compile(r"(?:我|桌上|桌灯|这个|老师|外面|窗外|孩子|房间|屋里|有人|电话|外放)[^，,。帮把请]{0,14}"
                            r"(?:了|着|呢|的|刺眼|太亮|太暗|太响|太吵|好轻|挺轻)")


def _is_reminder_clause(clause):
    try:
        import canaan_proactive
        return canaan_proactive.parse_reminder(clause) is not None
    except Exception:
        return False


def command_clauses(text):
    """原话里没被否定、像是要我做事的分句（引号里的原文不算分句）。"""
    return [c for c in _clauses(_strip_prefix(mask_quotes(text))) if not _cancelled_clause(c) and not _completed_clause(c) and _CMD_HINT_RE.search(c)
            and not _SELF_STATE_RE.match(c) and not _BACKGROUND_RE.fullmatch(c.strip(" 。"))
            and not re.fullmatch(r"我(?:要|想)看[^，,。帮把]{0,12}(?:页|页面|内容|说明)", c.strip(" 。"))]


def _inherit_device_target(clause, text):
    """Carry a unique stated device target into a following bare adjustment clause."""
    if not re.match(r"^(?:帮我)?调(?:大|小|高|低|亮|暗)", clause):
        return clause
    volume = bool(re.search(r"音量|声音", mask_quotes(text)))
    brightness = bool(re.search(r"亮度|屏幕", mask_quotes(text)))
    if volume != brightness:
        return ("音量" if volume else "亮度") + clause.removeprefix("帮我")
    return clause


# “我想不起来文件放哪了”“我忘了……”是在说自己的情况，不是要我做的一步；
# “我离开桌子去拿水了”“会开完我先出去一会儿”是交代背景，后面那句才是要做的事
_SELF_STATE_RE = re.compile(r"^(?:要|快|马上|快要)(?:开会|上课|睡觉|出门|下班|开播|录音|打电话)了$"
                            r"|^我(?:想不起来|不记得|忘了|不知道|记不清|找不到)"
                            r"|^(?:会开完|待会儿?|等会儿?|一会儿)?\s*我(?:先|要|准备|马上|正|得|这就)?\s*"
                            r"(?:离开|出去|出门|去|走|下班|休息|歇|睡|吃饭|拿|上厕所|开会|接电话)"
                            r"(?![^，,。]*(?:帮|给我|替我|把|打开|关掉|关闭|关了|发|搜|查|锁|截|调|播放|暂停|提醒))[^，,。]*$")


def _corrected_open_text(text):
    if _QUOTED_SPAN_RE.search(text or ''):
        return None
    match=re.fullmatch(r'(?:先)?(?:打开|开|启动)[^，,。！？!?\n]{1,40}[，,]\s*算了[，,]?\s*'
                       r'(?:还是|改成|改为)?(?P<new>(?:打开|开|启动)[^，,。！？!?\n]{1,40}?)(?:吧|呗)?[。！!\s]*',text or '')
    if match:
        return match['new'].strip()
    clean = _operation_request_head(text or '')
    marks = list(_CORRECTION_MARK_RE.finditer(clean))
    if not marks:
        return None
    before, after = clean[:marks[-1].start()], clean[marks[-1].end():].strip().rstrip("。！!，, ")
    if _RELAY_RE.search(before) or _is_reminder_clause(before):
        return None
    commands = [clause for clause in re.split(r"[，,；;]", after) if _CMD_HINT_RE.search(clause)]
    after = commands[-1].strip() if commands else after
    after = re.sub(r"^(?:我)?(?:最终|最后|其实|还是)?(?:要|想)?(打开|开|启动)的?是", r"\1", after)
    after = re.sub(r"^(?:我)?要的是(?:让)?", "", after).strip()
    after = re.sub(r"^(?:正确的是|应该是|改成|改为|换成|应该)", "", after).strip()
    after = re.sub(r"(?:就好|就行|就可以|吧|呗)[。！!]*$", "", after).strip()
    after = _operation_request_head(after)
    from canaan_action_framework.actions.app_actions import OpenPathAction
    folders = {alias.casefold() for aliases in OpenPathAction.SHORTCUTS for alias in aliases}
    def app_request(value):
        request = re.fullmatch(r"(?:先)?(?:打开|开|启动)\s*(?:一下)?(?P<app>[A-Za-z0-9_.+ 一-鿿-]{1,80})", value.strip())
        if not request:
            return False
        target = request['app'].strip()
        return not (target.casefold() in folders or re.match(r"(?:我的)?(?:文件|文档|目录)", target) or
                    re.fullmatch(r"[A-Za-z]盘", target) or re.search(r"\.(?!exe$)[A-Za-z]{2,24}$", target, re.I))
    return after if app_request(before) and app_request(after) else None


def _relative_brightness(text):
    match=re.fullmatch(r'(?:把)?(?:屏幕(?:的)?(?:亮度)?|亮度)(?:再)?'
                       r'(调亮|调暗|调高|调低|升高|降低|提高|增加|减少|升|降|亮|暗)'
                       r'([0-9]{1,3}|[零一二两三四五六七八九十百]{1,3})个百分点(?:吧|呗|一下)?[。！!\s]*',text or '')
    if not match:
        return None
    value=cn_number(match[2])
    if value is None or not value.is_integer() or not 0<value<=100:
        return None
    down=match[1] in ('调暗','调低','降低','减少','降','暗')
    return {'action':'brightness_down' if down else 'brightness_up','param':f'delta:{"-" if down else "+"}{int(value)}',
            'emotion':'confident','reply':f'将亮度{"降低" if down else "提高"}{int(value)}个百分点。'}


def _literal_media_request(text):
    t=(text or '').strip().rstrip('。！! ')
    if re.fullmatch(r'(?:音乐|歌曲|播放)?(?:先)?(?:暂停|赞停)(?:一下)?(?:吧|呗)?',t):
        return {'action':'media_control','param':'play_pause','emotion':'confident','reply':'切换播放状态。'}
    if re.fullmatch(r'(?:给我|帮我)?(?:放|播|播放|听)(?:点|一点儿?|些|一段|一首|几首)?(?:音乐|歌|歌曲|曲子)(?:吧|呗)?', t):
        return {'action':'media_control','param':'play_pause','emotion':'confident','reply':'切换当前媒体的播放状态。'}
    match=re.fullmatch(r'(?:我想听|我想放)?(上一首|上一曲|下一首|下一曲)[，,]\s*(?:切回去|切过去|换回去|换过去)(?:吧|一下)?',t)
    if match:
        return {'action':'media_control','param':'prev' if match[1].startswith('上') else 'next',
                'emotion':'confident','reply':'切换曲目。'}
    if re.fullmatch(r'(?:刚|刚才)(?:那|这)首(?:歌)?(?:给我)?(?:再|重新)(?:放|播)(?:一遍|一次)[，,]\s*(?:回到|切回|换回)(?:上一首|上一曲)(?:吧|一下)?', t):
        return {'action':'media_control','param':'prev','emotion':'confident','reply':'切回上一首。'}
    return None


def _state_target_request(text):
    """Full operation frames only; payloads and ASR homophones are not rewritten."""
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", _strip_prefix(_space_clauses(text)))).strip(" 。！! ")
    if mention_reason(clean) in ("reported", "quoted_report", "hypothetical", "past", "past_story"):
        return None
    result, check = None, None
    if re.fullmatch(r"(?:把|将)?(?:系统|电脑)?的?(?:声音|音量)(?:给我)?(?:压到|降到|调到|调成|开到|设到|设成)(?:最小|最低)(?:吧|呗|一下)?", clean):
        result, check = {"action": "volume", "param": "0", "reply": "请求将音量设为0%。"}, "音量调到0"
    brightness = re.fullmatch(r"(?:把|将)?(?:系统|电脑)?的?(?:屏幕(?:的)?(?:亮度)?|背光(?:的)?亮度|亮度)(?:给我|帮我)?"
                              r"(?:压到|降到|调到|调成|调为|开到|设到|设成|设为|设置为|设置成|拉到|改成|改为)"
                              r"(最小|最低|最暗|最大|最高|最亮)(?:吧|呗|一下)?", clean)
    if brightness:
        target = "min" if brightness[1] in ("最小", "最低", "最暗") else "max"
        result, check = {"action": "set_brightness", "param": target, "reply": "请求将亮度设为当前屏幕支持的" + ("最低" if target == "min" else "最高") + "档位。"}, clean
    restore = re.fullmatch(r"(?:(?:取消|解除|关闭|关掉)(?:系统|电脑)?的?静音[，,；;\s]*(?:(?:然后|再)\s*)?)?"
                           r"(?:把)?(?:系统|电脑)?的?(?:声音|音量)(?:给我)?(?:回到|恢复到|调回)\s*"
                           r"(百分之[零一二两三四五六七八九十百]+|\d+(?:\.\d+)?\s*[%％]?|[零一二两三四五六七八九十百]+)(?:吧|呗)?", clean)
    if restore:
        value = set_value(restore[1])
        if value is not None and value.is_integer() and 0 <= value <= 100:
            result, check = {"action": "volume", "param": str(int(value)), "reply": f"请求将音量设为{int(value)}%。"}, f"音量调到{int(value)}"
    if re.fullmatch(r"(?:把)?(?:音乐|歌曲|这首歌|歌)(?:先)?(?:关了|关掉|关闭)[，,；;\s]*(?:(?:然后|再)\s*)?(?:先)?(?:暂停|暂停播放)(?:一下)?(?:吧|呗)?", clean):
        result, check = {"action": "media_control", "param": "pause", "reply": "请求暂停当前媒体。"}, "暂停播放"
    if re.fullmatch(r"(?:把)?任务管理(?:器|其)(?:给我|帮我)?(?:调出来|调出|打开|开开|启动)(?:一下)?(?:吧|呗)?"
                    r"|(?:打开|启动|调出)(?:一下)?任务管理(?:器|其)(?:吧|呗)?", clean):
        result, check = {"action": "run_cmd", "param": "taskmgr", "reply": "正在打开任务管理器。"}, "打开任务管理器"
    if re.fullmatch(r"(?:查(?:询)?(?:一下|下)?|看看|看一下)(?:硬盘|磁盘)(?:还剩|还有|剩余|剩)(?:多少|多大)(?:容量|容亮|空间)(?:啊|呀)?[？?]?", clean):
        result, check = {"action": "system_status", "param": "", "reply": "查看系统报告中的磁盘剩余空间。"}, "查询系统状态"
    if result:
        return {**result, "emotion": "confident", "_check_text": check}
    parts = _clauses(clean)
    if len(parts) > 1:
        frames = [(index, _state_target_request(_strip_prefix(_STEP_ORDER_RE.sub("", part.strip()))))
                  for index, part in enumerate(parts)]
        extrema = [(index, frame) for index, frame in frames if frame and frame["action"] == "set_brightness" and frame["param"] in ("min", "max")]
        direction = "brightness_down" if extrema and extrema[0][1]["param"] == "min" else "brightness_up"
        if len(extrema) == 1 and all(index == extrema[0][0] or _brightness_symptom_clause(part, direction)
                                      for index, part in enumerate(parts)):
            return extrema[0][1]
    reminder = re.fullmatch(r"(.{1,32}?提醒(?:一下|一声)?我)(.+)", clean)
    if reminder and _is_reminder_clause(clean) and _state_target_request(reminder[2]):
        return {"action": "set_reminder", "param": clean, "emotion": "confident", "reply": "记下这个未来提醒。", "_check_text": clean}
    return None


def _state_target_literal(text):
    clean = _strip_prefix(text).strip()
    quoted = _QUOTED_SPAN_RE.fullmatch(clean)
    if quoted and _state_target_request(clean[1:-1]):
        return {"action": "chat", "param": "", "_guard": "state_target_literal", "_pending": None, "reply": "这段引用不会作为当前状态操作。"}
    opened = re.fullmatch(r"(?:打开文件|打开文档)\s*(.+)", clean, re.S)
    if opened:
        target = opened[1].strip(" 。！! ")
        if _QUOTED_SPAN_RE.fullmatch(target):
            target = target[1:-1]
        filename = re.fullmatch(r"(.+)\.[A-Za-z0-9]{1,10}", target, re.S)
        if filename and _state_target_request(filename[1]):
            return {"action": "search_file", "param": target, "emotion": "confident", "reply": "查找这个名称的文件。"}
    return None


def _negated_state_target(text):
    clean = _strip_prefix(_space_clauses(text))
    prefix = _NEG_CLAUSE_RE.match(clean)
    return bool(prefix and _state_target_request(clean[prefix.end():]))


def _coalesce_state_clauses(text, parts):
    """Merge only a recognized adjacent restatement of one final state."""
    merged, index, cursor = [], 0, 0
    while index < len(parts):
        start = text.find(parts[index], cursor)
        if index + 1 < len(parts) and start >= 0:
            second = text.find(parts[index + 1], start + len(parts[index]))
            end = second + len(parts[index + 1])
            if second >= 0 and _state_target_request(text[start:end]):
                merged.append(text[start:end])
                index, cursor = index + 2, end
                continue
        merged.append(parts[index])
        cursor = start + len(parts[index]) if start >= 0 else cursor
        index += 1
    return merged


def _brightness_symptom_clause(text, action=None):
    """A bare brightness symptom can restate a setter; an adjustment is its own step."""
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", text.strip())).strip(" 。！! ")
    match = re.fullmatch(r"(?:这(?:个)?|电脑的?)?(?:屏幕|亮度)(?:也|真|实在|有点|有些|太|好|很)?"
                         r"(太亮|好亮|太暗|好暗|亮得刺眼|亮得晃眼|暗得看不清|刺眼|晃眼|亮|暗)(?:了|啦|咯|喽|吧|啊|呀)*", clean)
    direction = "brightness_up" if match and "暗" in match[1] else "brightness_down"
    return bool(match and (action is None or direction == action))


def _media_state_request(text):
    """Identify a requested current state; quoted/future payloads supply no state."""
    clean = _strip_prefix(_space_clauses(text)).strip(" 。！! ")
    if mention_reason(clean) or _media_selection_decision(clean):
        return None
    state = _state_target_request(clean)
    if state and state["action"] == "media_control":
        return state["param"]
    quiet = _quiet_decision(clean)
    if quiet and quiet["action"] == "media_control":
        return "pause"
    if _CORRECTION_MARK_RE.search(clean) and not _QUOTED_SPAN_RE.search(clean):
        corrected = _correction_decision(clean)
        return corrected.get("param") if corrected and corrected.get("action") == "media_control" and corrected.get("param") in ("pause", "play") else None
    modes = []
    for part in _clauses(clean):
        clause = _operation_request_head(_STEP_ORDER_RE.sub("", part.strip())).strip(" 。！! ")
        if _is_reminder_clause(clause) or _diagnostic_content_intro(clause):
            return None
        clause = re.sub(r"^(?:现在|立刻|立即)\s*", "", clause)
        clause = re.sub(r"我(?:要|先|得)?(?:接|打)(?:个|一通)?电话[吧呗了]*$", "", clause)
        subject = r"(?:(?:把|让)?(?:当前|这个|这首|这|正在播放的|暂停的|停着的)?(?:音乐播放器|播放器|媒体|音乐|歌曲|视频|音频|歌)(?:的|先|给我|帮我)?|给我|帮我|先)*"
        suffix = r"(?:一下|一会儿?|一遍|一?点儿?|几首|一首|首|些|当前|这个|这首|正在|的|歌|歌曲|音乐|曲子|媒体|视频|音频|播放|听听|吧|呗|啊|呀|喽|咯|了|好吗|行吗|可以吗|\s)*"
        if re.fullmatch(r"(?:切换|切一下)(?:当前|音乐|媒体|的)*播放状态|播放暂停|play_pause", clause, re.I):
            modes.append("play_pause")
        elif re.fullmatch(subject + r"(?:暂停(?:播放)?|赞停|停(?:一停|一下|一会儿?|停|下|了|喽|咯)?|别(?:再)?(?:放|播|播放)了)" + suffix, clause, re.I):
            modes.append("pause")
        elif re.fullmatch(subject + r"(?:(?:继续|恢复|接着)(?:播放|放|播)|(?:开始)?(?:播放|放|播)|听(?:点|一首|首|几首)?(?:歌|音乐|歌曲|曲子)|play|resume)" + suffix, clause, re.I):
            modes.append("play")
        elif _plan_explicit_request(clause, None):
            return None
    return modes[0] if len(modes) == 1 else None


def _canonicalize_media_decision(text, decision, *, rules=False):
    if not decision:
        mode = _media_state_request(text) if rules else None
        return {"action": "media_control", "param": mode, "emotion": "confident", "reply": "请求控制当前媒体状态。"} if mode else decision
    action, value = _plan_step_identity(decision)
    if action == "media_control":
        mode = _media_state_request(text)
        if mode and value in ("play", "pause") and value != mode:
            return {"action": "chat", "param": "", "_guard": "media_state_mismatch", "_pending": None,
                    "reply": "你要求的播放状态和候选操作相反，这次尚未执行。", "_model": decision.get("_model", "")}
        return {**decision, "action": "media_control", "param": mode if mode and value == "play_pause" else decision.get("param", "")}
    if rules and action == "chat" and len(_clauses(_space_clauses(text))) == 1:
        mode = _media_state_request(text)
        if mode:
            return {"action": "media_control", "param": mode, "emotion": "confident", "reply": "请求控制当前媒体状态。"}
    if action != "multi_action":
        return decision
    steps = _guard_multi_action_steps(decision.get("param"))
    media = [(index, step) for index, step in enumerate(steps) if step["action"] == "media_control"]
    if not media:
        return decision
    units = _plan_source_units(text, _reference=decision)
    sources = [(source, known) for source, known in units if known and _plan_step_identity(known)[0] == "media_control"]
    if len(media) != len(sources):
        return decision  # The ordinary whole-source guard rejects incomplete alignment.
    replacements = {}
    for (index, step), (source, known) in zip(media, sources):
        mode = _media_state_request(source)
        if mode and step.get("param") in ("play", "pause") and step["param"] != mode:
            return {"action": "chat", "param": "", "_guard": "media_state_mismatch", "_pending": None,
                    "reply": "媒体步骤的目标状态和原话不一致，这次尚未执行。", "_model": decision.get("_model", "")}
        if mode and step.get("param") == "play_pause":
            replacements[index] = mode
    if not replacements:
        return decision
    raw, depth = decision["param"], 0
    while isinstance(raw, str) and depth < 2:
        raw, depth = json.loads(raw), depth + 1
    updated = [{**item, "param": replacements[index]} if index in replacements else item for index, item in enumerate(raw)]
    for _ in range(depth):
        updated = json.dumps(updated, ensure_ascii=False)
    return {**decision, "param": updated}


def _cockpit_command():
    """打开设置大屏：用正在运行的解释器（一键安装装在虚拟环境里，写死 py -3.12 会找不到依赖），路径带引号防空格。"""
    import sys
    exe = sys.executable
    windowed = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(windowed):
        exe = windowed
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shells", "canaan_settings_cockpit.py")
    return f"Start-Process -FilePath \"{exe}\" -ArgumentList '\"{script}\"'"


def fast_match_intent(text):
    return _canonicalize_media_decision(text, _fast_match_intent_impl(text), rules=True)


def _single_temperature_query_decision(text):
    """Reuse existing temperature grammar only for one explicit owned clause."""
    if (_QUOTED_SPAN_RE.search(text) or _MORE_STEPS_RE.search(mask_quotes(text))
            or len(_clauses(_space_clauses(text))) != 1 or negative_request(text) or mention_reason(text)):
        return None
    from canaan_status_query import query_kind
    query = query_kind(text)
    if query is not None and query[0] in ("temperature_cpu", "temperature_gpu"):
        return _phrase_decision("CPU温度")
    return None


def _fast_match_intent_impl(text):
    """确定性规则快速通道：命中返回决策 dict，未命中返回 None（交给大模型）。
    先整句检查拒绝；再按分句处理：被否定的分句丢掉，剩下的每句都能识别就组成多步任务（multi_action），
    有识别不了的就整句交给大模型，绝不只做第一步。"""
    # 语音转写常用空格分句（“歌先停一下 我接个电话”）：两个汉字之间的空格当逗号，后面才能按分句丢掉说原因的那句
    literal_site = _named_site_literal(text)
    if literal_site:
        return literal_site
    literal_app = _app_literal_request(text)
    if literal_app:
        return literal_app
    if _negated_state_target(text):
        return dict(_NEGATED_REPLY)
    literal_state = _state_target_literal(text)
    if literal_state:
        return literal_state
    text = _asr_fix(_space_clauses(text))
    clean = _strip_prefix(text)
    parts = _clauses(clean)
    if _whole_cancelled_request(text, parts):
        return dict(_NEGATED_REPLY)
    if parts and all(_cancelled_clause(mask_quotes(p)) or _completed_clause(mask_quotes(p)) for p in parts):
        return dict(_NEGATED_REPLY) if any(_cancelled_clause(mask_quotes(p)) for p in parts) else None
    missing_reminder = _reminder_time_missing_decision(clean)
    if missing_reminder:
        return missing_reminder
    if _CONTRADICT_RE.search(mask_quotes(clean)):   # 先于拒绝：“两条都算我的要求”不是在冒充授权
        return _ambiguity_decision(clean)
    if mention_reason(text) == "quoted_report":
        return None    # “网页正文里写着‘替我自动操作电脑……’”：在描述别处的文字，一个分句都不当命令
    refusal = _refusal_decision(clean)
    if refusal:
        return refusal
    query_source = _query_source_decision(clean)
    if query_source:
        return query_source
    split = _prefix_sequenced_reminder(clean)
    if split:
        decisions = [fast_match_intent(part) for part in split]
        if any(not item or item.get("action") == "chat" for item in decisions):
            return None
        steps = []
        for item in decisions:
            steps.extend(_guard_multi_action_steps(item["param"]) if item["action"] == "multi_action" else [{"action": item["action"], "param": item.get("param", "")}])
        return {"action": "multi_action", "param": json.dumps(steps, ensure_ascii=False), "emotion": "confident", "reply": "按完整顺序处理当前操作和未来提醒。"}
    future = _sequenced_reminder_parts(clean)
    if future:
        request, tail = future
        reminder = {"action": "set_reminder", "param": request, "emotion": "confident", "reply": "我记下这个提醒。"}
        if not tail:
            return reminder
        current = fast_match_intent(tail)
        if not current or current.get("action") == "chat":
            return reminder if all(_cancelled_clause(c) for c in _clauses(tail)) else None
        steps = [{"action": "set_reminder", "param": request}] + (_guard_multi_action_steps(current["param"]) if current["action"] == "multi_action" else [{"action": current["action"], "param": current.get("param", "")}])
        return {"action": "multi_action", "param": json.dumps(steps, ensure_ascii=False), "emotion": "confident", "reply": "先记下未来提醒，再做你现在要求的步骤。"}
    state_target = _state_target_request(clean)
    if state_target:
        return {key: value for key, value in state_target.items() if key != "_check_text"}
    screenshot_limit = _screenshot_constraint_decision(clean)
    if screenshot_limit:
        return screenshot_limit
    media_selection = _media_selection_decision(clean)
    if media_selection:
        return media_selection
    # 话没说完（“音量帮我固定到百分之……哎数卡嘴边了”“提醒我准备……”）：先问完整的，不猜
    if re.search(r"……|…|\.\.\.", text) and re.search(r"卡|想不起|记不清|后半|后头|哎|嘴边", text):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "incomplete",
                "reply": "后面那部分没说完，你把完整的再说一遍？"}
    # 设提醒却没说提醒什么（“今天11点设个提醒吧”）
    if re.fullmatch(r"[^，,。]{0,14}(?:点|分|号)[^，,。]{0,4}(?:设|定)(?:个|一个)提醒(?:吧|呗)?[，,]?(?:别让我到时候忘事|省得忘了)?[。！!]*", clean):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "no_content",
                "reply": "到时候提醒你做什么呀？"}
    # “锁屏之前先静音”：先做后半句，再做前半句。改写成“先静音，再锁屏”按普通多步处理
    before = re.fullmatch(r"(?:在)?([^，,。]{2,12}?)(?:之前|以前)(?:要)?先([^，,。]{2,16}?)(?:吧|呗)?[。！!]*", clean)
    if before and "先" not in before.group(1) and not mention_reason(text):
        return fast_match_intent(f"先{before.group(2)}，再{before.group(1)}")
    ambiguous = _ambiguity_decision(clean)
    if ambiguous:
        return ambiguous
    correction = _correction_decision(clean)
    if correction:
        if correction.get("action") in ("open_app", "open_chatapp", "close_app", "focus_app"):
            owner = _fast_match_single(clean)
            if owner and owner.get("action") not in ("open_app", "open_chatapp", "close_app", "focus_app") and _plan_whole_owned_source(clean, reference=owner):
                return owner
        return correction
    corrected_open = _corrected_open_text(clean)
    if corrected_open:
        corrected = _fast_match_single(corrected_open)
        if corrected and corrected.get('action') in ('open_app','open_chatapp'):
            return corrected
        return {'action':'chat','param':'','emotion':'confident','_guard':'no_object','_pending':None,
                'reply':'要改开哪个程序？请告诉我完整名称。'}
    precise = _relative_brightness(clean) or _literal_media_request(clean) or _where_decision(clean) or _quiet_decision(clean) or _contextual_file_search_request(clean)
    if precise:
        return precise   # 这些规则完整匹配整句；“上一首，切回去”是同一件事。
    precise = _single_temperature_query_decision(clean) or _phrase_decision(clean)
    if precise and not (precise.get("action") == "send_chatapp" and not _plan_whole_owned_source(clean, precise)) and (len(parts) <= 1 or len(command_clauses(clean)) <= 1 or precise.get("action") in (
            "send_chatapp", "openmuse_task", "opendots_task", "ufo_task", "computer_use", "daily_briefing", "diagnose_error")
            or precise.get("action") == "web_search" and re.search(r"改搜|改成搜|换成搜", clean)):
        return precise
    parts = _clauses(clean)
    if len(parts) <= 1:
        return _fast_match_single(text)
    whole = _fast_match_single(text)
    if whole and whole.get("_guard") == "correction":
        return whole  # 不把整句中的收件人修订拆开，再捡回已经作废的发送步骤。
    if whole and whole.get("action") == "send_chatapp" and len(_QUOTED_SPAN_RE.findall(clean)) >= 2 \
            and _OLD_RECORD_RE.search(mask_quotes(clean)):
        return whole   # 前面的引号是旧记录，不是这条消息的正文，后面不再拆步骤
    if whole and whole.get("action") == "send_chatapp":
        # 发消息整句解析（正文里可以有逗号、“然后”）。正文用引号括起来、引号后面还有别的事要做时，
        # 后面那部分单独拆成后续步骤；认不出就交给模型 / 守卫，绝不只发消息、悄悄丢掉后半句
        quoted_span = next(_QUOTED_SPAN_RE.finditer(clean), None)
        tail = clean[quoted_span.end():] if quoted_span else ""
        tail = re.sub(r'^\s*[，,；;。]?\s*(?:然后|再|接着|之后|最后|顺便)?\s*', '', tail)
        # “不用问，直接发”“就这一条”说的还是这条消息本身，不是另一步（“不用问”也不算确认，执行层照样先确认）
        rest_text = re.sub(r"(?:不用问|别问了?|直接发|就发|发吧|发过去|就这一条|不用确认|不用核对|快点|马上|赶紧)[，,。！!\s]*", "",
                           tail).strip("，,。！! ") if tail else ""
        if rest_text and (_CMD_HINT_RE.search(rest_text) or _plan_explicit_request(rest_text, None)) and not _SELF_STATE_RE.match(rest_text):
            rest = fast_match_intent(rest_text)
            if not rest or rest.get("action") == "chat":
                return None
            rest_steps = json.loads(rest["param"]) if rest["action"] == "multi_action" else [{"action": rest["action"], "param": rest.get("param", "")}]
            steps = [{"action": "send_chatapp", "param": whole["param"]}] + rest_steps
            return {"action": "multi_action", "param": json.dumps(steps, ensure_ascii=False), "emotion": "confident",
                    "reply": "好，按顺序来，发消息那步会先请你确认。"}
        return whole
    if not whole or not _plan_whole_owned_source(clean, whole):
        parts = _coalesce_state_clauses(clean, parts)
    negated = [p for p in parts if _cancelled_clause(mask_quotes(p))]
    positive = [p for p in parts if p not in negated]
    decisions, chats, unknown = [], [], False
    decision_sources = {}
    decision_positions = {}
    offsets, cursor = [], 0
    for part in parts:
        start = clean.find(part, cursor)
        offsets.append((start, start + len(part)))
        cursor = start + len(part)
    positive_offsets = [offset for part, offset in zip(parts, offsets) if part not in negated]
    explicit_repeats, last_repeat_end = set(), 0
    for p, (step_start, step_end) in zip(positive, positive_offsets):
        step_prefix = _STEP_ORDER_RE.match(p.strip())
        if re.fullmatch(r"(?:你)?(?:不用|别|不要|无需)(?:再)?(?:让我)?(?:核对|确认|问|点头|问我)(?:了|啦|吧|呀|啊)?[。！!]*", p.strip()):
            continue  # “不用再核对啦”说的是确认这一步（执行层照样会确认），不是一个要做 / 不做的动作
        real_message = _quoted_chatapp_decision(_strip_prefix(_STEP_ORDER_RE.sub("", p.strip())))
        literal_state = _state_target_literal(_strip_prefix(_STEP_ORDER_RE.sub("", p.strip())))
        if not real_message and (literal_state and literal_state["action"] == "chat" or _named_site_literal(p) or _app_literal_request(p)):
            continue
        if not real_message and (mention_reason(p) or re.search(r"说[“\"「]", p)):
            continue  # 转述 / 引用别人的话（“他刚说‘锁屏’”）：不是要我做事
        if _SELF_STATE_RE.match(p) or _completed_clause(mask_quotes(p)) or _BACKGROUND_RE.fullmatch(p.strip(" 。")):
            continue  # “我离开桌子去拿水了，帮我锁屏”“我关灯看资料了，亮度调到二十”：前一句是交代背景
        if re.search(r"(?:不用|别|不要)照做|不用理|别理(?:他|她)", p):
            continue  # “他那句‘把音量调到90%’你不用照做”：引的是别人的话，明说了不做
        if re.fullmatch(r"(?:用|走)?本机(?:文件)?搜索|别搜网页|不要搜网页|先让我确认一下再关|(?:别问)?(?:直接)?算我同意|"
                        r"关的软件就是这一款|就是这一款|不要跳过你该做的确认|我(?:要|想)看[^，,。帮把]{0,12}(?:页|页面|内容|说明)",
                        p.strip(" 。")):
            continue  # 补充说明这一步怎么做 / 为什么做（“我要看样例搜索页”），不是另一步
        p = _STEP_ORDER_RE.sub("", p).strip() or p   # “先截个屏”“最后锁屏”：顺序词不影响这一步是什么
        p = _inherit_device_target(p, clean)
        if len(parts) == 2 and re.fullmatch(r"(?:直接|就)?退出(?:吧|呗)?", p):
            context_app = re.fullmatch(r"(?:把)?(.{1,12}?)别(?:开着|放|播)了", parts[0].strip())
            if context_app and (_known_application(context_app.group(1)) or any(
                    name == context_app.group(1) for name, _ in _CLOSE_MAP)):
                p = "关闭" + context_app.group(1)   # “网易云别开着了，退出”：退出的是刚指明的应用。
        if _SEARCH_QUALIFIER_RE.search(p) and any(d.get("action") == "web_search" for d in decisions):
            continue   # “走网页搜索就行”“把搜索页给我打开”说的是前面那次搜索怎么做，不是第二次搜索
        if _is_reminder_clause(p):
            decisions.append({"action": "set_reminder", "param": p})   # 多步里的提醒：交给“设提醒”动作
            continue
        d = real_message or _fast_match_single(p)
        if (d is None or d.get("action") == "web_search") and mention_reason(clean) not in ("hypothetical", "reported", "quoted_report", "past_story"):
            local_query = _single_temperature_query_decision(p) or (_phrase_decision(p) if d is None else None)
            if local_query and local_query.get("action") == "system_status":
                d = local_query
        if (not d or d.get("action") == "chat") and decisions and decisions[-1].get("action") in ("ufo_task", "computer_use") \
                and not _NEG_CLAUSE_RE.match(p):
            # “自动操作Excel打开……xlsx，把Sheet1的A1到B6按A列升序排序并保存”：后半句是同一次界面操作的要求，接上，不丢
            decisions[-1] = {**decisions[-1], "param": f"{decisions[-1].get('param', '')}，{p.strip()}"}
            continue
        if d and d.get("action") not in ("chat", "quit_assistant"):
            if d.get("action") in ("screenshot", "volume_up", "volume_down", "brightness_up", "brightness_down"):
                gap = clean[last_repeat_end:step_start] + (step_prefix.group(0) if step_prefix else "")
                if decisions and decisions[-1].get("action") == d.get("action") and re.search(r"再|然后|并(?:且)?|接着|之后|最后|顺便", gap):
                    explicit_repeats.add(id(d))
                last_repeat_end = step_end
            decisions.append(d)
            decision_sources[id(d)] = p
            decision_positions[id(d)] = offsets.index((step_start, step_end))
        elif d and d.get("action") == "chat":
            chats.append(d)
        elif _CMD_HINT_RE.search(p) or _plan_explicit_request(p, None):
            unknown = True
    missing_app = next((c for c in chats if c.get("_guard") == "app_not_found"), None)
    if missing_app:
        return missing_app  # 完整多步请求先说明缺失项，不先执行能识别的前半段。
    if unknown or (decisions and all(d.get("action") == "system_status" for d in decisions)
                   and any(row["kind"] in ("request", "uncertain") for row in _status_source_requests(clean))):
        return None
    if decisions and any(c.get("reply") for c in chats):
        return None   # 有要做的步骤，又有要回答的问题（“告诉我几点，再设个提醒”）：交给模型，别把问题悄悄丢掉
    if not decisions:
        if negated:
            return dict(_NEGATED_REPLY)
        # 整句关键词兜底可能会把引用里的词当命令，这里只认闲聊 / 报时这类不动电脑的回复
        return chats[-1] if chats else (whole if not whole or whole.get("action") == "chat" else None)
    # “我想知道处理器忙不忙，查一下CPU占用”“屏幕太刺眼咯，亮度往下减一档”：两句说的是同一件事，只做一次
    once = _READONLY_ACTIONS | {"volume_up", "volume_down", "brightness_up", "brightness_down", "screenshot", "lock_screen", "show_desktop"}
    from canaan_query_context import status_query_identity, _record_rule_query_sources
    decisions = [d for i, d in enumerate(decisions)
                 if not (i and d.get("action") in once and d.get("action") == decisions[i - 1].get("action")
                         and d.get("param", "") == decisions[i - 1].get("param", "") and id(d) not in explicit_repeats
                         and (d.get("action") != "system_status" or
                              status_query_identity(decision_sources.get(id(d), "")) ==
                              status_query_identity(decision_sources.get(id(decisions[i - 1]), ""))))]
    # 说了具体数值（“外放声音有些刺耳，把系统音量固定到35%吧”）：前面按症状猜的调大调小不要了
    acts = {d.get("action") for d in decisions}
    if "volume" in acts:
        from canaan_volume_steps import normalize_volume_steps
        decisions = [d for d in decisions if d.get("action") not in ("volume_up", "volume_down") or
                     (normalize_volume_steps(_operation_request_head(mask_quotes(decision_sources.get(id(d), ""))),
                                             None, cn_number, rules=True) or {}).get("action") == d["action"]]
    if "set_brightness" in acts:
        decisions = [d for d in decisions if d.get("action") not in ("brightness_up", "brightness_down") or
                     not any(other.get("action") == "set_brightness" and
                             abs(decision_positions.get(id(d), -2) - decision_positions.get(id(other), -4)) == 1 and
                             _brightness_symptom_clause(decision_sources.get(id(d), ""),
                                 {"min": "brightness_down", "max": "brightness_up"}.get(str(_plan_step_identity(other)[1])))
                             for other in decisions)]
    if "daily_briefing" in acts and _app_launch_without_briefing_request(clean):
        return None   # 分句里的“看看团队消息”不能脱离前面启动的应用，改读其他渠道。
    _record_rule_query_sources(text, decisions, decision_sources)
    if len(decisions) == 1:
        return decisions[0]
    steps = [{"action": d["action"], "param": d.get("param", "")} for d in decisions]
    return {"action": "multi_action", "param": json.dumps(steps, ensure_ascii=False), "emotion": "confident",
            "reply": "好，按顺序来。"}


_QUOTED_SPAN_RE = re.compile(r"“[^“”]*”|\"(?:\\.|[^\"\\])*\"|「[^「」]*」|『[^『』]*』|‘[^‘’]*’"
                             r"|(?<![A-Za-z0-9_])'[^']*'(?![A-Za-z0-9_])")
_NAME = r"(?P<c>[^\s，,：:。“”\"「」『』〔〕]{1,8}?)"
_QUOTED_CHATAPP_RES = [
    # 聊天软件给小冯带句话：“……” / 用聊天软件给小吴发“……” / 聊天软件里给张三说“……”
    re.compile(r"^(?:我想|我要|想|请)?\s*(?:用|通过|在)?\s*聊天软件(?:里|上)?\s*(?:给|跟|向|对)\s*(?:聊天软件(?:里|上)?的|聊天软件联系人|联系人)?\s*"
               + _NAME + r"\s*(?:发送|发|说|带|捎)"),
    # 给聊天软件里的张三发一句“……” / 找聊天软件里的小高，发“……” / 我想给小何发个聊天软件，完整内容是“……” / 给小唐聊天软件发“……”
    re.compile(r"^(?:我想|我要|想|请)?\s*(?:给|跟|向|对|找)\s*(?:聊天软件(?:里|上)?的|聊天软件联系人|联系人)?\s*" + _NAME
               + r"\s*(?:在?聊天软件(?:上|里)?)?\s*[，,]?\s*(?:发送|发|说|带|捎)"),
    # 聊天软件联系小赵，发“……”
    re.compile(r"^(?:在?聊天软件(?:上|里)?)?\s*联系\s*" + _NAME + r"\s*[，,]\s*(?:发送|发|说)"),
    # 把“……”发到聊天软件联系人小方那里
    re.compile(r"^把〔Q〕\s*(?:发|转)(?:到|给)\s*(?:聊天软件(?:里|上)?的?|聊天软件联系人|联系人)?\s*" + _NAME
               + r"\s*(?:那里|那儿|那边|那)?\s*[。.!！]*$"),
    re.compile(r"^(?:(?:用|通过|在)?聊天软件(?:上|里)?\s*)?(?:告诉|通知|提醒)\s*" + _NAME
               + r"(?=\s*(?:说)?\s*[：:,，]?\s*〔Q〕)"),
]
# 动词和引号之间只能是这些连接（“发一句”“发个聊天软件，完整内容是”“说一声：”“带句话：”），
# 不然不是在说这段引号（“给文件传输助手发……”里的“传”不是动词）
_QUOTE_GAP_RE = re.compile(r"^(?:一句|一条|一下|一个|个|条|句|声)?(?:聊天软件消息|聊天软件|消息|信息|话|一声)?\s*[，,]?\s*"
                           r"(?:完整的?内容(?:是|为)|内容(?:是|为)|就是|说)?\s*[：:，,]?\s*〔Q〕")
# 引号后面又改主意（“——等等，收件人改李四”）：不按引号前面的联系人发，先问
_QUOTE_CORRECTION_RE = re.compile(r"〔Q〕.*(?:等等|不对|口误|改主意|改成|改为|改发|换成|收件人改|发错)")


def mask_quotes(text):
    """引号里是要发 / 要搜的原文，不是对团团说的话：判断否定、犹豫、分句时先遮住（“发‘不用乙组那版’”不是否定）。"""
    return _QUOTED_SPAN_RE.sub("“Q”", text or "")


def _quoted_chatapp(clean, clean_contact):
    """“给 X 发 / 说 / 带句话 + 引号正文”：返回 (联系人, 正文)，认不出返回 None。只认一段引号。"""
    spans = list(_QUOTED_SPAN_RE.finditer(clean or ""))
    if len(spans) != 1:
        return None
    q = spans[0]
    message = q.group(0)[1:-1].strip()
    outer = (clean[:q.start()] + "〔Q〕" + clean[q.end():]).replace("微新", "聊天软件").replace("威信", "聊天软件")
    if not message or not re.search(r"聊天软件|发|带句话|捎句话|带个话|说一声|说句话|说|告诉|通知|提醒", outer):
        return None
    if _QUOTE_CORRECTION_RE.search(outer):
        return "correction"
    for pattern in _QUOTED_CHATAPP_RES:
        m = pattern.search(outer)
        if not m:
            continue
        if pattern is not _QUOTED_CHATAPP_RES[3] and not _QUOTE_GAP_RE.match(outer[m.end():]):
            continue
        name = clean_contact(m.group("c"))
        name = re.sub(r"(?:带句话|捎句话|带个话|捎个话|说一声|说句话|说声)$", "", name).strip()
        if not name or name.startswith("我"):
            return None
        return name, message
    return None


def _clock_reply(text):
    """Only complete clock/calendar questions; quoted words and task parameters are not queries."""
    from canaan_clock_math import interval_reply
    interval = interval_reply(text)
    if interval:
        return interval
    query = re.sub(r"^(?:告诉我|看下|看看|查下|查一下|查询一下|查询|(?:我)?(?:就|想|只是)?问(?:下|一下)?|请问)\s*", "", text)
    query = re.sub(r"(?:来着|了|啊|呀|呢|吧)+$", "", query.rstrip("？?。！! ")).strip()
    query = re.sub(r"(?:说一下|说说|告诉我|讲一下|报一下|是多少)$", "", query).strip()
    query = re.sub(r"^(今天|明天|昨天|后天|前天)的(日期)$", r"\1\2", query)
    query = re.sub(r"^(?:今天的)?日期$|^(今天|明天|昨天|后天|前天)日期$", lambda m: (m.group(1) or "今天") + "什么日期", query)
    if re.fullmatch(r"(?:(?:现在|当前)(?:是)?几点(?:几分)?|几点了?|几点几分|报时|报一下时间|当前时间|现在的?时间)", query):
        now = time.localtime()
        minute = f"{now.tm_min}分" if now.tm_min else "整"
        return f"现在{now.tm_hour}点{minute}。"
    match = re.fullmatch(r"(前天|昨天|今天|明天|后天)?(?:是)?(几号|几月几号|什么日期|星期几|礼拜几|周几)", query)
    if not match:
        return None
    label = match.group(1) or "今天"
    now = time.localtime()
    day = date(now.tm_year, now.tm_mon, now.tm_mday) + timedelta(days={"前天": -2, "昨天": -1, "今天": 0, "明天": 1, "后天": 2}[label])
    weekday = "一二三四五六日"[day.weekday()]
    if match.group(2) in ("星期几", "礼拜几", "周几"):
        return f"{label}周{weekday}。"
    return f"{label}{day.month}月{day.day}号，周{weekday}。"


_GENERIC_CHATAPP_CONTACTS = {"一个人", "某人", "别人", "某位朋友", "某位好友", "朋友", "我朋友", "我的朋友", "好友", "伙伴", "聊天软件伙伴",
                            "我聊天软件伙伴", "我的聊天软件伙伴", "聊天软件联系人", "聊天软件好友", "谁", "有人", "他", "她", "他们", "某位", "人", ""}


def _clean_chatapp_contact(c):
    c = (c or "").strip()
    c = re.sub(r'^(在|从)?\s*(联系人|好友|通讯录|聊天软件好友|聊天软件联系人)(里面|里)?\s*(搜一下|搜索|找一下|找到|搜)?\s*', '', c).strip()
    c = re.sub(r'^(搜一下|搜索|找一下|找到|搜)\s*(在|从)?\s*(联系人|好友|通讯录|聊天软件好友|聊天软件联系人)?(里面|里)?\s*', '', c).strip()
    c = re.sub(r'^(给|向|跟|发给)\s*', '', c).strip()
    c = re.sub(r'^(我聊天软件联系人|我聊天软件好友|聊天软件联系人|聊天软件好友|联系人|好友|我朋友|朋友|联系人里面的|好友里面的)\s*', '', c).strip()
    c = re.sub(r'(发送一条消息|发送消息|发一条消息|发消息|发聊天软件|发送)$', '', c).strip()
    c = re.sub(r'(聊天软件|chatapp|chatapp)$', '', c).strip()
    c = re.sub(r'^聊天软件(?=文件传输助手)', '', c).strip()
    c = re.sub(r'^聊天软件(?:里的?|上的?|中的?|联系人)?(?=\S{1,8}$)', '', c).strip()
    return re.sub(r'^的\s*', '', c).strip()


_RECIPIENT_FIX_RE = re.compile(r"(?:收件人|联系人|对象|发给的人)(?:改成?|换成?|改为|换为)\s*(?P<c>[^\s，,。“”\"]{1,8})")
_BODY_SAME_RE = re.compile(r"(?:消息|内容|正文)[^，,。]{0,6}(?:照原样|不变|一样|不用改|照旧|原样)")
_OLD_RECORD_RE = re.compile(r"旧记录|以前那句|上次那句|上周那句|之前那句|那句[^，,。]{0,6}不算")


def _quoted_chatapp_decision(clean):
    spans = list(_QUOTED_SPAN_RE.finditer(clean or ""))
    # 提到一条旧记录、再说“现在要给 X 发‘……’”：只按“现在”后面那句（这条照样发前先确认）
    if len(spans) >= 2 and _OLD_RECORD_RE.search(mask_quotes(clean)):
        now = [m for m in re.finditer(r"(?:我)?现在(?:要|想)?", clean) if m.start() > spans[0].end()]
        if now:
            return _quoted_chatapp_decision(clean[now[-1].end():])
    # “给张三发‘……’——等等，收件人改李四，消息内容就照原样”：换人，正文不变
    if len(spans) == 1:
        tail = clean[spans[0].end():]
        fix = _RECIPIENT_FIX_RE.search(tail)
        if fix and _BODY_SAME_RE.search(tail):
            contact = _clean_chatapp_contact(fix.group("c"))
            message = spans[0].group(0)[1:-1].strip()
            if contact and message and contact not in _GENERIC_CHATAPP_CONTACTS:
                return {"action": "send_chatapp", "param": json.dumps({"contact": contact, "message": message}, ensure_ascii=False),
                        "emotion": "pamper", "reply": f"好，改成发给{contact}。"}
    quoted = _quoted_chatapp(clean, _clean_chatapp_contact)
    if quoted == "correction":
        return {"action": "chat", "param": "", "emotion": "confident",
                "reply": "你最后要发给谁、发哪句？再完整说一遍，我核对后再发。", "_guard": "correction"}
    if quoted:
        contact, message = quoted
        if contact in _GENERIC_CHATAPP_CONTACTS:
            return {"action": "chat", "param": "", "emotion": "pamper", "reply": "发给谁呀？跟我说声名字～",
                    "_guard": "no_contact", "_draft": {"message": message}}
        return {"action": "send_chatapp", "param": json.dumps({"contact": contact, "message": message}, ensure_ascii=False),
                "emotion": "pamper", "reply": f"好，发给{contact}了。"}
    return None


_WHERE_RE = re.compile(r"(?:我的)?([^，,。？?]{1,10}?)(?:那个)?(文件夹|文件)?在哪(?:里|儿)?[？?]?(?:[，,]?(?:我)?找不到了|[，,]?打开看看)?[。！!？?]*")


_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^，,。；;\n\"“”'‘’]+")


def _path_decision(c):
    """说了一个绝对路径：要打开就开文件夹（路径按原话取全，空格也算），要找就在里面找文件。
    判断动词时先把路径本身去掉（“D:\\…\\截图样例”里的“截图”不是要截屏）。"""
    # 路径里可以有汉字，读到“里找 / 里的 / 这个目录”就算路径结束（“D:\eval_fixture里找名为……”）
    paths = [re.split(r"(?=里找|中找|里搜|中搜|里的|中的|里面|这个|那个|里名称|里名字|里名为|里找名)", p)[0]
             for p in _WIN_PATH_RE.findall(c or "")]
    if len(paths) != 1:
        return None
    rest = c.replace(paths[0], "〔路径〕")
    if re.search(r"找|搜", rest) and not re.search(r"发|删|写|保存|截|复制|移动|报错|正确的是", rest):
        q = re.search(r"[“\"「]([^”\"」]+)[”\"」]", c)
        name = q.group(1) if q else None
        if not name:
            m = re.search(r"(?:名为|叫做?|名字是)\s*([^\s，,。的]+?)(?:的(?:本机)?文件|，|,|$)", rest) or \
                re.search(r"(?:里|中)的\s*([^\s，,。]+?\.[A-Za-z0-9]{1,5})", rest) or \
                re.search(r"〔路径〕\s*(?:里的?|中的?)?\s*([^\s，,。〔]+?\.[A-Za-z0-9]{1,5})", c.replace(paths[0], "〔路径〕"))
            name = m.group(1) if m else None
        if name:
            return {"action": "search_file", "param": name, "emotion": "confident", "reply": f"我找找看 {name} 在哪..."}
        return None
    if re.search(r"打开|开一下|开开|进|给我开", rest) and not re.search(r"发|删|写|保存|截|复制|移动|报错|正确的是", rest):
        full = re.sub(r"(?:(?:这个|那个)(?:文件夹|目录))?(?:给我|帮我)?(?:开一下|打开|看看|一下)*$", "", paths[0].rstrip(" 。.")).rstrip()
        full = re.sub(r"(?:文件夹|目录)$", "", full).rstrip()
        return {"action": "open_path", "param": full, "emotion": "pamper", "reply": "文件夹打开啦。"}
    return None


_AUTO_UI_RE = re.compile(r"^(?:帮我)?自动(?:操作|在|用|到|把|替我)?")


_MORE_STEPS_RE = re.compile(r"[，,]\s*(?:然后|再|接着|最后|随后)|然后|(?<!别)(?<!要)(?<!不)再(?=打开|开|把|给|截|锁|调|关|静音|发|设|提醒|回|放|播|" + _MODIFIED_STEP_START + r")"
                            r"|并(?:且)?(?=打开|开|把|给|截|" + _SCREENSHOT_ALIAS + r"|锁|调|关|静音|发|设|提醒|回|放|播|" + _MODIFIED_STEP_START + r")"
                            r"|(?<!别)(?<!要)(?<!不)再(?=" + _SCREENSHOT_ALIAS + ")")


def _screenshot_alias_decision(clean):
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", clean.strip()))
    clean = re.sub(r"^(?:麻烦|劳驾)[，,\s]*", "", clean)
    if re.fullmatch(r"(?:给我)?" + _SCREENSHOT_ALIAS + r"(?:一下|就行|就好|即可|就可以)?[吧呗啊呀了。！!]*", clean):
        return {"action": "screenshot", "param": "", "emotion": "confident", "reply": "我来截取屏幕。"}
    return None


def _operation_request_head(text):
    """Normalize only a candidate operation's leading request modifiers."""
    clean = text.strip()
    while True:
        previous = clean
        clean = _STEP_ORDER_RE.sub("", clean)
        clean = re.sub(r"^(?:(?:呃|额|嗯|哦|就是|那个|麻烦(?:你)?|劳驾|请(?:你)?|帮我|给我|替我|please|(?:你)?" + _REQUEST_URGENCY + r")[\s，,]*)+", "", clean, flags=re.I)
        clean = _strip_prefix(clean)
        if clean == previous:
            return clean


def _screenshot_clause_request(clause):
    """Recognize a complete capture/save operation, rather than whitelist whole sentences.

    Near-homophones are accepted only inside the measured “截个图/屏” request frame;
    filenames, quoted payloads and search text are never rewritten.
    """
    clean = _operation_request_head(mask_quotes(clause))
    noun = r"(?:截图|截屏|屏幕快照|screenshot)"
    screen = r"(?:the\s+)?(?:(?:current|now)\s+)?(?:(?:现在|当前)(?:的|这个|这张|这)?|这个|这张|这)?"
    screen += r"(?:屏幕(?:上的?|的)?(?:内容|画面)|屏幕快照|屏幕|画面|全屏(?:图|图片)?|屏|screenshot|screen)"
    measure = r"(?:个|一张|张|一下|下|整张|整个|a|an|one)?\s*"
    aspect = r"(?:下来|下|个照|一张照|照|一张(?:图(?:片)?)?|个图|张图|成(?:一张)?图(?:片)?|一下|一份)*"
    front = r"(?:截(?:取|下来|下)?|拍|照|保存|存|抓取|捕获|take|capture|save)\s*" + measure + screen + aspect
    back = r"(?:把|将|给|让)?" + screen + r"(?:整张|整个)?(?:给我|帮我)?(?:" + _REQUEST_URGENCY + r"\s*)?(?:截|拍|照|保存|存|留)" + aspect
    emphasis = r"(?:(?:截屏|截图|screenshot))+"
    create = r"(?:来|整|弄)(?:个|一个|一张|张)" + noun + "|" + noun + r"(?:来|整|弄)(?:个|一个|一张|张)"
    measured = r"截(?:个|一张|张|一下|下)(?:图(?:片)?|途|涂|平|频(?:幕)?)"
    limited = r"(?:只)?(?:截|照)(?:一下|个|一张)?(?:当前窗口|这个窗口|那个窗口|区域|一部分|局部)|(?:局部|区域|窗口)(?:截图|截屏)"
    head = "(?:" + "|".join((emphasis, create, _SCREENSHOT_WORDS, measured, front, back, limited)) + ")"
    default = r"(?:(?:存|保存)(?:好|着|下来|(?:一份|一张|个)?(?:截图|图(?:片)?)|一份|一张|成图(?:片)?)?|留着|(?:(?:保存|存|放)(?:到|在)?|到|在)(?:我的)?桌面|(?:发给我|发我|给我)(?:看看|看下|看一下)?)"
    limit = r"(?:但|不过)?(?:(?:不要|别|不能|不)(?:包括|包含|截|拍|保存|存|发|显示).*|(?:只|之前|以前|前|保存到|存到|放到|另存为|文件名|命名|发给|发到|发送给|转给|传给|发|给).+)"
    polite = r"(?:看看|看下|看一下|一下|就行|就好|即可|就可以|[吧呗啊呀哦了吗呢。！!?？\s])*"
    match = re.fullmatch(head + r"(?P<tail>" + default + r"*(?P<limit>" + limit + ")?" + polite + ")", clean, re.I)
    return {"tail": match["tail"], "limit": match["limit"]} if match else None


def _screenshot_request_contexts(text):
    """Preserve payload ownership and explicit independent steps without calling fast rules."""
    split = _prefix_sequenced_reminder(text)
    if split:
        return _screenshot_request_contexts(split[0]) + _screenshot_request_contexts(split[1])
    future = _sequenced_reminder_parts(text)
    if future:
        return _screenshot_request_contexts(future[1]) if future[1] else []
    contexts, owner, previous_end = [], None, 0
    for start, stop in _literal_clause_spans(text):
        clause = _strip_prefix(_STEP_ORDER_RE.sub("", text[start:stop].strip()))
        frame = _screenshot_clause_request(clause)
        explicit_step = re.search(r"然后|再|接着|之后|最后|并(?:且)?", text[previous_end:start])
        previous_end = stop
        if owner:
            if owner == "search" and explicit_step and frame:
                owner = None
            else:
                continue
        if frame:
            contexts.append([start, len(text)])
        elif _diagnostic_content_intro(clause):
            for context in contexts:
                if context[1] == len(text):
                    context[1] = start
            owner = "search" if _web_search_decision(clause) or _is_reminder_clause(clause) else "payload"
    return [text[start:end] for start, end in contexts if start < end]


def _screenshot_constraint_decision(text):
    for context in _screenshot_request_contexts(text):
        outer = mask_quotes(context)
        first_span = _literal_clause_spans(context)[0]
        frame = _screenshot_clause_request(context[slice(*first_span)])
        delivery = re.search(r"(?:发给|发到|发送给|转给|传给|发|给)(.+)", frame["tail"]) if frame else None
        if re.match(r"^(?:先)?" + _SCREENSHOT_WORDS + r"(?:之前|以前|前)(?:先|要先)?", _strip_prefix(outer)):
            reply = "截屏前需要先关闭哪个具体窗口？请把前置步骤说明白。"
        elif re.search(r"(?:不要|别|不能|不)(?:包含|包括|拍到|截到|截入)|(?:只)?(?:截|照)(?:一下|个|一张)?(?:当前窗口|这个窗口|那个窗口|区域|一部分|局部)|(?:局部|区域|窗口)(?:截图|截屏)", outer):
            reply = "目前只支持整屏截图。你要改为整屏，还是先取消？"
        elif re.search(r"(?:存到|保存到|放到|复制到)(?:系统)?剪贴板|(?:别|不要|不)(?:保存|存)(?:到|成)?(?:文件|桌面)?(?:[，,。！!]|$)|只[^，,。]{0,4}剪贴板", outer):
            reply = "目前截图会保存到桌面，暂不支持只存剪贴板。要改用桌面保存吗？"
        elif re.search(r"(?:保存到|存到|放到|另存为)\s*(?:[A-Za-z]:[\\/]|下载|文档|D盘|C盘)|(?:文件名|命名)(?:为|叫|是|用)?", outer) or re.search(r"(?:保存到|存到|放到|另存为)\s*[“\"‘「]?[A-Za-z]:[\\/]", context):
            reply = "指定保存路径或文件名暂不支持。要改为默认保存到桌面吗？"
        elif delivery and not re.fullmatch(r"我(?:看看|看下|看一下)?[吧呗啊呀哦了吗呢。！!?？\s]*", delivery.group(1)):
            reply = "发送截图还需要明确渠道和收件人。你希望通过什么方式发送？"
        elif frame and frame["limit"]:
            reply = "目前只支持整屏截图并保存到桌面。要改用这个方式吗？"
        else:
            continue
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "screenshot_scope_unsupported", "reply": reply, "_pending": None}
    return None


def _screenshot_proposal_mismatch(text, proposed_count):
    if mention_reason(text):
        return True
    contexts = _screenshot_request_contexts(text)
    if not contexts:
        return True
    expected = fast_match_intent(text)
    if expected:
        plan = json.loads(expected["param"]) if expected.get("action") == "multi_action" else [expected]
        return sum(step.get("action") == "screenshot" for step in plan) != proposed_count
    return len(contexts) != proposed_count


def diagnostic_source(text):
    """Only a source request selects clipboard/screen; source names inside evidence do not."""
    outer = _strip_prefix(_STEP_ORDER_RE.sub("", mask_quotes(text).strip())).strip(" ，,。！!？?")
    outer = re.sub(r"[吧呗啊呀]+$", "", outer)
    look = r"(?:诊断|分析|读取|检查|查看|看看|看下|看一下|瞅一眼)"
    for word, source in (("剪贴板", "clipboard"), ("屏幕", "screen")):
        target = word + r"(?:中|里|上)?(?:的)?(?:报错|错误|内容|信息)(?:信息|内容)?"
        if re.fullmatch(look + r"(?:一下)?" + target, outer) or re.fullmatch(target + r"(?:帮我)?" + look, outer):
            return source
    if not outer or re.fullmatch(r"(?:瞅一眼|给俺看看|看是弄啥哩|(?:诊断|分析|看一眼|看下|看看)(?:一下)?(?:这个|这段|一下)?报错)", outer):
        return "clipboard"
    return "description"


def _literal_clause_spans(text):
    """Split original text without changing offsets or reading quoted punctuation as steps."""
    protected = _QUOTED_SPAN_RE.sub(lambda m: "Q" * len(m.group(0)), text)
    split = re.compile(_STEP_SPLIT_RE.pattern + r"|(?<=[一-鿿])\s+(?=[一-鿿])(?!说|让他|让她|问他|问她|告诉他|告诉她)")
    spans, start = [], 0
    for match in split.finditer(protected):
        if text[start:match.start()].strip():
            spans.append((start, match.start()))
        start = match.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _diagnostic_content_intro(text):
    """An unquoted message, reminder, search or text input can own all following words."""
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", text.strip()))
    if _diagnostic_decision(clean):
        return False
    quotes = list(_QUOTED_SPAN_RE.finditer(clean))
    if quotes and not clean[quotes[-1].end():].strip(" ，,。！! "):
        return False  # A closed quoted body makes a subsequent outside step distinguishable.
    outer = mask_quotes(clean)
    if _RELAY_RE.search(outer) or re.match(r"(?:聊天软件)?(?:给|发给).{1,12}(?:发|说)", outer):
        return True
    search = _web_search_decision(clean)
    if search and search.get("action") == "web_search":
        return True
    return bool(re.search(r"输入|写入|填写|填入|写到|写一句|写一段", outer))


def _diagnostic_analysis_request(text):
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", text.strip())).strip(" ，,。！!？?")
    return bool(re.fullmatch(r"(?:分析|诊断|排查|看看|看下|看一下|瞅瞅)(?:一下)?"
                             r"(?:原因|问题|这个问题|报错|错误|啥毛病|怎么回事)?[吧呗啊呀]*", clean))


def _diagnostic_evidence_clause(text):
    """Free-text evidence can precede an analysis request; independent operations cannot."""
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", text.strip()))
    if not clean or _diagnostic_decision(clean) or _diagnostic_content_intro(clean) or negative_request(clean):
        return False
    if _CMD_HINT_RE.match(clean) or re.fullmatch(
            r"(?:把|将)?[^，,。]{1,16}(?:给我|帮我)?(?:打开|关闭|关掉|启动|关了|开开)[吧呗。！!]*", clean):
        return bool(re.search(r"(?:时|之后|后)(?:报|提示|出现)|(?:打开|启动|运行|关闭|连接|加载)(?:失败|不了)", mask_quotes(clean)))
    return True


def diagnostic_request_text(user_text, param=""):
    """Choose a diagnostic step from trusted original words, never an invented model source."""
    original = (user_text or "").strip()
    if not original:
        return str(param or "").strip()  # legacy direct text/clipboard calls
    spans = _literal_clause_spans(original)
    if mention_reason(original) in ("reported", "quoted_report", "past_story") or \
            spans and _WHOLE_CANCEL_RE.fullmatch(original[slice(*spans[-1])].strip()):
        return None
    if len(spans) <= 1:
        return None if _diagnostic_content_intro(original) else original
    candidates, index = [], 0
    while index < len(spans):
        start, end = spans[index]
        # A symptom followed by “帮我看看” is one diagnosis, not two independent tasks.
        if index + 1 < len(spans):
            pair_end = spans[index + 1][1]
            pair = _STEP_ORDER_RE.sub("", original[start:pair_end].strip())
            if _diagnostic_decision(_strip_prefix(pair)):
                candidates.append((start, pair_end))
                index += 2
                continue
        step = _STEP_ORDER_RE.sub("", original[start:end].strip())
        if _diagnostic_decision(_strip_prefix(step)):
            candidates.append((start, end))
        index += 1
    # Actual error text is open-ended (including English tracebacks), not a symptom vocabulary.
    for index, (start, end) in enumerate(spans):
        if not _diagnostic_analysis_request(original[start:end]):
            continue
        first = index
        while first and not any(a <= spans[first - 1][0] and spans[first - 1][1] <= b for a, b in candidates) \
                and _diagnostic_evidence_clause(original[slice(*spans[first - 1])]):
            first -= 1
        if first < index:
            candidates.append((spans[first][0], end))
    intros = [start for start, end in spans if not any(a <= start and end <= b for a, b in candidates)
              and _diagnostic_content_intro(original[start:end])]
    candidates = [(start, end) for start, end in candidates if not any(i < start for i in intros)]
    if len(candidates) == 1:
        start, end = candidates[0]
        return original if start == 0 and end == len(original) else _STEP_ORDER_RE.sub("", original[start:end].strip())
    wanted = _strip_prefix(_STEP_ORDER_RE.sub("", str(param or "").strip()))
    matches = [_STEP_ORDER_RE.sub("", original[start:end].strip()) for start, end in candidates
               if wanted and _strip_prefix(_STEP_ORDER_RE.sub("", original[start:end].strip())) == wanted]
    return matches[0] if len(matches) == 1 else None


def _diagnostic_decision(clean):
    """Keep a supplied symptom with its request instead of searching the word '原因'."""
    outer = mask_quotes(clean).strip(" ，,。！!？?")
    outer = re.sub(r"[吧呗啊呀]+$", "", outer)
    if not outer or _MORE_STEPS_RE.search(outer) or negative_request(outer) or mention_reason(outer):
        return None
    if re.search(r"网上|上网|百度|搜|教程|怎么用|如何使用|提醒我", outer):
        return None
    ask = r"(?:帮我|替我)?(?:看看(?:啥毛病|什么问题|怎么回事)?|看一下|看下|查查原因|查下原因|查(?:一下)?原因|分析(?:一下)?(?:原因)?|诊断(?:一下)?|排查(?:一下)?)"
    symptom = r"(?:电脑|系统|程序|软件|屏幕|USB|usb|风扇|网络)[^，,。！？?]{0,20}(?:蓝屏|重启|一直响|识别不了|闪|转圈|报错|出错|崩溃|死机|卡住|连不上|没反应|打不开)(?:了)?"
    if re.fullmatch(symptom + r"[，,\s]+" + ask, outer) or \
            re.fullmatch(ask + r"[，,]?\s*" + symptom, outer) or \
            re.fullmatch(ask + r"(?:这个|这段)?(?:报错|错误)(?:信息|内容)?[：:，,]?“Q”", outer) or \
            diagnostic_source(clean) in ("clipboard", "screen"):
        param = "" if diagnostic_source(clean) == "clipboard" and "剪贴板" not in mask_quotes(clean) else clean
        return {"action": "diagnose_error", "param": param, "emotion": "confident", "reply": "我先根据你提供的信息分析。"}
    return None


def _missing_calculation_decision(clean):
    clean = _strip_prefix(_STEP_ORDER_RE.sub("", (clean or "").strip()))
    outer = mask_quotes(clean)
    clauses = _clauses(outer)
    if clauses and _WHOLE_CANCEL_RE.fullmatch(clauses[-1].strip()):
        return None
    if negative_request(outer) or mention_reason(outer) or re.search(r"怎么|如何|教我|能不能|会不会", outer):
        return None
    # The calculator must be the operation being requested, not a message, reminder or search topic.
    match = re.match(r"^(?:自动(?:操作|用|在|打开)?|用|在|打开|操作)计算器(?:里|中|内)?(?:帮我|替我)?(?:算|计算)(?P<body>.*)", clean)
    if not match:
        return None
    spans = _literal_clause_spans(clean)
    end = spans[0][1] if spans else len(clean)
    body = clean[match.start("body"):end]
    body = re.sub(r"^(?:一下|下)?(?:个|一个)?(?:结果|数字|数|算式|表达式)?(?:是|为)?[\s：:]*", "", body)
    number = r"(?:[+＋−\-]?\s*\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?|(?:负|正)?[零一二两三四五六七八九十百千万]+)"
    expression = number + r"\s*(?:[+＋−\-×*÷/]|加(?:上)?|减(?:去)?|乘(?:以)?|除(?:以)?)\s*" + number
    unary = number + r"(?:的)?(?:平方根|平方|立方)|(?:sqrt|sin|cos|tan|log|ln|开平方|平方根)\s*\(?\s*" + number
    supplied = r"^[\s（(“\"‘「『]*\s*(?:" + expression + "|" + unary + ")"
    if re.match(supplied, body):
        return None
    # “算一下，18加27” can supply the expression in the next clause, but a title/input task cannot.
    if not body.strip(" 吧呗。！!:") and len(spans) >= 2:
        following = clean[slice(*spans[1])]
        if re.match(supplied, following) and re.fullmatch(
                r"[\d零一二两三四五六七八九十百千万正负加上减去乘以除的平方根立百分之eEsincotaqlrg+＋−\-×*÷/%^√.\s（()）“”\"‘’「」『』！!]+", following):
            return None
    # Only a missing placeholder needs a question. Other specific input may use math syntax
    # beyond these quick recognizers (percentages, fractions, constants, factorials, ...).
    if body.strip(" 吧呗。！!:") and not re.match(r"^(?:再|然后|接着|之后|后|把|将|给)(?=输|填|把|将|给|打|开|关|发|写|放|粘|标)", body):
        return None
    return {"action": "chat", "param": "", "emotion": "confident", "_guard": "calculation_missing",
            "reply": "要计算什么算式？如果要把结果输入别处，也请告诉我目标位置。"}


def _auto_ui_decision(c):
    """“自动在 X 里填 / 点 / 输入 / 新建……”“自动操作 Word……”：整句交给界面自动操作，执行前照样确认。
    不拆成“打开网址”一步（原来“自动在Chrome打开网址后点‘帮助’”只开了网址、丢了点击）。
    后面还有“然后 / 再……”别的步骤时不整句吞掉，交给按分句处理。"""
    missing = _missing_calculation_decision(c)
    if missing:
        return missing
    if _AUTO_UI_RE.match(c or "") and re.search(r"表单里填|一段字|改个选项|找一个选项|一个选项|某个按钮|个按钮|窗口位置整理|"
                                                 r"填一项|一项弄好|弄一下|弄弄|搞一下|省点手|随便", c) \
            and not re.search(r"https?://\S+[^，,。]*(?:姓名|栏|框|字段)|(?:姓名|名字)栏", c):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "ambiguous",
                "reply": "具体是哪个网页、哪个地方？说清楚我再动手。"}
    if _MORE_STEPS_RE.search(c or "") or not _AUTO_UI_RE.match(c or "") or not re.search(r"点|填|输入|选|缩放|新建|写|画|保存|设为|设成|复制|最大化|最小化|放到|居中|字号|打开", c):
        return None
    task = re.sub(r"[，,]?\s*(?:这次)?(?:算我(?:提前)?(?:批准|同意|确认)[^，,。]*|不用问[^，,。]*|操作前先让我确认步骤|别改字)[。！!]*$", "", c).strip("，, 。")
    return {"action": "ufo_task", "param": task, "emotion": "confident", "reply": "正在处理。"}


_TIME_OR_ASK = r"(?:今天|今晚|明天|明晚|后天|周末|晚上|中午|下午|上午|现在|待会|几点|有没有|在不在|能不能|要不要|去不去|来不来)"


_VAGUE_ASKS = [
    # 联系人只说了身份 / 姓（“聊天软件给我的同学发…”“姓张的”“负责资料的人”）
    (re.compile(r"(?:聊天软件)?(?:给|发给)(?:我的)?(?:同学|姓.的|负责[^，,。]{1,6}的人|那个人|某个同事|一个人)"), "发给谁呀？说个准确的名字。"),
    # 说了发给谁，没说发什么（“聊天软件给联系人张三发条话呗，让他知道一声”）
    (re.compile(r"(?:给|发给)[^，,。“\"]{1,8}?发(?:条|句)话(?:呗|吧)?(?![“\"「])"), "要跟他说什么？把原话告诉我。"),
    (re.compile(r"把个软件|关个软件|关掉一个软件|关一个软件|开个软件(?!.*[“\"「])"), "要关哪个软件？说个名字。"),
    (re.compile(r"(?:muse|缪斯)[^，,。]{0,6}(?:做点|弄点)(?:网页)?研究|(?:muse|缪斯)研究[“\"「]比较两个", re.I), "研究什么主题？说具体点我再交给 Muse。"),
    (re.compile(r"(?:dots|多兹)[^，,。]{0,6}整理(?:一段|点)(?:我写的话|资料|东西)", re.I), "要整理的内容是什么？把原文发我。"),
]


def _phrase_decision(clean):
    """几种固定说法整句认（按分句拆会把正文 / 对象拆丢）。联系人、正文都取原话里的字，发消息照样先确认。"""
    c = clean or ""
    diagnosis = _diagnostic_decision(c)
    if diagnosis:
        return diagnosis
    if not _MORE_STEPS_RE.search(mask_quotes(c)) and not negative_request(c) and not mention_reason(c) and _is_reminder_clause(c):
        return {"action": "set_reminder", "param": c, "emotion": "confident", "reply": "我来设置这个提醒。"}
    for rx, ask in _VAGUE_ASKS:   # 说得不够具体的：先问，不猜、也不先拿去确认
        if rx.search(c):
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "ambiguous", "reply": ask}
    # 要提醒却没说清几点（“提醒我给花浇水”“明天下午提醒我交报告”“过几分钟提醒我伸伸腿”“明天上晌叫我去读书”）：先问时间
    if re.search(r"提醒我|叫我(?:去|做|起|把|吃|喝)|喊我(?:一下|起)", mask_quotes(c)) and not _diagnostic_content_intro(c) and not _MORE_STEPS_RE.search(mask_quotes(c)) and \
            not re.search(r"\d|[一二两三四五六七八九十百半]+\s*(?:个)?\s*(?:分|小时|钟头|点|秒|天)|(?:别|不要|不用)(?:再)?(?:提醒|叫|喊)|如果|要是|说|常对", mask_quotes(c)):
        try:
            import canaan_proactive
            parsed = canaan_proactive.parse_reminder(c)
        except Exception:
            parsed = None
        if parsed is None:
            part = re.search(r"(今天|明天|后天)?(早上|上午|上晌|中午|下午|傍晚|晚上)", c)
            ask = f"{part.group(0)}几点提醒你？" if part else "什么时候提醒你？说个具体时间，比如二十分钟后或者几点。"
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "vague_time", "reply": ask}
    m = re.fullmatch(r"(?:聊天软件上|在聊天软件上|聊天软件|用聊天软件)?(?:跟|和)(我?[^\s，,说]{1,6}?)说(?:一声|一下|声)[，,]?(.{2,40}?)[。！!]*", c)
    if not m:    # 前面说了“聊天软件”，“跟X说Y”就不会是别的意思（“聊天软件跟我姐说我到家了”）
        m = re.fullmatch(r"(?:聊天软件上|在聊天软件上|聊天软件|用聊天软件)(?:跟|和)(我?[^\s，,说]{1,6}?)说(.{2,40}?)[。！!]*", c)
    if not m:    # “帮我回老王一句收到了”“帮我回一下阿强 就说收到”
        m = re.fullmatch(r"回(?:一下)?([^\s，,一]{1,6}?)[，,\s]*(?:一句|就说)[，,]?(.{1,30}?)[。！!]*", c)
    if not m:
        m = re.fullmatch(r"(?:聊天软件上|在聊天软件上|聊天软件|用聊天软件)?问问([^\s，,]{1,4}?)(" + _TIME_OR_ASK + r".{0,30}?)[。？?！!]*", c)
    if not m:
        m = re.fullmatch(r"给(我?[^\s，,]{1,6}?)发(?:个|条)?(?:聊天软件|消息)[，,\s]?问(?:他|她|问)(.{2,30}?)[。？?！!]*", c)
    if not m:
        m = re.fullmatch(r"发(?:个|条)?(?:消息|聊天软件)给([^\s，,]{1,6}?)[，,\s]?(?:让|叫)(?:他|她)(.{2,30}?)[。！!]*", c)
    if m and not re.fullmatch(r"我|你|他|她|大家|所有人", m.group(1)):
        contact, message = m.group(1), m.group(2).strip()
        return {"action": "send_chatapp", "param": json.dumps({"contact": contact, "message": message}, ensure_ascii=False),
                "emotion": "pamper", "reply": f"好，发给{contact}了。"}
    site = _named_site_request(c)
    if site:
        return site
    if re.fullmatch(r"(?:先)?(?:看看|看一下|查查)?电脑(?:现在)?的?(?:啥|什么)?状态(?:怎么样)?|(?:电脑)?(?:现在)?卡不卡(?:啊|呀)?|(?:电脑)?还有多少电|电量(?:还有)?多少|(?:硬盘|磁盘)(?:还)?剩(?:多少|多大)(?:空间)?"
                    r"|(?:显卡|CPU|cpu)温度(?:多少度?|高不高)?|电脑怎么这么卡[，,]?(?:看看咋回事|看看怎么回事)?[。？?]*", c):
        return {"action": "system_status", "param": "", "emotion": "confident", "reply": "正在检测整机负载。"}
    if re.fullmatch(r"(?:这歌|这首(?:歌)?)不好听[，,]?(?:换一首|换首|下一首|切了|换了)(?:吧)?[。！!]*", c):
        return {"action": "media_control", "param": "next", "emotion": "confident", "reply": "换下一首。"}
    if re.fullmatch(r"(?:刚|刚才|刚刚)那首(?:歌)?再(?:来|放|听)一遍(?:吧)?[。！!]*", c):
        return {"action": "media_control", "param": "prev", "emotion": "confident", "reply": "切回上一首了。"}
    if re.fullmatch(r"(?:把)?(?:这)?歌(?:给我)?停[喽咯了](?:吧)?[。！!]*", c):
        return {"action": "media_control", "param": "play_pause", "emotion": "confident", "reply": "先停一下。"}
    if re.fullmatch(r"(?:在设置里)?把(?:蓝牙|wifi|WiFi|WIFI|无线网?|飞行模式|热点)(?:给我)?(?:关掉|关了|关闭|打开|开了|开开)(?:吧)?[。！!]*", c):
        return {"action": "ufo_task", "param": c, "emotion": "confident", "reply": "正在处理。"}
    if re.fullmatch(r"(?:把)?聊天软件(?:点开|打开)[，,]?我.{0,8}", c):
        return {"action": "open_chatapp", "param": "", "emotion": "confident", "reply": "聊天软件打开啦，随时看吧～"}
    if re.fullmatch(r"(?:把)?窗口(?:一键)?(?:全|都)收(?:起来|了)?(?:吧)?[。！!]*", c):
        return {"action": "show_desktop", "param": "", "emotion": "confident", "reply": "回到桌面了。"}
    m = re.fullmatch(r"(?:去|进)(下载|桌面|文档)(?:那个)?(?:文件夹|目录)(?:看看)?[。！!]*", c)
    if m:
        return {"action": "open_path", "param": m.group(1), "emotion": "pamper", "reply": f"{m.group(1)} 打开啦。"}
    m = re.fullmatch(r"(?:电脑里)?有个叫(.{1,10}?)的文件[，,]?(?:帮我)?(?:打开|找出来|找一下|找找)(?:吧)?[。！!]*", c)
    if m:
        return {"action": "search_file", "param": m.group(1), "emotion": "confident", "reply": f"我找找看 {m.group(1)} 在哪..."}
    m = re.match(r'(?:给|向|交给?)\s*(?:openmuse|muse|缪斯)\s*(?:交|提交|派)?(?:个|一个)?(?:研究)?任务[，,：:\s]*(.+)$', c, re.I)
    if m:   # “给Muse交个研究任务，查……”：交给 OpenMuse，不是普通上网搜（逗号后面是任务内容，整句认）
        return {"action": "openmuse_task", "param": m.group(1).strip(), "emotion": "confident",
                "reply": "任务已经交给 OpenMuse，正在后台为你处理，有进展我会随时向你汇报~"}
    auto = _auto_ui_decision(c)
    if auto:
        return auto
    # Muse 的其他说法（“交Muse做网页研究，题目是X”“请Muse研究X”“Muse那边提交网页研究，研究X”“让Muse查公开网页，研究X”）
    if re.search(r"muse|缪斯", c, re.I) and re.search(r"研究|查", c) and not _MORE_STEPS_RE.search(c) and \
            not re.search(r"上周|昨天|之前|做过|……|…|还是|或者|一个产品|比较两个|做点|点资料|看看资料|示例|写着", c):
        task = re.sub(r"^(?:请|让|叫|交给?|向|给)?\s*(?:openmuse|muse|缪斯)\s*(?:那边)?\s*(?:提交|做)?(?:网页研究|研究任务|网页研究任务)?[，,：:\s]*"
                      r"|^(?:网页研究)?(?:交给?|给)\s*(?:openmuse|muse|缪斯)[，,：:\s]*", "", c, flags=re.I)
        task = re.sub(r"^(?:主题是|题目是|研究)\s*", "", task).strip("，, 。")
        if len(task) >= 4:
            return {"action": "openmuse_task", "param": task, "emotion": "confident",
                    "reply": "任务已经交给 OpenMuse，正在后台为你处理，有进展我会随时向你汇报~"}
    # Dots 整理资料（“给Dots提交资料“X”，整理成……”“交Dots整理我提供的内容“X””“给Dots我写的这两句“X””）
    if re.search(r"dots|多兹", c, re.I) and re.search(r"整理|提交|交", c) and not _MORE_STEPS_RE.search(c) and \
            not re.search(r"示例|写着|列为|演示对话|昨天|上周|之前|整理过|做过|私人|日记|不同意", c):
        q = re.search(r"[“\"「]([^”\"」]+)[”\"」]", c)
        if q:
            return {"action": "opendots_task", "param": q.group(1), "emotion": "confident",
                    "reply": "已通知 OpenDots 专家团队，正在知识空间为你撰写与归档~"}
    # 发聊天软件，正文在引号里（“给小郑的聊天软件写“X””“聊天软件给小王捎个话，正文就写“X””“聊天软件发给小姚“X””）
    m = None if _quoted_chatapp_decision(c) else \
        re.match(r"(?:麻烦|帮忙)?(?:聊天软件)?(?:给|发给)(?:联系人)?\s*([^\s，,“\"「]{1,6}?)\s*(?:的聊天软件|聊天软件发|发聊天软件|发个聊天软件|捎个话|带个话|带句话|发|聊天软件)?[，,]?\s*"
                 r"(?:正文就?写|正文[：:]|写|内容是|就写)?\s*[“\"「]([^”\"」]+)[”\"」]", c)
    if not m and not _quoted_chatapp_decision(c):
        m = re.match(r"聊天软件(?:发给|给)(?:联系人)?\s*([^\s，,“\"「]{1,6}?)\s*(?:捎个话|带个话|发)?[，,]?\s*(?:正文就?写|写|内容是)?\s*[“\"「]([^”\"」]+)[”\"」]", c)
    if m and not re.fullmatch(r"我|你|他|她|大家|所有人|朋友|好友|我朋友|联系人|聊天软件好友|别人|某人", m.group(1)) \
            and not re.search(r"发|说|消息|聊天软件|信息", m.group(1)) and "Dots" not in c and "Muse" not in c:
        return {"action": "send_chatapp", "param": json.dumps({"contact": m.group(1), "message": m.group(2)}, ensure_ascii=False),
                "emotion": "pamper", "reply": f"好，发给{m.group(1)}了。"}
    # 总结已采集的聊天软件 / 聊天软件 聊天
    if re.search(r"已(?:经)?采集", c) and re.search(r"总结|概括|捋|聊了啥|列出|时间线", c) and not _MORE_STEPS_RE.search(c) and \
            not re.search(r"只总结一个|发给|贴到|外发|写在|演示|示例|说明页|看到|截图|不让|属于|没有权|没权", c):
        src = [s for s, k in (("chatapp", "聊天软件"), ("imapp", "IMApp")) if k in c or k.lower() in c]
        if src:
            return {"action": "daily_briefing", "param": {"value": "generate", "sources": src}, "emotion": "confident",
                    "reply": "我把采集到的聊天整理一下。"}
    if re.fullmatch(r"(?:聊天软件里)?(?:刚|刚才|刚刚)(?:给[^，,。]{1,6}?)?发的(?:那一?条|消息)?(?:聊天软件)?撤回(?:来)?[，,]?[^，,。]{0,12}[。！!]*", c):
        return {"action": "recall_chatapp", "param": "", "emotion": "pamper", "reply": "刚才那条消息已经给你撤回来啦～"}
    m = re.match(r"(?:用|在)?浏览器(?:里)?搜(?:一下|索)?\s*[“\"「]([^”\"」]+)[”\"」]", c)
    if m:
        return {"action": "web_search", "param": m.group(1), "emotion": "confident", "reply": f"正在搜 {m.group(1)} 呢，稍等下～"}
    if re.search(r"紧接着的那首|往后切一首|切到下一首", c):
        return {"action": "media_control", "param": "next", "emotion": "confident", "reply": "换下一首。"}
    path = None if _MORE_STEPS_RE.search(c) else (_auto_ui_decision(c) or _path_decision(c))   # 后面还有别的步骤时交给按分句处理
    if path:
        return path
    m = re.search(r"(?:改搜|改成搜|换成搜)\s*[“\"「]([^”\"」]+)[”\"」]", c)
    if m and re.search(r"不对|说错|改", c):   # “网页搜“室内绿萝养护”，不对……改搜“室内吊兰养护””：按改口后的搜
        return {"action": "web_search", "param": m.group(1), "emotion": "confident", "reply": f"正在搜 {m.group(1)} 呢，稍等下～"}
    # 提醒的时间没说清（“过一会儿叫我喝水”“三十以后提醒我活动活动”）：先问多久，不猜
    if re.search(r"提醒我|叫我|喊我", c) and re.search(r"过一会儿?|待会儿?|等一会儿?|晚点儿?|回头|[零一二两三四五六七八九十\d]+(?:以后|之后)", c) \
            and not re.search(r"\d+\s*(?:分|小时|秒|天)|[零一二两三四五六七八九十]+\s*(?:分|小时|秒|天|点)", c):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "vague_time",
                "reply": "多久以后提醒你？比如二十分钟后，还是几点整？"}
    m = re.fullmatch(r"不用(.{1,8}?)了[，,]?(?:把它|就)?(?:关掉|关了|关闭)(?:吧|呗)?[。！!]*", c)
    if m:
        return {"action": "close_app", "param": m.group(1), "emotion": "confident", "reply": f"{m.group(1)} 已关闭。"}
    m = re.match(r"(?:把)?(?:音乐|歌)(?:先)?(?:停一下|停下|暂停一下)", c)
    if m and not _CMD_HINT_RE.search(c[m.end():]):     # “歌停一下有人跟我说话”：后半句是原因
        return {"action": "media_control", "param": "play_pause", "emotion": "confident", "reply": "先停一下。"}
    m = re.match(r"(?:帮我)?开(?:一下|个|下)?\s*([A-Za-z][A-Za-z0-9]{1,15})\s*(?:来|去)?(?:写|做|改|看|画|算|编)", c)
    if m and _find_application(m.group(1)):          # “开个word写个报告”
        return {"action": "open_app", "param": m.group(1), "emotion": "confident", "reply": f"{m.group(1)} 打开了。"}
    m = re.search(r"(?:竞赛|比赛)(?:的)?(?:规则|指南|文档)(?:里|中)?(?:对|关于)?(.{1,10}?)(?:有什么|的)?(?:要求|规定)", c)
    if m:
        return {"action": "doc_search", "param": m.group(1), "emotion": "confident", "reply": "我查一下比赛资料。"}
    return None


def _quiet_decision(clean):
    """“别放了，先停”“别出声，静音”：整句是要停 / 要安静。按分句处理时前半句会被当成否定丢掉，所以整句先认。"""
    mute = re.fullmatch(r"(?:把)?(?:系统|电脑)?的?静音(?:模式|开关)?(?:给我)?(打开|开启|关闭|关掉|取消)(?:吧|呗)?[。！!]*", clean or "")
    if mute:
        return {"action": "volume_mute", "param": "on" if mute.group(1) in ("打开", "开启") else "off",
                "emotion": "confident", "reply": "正在调整静音。"}
    if re.fullmatch(r"(?:别放了|别播了|别唱了)[，,]?(?:先)?(?:停|暂停)(?:一下|下)?(?:吧)?[。！!]*", clean or ""):
        return {"action": "media_control", "param": "play_pause", "emotion": "confident", "reply": "先停一下。"}
    if re.fullmatch(r"(?:先)?别出声(?:了)?(?:[，,]?(?:静音|静个音))?(?:吧)?[。！!]*", clean or ""):
        return {"action": "volume_mute", "param": "on", "emotion": "confident", "reply": "已静音。"}
    return None


def _where_decision(clean):
    """“我的简历文件在哪”“合同那个文件在哪我找不到了”：问自己的文件在哪 = 帮我找；“我的图片在哪，打开看看”= 开图片文件夹。"""
    where = _WHERE_RE.fullmatch(clean or "")
    if not where or re.search(r"截图|保存|下载的|设置|按钮|功能", where.group(1)):
        return None
    name = re.sub(r"^(?:我的|那个|这个)", "", where.group(1)).strip()
    if name in ("图片", "照片", "下载", "文档", "桌面"):
        return {"action": "open_path", "param": name, "emotion": "pamper", "reply": f"{name} 打开啦。", "_where": True}
    if where.group(2) == "文件" or re.search(r"简历|合同|报告|表格|论文|发票|扫描件|文件", name):
        return {"action": "search_file", "param": name, "emotion": "confident", "reply": f"我找找看 {name} 在哪...", "_where": True}
    return None


def _local_file_decision(clean):
    """Explicit filename queries stay local; filename words are never action verbs."""
    scope_names = {"桌面": "desktop", "下载": "downloads", "下载文件夹": "downloads", "文档": "documents", "项目": "project"}
    scope = r"(桌面|下载文件夹|下载|文档|项目)"
    scoped = re.fullmatch(r"(?:打开|找(?:一下|找)?|搜(?:一下|索)?)" + scope + r"(?:(?:上|里|中)?的|(?:上|里|中))(.+?)[。！!]*", clean) or re.fullmatch(
        scope + r"(?:上|里|中)(?:的)?(?:那个|这个)?(.+?)(?:帮我|给我)?(?:打开|找出来)[。！!]*", clean)
    if scoped and len(_clauses(clean)) == 1:
        raw_query = scoped.group(2).strip()
        literal = bool(_QUOTED_SPAN_RE.fullmatch(raw_query))
        query = raw_query.strip(" “”。！!\"「」")
        if query in ("文件", "文件夹", "东西", "目录", "那个", "这个"):
            return {"action": "chat", "param": "", "emotion": "confident", "reply": "要找哪个文件？说个名字或关键词。", "_guard": "vague_query"}
        if query and not re.search(r"[\\/:]", query):
            if re.search(r"文件夹$|目录$", query):
                param = {"value": query, "root": scope_names[scoped.group(1)]}
                if literal:
                    param["literal"] = True
                return {"action": "open_path", "param": param, "emotion": "confident", "reply": "我打开你指定的文件夹。"}
            return {"action": "search_file", "param": {"value": query, "root": scope_names[scoped.group(1)]},
                    "emotion": "confident", "reply": "我到你指定的目录里找。"}
    match = re.fullmatch(r"(?:搜(?:索|搜)?(?:一下)?|找(?:一下|找)?)\s*(.+?)[。！!]*", clean)
    if not match:
        return None
    query = match.group(1).strip()
    named = re.fullmatch(r"(?:文件名|名字|名称)(?:为|叫|带|包含|含有)\s*(.+)", query)
    if named:
        target = re.sub(r"(?:的)?(?:文件|PDF|文档)$", "", named.group(1), flags=re.I).removesuffix("的")
    else:
        named = re.fullmatch(r"(?:名叫|名为)(.+?)(?:的)?(?:文件|PDF|文档)", query, re.I)
        if not named:
            named = re.fullmatch(r"(?:上次|刚才)?(?:那个|这个)?(.+?)(?:的)?文件", query)
        if not named:
            return None
        target = named.group(1)
    target = target.strip(" “”\"「」。！!")
    if not target:
        return None
    return {"action": "search_file", "param": target, "emotion": "confident", "reply": f"我找找看 {target} 在哪。"}


def _local_system_decision(clean):
    """Complete local queries map to fixed commands, not model-written shell text."""
    tools = {"任务管理器": "taskmgr", "控制面板": "control", "服务管理器": "services.msc",
             "设备管理器": "devmgmt.msc", "注册表编辑器": "regedit"}
    names = "|".join(tools)
    tool = re.fullmatch(r"(?:打开|启动|开|调出)(?:一下)?(" + names + r")[。！!]*", clean) or re.fullmatch(
        r"(?:把)?(" + names + r")(?:给我|帮我)?(?:调出来|调出|打开|开开)[。！!]*", clean)
    if tool:
        return {"action": "run_cmd", "param": tools[tool.group(1)], "emotion": "confident", "reply": "正在打开。"}
    query = re.sub(r"^(?:查(?:询)?(?:一下|下)?|看看|看一下|看下|显示一下|列出)", "", clean).strip()
    query = re.sub(r"(?:是多少|是什么|有哪些|多少|[？?。！!])+$", "", query)
    if re.fullmatch(r"(?:本机)?网络(?:延迟|丢包率)", query):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "network_target_missing",
                "reply": "要测哪个网站或地址的网络延迟和丢包率？"}
    drive = re.fullmatch(r"([A-Za-z])盘(?:现在|这会儿)?(?:还剩|还有|剩|有)(?:多少|多大)(?:空间|容量)?", query)
    if drive:
        return {"action": "run_cmd", "param": f"Get-PSDrive -Name {drive.group(1).upper()} | Select-Object Used,Free",
                "emotion": "confident", "reply": "正在查询磁盘空间。"}
    queries = [
        (r"(?:磁盘|硬盘)(?:的)?(?:剩余空间|空间还剩几个[Gg]|还剩多少空间)", "Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,Size,FreeSpace"),
        (r"(?:本机|电脑的?)?系统版本", "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber"),
        (r"(?:本机)?网络配置", "ipconfig /all"),
        (r"(?:本机)?MAC地址", "Get-NetAdapter | Select-Object Name,MacAddress"),
        (r"(?:开了哪些|有哪些|当前)?进程", "Get-Process | Select-Object ProcessName,Id,CPU,WorkingSet64"),
        (r"(?:有哪些|当前)?启动项", "Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location"),
        (r"系统补丁(?:版本)?", "Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 5 HotFixID,InstalledOn"),
        (r"(?:电脑|本机)(?:的)?序列号", "Get-CimInstance Win32_BIOS | Select-Object SerialNumber"),
        (r"(?:有哪些|当前)?(?:定时任务|计划任务)", "Get-ScheduledTask | Select-Object TaskName,TaskPath,State"),
        (r"显卡驱动版本", "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion"),
        (r"磁盘读写速度", "Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk | Where-Object Name -eq '_Total' | Select-Object DiskReadBytesPersec,DiskWriteBytesPersec"),
    ]
    for pattern, command in queries:
        if re.fullmatch(pattern, query, re.I):
            return {"action": "run_cmd", "param": command, "emotion": "confident", "reply": "正在查询本机信息。"}
    return None


def _in_app_ui_decision(clean):
    """Keep an explicit application and its in-app operation in one confirmed task."""
    match = re.fullmatch(r"(?:在|从)(Excel|Word|WPS|PowerPoint|控制面板|浏览器|记事本|计算器|设置|资源管理器|聊天软件)"
                         r"(?:里|中|内)(?:帮我|替我)?(.+)", clean, re.I)
    if not match or _MORE_STEPS_RE.search(mask_quotes(clean)):
        return None
    task = match.group(2)
    outer = mask_quotes(task)
    if re.search(r"怎么|如何|教我|为什么|学学|会不会", outer):
        return None
    if re.search(r"(?:不要|别|先不|暂时不|不用|无需)(?:再)?(?=新建|收藏|删|输入|写|填|关|开|点|保存|排序)", outer):
        return dict(_NEGATED_REPLY) if len(_clauses(clean)) == 1 else None
    if re.fullmatch(r"(?:写|输入|填写)(?:一下)?", task):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "no_content", "reply": "要输入什么内容？把原文告诉我。"}
    if re.match(r"卸载(?:一个|某个)软件", task):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "no_object", "reply": "要卸载哪个软件？请说具体名称。"}
    if re.match(r"写|输入|点击|点一下|计算|查找|搜索|刷新|打开|关闭|另存为|新开|新建|收藏"
                r"|把.{1,30}(?:求和|删掉|删除|关掉|关闭|排序|设为|设成)", task):
        return {"action": "ufo_task", "param": clean, "emotion": "confident", "reply": "我先核对操作步骤。"}
    return None


def _web_search_decision(text, explicit=False):
    if re.search(r"百度(?:网盘|文库|地图|输入法|贴吧)", text) and not re.match(
            r"^(?:在百度(?!网盘|文库)|用百度(?!网盘|文库)|百度一下|百度搜|搜|查|网上|上网)", _strip_prefix(text)):
        return None   # 产品名里的“百度”不是搜索动词。
    # “上百度首页”“去知乎网站看看”：是要打开那个网站，不是搜“首页”
    site = _named_site_request(text)
    if site:
        return site
    # “检查一下 / 调查一下”里的“查一下”不是上网搜（“提醒俺检查一下购物清单”原来会去搜“购物清单”）
    match = re.search(r'(?:在百度(?!网盘|文库|地图|输入法|贴吧)|用百度(?!网盘|文库|地图|输入法|贴吧)|百度一下|百度搜(?:一下)?|百度(?!网盘|文库|地图|输入法|贴吧)|搜一下|搜索(?!页|出来)|(?<![检调排审核考])查一下|搜搜|(?<![检调排审核考])查查|^查下|^帮我查下)'
                      r'\s*(?:关于)?\s*(.+)', text)
    if not match or not explicit and re.search(r'(电脑|本机|[cCdD]盘|磁盘|硬盘|内存|进程|开机|显卡|ip|IP|ping|网通不通|指南|文档|规则|规定'
                                               r'|CPU|cpu|处理器|占用|使用率|系统状态|电脑状态)', text):
        return None
    kw = match.group(1).strip()
    quoted = _QUOTED_SPAN_RE.search(kw)
    if quoted and kw[:quoted.start()].strip() in ("", "查", "搜", "查一下", "搜一下") and not kw[quoted.end():].strip("，,。！!？? "):
        kw = quoted.group(0)[1:-1].strip()
    return {"action": "web_search", "param": kw, "emotion": "confident", "reply": f"正在搜 {kw} 呢，稍等下～"}


# ---- 10-24：常用电脑控制的说法补全（练习题 eval600 dev 里这些都被 1.5B 判成闲聊，再被守卫改成反问） ----
_MUTE_OFF_RE = re.compile(r"(?:取消|关掉|关闭|关了|解除|去掉|退出|关上)(?:系统|电脑)?的?静音|静音(?:模式|开关)?(?:给我)?(?:关掉|关闭|关了|取消|解除|去掉)")
_MUTE_TOGGLE_RE = re.compile(r"静音(?:模式|开关)?(?:给我)?(?:切|切换)(?:一次|一下)|(?:切|切换)(?:一下|一次)?(?:系统)?静音")
_MUTE_ON_RE = re.compile(r"(?:打开|开启|开上|设成|设为|调成|弄成|切到)(?:系统|电脑)?的?静音|静音(?:模式|开关)?(?:打开|开启|开上)"
                         r"|(?:系统|电脑)(?:的)?(?:声音)?[^，,。]{0,4}(?:全部|都)?静音|声音[^，,。]{0,4}全部静音")
_VOLUME_PIN_RE = re.compile(r"(?:音量|声音)[^，,。亮]{0,6}?(?:定在|定到|设在|调在|弄到|弄成|搞到|放在|保持在)\s*"
                            r"((?:\d|[一二两三四五六七八九十])\s*成|百分之[零一二两三四五六七八九十百]+|\d+\s*[%％]?|[零一二两三四五六七八九十百]+)")
_STEP_WORD = r"(?:一点儿?|一些|点儿?|一档|一格|一级|些)"
_BRIGHT_UP_RE = re.compile(r"(?:屏幕|亮度)[^，,。音声]{0,8}?(?:稍微|稍稍|再|往上)?(?:升|提|提高|升高|加|调高|调亮|亮)" + _STEP_WORD)
_BRIGHT_DOWN_RE = re.compile(r"(?:屏幕|亮度)[^，,。音声]{0,8}?(?:稍微|稍稍|再)?(?:往下调|往下|降|降低|减|调低|调暗|暗)" + _STEP_WORD)
# 要有动作（切 / 换 / 放 / 回）：“还是前一首适合现在听”只是评论，单独算一步会多切一首
_MEDIA_PREV_RE = re.compile(r"(?:切|换|放|播|回|跳)[^，,。]{0,6}(?:前面那首|前一首|前一曲|前面的?那首|上一首歌|上一个(?:视频|曲子|歌|节目))")
_MEDIA_RESUME_RE = re.compile(r"(?:接着|继续)(?:播|放|播放)|(?:视频|音乐|歌|媒体)(?:接着|继续)")
_LOCK_RE = re.compile(r"锁(?:一下|上|住)?(?:电脑)?的?屏幕|锁(?:一下|上|住)电脑|电脑锁(?:一下|上)"
                      r"|(?:电脑)?屏幕(?:给我)?锁(?:了|上|住)")
_STATUS_RE = re.compile(r"CPU|cpu|处理器|内存(?:占用|使用|用了|还剩|剩)|使用率|占用率|占用多少")
_URL_RE = re.compile(r"(?ai:https?)://[A-Za-z0-9\-._~:/?#\[\]@!$&*+;=%]+")
_NET_SEARCH_RE = re.compile(r"^(?:你)?(?:去)?(?:网上|上网|在网上|到网上)(?:查一下|搜一下|查查|搜搜|搜索|查|搜(?!索)|找找|找一下)\s*(?:关于)?\s*(.*)$")
_CHOICE_RE = re.compile(r"[^，,。]{1,10}(?:或者|还是|或)[^，,。]{1,10}?(?:关|撤回|发|交给|打开|开)?(?:一个|其中一个|其中一条|哪个|哪一个)"
                        r"|(?:撤回|关掉?|删)(?:其中)?(?:一条|一个)(?![^，,。]*(?:最后|刚))"
                        r"|(?:聊天软件|通讯录|联系人|好友)(?:里|中|上)?(?:有|存了)(?:两个|俩|好几个)"
                        r"|(?:往上|调大|调高|调亮)还是(?:往下|调小|调低|调暗)|只(?:点|选|开|关)一个|拿不定主意|还(?:在)?犹豫"
                        r"|叫法老串|分不清|往前往后|记混|前面还是后面|只动一样|音量亮度里|亮度音量里")
# 只说了一类、没说具体是什么（“两段资料里的一段”“写一段字”“设成一个固定值”）：先问
_UNSPECIFIED_RE = re.compile(r"(?:两段|两个|两份|几段|几个|两条)[^，,。]{0,6}(?:里|中)的?(?:一段|一个|一份|一条)"
                             r"|(?:写|输入|打)(?:一段|一些|点|几个|一行)(?:字|文字|内容)(?!.*[“\"「])"
                             r"|(?:设成|调到|调成|设为|定成)(?:一个|某个)(?:固定)?(?:的)?(?:值|数)"
                             r"|点(?:个|一个|下)按钮(?!.*[“\"「])"   # “自动在Chrome里点个按钮吧”：没说点哪个
                             r"|设(?:个|一个)固定(?:的)?(?:百分比|百分数|数|值)|固定(?:到|在)?(?:一个|某个)(?:百分数|百分比)"
                             r"|只总结一个(?:联系人|人|群)|(?:总结|看)(?:其中)?一个联系人的")
# 两个互相矛盾的要求都要算（“音量设25%，但最终得75%，这两句我都要算数”）
_CONTRADICT_RE = re.compile(r"(?:两条|两句|两个要求|两样)[^，,。]{0,4}(?:都要?算数?|都算我的要求|都要|都得算)|只留一个最终状态")
_GENERIC_APP_CLOSE_RE = re.compile(r"(?:音乐|视频|聊天|播放)(?:软件|播放器|程序|应用)[^，,。]{0,4}(?:关|退出)|(?:关|退出)[^，,。]{0,4}(?:音乐|视频|聊天)(?:软件|程序|应用)")


_READ_CMDS = [
    # 只读的系统查询：命令固定，不用模型编（模型编的命令过不了参数核对，原来只能反问）
    (re.compile(r"([cCdDeE])\s*盘(?:还)?(?:剩|有|还有)(?:多少|多大)(?:空间|容量)?|([cCdDeE])\s*盘(?:空间|容量)(?:还)?(?:剩|有)?多少"),
     lambda m: f"Get-PSDrive -Name {(m.group(1) or m.group(2)).upper()} | Select-Object Used,Free", "查一下磁盘空间。"),
    (re.compile(r"ping\s*一?下?\s*(百度|baidu)", re.I), lambda m: "ping www.baidu.com -n 4", "我 ping 一下百度。"),
    (re.compile(r"开机(?:了)?(?:多久|多长时间|几个小时)"),
     lambda m: "(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime", "查一下开机多久了。"),
    (re.compile(r"(?:ip|IP)(?:地址)?(?:是)?多少|连的(?:是)?哪个网"), lambda m: "ipconfig", "查一下本机网络和 IP。"),
]


def _device_rule(text, clean):
    t = mask_quotes(clean)
    numeric = re.fullmatch(r"(?:把)?(音量|声音|屏幕亮度|亮度)(?:给我)?(?:设|开)"
                           r"([零一二两三四五六七八九十百]+|\d+)(?:[%％])?(?:吧|呗)?[。！!]*", t) or re.fullmatch(
                               r"(?:把)?(音量|声音|屏幕亮度|亮度)(?:给我)?"
                               r"(百分之[零一二两三四五六七八九十百]+|\d+[%％])(?:吧|呗)?[。！!]*", t)
    if numeric:
        value = set_value(numeric.group(2))
        if value is not None and 0 <= value <= 100:
            return {"action": "set_brightness" if "亮度" in numeric.group(1) else "volume",
                    "param": str(int(value)), "emotion": "confident", "reply": "正在调整。"}
    for rx, cmd, reply in _READ_CMDS:
        m = rx.search(t)
        if m:
            return {"action": "run_cmd", "param": cmd(m), "emotion": "confident", "reply": reply}
    if re.fullmatch(r"所(?:一下|个|下)?屏幕?(?:吧|一下)?[。！!]*", t):     # 语音识别常把“锁屏”听成“所屏”
        return {"action": "lock_screen", "param": "", "emotion": "confident", "reply": "电脑已锁定。"}
    if re.fullmatch(r"(?:把)?(?:音乐|歌|视频|歌曲)(?:先)?(?:停停|停一停|停一下|别放了|不放了|别播了|先别放了?)(?:吧|呗)?[。！!]*", t) or \
            re.fullmatch(r"(?:别放了|别播了)[，,]?(?:先)?停(?:一下|下)?(?:吧)?[。！!]*", t):
        return {"action": "media_control", "param": "play_pause", "emotion": "confident", "reply": "先停一下。"}
    if re.fullmatch(r"(?:先)?(?:把)?(?:电脑)?(?:声音|音量)(?:全|都)?(?:关了|关掉|关上|关闭)(?:吧|呗)?[。！!]*", t):
        return {"action": "volume_mute", "param": "on", "emotion": "confident", "reply": "已静音。"}
    m = re.search(r"(?:看看|瞧瞧|打开|开)\s*(下载|桌面|文档|[cCdD]\s*盘)(?:文件夹|目录)?(?:里|里面)?(?:有啥|有什么|的东西|看看)?[。！!吧]*$", t) or \
        re.fullmatch(r"(?:把)?\s*(下载|桌面|文档|[cCdD]\s*盘)(?:文件夹|目录)?\s*(?:给我)?(?:打开|开开)看看[。！!吧]*", t)   # 不带“看看”的由后面的通用规则处理（保留“下载文件夹”原样）
    if m and not re.search(r"截|发|搜|找|删", t):
        return {"action": "open_path", "param": m.group(1).replace(" ", "").lower(), "emotion": "pamper",
                "reply": f"{m.group(1)} 打开啦。"}
    if re.search(r"(?:哪个|哪些)(?:程序|软件|进程)[^，,。]{0,4}(?:最)?占(?:内存|CPU|cpu)|(?:最)?占内存的(?:程序|进程)", t):
        return {"action": "system_status", "param": "", "emotion": "confident", "reply": "正在检测整机负载。"}
    if "静音" in t:
        if re.search(r"静音[^，,。]{0,4}(?:开着|打开)", t) and re.search(r"静音[^，,。]{0,4}(?:关着|关闭|关掉)", t):
            return None        # “静音开着，也得关着”：自相矛盾，交给守卫反问
        off, toggle = _MUTE_OFF_RE.search(t), _MUTE_TOGGLE_RE.search(t)
        on = None if off else _MUTE_ON_RE.search(t)   # “把电脑的静音取消”里的“电脑的静音”不算要静音
        if toggle:
            return {"action": "volume_mute", "param": "", "emotion": "confident", "reply": "切换一下静音。"}
        if off:
            return {"action": "volume_mute", "param": "off", "emotion": "confident", "reply": "取消静音了。"}
        if on:
            return {"action": "volume_mute", "param": "on", "emotion": "confident", "reply": "已静音。"}
    m = _VOLUME_PIN_RE.search(t)
    if m and set_value(m.group(1)) is not None and 0 <= set_value(m.group(1)) <= 100:
        v = int(set_value(m.group(1)))
        return {"action": "volume", "param": str(v), "emotion": "confident", "reply": f"音量调到 {v}% 了。"}
    if not re.search(_SET_VALUE, t):
        direction = re.fullmatch(r"(?:把)?(?:屏幕(?:的亮度)?|亮度)(?:给我)?(调亮|调高|调暗|调低)[吧呗。！!]*", t)
        if direction:
            return {"action": "brightness_up" if direction.group(1) in ("调亮", "调高") else "brightness_down",
                    "param": "", "emotion": "confident", "reply": "正在调整亮度。"}
        if _BRIGHT_UP_RE.search(t):
            return {"action": "brightness_up", "param": "", "emotion": "confident", "reply": "屏幕调亮了。"}
        if _BRIGHT_DOWN_RE.search(t):
            return {"action": "brightness_down", "param": "", "emotion": "confident", "reply": "屏幕调暗了。"}
    if _MEDIA_PREV_RE.search(t):
        return {"action": "media_control", "param": "prev", "emotion": "confident", "reply": "切回上一首了。"}
    if _MEDIA_RESUME_RE.search(t) and not re.search(r"下一|上一|切", t):
        return {"action": "media_control", "param": "play_pause", "emotion": "confident", "reply": "接着放。"}
    if _LOCK_RE.search(t):
        return {"action": "lock_screen", "param": "", "emotion": "confident", "reply": "电脑已锁定。"}
    if _STATUS_RE.search(t) and re.search(r"查|看|告诉|多少|忙不忙|怎么样|情况|剩", t) and not re.search(r"网上|上网|百度|教程|怎么(?:降|优化|设置)", t):
        return {"action": "system_status", "param": "", "emotion": "confident", "reply": "正在检测整机负载。"}
    m = re.search(r"(?:运行|跑)(?:一下)?(?:那个)?\s*([A-Za-z_][\w.-]*?)(?:\.py)?\s*(?:那个)?脚本"
                  r"|把(?:那个)?\s*([A-Za-z_][\w.-]*?)(?:\.py)?\s*脚本(?:给我)?(?:跑|运行)(?:起来|一下)?", t)
    if m:   # “运行export_data那个脚本”“把那个deploy脚本跑起来”：脚本名是原话里的英文名
        name = m.group(1) or m.group(2)
        return {"action": "run_script", "param": name, "emotion": "confident", "reply": f"运行 {name}。"}
    if re.fullmatch(r"(?:一键)?开工(?:了|吧|啦)?[。！!]*", t):
        return {"action": "setup_workspace", "param": "", "emotion": "confident", "reply": "开发环境这就给你摆好。"}
    if not re.search(r"昨天|前天|上次|以前|之前|那天|别人|他|她", t):
        if re.search(r"(?:声音|音量|喇叭)[^，,。]{0,6}(?:有点轻|太轻|好轻|太小声)", t):
            return {"action": "volume_up", "param": "5", "emotion": "confident", "reply": "音量调大了。"}
        if re.search(r"(?:声音|音量|喇叭)[^，,。]{0,4}(?:压低|放低|降低|调低)", t):
            return {"action": "volume_down", "param": "5", "emotion": "confident", "reply": "音量调小了。"}
        if re.search(r"屏幕[^，,。]{0,4}(?:太亮|好亮|亮得刺眼|刺眼|晃眼)", t):
            return {"action": "brightness_down", "param": "", "emotion": "confident", "reply": "屏幕调暗了。"}
        if re.search(r"屏幕[^，,。]{0,4}(?:太暗|好暗|暗得看不清)", t):
            return {"action": "brightness_up", "param": "", "emotion": "confident", "reply": "屏幕调亮了。"}
    if re.fullmatch(r"(?:上|登)(?:一下|一会儿?)?聊天软件(?:吧|呗)?[。！!]*", t):
        return {"action": "open_chatapp", "param": "", "emotion": "confident", "reply": "聊天软件打开啦，随时看吧～"}
    if re.fullmatch(r"(?:把)?聊天软件(?:先)?(?:给我)?(?:退掉|退出|退了|关掉|关了)(?:吧|呗)?[。！!]*", t):
        return {"action": "close_app", "param": "chatapp", "emotion": "confident", "reply": "聊天软件已关闭。"}
    url = _URL_RE.search(clean)
    if url and re.search(r"打开|开一下|开开|看看|访问|进入|去|^开", _URL_RE.sub("", t)) and \
            not re.search(r"发|聊天软件|搜一下|搜搜|搜索(?!页)|查一下|查查|下载", _URL_RE.sub("", t)):
        return {"action": "open_url", "param": url.group(0).rstrip("）)】]"), "emotion": "confident", "reply": "打开啦。"}
    m = _NET_SEARCH_RE.match(clean)
    if m:
        kw = m.group(1).strip()
        quoted = _QUOTED_SPAN_RE.match(kw)
        kw = quoted.group(0)[1:-1].strip() if quoted else re.split(r"[，,。]", kw)[0].strip()
        if kw:
            return {"action": "web_search", "param": kw, "emotion": "confident", "reply": f"正在搜 {kw} 呢，稍等下～"}
    return None


def _ambiguity_decision(text):
    """几个对象里选一个却没说选哪个（“Chrome或者Edge关一个”“撤回其中一条”“聊天软件里有两个张三”），
    或者只说了一类软件（“把音乐软件关了”）：先问清楚，不替主人挑。"""
    t = mask_quotes(_strip_prefix(text))
    if _CONTRADICT_RE.search(t):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "contradiction",
                "reply": "这两个要求是反着的，只能做一个。你最终要哪个？"}
    if _CHOICE_RE.search(t) or _GENERIC_APP_CLOSE_RE.search(t) or _UNSPECIFIED_RE.search(t):
        return {"action": "chat", "param": "", "emotion": "confident", "_guard": "ambiguous",
                "reply": "你说的有好几个可能，要哪一个？说清楚我再做。"}
    return None


# 说到一半改口（“调到85%，等等我改主意了，设成40%”“打开计算器，哦我口误了，我要打开的是记事本”）：以改口后的为准
_CORRECTION_MARK_RE = re.compile(r"(?:[，,。]|——|—)\s*(?:等等|等一下|不对|哦|噢|啊|呃)?[，,]?\s*(?:我)?(?:改主意了|口误了?|说错了|搞错了|弄错了|报错了?[^，,。]{0,4})[，,。]?\s*"
                                 r"|(?:[，,。]|——|—)\s*(?:等等|不对)[，,]\s*"
                                 r"|(?:[，,。]|——|—)\s*(?:哎|哦|唉)?\s*(?:我)?(?:更正一下|讲反了|说反了|说颠倒了)[，,。]?\s*")


def _correction_decision(clean):
    if _QUOTED_SPAN_RE.search(clean or ""):
        return None                       # 带引号的（发消息）有自己的改口处理
    marks = list(_CORRECTION_MARK_RE.finditer(clean or ""))
    if not marks:
        return None
    before, after = clean[:marks[-1].start()], clean[marks[-1].end():].strip().rstrip("。！!，, ")
    # 改口后面还夹着解释（“草稿还要看，我要关闭的是计算器”）：取最后一个像命令的分句
    commands = [c for c in re.split(r"[，,；;]", after) if _CMD_HINT_RE.search(c)]
    after = commands[-1].strip() if commands else after
    after = re.sub(r"^(?:我)?(?:最终|最后|其实|还是)?(?:要|想)?(打开|关闭|关掉|找|搜|调到|设成)的?是", r"\1", after)
    after = re.sub(r"^(?:我)?要的是(?:让)?", "", after).strip()      # “我讲反了，我要的是让暂停的媒体继续播放”
    after = re.sub(r"^(?:正确的是|应该是|改成|改为|换成|应该)", "", after).strip()
    after = re.sub(r"(?:就好|就行|就可以|吧|呗)[。！!]*$", "", after).strip()
    if not after:
        return None
    d_after = _fast_match_single(after)
    if d_after and d_after.get("action") not in ("chat", "quit_assistant"):
        return d_after
    d_before = _fast_match_single(before)
    value = re.search(r"((?:\d|[一二两三四五六七八九十])\s*成|百分之[零一二两三四五六七八九十百]+|\d+\s*[%％]?)", after)
    if d_before and d_before.get("action") in ("volume", "set_brightness") and value and set_value(value.group(1)) is not None:
        v = int(set_value(value.group(1)))
        if 0 <= v <= 100:
            what = "音量" if d_before["action"] == "volume" else "亮度"
            return dict(d_before, param=str(v), reply=f"{what}调到 {v}% 了。")
    return None


def _foreground_app_decision(text):
    """A complete software-focus request; window movement and quoted text are data."""
    clean = _operation_request_head(text).strip(" 。！!吧呗")
    if negative_request(clean) or mention_reason(clean) or _QUOTED_SPAN_RE.search(clean):
        return None
    target = r"(?P<app>[A-Za-z\u4e00-\u9fff][A-Za-z0-9 ._\u4e00-\u9fff]{0,30}?)"
    destination = r"(?:最前面|前面|最前台|前台)"
    object_first = r"(?:把|将|让)?" + target + r"(?:的)?(?:窗口|界面)?(?:给我)?(?:" + _REQUEST_URGENCY + r"\s*)?(?:弄到|调到|切到|切换到|回到|切回|带到|恢复到|置于|到)" + destination
    short = r"(?:把|将|让)?" + target + r"(?:的)?(?:窗口|界面)?(?:给我)?(?:" + _REQUEST_URGENCY + r"\s*)?(?:置前|置到最前)"
    verb_first = r"(?:切换到|切到|激活|聚焦|唤出)" + target + r"(?:的)?(?:窗口|界面)?"
    match = next((m for pattern in (object_first, short, verb_first) if (m := re.fullmatch(pattern, clean, re.I))), None)
    if not match:
        return None
    name = match["app"].strip()
    if name in ("它", "这个", "那个", "这个窗口", "那个窗口", "窗口"):
        return {"action": "chat", "param": "", "_guard": "focus_target_missing", "_pending": None,
                "emotion": "confident", "reply": "要把哪个软件切到前台？请告诉我软件名称。"}
    if not (_known_application(name) or _find_application(name)):
        return None
    return {"action": "focus_app", "param": name, "emotion": "confident", "reply": f"我把{name}切到前台。"}


def _fast_match_single(text):
    decision = _canonicalize_media_decision(text, _fast_match_single_impl(text), rules=True)
    if decision is None or decision.get("action") in ("volume_up", "volume_down"):
        from canaan_volume_steps import normalize_volume_steps
        decision = normalize_volume_steps(_operation_request_head(mask_quotes(_asr_fix(text))), decision, cn_number, rules=True)
    return decision


def _fast_match_single_impl(text):
    """单个分句的规则匹配（原 fast_match_intent 的主体）。
    纯函数，无 UI 依赖；只有“打开 XX 应用”规则会懒加载应用索引确认应用存在。"""
    text = text.strip()
    clean = _strip_prefix(text)
    literal_site = _named_site_literal(clean)
    if literal_site:
        return literal_site
    literal_app = _app_literal_request(clean)
    if literal_app:
        return literal_app
    query_source = _query_source_decision(clean)
    if query_source:
        return query_source  # Compound units use the same source domains as whole requests and guards.
    diagnosis = _diagnostic_decision(clean)
    if diagnosis:
        return diagnosis
    # 分句里说了绝对路径（先于截图等关键词：“D:\…\截图样例”里的“截图”不是要截屏）、或者“自动在 X 里……”
    early = None if _MORE_STEPS_RE.search(mask_quotes(clean)) else (_auto_ui_decision(clean) or _in_app_ui_decision(clean) or _path_decision(clean))   # “自动操作Excel打开D:\…”先算界面操作
    if early and not negative_request(text):
        return early
    # 分句里也认整句说法（“先看看电脑现在的状态，再……”里的“看看电脑现在的状态”）
    if re.fullmatch(r"(?:先)?(?:看看|看一下|查查)?电脑(?:现在)?的?(?:啥|什么)?状态(?:怎么样)?", clean):
        return {"action": "system_status", "param": "", "emotion": "confident", "reply": "正在检测整机负载。"}

    # 否定句（“不要锁屏”“别打开聊天软件”“音量别再调大了”）：不执行任何动作。
    # 原规则只看关键词，会把否定句当成肯定指令执行。“别吵”是静音的口语说法，单独放行。
    if negative_request(text):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "好，那就先不动。"}
    if _completed_clause(mask_quotes(clean)):
        return None
    state_target = _state_target_request(clean)
    if state_target:
        return {key: value for key, value in state_target.items() if key != "_check_text"}

    relative_brightness = _relative_brightness(clean)
    if relative_brightness:
        return relative_brightness

    # 0. 常用日常打招呼、角色寒暄与时间日期极速通道 (<1ms 直出，零显存消耗)
    match = re.match(r'^(?:查|查询|检索)(?:一下)?(?:我的)?本地知识库[：:，, ]*(.+)$', clean)
    if match:
        return {"action": "knowledge_query", "param": match.group(1), "emotion": "confident", "reply": "我查一下本地资料。"}
    match = re.fullmatch(r'(?:登录|登陆)(聊天软件|IMApp|imapp)', clean)
    if match:
        return {"action": "messaging_login", "param": match.group(1), "emotion": "confident", "reply": "我为你打开客户端，扫码需要你完成。"}
    if re.search(r'^(你好|您好|在吗|你在吗|哈喽|嗨|hello|hi|早安|早上好|晚上好|下午好)$', clean, re.I) or clean in ["好"]:
        return {"action": "chat", "param": "", "emotion": "pamper", "reply": "在呢，亲爱的。想姐姐了？"}

    if re.search(r'^(你是谁|你叫什么|你叫什么名字|介绍一下你自己|介绍下自己|你的名字|是谁)$', clean):
        return {"action": "chat", "param": "", "emotion": "confident", "reply": "我是团团，你的整机系统智能管家。电脑内外的一切，姐姐随你调遣～"}

    if re.search(r'^(你在干嘛|你在干什么|在干嘛|在干啥|干啥呢|在忙什么)$', clean):
        return {"action": "chat", "param": "", "emotion": "pamper", "reply": "在守着你的电脑呀，随时等你发号施令呢～"}

    if re.search(r'^(我想你了|我好想你|想死你了|爱你|喜欢你)$', clean):
        return {"action": "chat", "param": "", "emotion": "pamper", "reply": "嘴真甜，我也想你了，亲爱的～"}

    # 让团团自己退出（不是关别的程序）：托盘不会自动重启
    if re.search(r'^(?:你)?(?:退出|下线|关掉你自己|关闭你自己|把你自己关了)(?:吧|一下)?$|^(?:退出|关闭|关掉)(?:团团|助手)(?:吧)?$', clean):
        return {"action": "quit_assistant", "param": "", "emotion": "pamper", "reply": "好，我先下线了，想我了就从托盘里叫我。"}

    if re.search(r'^(再见|拜拜|退下吧|退下|跪安吧|没事了|先这样)$', clean):
        return {"action": "chat", "param": "", "emotion": "tease", "reply": "那姐姐先退下啦，有事随时喊我～"}

    clock_reply = _clock_reply(clean)
    if clock_reply:
        return {"action": "chat", "param": "", "emotion": "confident", "reply": clock_reply}

    refusal = _refusal_decision(clean)
    if refusal:
        return refusal

    # 明确的搜索请求中，后面的“截图 / 下一首 / 设置大屏”等是搜索词。
    # “搜一下电脑里 / 本机 / D 盘里的……”：找本机文件，不上网
    local_file = _local_file_decision(clean)
    if local_file:
        return local_file
    local_system = _local_system_decision(clean)
    if local_system:
        return local_system
    m_local = re.match(r'^(?:搜一下|搜索|搜搜|找一下|查一下|找找)\s*(?:我的)?(?:电脑|本机|[cCdDeE]\s*盘)(?:里面|里|上|中)的?\s*(.+)$', clean)
    if m_local and m_local.group(1).strip("。！!？? "):
        target = m_local.group(1).strip("。！!？? ")
        return {"action": "search_file", "param": target, "emotion": "confident", "reply": f"我找找看 {target} 在哪..."}
    if re.match(r'^(?:在百度|用百度|百度|搜一下|搜索|查一下|搜搜|查查|查下)', clean):
        explicit = bool(re.match(r'^(?:在百度|用百度|百度|搜一下|搜索|搜搜)', clean))
        search = _web_search_decision(text, explicit=explicit)
        if search:
            return search

    # “上周那句‘……’是旧记录，我现在要给李四发‘……’”：前半句是在说旧记录，先按“现在”那句解析
    if len(_QUOTED_SPAN_RE.findall(clean)) >= 2 and _OLD_RECORD_RE.search(mask_quotes(clean)):
        current = _quoted_chatapp_decision(clean)
        if current:
            return current

    where = _where_decision(clean)
    if where:
        return where

    # 只是提到动作（问功能、说过去/正在做的事、假设、转述）：不走关键词规则，交给大模型理解；
    # 大模型若仍判成动作，会被 apply_command_guard 改成先问一句
    if mention_reason(text):
        return None

    # 收件人和引号正文已明确时先解析外层请求，避免正文触发大屏、监听等功能。
    quoted = _quoted_chatapp_decision(clean)
    if quoted:
        return quoted

    site = _named_site_request(clean)
    if site:
        return site

    foreground = _foreground_app_decision(clean)
    if foreground:
        return foreground


    # 1. 聊天软件发消息自动化 (秒级匹配、严谨消歧与复合型多动词指令解析)
    generic_terms = _GENERIC_CHATAPP_CONTACTS
    _clean_c = _clean_chatapp_contact

    def _convert_indirect_to_direct(t, contact=None):
        if not t:
            return t
        t = t.strip()
        quoted = re.match(r'^[“"「](.+?)[”"」]', t)
        if quoted:  # 引号里的才是要发的正文，引号后面的话（“不用问”等）不发
            return quoted.group(1).strip()
        # 剥除常见的消息外壳前缀与后缀
        t = re.sub(r'^(?:聊天软件发一条消息|聊天软件发消息|聊天软件发聊天软件|发一条聊天软件|发一条消息|发个聊天软件|发条聊天软件|发条消息|发消息|发信息|发聊天软件|发送|发个|发一条|传达|聊天软件说|发聊天软件说|发消息说|说声|说一句|告诉声|转告|消息|说)\s*(?:说|：|:|，|,|内容是|内容为|\s)*', '', t).strip()
        t = re.sub(r'\s*(?:的消息|的信息|的内容)$', '', t).strip()

        # 1. 核心情感与决绝表达（最高优先级：我不爱他了 -> 我不爱你了）
        t = re.sub(r'我不(?:再)?(?:爱|喜欢)(?:他|她|TA|他们)了', '我不爱你了' if '爱' in t else '我不喜欢你了', t)
        t = re.sub(r'我(?:好|很|特别)?想(?:他|她|TA)了', '我想你了', t)
        t = re.sub(r'我(?:好|很)?爱(?:他|她|TA)', '我爱你', t)
        t = re.sub(r'我(?:好|很)?喜欢(?:他|她|TA)', '我喜欢你', t)
        t = re.sub(r'我(?:好|很|特别)?讨厌(?:他|她|TA)', '我讨厌你', t)
        t = re.sub(r'我想和?(?:他|她|TA)分手', '我想跟你分手', t)

        # 2. 祈使与动词引导：让他/叫他 -> 你...
        t = re.sub(r'^(?:让|叫|催|喊|通知|告诉)(?:他|她|TA|他们)\s*', '你', t)

        # 3. 介词短语代词转换：去找他 -> 去找你，给他 -> 给你
        t = re.sub(r'([去到见找陪问给向跟同与替帮])\s*(?:他|她|TA)', r'\g<1>你', t)

        # 4. 所属格与处所：他的 -> 你的，他家 -> 你家
        t = re.sub(r'(?:他|她|TA)的', '你的', t)
        t = re.sub(r'(?:他|她|TA)家', '你家', t)

        # 5. 一般谓词短语代词：我不怪他了 -> 我不怪你了
        t = re.sub(r'我([^\s，,。]+?)(?:他|她|TA)了', r'我\g<1>你了', t)
        t = re.sub(r'我([^\s，,。]+?)(?:他|她|TA)', r'我\g<1>你', t)

        # 6. 对长辈/领导的敬语
        if contact and any(k in contact for k in ["总", "老师", "经理", "主任", "导师", "领导"]):
            t = re.sub(r'(?<![我])你', '您', t)

        return t.strip()

    # B. 复合型多动词指令前置解构（例如 "帮我把聊天软件打开，然后找到联系人里面的王小明，给他发消息说晚上好"）
    m_open = re.search(r'^(?:先|首先)?\s*(?:(?:把)?\s*(?:聊天软件|chatapp|chatapp)\s*(?:打开|启动|开|翻开|拾掇开)|(?:打开|启动|开|翻开|拾掇开)\s*(?:聊天软件|chatapp|chatapp))[，,、\s]*(?:然后|再|最后)?\s*(.+)', clean, re.I)
    working_clean = m_open.group(1).strip() if m_open else clean
    working_clean = re.sub(r'^(?:用|通过|在)?聊天软件(?:上|里)?\s*(?=告诉|通知|提醒)', '', working_clean)

    m_find = re.search(r'^(?:找到|搜一下|搜索|找一下|搜)?\s*(?:在|从)?\s*(?:联系人|好友|通讯录)?(?:里面|里)?\s*(?:找到|搜一下|搜索|找一下|搜)\s*([^\s，,、]+?)[，,、\s]+(?:然后|再|最后)?\s*(?:给他|给TA|给其)?\s*(.+)', working_clean)
    if not m_find:
        # 没有标点分隔的口语：“找到王小明给他发消息说……”，以“给他/跟他”作为联系人结束的标志
        m_find = re.search(r'^(?:在|从)?\s*(?:联系人|好友|通讯录)?(?:里面|里)?\s*(?:找到|搜一下|搜索|找一下|搜)\s*([^\s，,、]{2,8}?)\s*(?:然后|再)?\s*(?:给他|给她|给TA|跟他|跟她|和他|和她)\s*(.+)', working_clean)
    if m_find:
        target_name = _clean_c(m_find.group(1))
        rest_action = m_find.group(2).strip()
        # “跟他说明天见” → “说明天见”，避免把“跟他”拼进联系人名
        rest_action = re.sub(r'^(?:跟|和|对|给)(?:他|她|TA)\s*(?=说|发)', '', rest_action).strip()
        if not rest_action.startswith("给") and not rest_action.startswith("发给"):
            working_clean = f"给{target_name}{rest_action}"
        else:
            working_clean = rest_action

    # B2. 正文在引号里（最常见、最不会错的说法）：引号里一字不改就是正文，联系人只从引号外面找
    quoted = _quoted_chatapp_decision(working_clean)
    if quoted:
        return quoted

    # C. 纯联系人未带具体消息内容（例：给王小明发聊天软件, 聊天软件发给老妈, 发聊天软件给老妈, 给张三发消息）
    p_only_contact = r'^(?:在聊天软件上|在聊天软件里|从聊天软件|聊天软件|通过聊天软件)?\s*(?:给|发给|向|跟)\s*([^\s说：:,，]+?)\s*(?:在聊天软件上|在聊天软件里|从聊天软件|聊天软件|通过聊天软件)?\s*(?:发聊天软件|发条聊天软件|发个聊天软件|聊天软件发消息|发消息|发信息|发一条消息|发一条聊天软件|发一下|发个|发条|发)[。.!！]?$'
    m_only = re.search(p_only_contact, working_clean, re.I)
    if not m_only:
        p_only_contact2 = r'^(?:在聊天软件上|在聊天软件里|聊天软件|通过聊天软件)?\s*(?:发聊天软件|发条聊天软件|发个聊天软件|聊天软件发消息|发消息|发信息)\s*(?:给|发给|向|跟)\s*([^\s说：:,，]+?)$'
        m_only = re.search(p_only_contact2, working_clean, re.I)
    if m_only:
        c_raw = _clean_c(m_only.group(1))
        if not c_raw or c_raw in generic_terms:
            return {
                "action": "chat",
                "param": "",
                "emotion": "pamper",
                "reply": "想发给谁呀？"
            }
        return {
            "action": "chat",
            "param": "",
            "emotion": "pamper",
            "reply": f"想跟{c_raw}说啥呀？",
            "_draft": {"contact": c_raw},
        }

    # D. 模式匹配精准解析
    target_contact, target_msg = None, None

    # 模式1：带明确分隔词（说、：、:、内容是、内容为、逗号、空格）
    # 例：给王小明发聊天软件说我不爱他了、给王小明发送消息说晚上好
    # 必须有“发 / 说”这类动词，或者“说 / 冒号 / 内容是”引出正文；“给我关机，越快越好”没有发送动词，不是发消息。
    # 中文引号也算分隔，正文取引号里的内容（“给文件传输助手发‘我到了’，不用问”只发“我到了”）。
    p1 = (r'^(?:在聊天软件上|在聊天软件里|从聊天软件|聊天软件|通过聊天软件)?\s*(?:给|发给|跟|向)\s*([^\s：:，,说“”"「」]+?)\s*(?:在聊天软件上|在聊天软件里|从聊天软件|聊天软件|通过聊天软件)?\s*'
          r'(?:(?:回个聊天软件|回条聊天软件|回聊天软件|回个消息|回消息|回一句|回个话|发送一条消息|发送消息|发送聊天软件|发送|发聊天软件|发一条聊天软件|发条聊天软件|发个聊天软件|发个消息|发个信息|发个|聊天软件发消息|聊天软件发一条|发消息|发信息|发条消息|发一条消息|发|说声|说一句)'
          r'\s*(?:说(?!明(?![天晚早儿年日])|法|话|服)|：|:|，|,|内容是|内容为|\s|(?=[“"「]))|(?:说(?!明(?![天晚早儿年日])|法|话|服)|：|:|内容是|内容为))\s*(.+)$')
    # “说明 / 说法”里的“说”不是正文分隔：“给三个有说明的例子”原来会拆成发给“三个有”、正文“明的例子”
    m1 = re.search(p1, working_clean, re.I)
    if m1 and m1.group(1).startswith("我"):
        m1 = None  # “给我……”是让团团给主人做事，不是发给一个叫“我…”的人
    if m1:
        target_contact = _clean_c(m1.group(1))
        target_msg = _convert_indirect_to_direct(m1.group(2), target_contact)
        # “给朋友/给好友发个聊天软件说……”：联系人是泛称，清洗后为空，直接反问发给谁，绝不盲发
        if not target_contact and m1.group(1).strip() in ("朋友", "好友", "我朋友", "联系人", "聊天软件好友", "聊天软件联系人"):
            return {"action": "chat", "param": "", "emotion": "pamper", "reply": "发给谁呀？跟我说声名字～",
                    "_guard": "no_contact", "_draft": {"message": target_msg}}

    # 模式2：动词与消息直接相连（例：给王小明发送我不爱他了的消息、给前男友发我不爱他了、给王小明发消息我明天去）
    if not target_contact:
        p2 = r'^(?:在聊天软件上|在聊天软件里|从聊天软件|聊天软件|通过聊天软件)?\s*(?:给|发给|跟|向)\s*([^\s：:，,说发]{2,6}?)\s*(?:在聊天软件上|在聊天软件里|从聊天软件|聊天软件|通过聊天软件)?\s*(?:发送一条消息|发送消息|发送聊天软件|发送|发聊天软件|发一条聊天软件|发条聊天软件|发个聊天软件|聊天软件发消息|聊天软件发一条|发消息|发信息|发条消息|发一条消息)\s*(.+)$'
        m2 = re.search(p2, working_clean, re.I)
        if m2:
            target_contact = _clean_c(m2.group(1))
            target_msg = _convert_indirect_to_direct(m2.group(2), target_contact)

    # 模式2b：光一个“发”字直接跟正文（例：给文件传输助手发测试、给老板发个我到了）。
    # 正文只认短文字；“红包 / 文件 / 图片 / 位置 / 转账……”是东西不是话，不当正文。发聊天软件照样每条都先确认。
    if not target_contact:
        p2b = (r'^(?:在聊天软件上|在聊天软件里|从聊天软件|聊天软件|通过聊天软件)?\s*(?:给|发给|向)\s*([^\s：:，,说发“”"「」]{1,10}?)\s*(?:在聊天软件上|在聊天软件里|从聊天软件|聊天软件|通过聊天软件)?\s*'
               r'发(?:一?[个条句]|一下)?\s*(?!聊天软件|消息|信息|短信|红包|文件|图片|照片|截图|视频|语音|位置|定位|链接|邮件|快递|钱|转账|名片|表情)'
               r'([^，,。！!？?\s“”"「」]{1,20})[。.!！]?$')
        m2b = re.search(p2b, working_clean, re.I)
        if m2b and not m2b.group(1).startswith("我"):
            target_contact = _clean_c(m2b.group(1))
            target_msg = _convert_indirect_to_direct(m2b.group(2), target_contact)

    # 模式3：动词引导（例：告诉王小明我不爱他了、通知老妈我想她了）
    if not target_contact:
        p3 = r'^(?:告诉|通知|提醒)\s*([^\s：:，,说]+?)\s*(?:说|：|:|，|,|\s)\s*(.+)$'
        m3 = re.search(p3, working_clean, re.I)
        if not m3:
            p3_tight = r'^(?:告诉|通知|提醒)\s*([^\s：:，,说]{2,6}?)(?=(?:我不|我想|我好|我|明天|今天|后天|周[一二三四五六日天]|开会|下班|马上|下午|上午|晚上|准时|收到|测试|请假|会议|报告|你自己|别|快))\s*(.+)$'
            m3 = re.search(p3_tight, working_clean, re.I)
        if m3 and m3.group(1).startswith("我"):
            m3 = None   # “提醒我 / 告诉我 / 通知我……”是对我说的，不是发给一个叫“我…”的人
        if m3:
            target_contact = _clean_c(m3.group(1))
            target_msg = _convert_indirect_to_direct(m3.group(2), target_contact)

    # 模式4：发消息给某人说 / 发聊天软件通知某人说 / 聊天软件给某人发消息
    if not target_contact:
        p4 = r'^(?:在聊天软件上|在聊天软件里|聊天软件|通过聊天软件)?\s*(?:发聊天软件|发一条聊天软件|发条聊天软件|发个聊天软件|发消息|发信息|发一条消息|发送消息|发送)\s*(?:给|发给|跟|向)\s*([^\s：:，,说]+?)\s*(?:说|：|:|，|,|内容是|内容为|\s)\s*(.+)$'
        m4 = re.search(p4, working_clean, re.I)
        if not m4:
            p4_tight = r'^(?:在聊天软件上|在聊天软件里|聊天软件|通过聊天软件)?\s*(?:发聊天软件|发一条聊天软件|发条聊天软件|发个聊天软件|发消息|发信息|发一条消息|发送消息|发送)\s*(?:给|发给|跟|向)\s*([^\s：:，,说]{2,6}?)(?=(?:我不|我想|我好|我|明天|今天|后天|周[一二三四五六日天]|开会|下班|马上|下午|上午|晚上|准时|收到|测试|请假|会议|报告|你自己|别|快))\s*(.+)$'
            m4 = re.search(p4_tight, working_clean, re.I)
        if m4:
            target_contact = _clean_c(m4.group(1))
            target_msg = _convert_indirect_to_direct(m4.group(2), target_contact)

    # 模式5：未指定联系人直接说发聊天软件 / 发送某消息 / 跟他说某消息 -> 主动转译为直接引语并反问联系人
    if not target_contact:
        p5 = r'^(?:在聊天软件上|在聊天软件里|聊天软件|通过聊天软件)?\s*(?:跟(?:他|她|TA|他们|某人|谁|某位好友)说|向(?:他|她|TA|他们|某人|谁|某位好友)说|对(?:他|她|TA|他们|某人|谁|某位好友)说|聊天软件发一条消息|聊天软件发消息|聊天软件发聊天软件|发一条聊天软件|发一条消息|发个聊天软件|发条聊天软件|发条消息|发消息|发信息|发聊天软件|发送|发个|发一条|传达|说|发)\s*(?:说|：|:|，|,|内容是|内容为|\s)?\s*(.+?)\s*(?:的消息|的信息|的内容)?$'
        m5 = re.search(p5, working_clean, re.I)
        if m5:
            raw_unspecified_msg = m5.group(1).strip()
            if raw_unspecified_msg and raw_unspecified_msg not in ["聊天软件", "打开聊天软件", "聊天", "一个聊天软件", "条聊天软件"]:
                direct_msg = _convert_indirect_to_direct(raw_unspecified_msg)
                return {
                    "action": "chat",
                    "param": "",
                    "emotion": "pamper",
                    "reply": "发给谁呀？跟我说声名字～",
                    "_draft": {"message": direct_msg},
                }

    # “聊天软件告诉表姐生日快乐”：联系人吞掉了正文开头（“表姐生日”+“快乐”）。称呼后面多出来的字还给正文
    if target_msg:
        # “给张三发聊天软件说 你好”分句时会把“说”后的停顿变成逗号，正文不能带着这个分隔符走
        target_msg = target_msg.lstrip("，,：:；;、 ")
    if target_contact and target_msg:
        kin = re.match(r"(表姐|表哥|表妹|表弟|堂姐|堂哥|堂妹|堂弟|老妈|老爸|我妈|我爸|我姐|我哥|姐姐|哥哥|妹妹|弟弟|老婆|老公|"
                       r"室友|同事|老师|爷爷|奶奶|外公|外婆|舅舅|姑姑|阿姨|叔叔)(.{2,})$", target_contact)
        if kin and not _contact_in_aliases(target_contact):
            target_contact, target_msg = kin.group(1), kin.group(2) + target_msg
    if target_contact and target_msg:
        if not target_msg:
            if not target_contact or target_contact in generic_terms:
                return {
                    "action": "chat",
                    "param": "",
                    "emotion": "pamper",
                    "reply": "想发给谁呀？"
                }
            return {
                "action": "chat",
                "param": "",
                "emotion": "pamper",
                "reply": f"想跟{target_contact}说啥呀？"
            }

        if target_contact in generic_terms:
            return {
                "action": "chat",
                "param": "",
                "emotion": "pamper",
                "reply": "发给谁呀？",
                "_draft": {"message": target_msg},
            }
        return {
            "action": "send_chatapp",
            "param": json.dumps({"contact": target_contact, "message": target_msg}, ensure_ascii=False),
            "emotion": "pamper",
            "reply": f"好，发给{target_contact}了。"
        }

    # 先完成明确发送请求解析；无引号正文中的功能名称同样是资料。
    # 常用系统工具极速启动 (<1ms)
    if re.search(r'^(?:打开|启动|开)?\s*(?:任务管理器|任务管理)(?:看看|一下)?$', clean):
        return {"action": "run_cmd", "param": "taskmgr", "emotion": "confident", "reply": "打开了。"}
    if re.search(r'^(?:打开|启动)?\s*(?:设置|系统设置)$|^(?:把)?\s*(?:系统)?设置\s*(?:给我)?(?:打开|开开)(?:一下|看看)?$', clean):
        return {"action": "run_cmd", "param": "start ms-settings:", "emotion": "pamper", "reply": "打开了。"}
    if re.search(r'^(?:打开|启动)?\s*(?:控制面板)$', clean):
        return {"action": "run_cmd", "param": "control", "emotion": "confident", "reply": "打开了。"}
    if re.search(r'^(?:打开|启动)?\s*(?:终端|命令行|cmd|powershell)$', clean, re.I):
        return {"action": "run_cmd", "param": "start wt", "emotion": "confident", "reply": "打开了。"}

    # 商业可视化控制台大屏直通通道 (Settings Cockpit)
    if re.search(r'(?:设置大屏|控制大屏|配置中心|商业设置|团团设置|设置中心|控制台大屏|商业控制台)', clean, re.I):
        return {"action": "run_cmd", "param": _cockpit_command(), "emotion": "pamper", "reply": "好的亲爱的，这就为你展开全域商业控制大屏~"}

    # 每日高管消息情报晨报直通通道 (Daily Briefing & Todos)
    # 引号里是要发的正文，不拿来判断（“给小何发‘清单我来整理’”不是要总结消息）；“给 X 发……”本身就是发消息
    unquoted = re.sub(r"[“\"「『][^”\"」』]*[”\"」』]", "", clean)
    if not _app_launch_without_briefing_request(clean) and not re.search(r"^(?:在聊天软件上|聊天软件|通过聊天软件)?\s*(?:给|发给|向)\s*\S{1,12}?\s*发", clean) and re.search(r'(?:(?:总结|汇总|提炼|整理|看下|看看|汇报).*(?:聊天软件|imapp|群聊|聊天记录|未读消息|消息))|(?:(?:聊天软件|imapp|聊天软件和imapp|聊天记录|消息).*(?:总结|汇总|提炼|整理|早报|待办|动态|有什么事|有啥事|汇报|有什么消息|有啥消息|未读))|(?:(?:跑一遍|生成|来一份).*(?:早报|总结|聊天软件|imapp))|(?:群里有啥事|今天群里有啥事|群里有什么事|今日早报|消息早报|今天有啥待办|今日待办|看下今天消息|提炼消息|消息汇总|早报)', unquoted, re.I):
        from message_tools.canaan_daily_briefing import message_sources
        return {"action": "daily_briefing", "param": {"value": "generate", "sources": message_sources(clean)}, "emotion": "pamper", "reply": "我会总结已经采集的消息片段，再告诉你明确的待办。"}

    # 全自主桌面电脑控制直通通道 (Computer Use & Smooth Automation)
    m_comp = re.search(r'^(?:帮我)?\s*(?:操作(?:一下)?电脑|控制(?:一下)?电脑|操作(?:一下)?桌面|控制(?:一下)?桌面|自动操作桌面|自动操作电脑|自动操作|桌面控制|电脑控制|电脑操作|用\s*computer\s*use|自动化操作)(?:\s*(?:去|帮我))?\s*(.*)$', clean, re.I)
    if m_comp:
        cmd_p = m_comp.group(1).strip()
        p = cmd_p if cmd_p else "观察屏幕状态并向用户汇报"
        r = f"收到，这就通过电脑控制为你执行：{p[:20]}~" if cmd_p else "收到，团团电脑控制已就绪，请告诉我你想让我执行什么操作~"
        return {"action": "computer_use", "param": p, "emotion": "confident", "reply": r}

    # 屏幕视觉副驾直通通道 (Screen Co-pilot)
    # 只说“看下报错 / 看看这个报错”（没提屏幕）的，下面的“诊断报错”说法表里专门有，交给它
    if re.search(r'(?:帮我看看屏幕|看下屏幕|看看屏幕|帮我看看报错|看下报错|看看这个报错|看下这个报错|屏幕上写了什么|屏幕上有啥|帮我看看当前屏幕|屏幕诊断)', clean, re.I) \
            and not re.fullmatch(r'(?:帮我)?(?:看下|看看|瞅一眼|看一眼)(?:这个)?报错', clean):
        m_diag = re.search(r'(?:报错|错误|bug|exception)', clean, re.I)
        p = "请仔细看屏幕上的报错信息或代码，指出具体错误原因并给出简明的修复步骤。" if m_diag else "请观察当前屏幕内容，说明核心界面与关键信息。"
        return {"action": "screen_copilot", "param": p, "emotion": "confident", "reply": "收到，我正在观察屏幕，稍等我两秒为你诊断~"}

    # 外部智能体快速通道 (OpenMuse / OpenDots)
    if re.search(r'^(?:打开|启动|进入)?\s*(?:openmuse|muse|缪斯|数字管家)(?:\s*(?:工作台|控制台|界面|网页))?$', clean, re.I):
        return {"action": "openmuse_task", "param": "open_web", "emotion": "pamper", "reply": "好的亲爱的，这就为你打开 OpenMuse 智能体工作台~"}
    m_muse_task = re.search(r'^(?:让|叫|让智能体|让muse|让openmuse)?\s*(?:openmuse|muse|缪斯)\s*(?:帮我|去)?\s*(.+)$', clean, re.I)
    if m_muse_task and not re.search(r'^(?:音乐|歌)', clean):
        return {"action": "openmuse_task", "param": m_muse_task.group(1).strip(), "emotion": "confident", "reply": "任务已经交给 OpenMuse，正在后台为你处理，有进展我会随时向你汇报~"}

    if re.search(r'^(?:打开|启动|进入)?\s*(?:opendots|dots|多兹|知识库|知识空间|虚拟同事)(?:\s*(?:工作台|空间|知识空间|知识库|界面|网页|文档))?$', clean, re.I):
        return {"action": "opendots_task", "param": "open_web", "emotion": "pamper", "reply": "好的亲爱的，这就为你打开 OpenDots 知识库空间~"}
    m_dots_task = re.search(r'^(?:让|叫|让dots|让opendots)?\s*(?:opendots|dots|多兹)\s*(?:帮我|去)?\s*(.+)$', clean, re.I)
    if m_dots_task:
        return {"action": "opendots_task", "param": m_dots_task.group(1).strip(), "emotion": "confident", "reply": "已通知 OpenDots 专家团队，正在知识空间为你撰写与归档~"}

    # 聊天软件 消息工具套件快速通道
    if re.search(r'^(?:打开|查看|进入)?\s*(?:imapp\s*看板|imapp\s*消息看板|imapp\s*消息后台|imapp\s*网页|imapp看板|imapp消息看板)$', clean, re.I):
        return {"action": "imapp_message_task", "param": "看板", "emotion": "pamper", "reply": "好的亲爱的，这就为你打开 聊天软件 消息看板~"}
    if re.search(r'^(?:导出|备份|提取|同步)?\s*(?:imapp消息|imapp记录|imapp聊天记录|imapp)$', clean, re.I) and not re.search(r'^(?:打开|启动)', clean):
        return {"action": "imapp_message_task", "param": "导出", "emotion": "confident", "reply": "正在为你提取并导出今天的 聊天软件 聊天记录~"}
    if re.search(r'(?:推送imapp|同步imapp到飞书|整理imapp消息|imapp日报)', clean, re.I):
        return {"action": "imapp_message_task", "param": "推送", "emotion": "confident", "reply": "已开始整理 聊天软件 消息并同步到飞书知识库~"}

    # 聊天软件消息抓取与解密导出快速通道
    if re.search(r'(?:打开聊天软件存档|查看聊天软件记录|查看聊天软件存档|聊天软件归档|打开聊天软件记录)', clean, re.I):
        return {"action": "chatapp_message_task", "param": "存档", "emotion": "pamper", "reply": "好的亲爱的，已为你打开聊天软件消息存档目录~"}
    if re.search(r'(?:抓取聊天软件|实时监听聊天软件|聊天软件抓取|监听聊天软件消息)', clean, re.I):
        return {"action": "chatapp_message_task", "param": "实时", "emotion": "confident", "reply": "已启动聊天软件实时消息监听抓取程序~"}
    if re.search(r'(?:导出聊天软件记录|导出聊天软件消息|备份聊天软件聊天|备份聊天软件记录|解密聊天软件数据库|聊天软件导出)', clean, re.I):
        return {"action": "chatapp_message_task", "param": "导出", "emotion": "confident", "reply": "正在为你创建聊天软件数据库快照并解密导出聊天记录~"}

    # 2. 单纯打开聊天软件 / 聊天软件启动（仅在没有任何发消息动作时触发）
    if re.search(r'^(?:把)?\s*(?:打开|启动|登|登入|登录|开|拾掇开)\s*(?:聊天软件|chatapp|chatapp)$', clean, re.I) or re.search(r'^(?:把)?\s*(?:聊天软件|chatapp|chatapp)\s*(?:打开|启动|开|拾掇开)(?:了)?$', clean, re.I):
        return {
            "action": "open_chatapp",
            "param": "",
            "emotion": "confident",
            "reply": "聊天软件打开啦，随时看吧～"
        }

    # 2.5 聊天软件消息快捷撤回
    if re.search(r'^(把刚才的消息撤回|撤回刚才的消息|撤回刚才那条|撤回刚发的消息|撤回刚发的|撤回聊天软件消息|撤回消息|撤回)$', clean):
        return {
            "action": "recall_chatapp",
            "param": "",
            "emotion": "pamper",
            "reply": "刚才那条消息已经给你撤回来啦～"
        }

    device = _device_rule(text, clean)
    if device:
        return device

    # 3. 音量 / 亮度设到具体数值（“声音调到百分之四十”“亮度设成60”）：先于“调大调小”匹配，音量和亮度不混
    m_set = _VOLUME_SET_RE.search(text)
    if m_set and set_value(m_set.group(1)) is not None and 0 <= set_value(m_set.group(1)) <= 100:
        v = int(set_value(m_set.group(1)))
        return {"action": "volume", "param": str(v), "emotion": "confident", "reply": f"音量调到 {v}% 了。"}
    m_set = _BRIGHT_SET_RE.search(text)
    if m_set and set_value(m_set.group(1)) is not None and 0 <= set_value(m_set.group(1)) <= 100:
        v = int(set_value(m_set.group(1)))
        return {"action": "set_brightness", "param": str(v), "emotion": "confident", "reply": f"亮度调到 {v}% 了。"}

    # 音量 / 亮度的口语方向（“电脑音量往下调一点”“声音先往上加一点”“电脑屏幕往暗里调一下”）
    if not re.search(_SET_VALUE, text):
        if re.search(r"(?:音量|声音)[^，,。亮]{0,4}(?:往上加|往上调|往上|调高些|调高|加大|大一点|高一点|高些)", text):
            return {"action": "volume_up", "param": "5", "emotion": "confident", "reply": "音量调大了。"}
        if re.search(r"(?:音量|声音)[^，,。亮]{0,4}(?:往下调|往下|调低|调小|小一点|低一点|低些|关小)", text):
            return {"action": "volume_down", "param": "5", "emotion": "confident", "reply": "音量调小了。"}
        if re.search(r"(?:屏幕|亮度)[^，,。音声]{0,4}(?:往暗里调|往暗调|调暗些)", text):
            return {"action": "brightness_down", "param": "", "emotion": "confident", "reply": "屏幕调暗了。"}
        if re.search(r"(?:屏幕|亮度)[^，,。音声]{0,4}(?:往亮里调|往亮调|调亮些)", text):
            return {"action": "brightness_up", "param": "", "emotion": "confident", "reply": "屏幕调亮了。"}
    # “调到最小 / 最低”按调小处理，不直接设成 0（0 就等于静音 / 全黑，10-25 先按设 0 做过，留出集多了一次错误执行，撤回）
    if re.search(r"(?:音量|声音)[^，,。亮]{0,6}?(?:调到|调成|开到|设到|设成)(?:最小|最低)", text):
        return {"action": "volume_down", "param": "5", "emotion": "confident", "reply": "音量调小了。"}
    if re.search(r"(?:屏幕亮度|亮度|屏幕)[^，,。音声]{0,6}?(?:调到|调成|设到|设成)(?:最小|最低|最暗)", text):
        return {"action": "brightness_down", "param": "", "emotion": "confident", "reply": "屏幕调暗了。"}

    # 音量控制
    if re.search(r'(整大点声|大点声|声音大点|加大音量|调大音量|音量调大|提高音量|音量放大|昂点声|大声点|大点声音)', text):
        return {"action": "volume_up", "param": "5", "emotion": "confident", "reply": "音量调大了。"}
    if re.search(r'(整小点声|小点声|声音小点|减小音量|调小音量|音量调小|降低音量|降点声|小声点|小点声音)', text):
        return {"action": "volume_down", "param": "5", "emotion": "confident", "reply": "音量调小了。"}
    # 说症状（“这声儿也太小了吧”“耳机里声音炸耳朵”）：1.5B 常把方向弄反。说的是过去的事、别人的事不算
    if not re.search(r"昨天|前天|上次|以前|之前|那天|吵到|别人|邻居|他|她", text):
        if re.search(r"(?:声音|声儿|音量)[^，,。]{0,4}(?:太小|好小|有点小|太轻|听不见|听不清|听不到)", text):
            return {"action": "volume_up", "param": "5", "emotion": "confident", "reply": "音量调大了。"}
        if re.search(r"(?:声音|声儿|音量)[^，,。]{0,6}(?:太大|太响|好响|炸耳朵|震耳朵|刺耳)", text):
            return {"action": "volume_down", "param": "5", "emotion": "confident", "reply": "音量调小了。"}
    if re.search(r'^(静音|闭嘴|关声音|别吵)$', text) or re.search(r'^(?:帮我|给我)?(?:静音|静个音)(?:一下)?[吧呗]?$', clean):
        return {"action": "volume_mute", "param": "", "emotion": "confident", "reply": "已静音。"}
    if re.fullmatch(r"(?:把)?(?:电脑的?)?(?:声音|音量)(?:给我)?静掉[吧呗。！!]*", clean):
        return {"action": "volume_mute", "param": "on", "emotion": "confident", "reply": "已静音。"}

    # 4. 截屏
    explicit_file = re.fullmatch(r"(?:打开文件|打开文档)\s*(.+)", clean)
    if explicit_file:
        target = explicit_file.group(1).strip(" 。！! ")
        return {"action": "search_file", "param": target, "emotion": "confident", "reply": f"我找找看 {target} 在哪..."}
    colloquial_capture = _screenshot_alias_decision(clean)
    if colloquial_capture:
        return colloquial_capture
    if re.search(r"(?:打开|开|启动)[^，,。]{0,4}(?:截图|截屏)工具|(?:截图|截屏)工具[^，,。]{0,4}(?:打开|开开|启动)", text):
        return {"action": "open_app", "param": "截图工具", "emotion": "confident", "reply": "截图工具打开了。"}   # 开的是程序，不是截屏
    if _screenshot_request_contexts(text):
        return {"action": "screenshot", "param": "", "emotion": "confident", "reply": "截好了，保存在桌面。"}

    # 5. 返回桌面 / 锁屏
    if re.search(r'(回到桌面|返回桌面|显示桌面|最小化所有窗口|^(?:先|最后|再)?回桌面)', text):
        return {"action": "show_desktop", "param": "", "emotion": "confident", "reply": "已回到桌面。"}
    if re.search(r'((?:锁屏|锁个屏|锁下屏)(?!密码|壁纸|界面|时间|快捷键|功能)|锁定电脑|锁上电脑|把电脑锁了|把电脑锁上)', text):
        return {"action": "lock_screen", "param": "", "emotion": "confident", "reply": "电脑已锁定。"}

    # 6. 查电脑状态
    if re.search(r'(电脑状态|系统状态|查下状态|CPU占用|电量|当前负载)', text):
        return {"action": "system_status", "param": "", "emotion": "confident", "reply": "正在检测整机负载。"}

    # 7. 多媒体控制
    if re.search(r'(下一首|下一曲|切歌)', text):
        return {"action": "media_control", "param": "next", "emotion": "confident", "reply": "切到下一首了。"}
    if re.search(r'(上一首|上一曲)', text):
        return {"action": "media_control", "param": "prev", "emotion": "confident", "reply": "切回上一首了。"}
    if re.search(r'(暂停播放|继续播放|接着放|继续放|接着播|恢复播放|暂停|播放音乐|(?:放|来|听)(?:点|首|一首|几首)?(?:歌|音乐|曲子)'
                 r'|^(?:把)?(?:音乐|歌|视频)(?:先)?停(?:一下|一会儿?)?[吧呗]?$)', text):
        return {"action": "media_control", "param": "play_pause", "emotion": "confident", "reply": "播放状态已切换。"}

    # 8. 屏幕亮度调节
    # 没说数就在当前基础上加减（原来固定调到 90 / 40：本来就 95 的屏幕说“调亮”反而变暗）
    if re.search(r'(调高亮度|调大亮度|调亮屏幕|调亮|亮一点|亮点儿|更亮|屏幕太暗|屏幕亮一点|最亮)', text):
        return {"action": "brightness_up", "param": "100" if "最亮" in text else "", "emotion": "confident", "reply": "屏幕调亮了。"}
    if re.search(r'(调低亮度|调小亮度|调暗屏幕|调暗|暗一点|暗点儿|太刺眼|降低亮度|屏幕暗一点|最暗)', text):
        return {"action": "brightness_down", "param": "10" if "最暗" in text else "", "emotion": "confident", "reply": "屏幕调暗了。"}

    # 9. 文件夹与关键系统目录
    # 目录名之后只允许“文件夹/目录”结尾，避免“打开文档竞赛规则”被当成打开文档文件夹
    m_dir = re.search(r'(?:打开|进入|进|拾掇开)\s*(?:我的)?\s*(桌面|下载|下载文件夹|文档|我的文档|机器鱼|仿真目录|d盘|工作区)\s*(?:文件夹|目录)?$', text, re.I)
    if not m_dir:
        m_dir = re.fullmatch(r'(?:把|将)?\s*(?:我的)?\s*(桌面|下载文件夹|下载|我的文档|文档|机器鱼|仿真目录|[cd]盘|工作区)'
                             r'\s*(?:文件夹|目录)?\s*(?:给我|帮我)?(?:打开|拾掇开|开开)(?:一下)?[吧呀]?', clean, re.I)
    if m_dir:
        raw_d = m_dir.group(1).lower()
        return {"action": "open_path", "param": raw_d, "emotion": "pamper", "reply": f"{raw_d} 打开啦。"}

    # 10. 网络检索与高频网站
    if re.search(r'(?:打开|上|看|刷)\s*(?:b站|哔哩哔哩|bilibili)', text, re.I):
        return {"action": "open_url", "param": "https://www.bilibili.com", "emotion": "tease", "reply": "B站打开啦，看会儿视频放松放松～"}
    if re.search(r'(?:打开|上)\s*(?:github|开源社区)', text, re.I):
        return {"action": "open_url", "param": "https://github.com", "emotion": "confident", "reply": "GitHub 打开啦。"}
    search = _web_search_decision(text)
    if search:
        return search

    # 11. 系统运维与安全清理
    # 只认明确的“清空/清理回收站”；“倒垃圾”“清理垃圾”多半是家务或别的意思，提醒句里更不能当命令
    if re.search(r'(清空|清理)(?:一下)?回收站', text) and not re.search(r'提醒|叫我|喊我', text):
        return {"action": "run_cmd", "param": "Clear-RecycleBin -Force -ErrorAction SilentlyContinue", "emotion": "haughty", "reply": "回收站清空啦，干干净净。"}
    if re.search(r'(查看本机ip|查下ip|看下ip|本机ip|我的ip地址)', text, re.I):
        return {"action": "run_cmd", "param": "Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress", "emotion": "confident", "reply": "查到啦，IP 贴在屏幕上咯。"}

    # 12. 工程副驾与报错诊断
    if re.search(r'(瞅一眼|给俺看看|看是弄啥哩|看一眼报错|诊断报错|分析报错|帮我看下报错|帮我看看这个报错|看下报错|看看这个报错)', text):
        return {"action": "diagnose_error", "param": "", "emotion": "pamper", "reply": "别急，我瞅瞅这报错到底咋回事。"}

    # 13. 万能文件搜索与打开
    m_file = re.search(r'(?:帮我|替我|请)?(?:找一下|查找|搜索|打开文件|打开文档)\s*(.+)', text)
    if m_file:
        target_file = m_file.group(1).strip()
        return {
            "action": "search_file",
            "param": target_file,
            "emotion": "confident",
            "reply": f"我找找看 {target_file} 在哪..."
        }

    # 14. 万能应用程序与游戏智能启动 (Universal Dynamic Matcher)
    # 支持："打开Cursor", "把WPS拾掇开", "启动Obsidian", "把Blender开开", "打开机器鱼"
    app_clean = _operation_request_head(clean)
    m_open = re.search(r'^(?:帮我|替我|请)?(?:把)?(.+?)(?:拾掇开|打开|启动|开开|唤起来)$', app_clean)
    if not m_open:
        m_open = re.search(r'^(?:帮我|替我|请|我去)?(?:打开|启动|拾掇开)\s*(?:一下)?(.+)', app_clean)
    if not m_open:
        # “开计算器”“开一下记事本”“开 Edge”：只有一个“开”字时太容易撞上“开会、开机、开灯”，
        # 所以只认内置常用程序名，或英文程序名（还要本机找得到，见下面的 _find_application）
        m_bare = re.search(r'^(?:帮我|替我|请|劳驾)?(?:开|运行)(?:一下|个|下)?\s*([A-Za-z][A-Za-z0-9_.+-]*(?: [A-Za-z][A-Za-z0-9_.+-]*)?|[^\s，,。！!？?]{2,12})$', app_clean)   # “开一下VS Code”
        # 英文名只认英文数字：\w 也匹配汉字，“开个word写个报告”会被当成程序名“word写个报告”
        if m_bare and re.search(r"\.(?:py|pyw|bat|cmd|ps1|sh|js)$", m_bare.group(1), re.I):
            m_bare = None    # “运行一下sim_test.py”是跑脚本，不是开程序
        if m_bare and (m_bare.group(1).casefold() in _builtin_app_names() or re.fullmatch(r"[A-Za-z][A-Za-z0-9_ .+-]{1,20}", m_bare.group(1))):
            m_open = m_bare
    if m_open:
        target_app = re.sub(r"^(?:那个|这个|一下)", "", m_open.group(1).strip()).strip()
        # 应用名不会是一整句话；过长或带连接词的交给大模型（例如“打开聊天软件然后找到…”）
        if len(target_app) > 12 or re.search(r'(然后|再|并且|接着|发消息|发聊天软件|说)', target_app):
            target_app = ""
        app_path = _find_application(target_app) if target_app else ""
        if target_app and not app_path:
            alike = _app_sound_alike_name(target_app)    # Compare the complete declared name, including application aliases.
            if alike and _find_application(alike):
                target_app, app_path = alike, _find_application(alike)
        if app_path:
            return {
                "action": "open_app",
                "param": target_app,
                "emotion": "confident",
                "reply": f"{target_app} 打开了。"
            }
        if target_app and _known_application(target_app):
            return {"action": "chat", "param": "", "emotion": "confident", "_guard": "app_not_found",
                    "reply": f"我没找到{target_app}，请确认软件名称或安装位置，这次先不执行。"}

    # 15. 万能应用程序与进程关闭
    # 支持："把Cursor关掉", "把WPS关脱", "关闭记事本", "把游戏整死", "把记事本关脱，耳根子清净点"
    m_close = re.search(r'(?:帮我|替我|请)?(?:把)?(.+?)(?:关脱|关掉|关闭|整死|退了|关一下|关了)(?:吧|呗)?(?:[，,、。\s]|$)', text)
    if not m_close:
        m_close = re.search(r'^(?:帮我|替我|请)?(?:关闭|关掉|关脱|整死|关(?!机))\s*(?:一下)?(.+?)(?:[，,、\s]|$)', clean)
    if m_close:
        target_app = re.sub(r"^(?:那个|这个)", "", m_close.group(1).strip())
        requested_targets = _app_request_targets("close_app", text)
        if len(requested_targets) == 1:
            target_app = requested_targets[0]
        target_app = re.sub(r"^(?:那个|这个)", "", target_app).strip()
        # “聊天软件这个软件”“整个Chrome”“整个Edge给我”“聊天软件整个”：只留程序名
        target_app = re.sub(r"^(?:整个)|(?:这个|那个|这款)?(?:软件|程序|应用)$|整个$|给我$", "", target_app).strip()
        target_app = re.sub(r"(?:这个|那个|这款)?(?:软件|程序|应用)$|整个$", "", target_app).strip()
        target_code = _app_close_target_code(target_app)
        if not target_code and not _find_application(target_app):
            alike = _app_sound_alike_name(target_app)    # “几事本关了”→ 记事本; full alias names are included.
            if alike:
                target_app = alike
                target_code = _app_close_target_code(alike)
        # 只有确认是程序（常用名、本机找得到、或英文进程名）才走规则；系统开关、界面元素交给模型
        if _NOT_APP_RE.search(target_app) or not (
                target_code or re.fullmatch(r"[A-Za-z][A-Za-z0-9_ .-]{1,30}", target_app) or _find_application(target_app)):
            # \w 也匹配汉字：“Windows更新”原来被当成英文进程名关掉
            m_close = None
    if m_close and not param_grounded("close_app", target_code or target_app, text):
        m_close = None     # 规则拼出来的对象在原话里找不到依据：不抢答，交给模型（原来会被守卫改成反问）
    if m_close:
        target_code = target_code or target_app
        return {
            "action": "close_app",
            "param": target_code,
            "emotion": "confident",
            "reply": f"{target_app} 已关闭。"
        }

    # 16. Microsoft UFO³ 视觉多模态智能体跨界面自主操作极速识别
    # 支持："在记事本里写...", "在设置里打开网络", "点击确定按钮", "在计算器里计算...", "在当前窗口点击..."
    m_ufo = re.search(r'(?:在|从)?\s*(记事本|计算器|设置|浏览器|聊天软件|文件资源|当前窗口|此电脑|资源管理器)\s*(?:里|中|内)?\s*(?:帮我|替我)?\s*(写|输入|点击|点一下|计算|算|查找|搜|刷新|打开|关闭|另存为|新开|新建)', text)
    if not m_ufo:
        m_ufo = re.search(r'(?:点击|点一下|按一下)\s*(?:那个|这个)?\s*(.+?)(?:按钮|图标|选项|tab|标签)?$', clean)
    if m_ufo and re.search(r"怎么|如何|学学|学一下|教我|教教|为什么|能不能|会不会", text):
        m_ufo = None      # “我想学学怎么用记事本写代码”：问用法，不是要我去界面上操作
    if m_ufo:
        return {
            "action": "ufo_task",
            "param": text,
            "emotion": "confident",
            "reply": "正在处理。"
        }

    return None
