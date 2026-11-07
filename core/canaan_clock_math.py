"""Exact arithmetic for complete clock-interval questions, never task fragments."""
import re

_NUM = r'(?:[0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})'
_PHASE = r'(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|晚间)'
_DAY = r'(?:今天|明天|次日|第二天|翌日)'
_POINT_SHAPE = rf'(?:{_DAY})?\s*(?:{_PHASE})?\s*{_NUM}\s*(?:[:：]\s*{_NUM}|(?:点|时)(?:\s*(?:{_NUM}\s*分?|半|一刻|三刻|整))?)'
_POINT = re.compile(rf'(?P<day>{_DAY})?\s*(?P<phase>{_PHASE})?\s*(?P<hour>{_NUM})\s*'
                    rf'(?:[:：]\s*(?P<colon>{_NUM})|(?:点|时)(?:\s*(?:(?P<minute>{_NUM})\s*分?|(?P<part>半|一刻|三刻|整)))?)')
_QUESTION = re.compile(rf'(?:从|自)?\s*(?P<left>{_POINT_SHAPE})\s*(?:到|至|[-—~～])\s*'
                       rf'(?P<right>{_POINT_SHAPE})\s*[，,]?\s*'
                       r'(?:(?:一共|总共|相隔|间隔|相差|隔了|有|需要|是)\s*)?'
                       r'(?P<unit>(?:多少|几)(?:分钟|小时|时间)|多久|多长时间)'
                       r'(?:啊|呀|呢|吗)?[？?。！!\s]*')
_DIGITS = dict(zip('零〇一二两三四五六七八九', (0,0,1,2,2,3,4,5,6,7,8,9)))


def _number(text):
    if text.isascii() and text.isdigit():
        return int(text)
    if '十' in text:
        match = re.fullmatch(r'([一二两三四五六七八九]?)十([一二三四五六七八九]?)', text)
        if not match:
            return None
        a,b=match.groups()
        return (_DIGITS[a] if a else 1)*10+(_DIGITS[b] if b else 0)
    if all(c in _DIGITS for c in text):
        return int(''.join(str(_DIGITS[c]) for c in text))
    return None


def _read_point(text, inherited_phase=None, *, left_day=0, right=False):
    match = _POINT.fullmatch(text.strip())
    if not match:
        return None
    parts=match.groupdict()
    hour=_number(parts['hour'])
    raw_minute=parts['colon'] or parts['minute']
    minute=_number(raw_minute) if raw_minute else {'半':30,'一刻':15,'三刻':45,'整':0,None:0}[parts['part']]
    if hour is None or minute is None or not 0<=hour<24 or not 0<=minute<60:
        return None
    day=parts['day']
    # An explicit next day does not inherit yesterday's afternoon designation.
    phase=parts['phase'] or (inherited_phase if not day else None)
    if phase:
        if hour>12:
            return None
        if phase in ('下午','傍晚','晚上','夜里','晚间'):
            if hour==0:
                return None
            hour=(0 if phase in ('晚上','夜里','晚间') else 12) if hour==12 else hour+12
        elif phase=='中午':
            if hour not in (1,11,12):
                return None
            hour=13 if hour==1 else hour
        elif hour==12:
            if phase!='凌晨':
                return None
            hour=0
    if day in ('次日','第二天','翌日'):
        offset=left_day+1 if right else 1
    elif day=='明天':
        offset=1
    elif day=='今天':
        offset=0
    else:
        offset=left_day if right else 0
    return (offset*1440+hour*60+minute, parts['phase'], offset)


def interval_reply(text):
    """Return a grounded reply for an entire question, otherwise leave routing alone."""
    if not isinstance(text,str) or len(text)>240:
        return None
    query=re.sub(r'^(?:请问|问(?:一下|下)?|告诉我|帮我算一下|帮我算算|算一下|算算)\s*','',text.strip())
    query=re.sub(r'[，,。？?！!\s]*(?:请)?(?:直接说(?:答案|结果)|直接回答|只说答案)(?:就行|即可)?[。！!\s]*$','',query)
    match=_QUESTION.fullmatch(query)
    if not match:
        return None
    left=_read_point(match['left'])
    right=_read_point(match['right'],left[1] if left else None,left_day=left[2] if left else 0,right=True)
    if left is None or right is None:
        return '请提供有效的时间：小时应为0到23，分钟应为0到59，并核对上午或下午。'
    duration=right[0]-left[0]
    if duration<0:
        return '结束时间早于开始时间，是指次日吗？请明确日期后我再算。'
    if '小时' in match['unit']:
        hours,minutes=divmod(duration,60)
        return f'相隔{hours}小时{minutes}分钟。' if minutes else f'相隔{hours}小时。'
    return f'相隔{duration}分钟。'
