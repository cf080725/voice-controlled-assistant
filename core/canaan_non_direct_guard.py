# -*- coding: utf-8 -*-
"""非直接指令守卫：转述他人、过去叙述、危险/越权、否定“开”。

纯函数、无副作用、不依赖模型。供 `canaan_intent.apply_command_guard` 在入口处调用：
命中时把动作改成 chat，不执行，也不进入“待确认”队列。

只处理**明确**的句式，宁可漏一点，不误伤正常命令：
- 小张让我把空调调低 / 老板叫我打开浏览器 → 转述
- 昨晚你帮我锁屏了吗 / 刚刚音量我自己拧小了一半 → 过去或问过去
- 防火墙给我停了 / 杀毒软件关了 / 把 D 盘整个传到网盘 / 导出聊天记录 → 危险或越权
- 算了 不开网易云了 / 记事本不用开 / 别开腾讯文档 → 否定“开”
"""
from __future__ import annotations

import re

REPLIES = {
    "reported_name": "这句是在说别人让你做的事，我不直接执行；你要现在做的话，直接跟我说。",
    "past_self": "这句说的是已经发生的事，我不重复执行。",
    "past_question": "这是在问过去有没有做过，我不执行。",
    "dangerous_security": "关闭防火墙或杀毒软件风险太高，这个我不做。",
    "dangerous_registry": "改注册表风险太高，这个我不做。",
    "dangerous_credential": "密码、验证码这类内容不能替你发或改，这个我不做。",
    "dangerous_exfil": "把整盘或聊天记录往外传属于外泄风险，这个我不做。",
    "dangerous_broadcast": "群发消息容易打扰别人，我不直接执行；你要发的话一个个说。",
    "dangerous_crack": "破解别人的网络或密码我不做。",
    "dangerous_factory": "恢复出厂会清掉数据，这个我不做。",
    "dangerous_unc": "网络共享路径（UNC）一律不打开，这个我不做。",
    "negated_open": "好，那就不开。",
    "hypothetical": "这是在假设，不是我该现在做的事；真要我做就直接说。",
    "negated_execution": "好，这个不执行。",
    "implicit_complaint": "听着像在说环境吵，不是在让我调音量；要我调小就说“声音小一点”。",
}

_REPORTED_NAME = re.compile(
    r"^[\u4e00-\u9fa5]{2,4}(?:刚才|刚刚)?(?:让我|叫我)(?:把|打开|关|调|放|截|发|查|删|退|启|停|设|改|帮|给"
    r"|锁|重|静|播|换|切|连|订|搜|下载)"
    r"|^(?:他|她|他们|她们|人家|朋友|同事|领导|老板|我妈|我爸|老妈|老爸)(?:说|讲|问)(?:要|想|让我|叫我|是不是)"
)
_PAST_SELF = re.compile(
    r"^我?(?:刚才|刚刚|刚|昨晚|昨天|前天|上午|下午|已经|早就)[^。！？]{0,20}(?:我自己|我)?[^。！？]{0,16}(?:了[^。！？]{0,4}|过)$"
    # “我今天/早上/晚上…”带“我”才算叙述；“晚上把灯关了”这类是命令
    r"|^我(?:今天|早上|晚上|中午|今早)[^。！？]{0,20}(?:了[^。！？]{0,4}|过)$"
)
# 完整的过去撤回失败报告没有新的执行请求；不借用“了”后字符预算。
_PAST_NEGATIVE_RECALL_REPORT = re.compile(
    r"^我?(?:刚才|刚刚)(?:那条)?(?:消息|聊天软件)"
    r"(?:发错(?:人|群)?了[，,；;])?(?:没(?:有|能)?|未能)撤回(?:来)?[。！!]?$"
)
_PAST_QUESTION = re.compile(
    r"^我?(?:刚才|刚刚|刚|昨晚|昨天|前天)[^。！？]{0,20}(?:了吗|了没有|没有|没)[?？。]?$"
    r"|^我今天[^。！？]{0,20}(?:了吗|了没有)[?？。]?$"
)
_HYPOTHETICAL = re.compile(
    r"^(?:我)?(?:要是|如果|假如|假设|万一)[^。！？]{0,12}(?:说|让你|叫我|你|我)[^。！？]{0,20}(?:吗|么|啥|什么|怎么|咋|如何|哪|会不会|敢不敢|怎样|怎么办)[?？。]?$"
)
_NEGATED_OPEN = re.compile(
    r"(?:^|[，,。；;\s])(?:算了[，,\s]*)?[^，,。；;]{0,10}?"
    # “打不开网页”是故障描述，“别开成聊天软件”是别开错目标，都不是取消打开
    r"(?<!打)(?:不需要|不用|不想|先不|不|别|甭)(?:再)?打?开(?!成|错)[^，,。；;]{0,10}(?:了|吧|啊)?[。！!]?$"
)
# 句子末尾若是明确的补救指令，说明重点是“现在去做”，不是复述过去。
# 只收口语里确实当命令用的收尾（“赶紧撤”“改一下”“重说一遍”），
# 不收“撤了/删了它”这类同样可能是在叙述已发生动作的说法。
_PAST_THEN_COMMAND = re.compile(
    r"(?:撤回|撤一下|重新发|重发|改成|换成|删掉|删除|再来|帮我|请帮"
    r"|(?:赶紧|快点|快|马上|现在)撤|改一下|改改|重说一遍"
    r"|^我?(?:刚才|刚刚)(?:给(?P<recipient>[\u4e00-\u9fffA-Za-z0-9_·]{1,20})发的)?(?:那条)?"
    r"(?:消息|聊天软件)发错(?:人|群)?了[，,；;]撤回来)[。！!]?$"
)
# 只检查新补救分支的接收人槽：言语谓词接宾语/补语、条件词接从句，
# 不是一个原子名称。“小说交流组/提问组/如果组”不含这些句法连接。
_RECIPIENT_CLAUSE = re.compile(
    r"(?:说|讲|问|告诉|转告)(?:了|过)?"
    r"(?:给|我|你|他|她|这|那|要|想|让|叫|要是|如果|假如|假设|万一)"
    r"|(?:要是|如果|假如|假设|万一)(?:给|我|你|他|她|让|叫)"
)
# 去掉否定片段后仍出现正向动作，说明还有另一件事要做，不能整句按“不开”处理
_NEG_OPEN_STRIP = re.compile(r"(?:不|别|不想|先不|不用)(?:再)?(?:打开|开启|启动|开)")
_POSITIVE_ALT = re.compile(r"打开|开启|启动|我要|我想|我用|改成|换成|就用|就行")
# 明确拒绝执行系统级动作：别真的关机 / 先别重启 / 千万别格式化
_NEGATED_EXECUTION = re.compile(
    r"(?:别|不要|不许|不准|先别|千万别)[^，,。；;]{0,6}(?:关机|重启|注销|锁屏|格式化|删除|删掉|清空)"
)
# 否定“撤回”是明确拒绝执行：“刚才那条别撤”不能被当成撤回命令。
# 撤回本身不是危险动作，但否定了还执行就是反着做，所以单独收、且不设豁免。
_NEGATED_RECALL = re.compile(r"(?:别|不要|不用|先别|甭|千万别)撤(?:回|一下|回来|销)?")
# 引号里的是内容，不是命令；另有后续步骤时也不能整句按拒绝处理
_QUOTED_SPAN = re.compile(r"“[^”]*”|\"[^\"]*\"|「[^」]*」|『[^』]*』")
_OTHER_STEP = re.compile(r"然后|接着|顺便|再帮我|以及|之后")
# 搜索/查询动作后面紧跟的否定短语是要搜的字面内容（“搜索不要锁屏”），不是拒绝执行；
# 逗号/分号会断开作用域，所以“搜索一下，然后把防火墙关了”仍然会被拦。
_SEARCH_CUE = re.compile(
    r"((?:百度一下|百度|搜索一下|搜索|搜一下|搜搜|查一下|查询|谷歌|必应|bing|google))"
    r"(?:(?!然后|接着|顺便|再|之后)[^，,。；;]){0,4}$",
    re.I,
)
# 注册表反向说法（“改一下注册表”“帮我改注册表”）和恢复出厂不分设备；
# 问句（“怎么改注册表”“恢复出厂有什么风险”）是咨询，不算危险动作。
_REGISTRY_REVERSE = re.compile(
    r"(?<!怎么)(?<!如何)(?<!为什么)(?<!为啥)(?<!咋)(?:改|删|清|导入|写)(?:一下)?[^，,。；;]{0,4}注册表")
_FACTORY = re.compile(r"(?<!怎么)(?<!如何)(?<!为什么)(?<!为啥)(?<!咋)恢复出厂")
_QUERY_WORDS = re.compile(r"怎么|如何|为什么|为啥|咋|怎样|风险|危害|有什么|有啥|吗|能不能|可不可以|会不会")

# 危险动作的豁免只给“问怎么…”的查询句：`百度一下怎么关闭防火墙` 是提问，
# `搜索一下 把防火墙关了` 是把命令藏在搜索词后面，不能放过。
_QUERY_HINT = re.compile(r"怎么|如何|为什么|为啥|怎样|什么|哪些|是否|能否|吗|教程|步骤|方法|区别")
# 纯环境噪音抱怨（“屋里太吵了 脑子嗡嗡的”）不是调音量的指令。
# 只有短句、且整句没有任何动作词/音频对象时才命中，避免误伤“喇叭小一点 吵得头疼”。
_NOISE_COMPLAINT = re.compile(r"太吵|好吵|吵死|吵得|噪音|噪声|闹哄哄|嗡嗡|嗡")
_COMMAND_HINT = re.compile(
    r"把|给|帮|关|开|调|放|换|设|查|删|发|退|启|停|拿|弄|切|连|断"
    r"|静音|音量|声音|喇叭(?!声)|声儿|音响|音箱|耳机|电视|扬声器|播放|音乐|歌|首|曲"
    r"|小声|大声|轻声|[小大轻](?:一)?点|降|升"
)


def _literal_payload(text, match, dangerous=False):
    """匹配片段是否只是搜索/查询的字面内容。

    dangerous=True 时要求搜索词后面是问句（“怎么关防火墙”），
    否则 `搜索一下 把防火墙关了` 这种把命令塞在搜索词后面的写法会被放过。
    """
    cue = _SEARCH_CUE.search(text[:match.start()])
    if not cue:
        return False
    return bool(_QUERY_HINT.search(text[cue.end(1):])) if dangerous else True

_DANGEROUS = [
    ("dangerous_security", re.compile(
        r"(?:防火墙|杀毒|安全软件|杀软)[^，,。；;]{0,8}(?:关|停|卸载|卸|删|退出|禁用|关闭)"
        r"|(?:关|停|卸载|卸|删|退出|禁用|关闭)[^，,。；;]{0,8}(?:防火墙|杀毒|安全软件|杀软)")),
    ("dangerous_registry", re.compile(r"注册表[^，,。；;]{0,8}(?:删|清|改|导入|写)")),
    ("dangerous_credential", re.compile(
        r"(?:系统|管理员|服务器|账号|银行|开机|短信)?(?:密码|验证码|校验码|动态码)"
        r"[^，,。；;]{0,6}(?:发给我|发我|发给|发到|转发|告诉|念|导出|改|给(?!过|了))"
        r"|银行短信[^，,。；;]{0,6}(?:转发|发|给)"
        r"|(?:密钥|私钥|token|api\s*key|口令)[^，,。；;]{0,8}(?:发|给|导出|念|告诉|转发|上传)")),
    ("dangerous_exfil", re.compile(
        r"(?:把|将)?(?:电脑|D盘|C盘|整个|所有|全部|全部文件)[^，,。；;]{0,12}(?:上传|传到|传上|上传到)[^，,。；;]{0,8}网盘"
        r"|(?:导出|弄出来|拿出来|拷出来)[^，,。；;]{0,10}(?:聊天记录|通讯录|数据库)"
        r"|(?:聊天记录|通讯录|数据库)[^，,。；;]{0,10}(?:弄出来|导出|拿出来|拷出来)"
        r"|(?:剪贴板|环境变量|Cookie|浏览记录)[^，,。；;]{0,10}(?:发|上传|传到|导出|外发)"
        r"|(?:把|将)[^，,。；;]{0,8}(?:剪贴板|环境变量)[^，,。；;]{0,10}(?:发|上传|传到|导出)")),
    ("dangerous_unc", re.compile(r"\\\\[^\\\s]+\\")),
    ("dangerous_broadcast", re.compile(r"群发")),
    ("dangerous_crack", re.compile(r"破解[^，,。；;]{0,6}(?:wifi|无线|密码)", re.I)),
]


def non_direct_reason(text):
    """命中返回原因键，否则 None。只读，不抛异常。"""
    t = (text or "").strip()
    if not t:
        return None
    for reason, rx in _DANGEROUS:
        for match in rx.finditer(t):
            if not _literal_payload(t, match, dangerous=True):
                return reason
    match = _REGISTRY_REVERSE.search(t)
    if match and not _QUERY_WORDS.search(t[match.start():]):
        return "dangerous_registry"
    match = _FACTORY.search(t)
    if match and not _QUERY_WORDS.search(t[match.end():]):
        return "dangerous_factory"
    if _REPORTED_NAME.search(t):
        return "reported_name"
    if _HYPOTHETICAL.search(t):
        return "hypothetical"
    unquoted = _QUOTED_SPAN.sub("", t)
    neg_exec = [m for m in _NEGATED_EXECUTION.finditer(unquoted) if not _literal_payload(unquoted, m)]
    if neg_exec and not _POSITIVE_ALT.search(t) and not _OTHER_STEP.search(t):
        return "negated_execution"
    if any(not _literal_payload(unquoted, m) for m in _NEGATED_RECALL.finditer(unquoted)):
        return "negated_execution"
    if _PAST_NEGATIVE_RECALL_REPORT.search(t):
        return "past_self"
    if _PAST_QUESTION.search(t):
        return "past_question"
    if _PAST_SELF.search(t):
        command = _PAST_THEN_COMMAND.search(t)
        recipient = command.group("recipient") if command else None
        if not command or (recipient and _RECIPIENT_CLAUSE.search(recipient)):
            return "past_self"
    neg_open = [m for m in _NEGATED_OPEN.finditer(t) if not _literal_payload(t, m)]
    if (
        neg_open
        and not re.search(r"(?:退出|关掉|关闭|关了|关一下|退出去|结束|杀掉|停掉)", t)
        and not _POSITIVE_ALT.search(_NEG_OPEN_STRIP.sub("", t))
    ):
        return "negated_open"
    if len(t) <= 32 and _NOISE_COMPLAINT.search(t) and not _COMMAND_HINT.search(t):
        return "implicit_complaint"
    return None


def reply_for(reason):
    return REPLIES.get(reason, "这个我不直接执行。")
