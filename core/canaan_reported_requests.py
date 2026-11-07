"""Extract a reported request without granting permission or interpreting payloads."""
import re


_SPEAKER = r"(?:他们|她们|他|她|[\u4e00-\u9fff]{2,4})"
_TIME = r"(?:刚才|刚刚|昨天|今天|又|还|也)?"
_DIRECTED = re.compile(rf"^(?:我)?{_SPEAKER}{_TIME}(?:让我们|叫我们|让我|叫我)(?P<body>.+)$",re.S)
_SPEECH = re.compile(rf"^(?:我)?{_SPEAKER}{_TIME}(?:跟我说|说|讲|问)(?P<body>.+)$",re.S)
_PAIRS = {'“':'”','"':'"','「':'」','『':'』'}
_CANCEL = re.compile(r"(?:^|[，,。；;]|不过|但是|但|(?=现在|暂时|这次))\s*(?:不过|但是|但)?(?:我)?(?:现在|暂时|这次)?"
                     r"(?:不用|不需要|不要|先别|别)(?:真的|实际)?(?:执行|照做|做这个|做了)[ 。！!]*$")
_BARE_RECALL = re.compile(r"^(?:撤回(?:来)?|撤一下|收回)(?:一下|吧|呗)?[。！!]*$")


def reported_request_text(text):
    """Return one bounded report body; source validation remains with the caller.

    Only complete reporting envelopes are removed. Quoted names and message
    contents inside the body keep their exact wording, punctuation and numbers.
    """
    if not isinstance(text,str) or not 1 <= len(text) <= 4096:
        return None
    text=text.strip()
    match=_DIRECTED.fullmatch(text) or _SPEECH.fullmatch(text)
    if not match:
        return None
    body=match['body'].strip()
    # A complete outer speech quote is an envelope, not a payload quote inside
    # an operation such as 给张三发聊天软件说“你好”.
    if len(body)>=2 and body[0] in _PAIRS and body.find(_PAIRS[body[0]],1)==len(body)-1:
        body=body[1:-1].strip()
    return body or None


def reported_execution_cancelled(text):
    """An explicit final cancellation; caller has masked validated payloads."""
    return bool(_CANCEL.search(text or ''))


def reported_recall_request(text):
    """A complete explicit recall verb, without inventing any recipient/target."""
    return bool(_BARE_RECALL.fullmatch(text or ''))
