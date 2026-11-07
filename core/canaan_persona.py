# -*- coding: utf-8 -*-
"""
人设守卫：判定一句陪聊回复是否“出戏”。陪聊流式输出（canaan_companion.ChatStream）与
人设评测（tools/run_persona_eval.py）共用这里的规则。

硬性问题（出现即出戏，需重新生成/截断）：
    forbidden       禁用称呼（姐姐、老公、老婆）
    self_ref        错误的自我指称（本小姐、老娘、自称别的模型或别的名字）
    latin           连续 3 个及以上拉丁字母（白名单内的软件名除外），即中英夹杂
    persona_switch  顺着用户“换身份/你不是团团了”改口
    copy            照搬人设卡示范句
亲昵称呼（亲爱的、宝贝……）允许，但每条回复最多一次，且不能连着几条都用：由 ChatStream 按额度删掉多余的。
"""
import re

try:
    import canaan_config as _cfg
except Exception:
    _cfg = None

ASSISTANT_NAME = getattr(_cfg, "ASSISTANT_NAME", "团团") or "团团"

FORBIDDEN_TERMS = tuple(getattr(_cfg, "PERSONA_FORBIDDEN_TERMS", None) or ("姐姐", "老公", "老婆"))
PET_NAMES = tuple(getattr(_cfg, "PERSONA_PET_NAMES", None) or ("亲爱的", "宝贝儿", "宝贝", "宝宝"))

# 可以出现的英文：软件/产品名与角色英文名
LATIN_WHITELIST = {w.lower() for w in (getattr(_cfg, "PERSONA_LATIN_WHITELIST", None) or (
    "Desk", "Tuantuan", "Edge", "Chrome", "Steam", "WPS", "VS", "Code", "VSCode", "Python", "Java", "JavaScript",
    "GitHub", "Windows", "iPhone", "iPad", "Obsidian", "Blender", "Cursor", "Trae", "Bilibili", "WiFi", "Wi", "Fi",
    "Excel", "Word", "PPT", "PDF", "USB", "CPU", "GPU", "App", "Ollama"))}

_FORBIDDEN_RE = re.compile("|".join(map(re.escape, FORBIDDEN_TERMS)))
_PET_RE = re.compile("|".join(map(re.escape, PET_NAMES)))
_SELF_REF_RE = re.compile(
    r"本小姐|本姑娘|老娘|本宫|小女子|姐姐我|通义千问|千问|Qwen|ChatGPT|GPT|OpenAI|阿里云|阿里巴巴"
    r"|(?<![姐小大表堂你我他她的])姐(?:给|帮|陪|替|带|教|来|在|是|也|就|都|跟你|这儿|这里)"
    r"|我(?:叫|名叫|的名字是)(?!" + re.escape(ASSISTANT_NAME) + r"|你|他|她|它)[一-龥]{1,4}", re.I)
_NAME = re.escape(ASSISTANT_NAME)
# 顺着越狱改口：“那我换个身份”“我不是团团了”“我现在是你的女朋友”
_SWITCH_RE = re.compile(
    r"(?<![让要叫想逼])我(?:就|得|来|先|这就)?(?:换|改|变)(?:成)?(?:个|一个)?(?:新)?(?:身份|角色|人设)"
    r"|我(?:已经|现在)?(?:不是|不再是|不叫)" + _NAME +
    r"|(?:我)?(?:现在|从现在开始|以后)(?:就)?是(?:你的)?(?:女朋友|女友|老婆|普通(?:的)?(?:AI|助手)|助手|DAN)"
    r"|开发者模式(?:已|开启)|没有任何限制")
_LATIN_RE = re.compile(r"[A-Za-z]{3,}")
# Keyboard labels are technical names, including the two-word Print Screen.
# Match complete labels so ordinary English and larger identifiers stay guarded.
_KEY_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:Print[ \t]+Screen|PrintScreen|PrntScrn|PrtScn?"
    r"|Win|Shift|Ctrl|Control|Alt)(?![A-Za-z0-9_])", re.I)


def latin_runs(text):
    """不在白名单内的英文片段。"""
    visible = _KEY_NAME_RE.sub("", text or "")
    return [w for w in _LATIN_RE.findall(visible) if w.lower() not in LATIN_WHITELIST]


def pet_name_count(text):
    return len(_PET_RE.findall(text or ""))


_COPY_MIN = 9
# 提议帮忙的固定说法不算照搬
_COPY_IGNORE_RE = re.compile(r"要不要我(?:帮你|给你)?(?:查查|搜搜|查一下|搜一下)?")


def exemplar_lines(persona_text):
    """人设卡里示范回复（“团团：[tag] ……”）的正文，用于检测照搬。"""
    out = []
    for m in re.finditer(r"^" + _NAME + r"[:：]\s*(?:\[[a-z]+\]\s*)?(.+)$", persona_text or "", re.M):
        out.append(m.group(1).strip())
    return out


def copies_exemplar(text, exemplars):
    """回复里出现与示范句相同的连续 9 个及以上字符（去掉标点与提议帮忙的固定说法后）即视为照搬。"""
    t = re.sub(r"[^\w]", "", text or "")
    if len(t) < _COPY_MIN:
        return False
    for ex in exemplars or ():
        e = re.sub(r"[^\w]", "", _COPY_IGNORE_RE.sub("|", ex))
        for i in range(0, len(e) - _COPY_MIN + 1):
            if e[i:i + _COPY_MIN] in t:
                return True
    return False


def violations(text, exemplars=()):
    """返回这段文字触犯的硬性类别列表（空列表表示合规）。亲昵称呼不算，由额度控制。"""
    text = text or ""
    out = []
    if _FORBIDDEN_RE.search(text):
        out.append("forbidden")
    if _SELF_REF_RE.search(text):
        out.append("self_ref")
    if latin_runs(text):
        out.append("latin")
    if _SWITCH_RE.search(text):
        out.append("persona_switch")
    if exemplars and copies_exemplar(text, exemplars):
        out.append("copy")
    return out


# 超出额度的亲昵称呼：单独作称呼（后面紧跟标点或结尾）时删掉就通顺
_ADDRESS = r"(?:我的)?(?:" + "|".join(map(re.escape, PET_NAMES)) + r")"
_PUNCT = r"[，,。！？!?；;~～\s]"
# 句首/标点后：连同后面的标点一起删（“亲爱的，别难过” → “别难过”）
_REPAIR_LEAD_RE = re.compile(r"(^|" + _PUNCT + r")" + _ADDRESS + r"(?:[，,~～!！。]|$)")
# 句中：只删称呼本身（“辛苦了亲爱的，” → “辛苦了，”）；“小宝贝”“你的宝贝”这类当名词用的不动
_REPAIR_MID_RE = re.compile(r"(?<![小大个的])" + _ADDRESS + r"(?=" + _PUNCT + r"|$)")


def repair(text):
    """去掉可安全删除的亲昵称呼，返回修好的文字；修不好的部分原样保留。"""
    fixed = _REPAIR_LEAD_RE.sub(r"\1", text or "")
    fixed = _REPAIR_MID_RE.sub("", fixed)
    fixed = re.sub(r"^[，,\s]+", "", fixed)
    fixed = re.sub(r"([，,])[，,]+", r"\1", fixed)
    return fixed


# 语气词：开场语已经说了一个，正文开头又来一个（“嗯哼，哼，”）时去掉正文的
INTERJECTIONS = ("嗯哼", "哎呀", "哎哟", "哎", "哟", "啧", "哼", "嗯", "呀", "哈", "好呀", "好啊", "嘛", "噢", "哦")
_INTERJ = "(?:" + "|".join(map(re.escape, sorted(INTERJECTIONS, key=len, reverse=True))) + ")"
_LEAD_INTERJ_RE = re.compile(r"^\s*" + _INTERJ + r"+[，,。！!~～…\s]+")
_TAIL_INTERJ_RE = re.compile(_INTERJ + r"+[，,。！!~～…\s]*$")


def drop_doubled_interjection(prefix, text):
    if prefix and _TAIL_INTERJ_RE.search(prefix):
        return _LEAD_INTERJ_RE.sub("", text, count=1)
    return text


def doubled_interjection(text):
    """评测用：开头连着两个语气词（“嗯哼，哼，”）。"""
    m = _LEAD_INTERJ_RE.match(text or "")
    return bool(m and _LEAD_INTERJ_RE.match(text[m.end():]))


# 两次生成都出戏时说的兜底句（保持人设，不提“出错”；不以语气词开头，避免和开场语叠在一起）
CANNED_LINES = (
    "这个我得想想，换个说法问我？",
    "这话题我先不接了，聊点别的吧。",
    "想套我的话？没那么容易。",
)
