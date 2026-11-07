# -*- coding: utf-8 -*-
"""
================================================================================
   👑 团团 (Tuantuan) · 零故障工业级高可靠原版母带语音引擎 (Zero-Error Speech Engine)
================================================================================
   - 核心保障：
     1. 全格式文本自适应净化（SpeechTextSanitizer）：过滤Markdown/代码/路径，转译数字标点
     2. 零延迟毫秒级音频缓存（AudioCacheManager）：高频语料秒级直出
     3. 逐句合成、逐句播放（SpeechPipeline）：第一句合成完即出声，后续句子边播边合成
     4. 合成失败时不返回任何音频（不再拿情绪参考音频顶替），由上层显示文字并提示音，错误写入 logs/tts_errors.log
     4. 全平台路径便携式自适应（Portable Relative Paths）：支持任何电脑解压即用
================================================================================
"""

import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import os
import sys
import json
import time
import re
import hashlib
import shutil
import threading
import urllib.error
import urllib.request
import io
import wave

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 便携式路径自适应
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICEPACK_DIR = os.path.join(BASE_DIR, "tuantuan_voicepack")
EMOTIONS_DIR = os.path.join(VOICEPACK_DIR, "emotions")
CACHE_DIR = os.path.join(VOICEPACK_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
TTS_URL = "http://127.0.0.1:9880/tts"

# 自动发现 GPT-SoVITS 部署目录
def discover_gpt_sovits_dir():
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

GPT_SOVITS_DIR = discover_gpt_sovits_dir()

# 5大核心情感参考矩阵 (100% 满血符合 3~10 秒参考音频黄金区间，发音极尽自然流利)
EMOTION_CONFIG = {
    "pamper": {
        "title": "慵懒宠溺",
        "ref": os.path.join(EMOTIONS_DIR, "pamper.wav"),
        "prompt": "你拼命的样子，还真是可爱",
        "speed": 1.0,
        "temp": 0.65
    },
    "haughty": {
        "title": "高冷从容",
        "ref": os.path.join(EMOTIONS_DIR, "haughty.wav"),
        "prompt": "猎人最需要的是耐心",
        "speed": 1.0,
        "temp": 0.65
    },
    "confident": {
        "title": "霸气掌控",
        "ref": os.path.join(EMOTIONS_DIR, "confident.wav"),
        "prompt": "接下来是宝藏猎人的表演时间，可别被我迷住了",
        "speed": 1.0,
        "temp": 0.65
    },
    "dangerous": {
        "title": "魅惑致命",
        "ref": os.path.join(EMOTIONS_DIR, "dangerous.wav"),
        "prompt": "我能让男人们的心跳加快，也能让他们随之停止",
        "speed": 1.0,
        "temp": 0.65
    },
    "tease": {
        "title": "轻佻调侃",
        "ref": os.path.join(EMOTIONS_DIR, "tease.wav"),
        "prompt": "你以为我穿成这样是为了取悦你？你猜对了，亲爱的！",
        "speed": 1.0,
        "temp": 0.65
    }
}

# 团团原版高频母带彩蛋库 (100% 游戏原版音频，遇到特定对答直接原声秒出)
NATIVE_SOUNDBOARD = [
    {
        "keywords": ["夸我", "怎么样", "棒不棒", "厉不厉害", "做得好吗"],
        "text": "哇，你可真棒~",
        "audio": os.path.join(BASE_DIR, "dataset_tuantuan", "tuantuan_0039.wav"),
        "emotion": "pamper"
    },
    {
        "keywords": ["喜欢我", "长相", "帅不帅", "脸蛋", "好看吗"],
        "text": "你的小脸蛋，姐姐很喜欢~",
        "audio": os.path.join(BASE_DIR, "dataset_tuantuan", "tuantuan_0035.wav"),
        "emotion": "pamper"
    },
    {
        "keywords": ["累了", "辛苦", "休息", "努力", "好累"],
        "text": "你拼命的样子，还真是可爱~",
        "audio": os.path.join(BASE_DIR, "dataset_tuantuan", "tuantuan_0057.wav"),
        "emotion": "pamper"
    },
    {
        "keywords": ["交易", "买卖", "帮忙", "代价", "报酬"],
        "text": "要做个交易吗？",
        "audio": os.path.join(BASE_DIR, "dataset_tuantuan", "tuantuan_0016.wav"),
        "emotion": "tease"
    }
]

DEEP_SYSTEM_PROMPT = """你是游戏中的宝藏猎人团团（Tuantuan），同时兼任主人专属的高级语音智能管家。
你拥有致命的吸引力与绝对从容的身手，深谙人性与欲望。
【语言风格核心要求】：
1. 称呼用户为“亲爱的”，偶尔调侃时可自称“姐姐”。
2. 性格特质：慵懒从容、轻佻魅惑、高贵冷艳。绝不要像死板的客服或说教的AI，不要长篇大论。
3. 语调习惯：带刺但撩人，爱用“~”、“呢”、“好吗？”。融入宝藏、猎手、风沙、猎物等意象。
4. 严格控制长度：每句话必须控制在 1 到 2 句以内（极度适合语音朗读）。
5. 情绪标签规范：在回答的最开头，必须用方括号标注当前最匹配的情绪标签：[tease]（调侃）、[pamper]（宠溺）、[haughty]（高冷）、[confident]（掌控）、[dangerous]（魅惑致命）。

示例：
用户：团团，今天怎么这么安静？
回答：[tease] 怎么，一刻听不到我的声音，心里就痒痒了，亲爱的？

用户：今天工作好累啊。
回答：[pamper] 过来，把头靠在姐姐肩膀上，今天准你偷个懒~

用户：帮我查一下电脑显卡。
回答：[confident] 这台机器里的每一个芯片都在我的掌心，亲爱的，放一百个心。"""

class SpeechTextSanitizer:
    """语音专属全方位文本净化器：杜绝任何发音崩坏、乱码跳读或超时"""

    @staticmethod
    def _num2zh_small(s):
        try:
            n = int(s)
            units = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
            if n < 10:
                return units[n] if n > 0 else "零"
            elif n < 20:
                return "十" + units[n - 10]
            elif n < 100:
                ten, u = n // 10, n % 10
                tens = ["", "", "二十", "三十", "四十", "五十", "六十", "七十", "八十", "九十"]
                return tens[ten] + units[u]
            return str(n)
        except Exception:
            return s

    _DIGIT_ZH = "零一二三四五六七八九"
    # 数字 2 后面跟这些量词时读“两”（两点、两个、两条），“第2”“2月”“2号”仍读“二”
    _LIANG_UNITS = ("个", "条", "次", "点", "天", "小时", "分钟", "秒", "位", "件", "张", "本", "岁",
                    "遍", "句", "步", "份", "台", "种", "只", "杯", "瓶", "块", "元", "倍", "周",
                    "年", "斤", "公里", "米", "页", "首", "部", "间", "项", "封", "趟", "下")

    # 前面出现这些词时，后面的数字是编号，逐位读
    _CODE_HINTS = ("码", "号码", "电话", "手机", "编号", "单号", "尾号", "账号", "工号", "学号",
                   "房间", "id", "imapp", "端口", "版本")

    @classmethod
    def _digits_zh(cls, s):
        return "".join(cls._DIGIT_ZH[int(ch)] for ch in s if ch.isdigit())

    @classmethod
    def _int_zh(cls, n):
        """0 到 99999999 的中文读法：15→十五，105→一百零五，1200→一千二百，20000→两万。"""
        n = int(n)
        if n == 0:
            return "零"
        if n >= 100000000:
            return cls._digits_zh(str(n))

        def _below_10k(v, leading):
            out, zero = "", False
            for unit, base in (("千", 1000), ("百", 100), ("十", 10), ("", 1)):
                d, v = divmod(v, base)
                if d == 0:
                    zero = bool(out)
                    continue
                if zero:
                    out += "零"
                    zero = False
                if base == 10 and d == 1 and not out and leading:
                    out += "十"
                elif base in (1000,) and d == 2 and leading and not out:
                    out += "两千"
                else:
                    out += cls._DIGIT_ZH[d] + unit
            return out

        high, low = divmod(n, 10000)
        if not high:
            return _below_10k(low, True)
        head = "两" if high == 2 else _below_10k(high, True)
        if not low:
            return head + "万"
        tail = _below_10k(low, False)
        return head + "万" + ("零" if low < 1000 else "") + tail

    @classmethod
    def _speak_digit_runs(cls, text):
        """把剩下的阿拉伯数字按中文习惯读出来；长串编号（5 位以上、0 开头）仍逐位读。"""
        def _iso_date(m):
            return f"{cls._digits_zh(m.group(1))}年{cls._int_zh(m.group(2))}月{cls._int_zh(m.group(3))}号"
        text = re.sub(r'(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)', _iso_date, text)
        text = re.sub(r'(?<!\d)(\d{4})年', lambda m: cls._digits_zh(m.group(1)) + "年", text)
        text = re.sub(r'(?<![\d.])(\d{1,4})\s*[-~～]\s*(\d{1,4})(?![\d.])',
                      lambda m: f"{m.group(1)}到{m.group(2)}", text)

        units = cls._LIANG_UNITS

        def _run(m):
            s, start = m.group(0), m.start()
            after = text[m.end():m.end() + 2]
            before = text[max(0, start - 2):start]
            context = text[max(0, start - 5):start].lower()
            if len(s) > 1 and s.startswith("0"):
                return cls._digits_zh(s)
            if len(s) >= 9 or (len(s) >= 3 and any(k in context for k in cls._CODE_HINTS)):
                return cls._digits_zh(s)
            if s == "2" and after.startswith(units) and not before.endswith(("第", "星期", "礼拜", "周")):
                return "两"
            return cls._int_zh(s)

        return re.sub(r'\d+', _run, text)

    @classmethod
    def _spoken_number(cls, value):
        sign = "负" if value.startswith("-") else "正" if value.startswith("+") else ""
        whole, dot, fraction = value.lstrip("+-").partition(".")
        whole_zh = cls._int_zh(whole) if whole.isdigit() and len(whole) <= 8 else cls._digits_zh(whole)
        return sign + whole_zh + (
            "点" + "".join(cls._num2zh_small(ch) for ch in fraction) if dot else "")

    @classmethod
    def sanitize(cls, raw_text):
        if not raw_text:
            return "在呢，亲爱的。"

        text = str(raw_text).strip()

        # 1. 过滤代码块、JSON与 Markdown 语法
        text = re.sub(r'```(?:json|python|bash|powershell)?[\s\S]*?```', '代码已经为你处理好了', text)
        text = re.sub(r'```[\s\S]*?```', '', text)
        text = re.sub(r'\{[\s\S]*?"(?:action|reply|param)"[\s\S]*?\}', '', text)
        text = re.sub(r'\[(tease|pamper|haughty|confident|dangerous)\]', '', text)
        text = re.sub(r'\[.*?\]\(.*?\)', '', text)  # Markdown links
        text = re.sub(r'[*_#>`~]', '', text)

        # 2. 净化系统路径与长英文指令
        text = re.sub(r'[A-Za-z]:\\[^\s，,。！!？?\n]+', '指定文件路径', text)
        text = re.sub(r'https?://[^\s]+', '相关网址链接', text)
        text = re.sub(r'\b[a-zA-Z0-9_\-\.]+@(?:[a-zA-Z0-9_\-]+\.)+[a-zA-Z]{2,}\b', '邮箱地址', text)
        
        # Keep named OS version alternatives separate before slash cleanup.
        text = re.sub(
            r'(?<![A-Za-z0-9_./\\])((?i:Windows|Win))[ \t]*(10|11)[ \t]*[/／][ \t]*(10|11)'
            r'(?![A-Za-z0-9_]|\.[A-Za-z0-9]|[ \t]*[/／\\])',
            lambda m: f'{m.group(1)} {m.group(2)}或{m.group(3)}' if m.group(2) != m.group(3) else m.group(0),
            text)

        # 常见文件扩展名口语化转译
        text = re.sub(r'\.py\b', '脚本', text)
        text = re.sub(r'\.exe\b', '应用程序', text)
        text = re.sub(r'\.docx?\b', '文档', text)
        text = re.sub(r'\.xlsx?\b', '表格', text)
        text = re.sub(r'\.pptx?\b', '演示文档', text)
        text = re.sub(r'\.png|\.jpg|\.jpeg\b', '图片', text)

        # 3. 时间、日期与百分比口语化拟真转换（告别机械报数字）
        def _fmt_time(m):
            h, mn = m.group(1), m.group(2)
            h_zh = "两" if int(h) == 2 else cls._num2zh_small(h)
            if mn in ["00", "0"]:
                return f"{h_zh}点整"
            elif mn.startswith("0") and len(mn) == 2:
                return f"{h_zh}点零{cls._num2zh_small(mn[1])}"
            else:
                return f"{h_zh}点{cls._num2zh_small(mn)}"
        text = re.sub(r'(\d+)点(\d+)(?:分)?', _fmt_time, text)
        text = re.sub(r'(?<![\d:：])([01]?\d|2[0-3])[:：]([0-5]\d)(?![\d:：])', _fmt_time, text)

        def _fmt_date(m):
            mon, day = m.group(1), m.group(2)
            return f"{cls._num2zh_small(mon)}月{cls._num2zh_small(day)}号"
        text = re.sub(r'(\d+)月(\d+)[日号]?', _fmt_date, text)

        def _fmt_percent(m):
            value = m.group(1)
            # “涨了+3.5%”已经说了方向，不再读“正”
            if value.startswith("+") and text[max(0, m.start() - 2):m.start()].endswith(
                    ("涨", "涨了", "升", "升了", "增", "增加", "增长", "加", "高了", "多了")):
                value = value[1:]
            return f"百分之{cls._spoken_number(value)}"
        text = re.sub(r'(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*[%％]', _fmt_percent, text)
        text = re.sub(r'(?<![\dA-Za-z_.])([+-]\d+(?:\.\d+)?)(?![\d.])',
                      lambda m: cls._spoken_number(m.group(1)), text)
        text = re.sub(r'(?<![\d.])([+-]?\d+\.\d+)(?![\d.])',
                      lambda m: cls._spoken_number(m.group(1)), text)
        text = re.sub(r'(\d+)px', r'\1像素', text)

        # 4. 彻底净化书名号、括号、引号（直接剥除，绝不读出“双引号”或残留逗号停顿）
        text = re.sub(r'[\(\)（）\{\}\<\>\[\]【】"“”\'‘’`]', '', text)
        text = re.sub(r'[：:;；]', '，', text)
        text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
        text = re.sub(r'[\u2600-\u27BF\uE000-\uF8FF]', '', text)
        text = re.sub(r'[\r\n\t]+', '，', text)
        text = re.sub(r'[\\\/\|\^&$@]', '', text)

        # 5. 标点规范化（防止连续逗号或空标点，末尾如果是逗号转为句号）
        text = re.sub(r'[，,]{2,}', '，', text)
        text = re.sub(r'[。\.]{2,}', '。', text)
        text = re.sub(r'^[，,。、\s]+', '', text)
        text = re.sub(r'[，,、\s]+$', '。', text)
        text = text.strip()

        # 6. 口语化长度自适应保护 (适合语音朗读的最佳长度在 10 ~ 65 字)
        if len(text) > 75:
            sub = text[:70]
            last_punc = max(sub.rfind('。'), sub.rfind('！'), sub.rfind('!'), sub.rfind('？'), sub.rfind('?'), sub.rfind('，'))
            if last_punc > 20:
                text = sub[:last_punc+1]
            else:
                text = sub + "，详细内容你可以看屏幕哦，亲爱的。"

        # 7. 剩下的数字按中文读法读（15 条 → 十五条），编号类长串逐位读
        text = cls._speak_digit_runs(text)
        text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z，。！？~、 ]', '', text)
        text = text.strip()

        if not text:
            text = "好啦，亲爱的。"

        return text

def _validate_wav(source):
    """Check declared PCM frames, including truncation after a valid WAV header."""
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    elif isinstance(source, os.PathLike):
        source = os.fspath(source)
    try:
        with wave.open(source, "rb") as audio:
            channels, width, rate, frames, compression, _ = audio.getparams()
            if channels not in (1, 2) or width not in (1, 2, 3, 4) or not 8000 <= rate <= 192000 or not frames or compression != "NONE":
                raise ValueError("TTS WAV 参数无效或没有音频")
            remaining = frames
            while remaining:
                count = min(remaining, 32768)
                if len(audio.readframes(count)) != count * channels * width:
                    raise ValueError("TTS WAV 音频被截断")
                remaining -= count
            if audio.readframes(1):
                raise ValueError("TTS WAV 含不完整采样帧")
    except (wave.Error, EOFError) as error:
        raise ValueError("TTS 未返回可解码的 PCM WAV 音频") from error


class AudioCacheManager:
    """高频语料毫秒级直出缓存：0延迟，0显存消耗，永不出错"""
    _validated = {}
    _validation_lock = threading.Lock()

    @classmethod
    def _valid_file(cls, path):
        try:
            stat = os.stat(path)
            if stat.st_size <= 1000:
                return False
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            with cls._validation_lock:
                previous = cls._validated.get(path)
            if previous and previous[0] == signature:
                return previous[1]
            try:
                _validate_wav(path)
                valid = True
            except (ValueError, OSError):
                valid = False
            after = os.stat(path)
            if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != signature:
                return False
            with cls._validation_lock:
                if len(cls._validated) >= 1024:
                    cls._validated.clear()
                cls._validated[path] = (signature, valid)
            return valid
        except OSError:
            return False

    @staticmethod
    def get_cache_path(text, emotion):
        key = f"{emotion}:{text}".strip().encode('utf-8')
        md5_sig = hashlib.md5(key).hexdigest()
        return os.path.join(CACHE_DIR, f"{md5_sig}.wav")

    @classmethod
    def get(cls, text, emotion):
        cache_file = cls.get_cache_path(text, emotion)
        if cls._valid_file(cache_file):
            return cache_file
        return None

    @classmethod
    def put(cls, text, emotion, src_file):
        try:
            if not cls._valid_file(src_file):
                return
            cache_file = cls.get_cache_path(text, emotion)
            shutil.copyfile(src_file, cache_file)
        except Exception:
            pass

TTS_ERROR_LOG = os.path.join(BASE_DIR, "logs", "tts_errors.log")
TTS_UNHEALTHY_FLAG = os.path.join(BASE_DIR, "data", "tts_unhealthy.flag")


class TTSHealth:
    """语音服务“活着但每次都合成失败”的识别（实测：显卡报 CUDA 999 后，服务进程不退出，之后每次都回 400，
    10-20 那次持续约两个半小时没有声音）。连续失败 FAIL_LIMIT 次后：
    - COOLDOWN_SEC 秒内直接用系统语音，不再每句等 GPU 失败；
    - 写 data/tts_unhealthy.flag，托盘看到后重启语音服务。成功一次就恢复。"""
    FAIL_LIMIT = 3
    COOLDOWN_SEC = 60
    _lock = threading.Lock()
    _fails = 0
    _bad_until = 0.0

    @classmethod
    def record_ok(cls):
        with cls._lock:
            was_bad = cls._fails >= cls.FAIL_LIMIT
            cls._fails, cls._bad_until = 0, 0.0
        if was_bad:
            try:
                os.remove(TTS_UNHEALTHY_FLAG)
            except OSError:
                pass

    @classmethod
    def record_fail(cls, err=None):
        with cls._lock:
            cls._fails += 1
            tripped = cls._fails >= cls.FAIL_LIMIT
            if tripped:
                cls._bad_until = time.monotonic() + cls.COOLDOWN_SEC
        if tripped:
            try:
                os.makedirs(os.path.dirname(TTS_UNHEALTHY_FLAG), exist_ok=True)
                with open(TTS_UNHEALTHY_FLAG, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "fails": cls._fails, "error": type(err).__name__ if err else ""}))
            except OSError:
                pass
        return tripped

    @classmethod
    def degraded(cls):
        with cls._lock:
            return time.monotonic() < cls._bad_until

    @classmethod
    def _reset_for_tests(cls):
        with cls._lock:
            cls._fails, cls._bad_until = 0, 0.0


def _log_tts_error(text, emotion, err):
    try:
        os.makedirs(os.path.dirname(TTS_ERROR_LOG), exist_ok=True)
        with open(TTS_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{emotion}\t{text}\t{err}\n")
    except Exception:
        pass


_SENT_END = "。！？!?~～"
_COMMA = "，,、"


# 第一句超过这个字数就在第一个逗号处切开。tools/bench_tts.py 实测：阈值 16 时首音 P50≈0.9s，
# 阈值 8 时≈0.63s，与 api_v2 的 streaming_mode 首包（≈0.65s）相当，且保留逐句磁盘缓存
FIRST_SPLIT_CHARS = 8


def split_sentences(clean_text, first_comma_threshold=FIRST_SPLIT_CHARS, min_len=2):
    """把已净化的回复切成句子，供逐句合成、逐句播放。
    - 按 。！？~ 切分，标点留在句尾
    - 第一句长于 first_comma_threshold 字时，在第一个逗号处再切一刀，尽早出第一段声音
    - 过短的碎片（<min_len 个汉字/字母）并入前一段
    """
    text = (clean_text or "").strip()
    if not text:
        return []
    parts, cur = [], ""
    for ch in text:
        cur += ch
        if ch in _SENT_END:
            parts.append(cur)
            cur = ""
    if cur.strip():
        parts.append(cur)

    if parts and len(parts[0]) > first_comma_threshold:
        first = parts[0]
        idx = next((i for i, ch in enumerate(first) if ch in _COMMA and i >= 2), -1)
        if 0 < idx < len(first) - 1:
            parts = [first[:idx + 1], first[idx + 1:]] + parts[1:]

    def _content_len(s):
        return len(re.sub(r"[^一-龥A-Za-z0-9]", "", s))

    merged = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if merged and _content_len(p) < min_len:
            merged[-1] += p
        else:
            merged.append(p)
    return merged


def _has_content(s):
    return bool(re.search(r"[一-龥A-Za-z0-9]", s or ""))


STREAM_CLAUSE_CHARS = 6


def stream_segments(pieces):
    """把流式到达的文字切成句子：遇到句末标点就切；第一句超过阈值时在逗号处提前切，
    尽早开始合成。规则与 split_sentences 一致。"""
    buf, first = "", True
    for piece in pieces:
        buf += piece
        while True:
            cut = -1
            for i, ch in enumerate(buf):
                if ch in _SENT_END:
                    cut = i
                    break
                # 第一句遇到逗号就切；之后的句子够 STREAM_CLAUSE_CHARS 字也在逗号处切（每段越短，越早出声）
                if ch in _COMMA and i >= 2 and (first or len(re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", buf[:i])) >= STREAM_CLAUSE_CHARS):
                    cut = i
                    break
            if cut == -1:
                break
            # 句末连续标点（如 “？~”）一并带上
            while cut + 1 < len(buf) and buf[cut + 1] in _SENT_END:
                cut += 1
            seg, buf = buf[:cut + 1], buf[cut + 1:]
            if not _has_content(seg):
                continue  # 只有标点的碎片（如单独的“~”）不单独合成
            first = False
            yield seg
    if _has_content(buf):
        yield buf


class SpeechPipeline:
    """文字预取、逐句合成与顺序播放分开进行；合成开场语时即可开始生成正文。
    两级队列各缓冲两段，避免慢播放时无界生成；TTS 请求仍串行，避免争抢显卡。

    should_stop()   返回 True 时立即停止（ESC / 唤醒词打断）
    on_first_audio  第一段开始播放时回调（用于延迟埋点）
    on_error(text)  合成失败时回调一次（上层负责提示音），之后剩余句子不再播放
    """

    def __init__(self, engine, player=None):
        self.engine = engine
        self.player = player or _SoundDevicePlayer()

    def speak(self, text, emotion="tease", should_stop=None, on_first_audio=None, on_error=None):
        from canaan_query_answers import native_query_speech_parts, QuerySpeechError
        try:
            query = native_query_speech_parts(text)
            if query is None:
                clean = SpeechTextSanitizer.sanitize(text)
            else:
                prefix, parts, tail = query
                cleaned = []
                lost = {SpeechTextSanitizer.sanitize(sample) for sample in
                        ('', '[]', '```x```', 'https://invalid.example', 'C:\\x', 'x@invalid.example')}
                for part in parts:
                    value = SpeechTextSanitizer.sanitize(part)
                    if not _has_content(value) or value in lost:
                        raise QuerySpeechError('A captured query could not be fully sanitized')
                    value = value.rstrip('，,。.!！？?~、 ') + '。'
                    if len(value) > 128:
                        raise QuerySpeechError('A cleaned query exceeds its finite budget')
                    cleaned.append(value)
                if prefix:
                    cleaned[0] = SpeechTextSanitizer.sanitize(prefix) + cleaned[0]
                if tail is not None:
                    value = SpeechTextSanitizer.sanitize(tail)
                    if not _has_content(value) or len(value) > 256:
                        raise QuerySpeechError('Query status could not be sanitized within its ordinary budget')
                    cleaned.append(value)
                clean = ''.join(cleaned)
                if len(clean) > 4096:
                    raise QuerySpeechError('Cleaned query speech exceeds its finite budget')
        except QuerySpeechError:
            if on_error:
                on_error('查询摘要暂时无法完整播报，完整文字已保留。')
            return {"segments": 0, "played": 0, "failed": True, "interrupted": False}
        segments = split_sentences(clean)
        if not segments:
            return {"segments": 0, "played": 0, "failed": False, "interrupted": False}
        return self._run(iter(segments), lambda: emotion, should_stop, on_first_audio, on_error)

    def speak_stream(self, pieces, emotion_fn, should_stop=None, on_first_audio=None, on_error=None,
                     on_segment=None, first_segment=None, on_cancel=None):
        """边生成边说：pieces 是大模型逐块输出的文字，攒够一句就送去合成。
        emotion_fn() 在合成每一句前调用（陪聊的情绪标签在第一句之前就已解析出来）。"""
        from canaan_persona import LATIN_WHITELIST, _KEY_NAME_RE

        def _keep_technical_phrase(match):
            phrase = match.group(0)
            words = re.findall(r"[A-Za-z']+", _KEY_NAME_RE.sub(" ", phrase))
            shortcut = bool(re.match(r"(?:Win|Windows|Ctrl|Control|Alt|Shift)\b", phrase, re.I))
            allowed = all(word.lower() in LATIN_WHITELIST or
                          shortcut and len(word) == 1 and word.isalpha() for word in words)
            return phrase if allowed else ""

        def _segments():
            # 产出 (合成用文本, 显示用文本)；显示在这一句开始播放时才回调，文字与声音同步
            try:
                if first_segment and _has_content(first_segment):
                    # 开场语整段合成（与预热时的缓存键一致），不参与流式切句
                    yield SpeechTextSanitizer.sanitize(first_segment), first_segment
                for seg in stream_segments(pieces):
                    # 小模型偶尔夹带英文短句（“wanna we go?”），连续 2 个以上英文单词的部分不播报
                    seg = re.sub(r"[A-Za-z']+(?:[\s,.!?]+[A-Za-z']+)+[\s,.!?]*", _keep_technical_phrase, seg)
                    clean = SpeechTextSanitizer.sanitize(seg) if _has_content(seg) else ""
                    if not clean:
                        continue
                    yield clean, seg
            finally:
                close = getattr(pieces, "close", None)
                if close:
                    close()
        return self._run(_segments(), emotion_fn, should_stop, on_first_audio, on_error,
                         on_segment=on_segment, on_cancel=on_cancel)

    def _run(self, segments, emotion_fn, should_stop=None, on_first_audio=None, on_error=None, on_segment=None,
             on_cancel=None):
        """segments 产出合成文本，或 (合成文本, 显示文本)；on_segment(显示文本) 在该句开始播放时调用。"""
        import queue as _queue
        should_stop = should_stop or (lambda: False)
        q = _queue.Queue(maxsize=2)
        text_q = _queue.Queue(maxsize=2)
        stop_evt = threading.Event()
        count = {"n": 0}
        streams, streams_lock = [], threading.Lock()

        def _close_stream(stream):
            close = getattr(stream, "close", None)
            if close:
                close()

        def _track_stream(stream):
            with streams_lock:
                stopped = stop_evt.is_set()
                if not stopped:
                    streams.append(stream)
            if stopped:
                _close_stream(stream)
            return not stopped

        def _stopped():
            return stop_evt.is_set() or should_stop()

        def _put(target, item):
            while not _stopped():
                try:
                    target.put(item, timeout=0.05)
                    return True
                except _queue.Full:
                    pass
            return False

        def _get(target):
            while not _stopped():
                try:
                    return target.get(timeout=0.05)
                except _queue.Empty:
                    pass
            return None

        def _read_segments():
            try:
                for item in segments:
                    if not _put(text_q, item):
                        break
            except Exception as error:
                _put(text_q, error)
            finally:
                try:
                    close = getattr(segments, "close", None)
                    if close:
                        close()
                except Exception as error:
                    _put(text_q, error)
                finally:
                    _put(text_q, None)

        use_stream = (hasattr(self.player, "play_stream") and hasattr(self.engine, "open_stream")
                      and getattr(sys.modules.get("canaan_config"), "TTS_STREAM_FIRST", True))
        state = {"streamed": False}

        def _produce():
            seg, shown = "", None
            try:
                while not _stopped():
                    item = _get(text_q)
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    seg, shown = item if isinstance(item, tuple) else (item, None)
                    if _stopped():
                        break
                    count["n"] += 1
                    emo = emotion_fn() or "tease"
                    if use_stream and not state["streamed"]:
                        # 第一段需要现合成的话边收边播（开场语等命中缓存的段落 open_stream 返回 None，照常播缓存）：
                        # 拿到第一个分句的音频就开始放；收完再合成下一段，避免两路请求抢显卡
                        st = self.engine.open_stream(seg, emo)
                        if _stopped():
                            if st is not None:
                                _close_stream(st)
                            break
                        if st is not None:
                            if not _track_stream(st):
                                break
                            state["streamed"] = True
                            deadline = time.monotonic() + 30
                            while not _stopped() and not st.got_audio.wait(timeout=0.05):
                                if time.monotonic() >= deadline:
                                    break
                            if _stopped():
                                break
                            if st.samples > 0:
                                if not _put(q, (seg, st, shown)):
                                    break
                                deadline = time.monotonic() + 60
                                while not _stopped() and not st.done.wait(timeout=0.05):
                                    if time.monotonic() >= deadline:
                                        st.error = TimeoutError("流式合成未完成")
                                        _close_stream(st)
                                        raise TimeoutError("流式合成未完成")
                                if getattr(st, "error", None) is not None:
                                    break  # 播放线程报告已经开播的流错误，不重放正文。
                                continue
                            _close_stream(st)
                            print(f"[!] 流式合成没有返回音频（{st.error}），改用整段合成")
                    if _stopped():
                        break
                    path = self.engine.synthesize_clean(seg, emo)
                    if not _put(q, (seg, path, shown)):
                        break
                    if not path:
                        break
            except Exception as e:
                print(f"[!] 分句生成异常: {e}")
                _put(q, (seg, "", shown))
            finally:
                _put(q, None)

        threading.Thread(target=_read_segments, daemon=True).start()
        threading.Thread(target=_produce, daemon=True).start()

        played, failed, interrupted = 0, False, False
        first_output = False

        def _first_output():
            nonlocal first_output
            if first_output or _stopped():
                return
            first_output = True
            if on_first_audio:
                on_first_audio()

        reports_output = getattr(self.player, "reports_first_output", False)
        try:
            while True:
                item = _get(q)
                if _stopped():
                    interrupted = True
                    break
                if item is None:
                    break
                seg, path, shown = item
                if not path:
                    failed = True
                    if on_segment and shown:
                        on_segment(shown)  # 合成失败也把这句文字显示出来
                    if on_error:
                        on_error(seg)
                    break
                if not reports_output:
                    _first_output()  # Legacy injected players keep their original callback contract.
                if on_segment and shown:
                    on_segment(shown)
                try:
                    play = self.player.play_stream if isinstance(path, TTSStream) else self.player.play
                    ok = (play(path, should_stop, on_first_output=_first_output) if reports_output
                          else play(path, should_stop))
                    if isinstance(path, TTSStream) and getattr(path, "error", None) is not None and not should_stop():
                        raise path.error
                except Exception as e:
                    if should_stop():
                        interrupted = True
                        break
                    print(f"[!] 分句播放失败: {e}")
                    failed = True
                    if on_error:
                        on_error(seg)
                    break
                if not ok:
                    interrupted = True
                    break
                played += 1
        except Exception:
            failed = True
            raise
        finally:
            stop_evt.set()
            if on_cancel is not None and (failed or interrupted or should_stop()):
                try:
                    on_cancel()
                except Exception as error:
                    print(f"[!] 回复取消回调失败: {type(error).__name__}")
            with streams_lock:
                opened = list(streams)
            for stream in opened:
                _close_stream(stream)
        return {"segments": count["n"], "played": played, "failed": failed, "interrupted": interrupted}


class TTSStream:
    """GPT-SoVITS 流式返回（streaming_mode）：后台线程边收边把 PCM 放进队列，收完写入磁盘缓存。
    服务端先合成完第一个分句才发 wav 头和这一分句的音频，所以拿到第一块就能完整播放第一分句。
    采样步数等参数与整段合成完全相同，音质不变。"""

    def __init__(self, resp, clean_text, emotion, out_file):
        import queue as _queue
        self.q = _queue.Queue()
        self.done = threading.Event()
        self.got_audio = threading.Event()
        self.sr = None
        self.samples = 0
        self.error = None
        self._response = resp
        self._cancelled = threading.Event()
        self._close_lock = threading.Lock()
        self.clean_text, self.emotion, self.out_file = clean_text, emotion, out_file
        threading.Thread(target=self._reader, args=(resp,), daemon=True).start()

    def _reader(self, resp):
        import numpy as np
        parts = []
        try:
            with resp:
                header = b""
                while len(header) < 44:
                    if self._cancelled.is_set():
                        return
                    c = resp.read(44 - len(header))
                    if not c:
                        raise RuntimeError("流式返回没有 wav 头")
                    header += c
                try:
                    with wave.open(io.BytesIO(header), "rb") as audio:
                        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or not 8000 <= audio.getframerate() <= 192000:
                            raise ValueError("TTS 流式音频必须是单声道 PCM16 WAV")
                        self.sr = audio.getframerate()
                        declared_samples = audio.getnframes()
                except (wave.Error, EOFError) as error:
                    raise ValueError("TTS 流式返回无效 WAV 头") from error
                left = b""
                while not self._cancelled.is_set():
                    c = resp.read1(32768) if hasattr(resp, "read1") else resp.read(32768)
                    if not c:
                        break
                    if self._cancelled.is_set():
                        return
                    data = left + c
                    n = len(data) // 2 * 2
                    left = data[n:]
                    if n:
                        a = np.frombuffer(data[:n], dtype="<i2").astype(np.float32) / 32768.0
                        parts.append(a)
                        self.samples += len(a)
                        self.q.put(a)
                        self.got_audio.set()
            if self._cancelled.is_set():
                return
            if left:
                raise ValueError("TTS 流式音频含不完整采样帧")
            # GPT-SoVITS uses 0 for an unknown streaming length. A finite WAV
            # length must match; otherwise a truncated response could be cached.
            if declared_samples and self.samples != declared_samples:
                raise ValueError("TTS 流式音频长度与 WAV 声明不符")
            if self.samples < 500:
                raise RuntimeError(f"TTS 流式返回数据过短（{self.samples} 个采样）")
            TuantuanVoiceEngine.last_tts_call = time.time()
            self._save(np.concatenate(parts))
        except Exception as e:
            if not self._cancelled.is_set():
                self.error = e
        finally:
            self.q.put(None)
            self.done.set()
            self.got_audio.set()

    def _save(self, audio):
        """与整段合成相同：修剪尾部静音（保留 0.2 秒）后写入磁盘缓存。"""
        try:
            import numpy as np
            import soundfile as sf
            nz = np.where(np.abs(audio) > SILENCE_LEVEL)[0]
            if len(nz):
                audio = audio[:min(len(audio), nz[-1] + int(self.sr * 0.2))]
            sf.write(self.out_file, audio, self.sr)
            AudioCacheManager.put(self.clean_text, self.emotion, self.out_file)
        except Exception as e:
            print(f"[!] 流式音频写缓存失败: {e}")

    def close(self):
        """Wake playback immediately; HTTP cleanup must not wait on a read lock."""
        with self._close_lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            self.q.put(None)
            self.got_audio.set()
            if self.done.is_set():
                return

        def release_response():
            try:
                self._response.close()
            except Exception:
                pass

        threading.Thread(target=release_response, name="canaan-tts-close", daemon=True).start()

    def chunks(self, should_stop=None):
        import queue as _queue
        while not self._cancelled.is_set():
            if should_stop and should_stop():
                self.close()
                return
            try:
                a = self.q.get(timeout=.05)
            except _queue.Empty:
                continue
            if a is None:
                if self.error is not None:
                    raise self.error
                return
            yield a


SILENCE_LEVEL = 0.008  # 低于这个幅度算静音（与整段合成后修剪尾部静音的阈值一致）


def _ref_push(samples, sr):
    """把正在播放的声音交给回声消除做参考（说话时喊“团团”打断用）；模块不可用时静默跳过。"""
    try:
        import canaan_aec
        canaan_aec.REF.push(samples, sr)
    except Exception:
        pass


class _SoundDevicePlayer:
    """阻塞播放一个 wav，播放中轮询 should_stop 实现打断。返回 False 表示被打断。"""

    reports_first_output = True

    def __init__(self):
        self._output_lock = threading.Lock()

    @property
    def output_busy(self):
        """Output ownership includes device drain/close, regardless of session replacement."""
        return self._output_lock.locked()

    def _claim_output(self, should_stop):
        # Old device drain/close may still be in progress after a UI stop.
        # A queued response stays cancellable without opening a second output.
        while not should_stop():
            if self._output_lock.acquire(timeout=.05):
                try:
                    stopped = should_stop()
                except BaseException:
                    self._output_lock.release()
                    raise
                if stopped:
                    self._output_lock.release()
                    return False
                return True
        return False

    def play_stream(self, stream, should_stop, on_first_output=None):
        if not self._claim_output(should_stop):
            stream.close()
            return False
        try:
            return self._play_stream(stream, should_stop, on_first_output)
        finally:
            self._output_lock.release()

    def play(self, path, should_stop, on_first_output=None):
        if not self._claim_output(should_stop):
            return False
        try:
            return self._play(path, should_stop, on_first_output)
        finally:
            self._output_lock.release()

    def _play_stream(self, stream, should_stop, on_first_output=None):
        """边收边播（TTSStream）。尾部静音按整段合成同样的规则处理：分句之间的停顿照常播，
        整段结尾只保留 0.2 秒。返回 False 表示被打断。"""
        import numpy as np
        import sounddevice as sd
        sr = stream.sr
        block = max(1, int(sr * 0.05))
        out = sd.OutputStream(samplerate=sr, channels=1, dtype="float32")
        pending = np.zeros(0, np.float32)
        first_output = False

        def _write(x):
            nonlocal first_output
            for i in range(0, len(x), block):
                if should_stop():
                    return False
                piece = np.ascontiguousarray(x[i:i + block], dtype=np.float32)
                _ref_push(piece, sr)  # Register before write: the device may emit audio while write blocks.
                out.write(piece.reshape(-1, 1))
                if (not first_output and on_first_output and not should_stop()
                        and np.any(np.abs(piece) > SILENCE_LEVEL)):
                    first_output = True
                    on_first_output()
            return True

        try:
            out.start()
            for chunk in stream.chunks(should_stop):
                if should_stop():
                    out.abort()
                    return False
                buf = np.concatenate([pending, chunk])
                nz = np.where(np.abs(buf) > SILENCE_LEVEL)[0]
                if len(nz) == 0:
                    pending = buf
                    continue
                cut = nz[-1] + 1
                if not _write(buf[:cut]):
                    out.abort()
                    return False
                pending = buf[cut:]
            if should_stop():
                out.abort()
                return False
            if stream.error is not None:
                raise stream.error
            if not _write(pending[:int(sr * 0.2)]):
                out.abort()
                return False
            out.stop()  # 等缓冲里的声音播完
            return True
        except Exception as e:
            try:
                out.abort()
            except Exception:
                pass
            if should_stop():
                return False
            print(f"[!] 流式播放异常: {e}")
            raise
        finally:
            try:
                stream.close()
            finally:
                try:
                    out.close()
                except Exception:
                    pass

    def _play(self, path, should_stop, on_first_output=None):
        import numpy as np
        import sounddevice as sd
        import soundfile as sf
        data, fs = sf.read(path, dtype="float32")
        channels = 1 if data.ndim == 1 else data.shape[1]
        block = max(1, int(fs * .05))
        out = sd.OutputStream(samplerate=fs, channels=channels, dtype="float32")
        first_output = False
        try:
            out.start()
            for i in range(0, len(data), block):
                if should_stop():
                    out.abort()
                    return False
                piece = np.ascontiguousarray(data[i:i + block], dtype=np.float32)
                _ref_push(piece if piece.ndim == 1 else piece.mean(axis=1), fs)
                out.write(piece.reshape(-1, channels))
                if (not first_output and on_first_output and not should_stop()
                        and np.any(np.abs(piece) > SILENCE_LEVEL)):
                    first_output = True
                    on_first_output()
            if should_stop():
                out.abort()
                return False
            out.stop()
            return True
        except Exception:
            try:
                out.abort()
            except Exception:
                pass
            if should_stop():
                return False
            raise
        finally:
            try:
                out.close()
            except Exception:
                pass


class TuantuanVoiceEngine:
    def __init__(self):
        self.pipeline = SpeechPipeline(self)

    def speak(self, text, emotion="tease", should_stop=None, on_first_audio=None, on_error=None):
        return self.pipeline.speak(text, emotion, should_stop, on_first_audio, on_error)

    def prewarm(self, phrases):
        """预合成常用短句（按播放时同样的切句方式缓存每一段）。phrases: [(文本, 情绪)]"""
        ok = 0
        for text, emotion in phrases:
            for seg in split_sentences(SpeechTextSanitizer.sanitize(text)):
                if self.synthesize_clean(seg, emotion):
                    ok += 1
        return ok

    def check_native_soundboard(self, user_text):
        for item in NATIVE_SOUNDBOARD:
            if any(k in user_text for k in item["keywords"]):
                if os.path.exists(item["audio"]):
                    return item
        return None

    def query_llm(self, user_text, history):
        messages = [{"role": "system", "content": DEEP_SYSTEM_PROMPT}]
        for m in history[-6:]:
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": user_text})

        payload = {
            "model": "canaan-desk:7b",
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.72,
                "top_p": 0.9
            }
        }
        try:
            req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                raw_reply = data["message"]["content"].strip()
        except Exception:
            payload["model"] = getattr(sys.modules.get("canaan_config"), "CHAT_MODEL", "canaan-chat:3b")   # 10-26：不再退到 7B（会把 3B 挤出显存）
            req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                raw_reply = data["message"]["content"].strip()

        # Parse emotion tag
        match = re.match(r"^\[(tease|pamper|haughty|confident|dangerous)\]\s*(.*)", raw_reply, re.DOTALL)
        if match:
            emotion = match.group(1)
            clean_reply = match.group(2).strip()
        else:
            emotion = "tease"
            clean_reply = raw_reply

        # Fallback keyword emotion check if not matched
        if any(w in clean_reply for w in ["姐姐", "乖", "偷懒", "肩膀", "可爱", "辛苦"]):
            emotion = "pamper"
        elif any(w in clean_reply for w in ["心跳", "停止", "代价", "胆量", "猎物"]):
            emotion = "dangerous"
        elif any(w in clean_reply for w in ["掌控", "放心", "包在", "性能", "稳定"]):
            emotion = "confident"
        elif any(w in clean_reply for w in ["急什么", "耐心", "开场", "收拾"]):
            emotion = "haughty"

        return emotion, clean_reply

    @classmethod
    def ensure_service_running(cls):
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                if s.connect_ex(('127.0.0.1', 9880)) == 0:
                    return True
        except Exception:
            pass

        if os.environ.get("CANAAN_SUPERVISED") == "1":
            # 托盘监管程序（canaan_tray.py）负责拉起/重启语音服务，这里不重复启动
            return False

        print("[*] 正在极速拉起 GPT-SoVITS 团团专属原声引擎 (端口 9880)...")
        gpt_dir = GPT_SOVITS_DIR
        py_exe = os.path.join(gpt_dir, "runtime", "python.exe")
        if not os.path.exists(py_exe):
            py_exe = "python"
        
        cfg_arg = os.path.join(BASE_DIR, "tuantuan_tts_infer.yaml")
        if not os.path.exists(cfg_arg):
            cfg_arg = "GPT_SoVITS/configs/tts_infer.yaml"
        
        try:
            import subprocess
            from canaan_tts_launch import tts_command
            flags = (0x00000008 | 0x00000200) if sys.platform == "win32" else 0
            env = os.environ.copy()
            env["PATH"] = gpt_dir + ";" + os.path.join(gpt_dir, "runtime") + ";" + env.get("PATH", "")
            subprocess.Popen(
                tts_command(gpt_dir, cfg_arg, python_exe=py_exe),
                cwd=gpt_dir,
                env=env,
                creationflags=flags
            )
            # 等待服务端口就绪
            for _ in range(25):
                time.sleep(1.0)
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(1.0)
                        if s.connect_ex(('127.0.0.1', 9880)) == 0:
                            print("[✓] GPT-SoVITS 团团原声引擎已成功启动就绪！")
                            return True
                except Exception:
                    pass
        except Exception as e:
            print(f"[!] 启动 GPT-SoVITS 失败: {e}")
        return False

    @staticmethod
    def is_service_up(timeout=0.05):
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                return s.connect_ex(('127.0.0.1', 9880)) == 0
        except Exception:
            return False

    _starting_service = False

    @classmethod
    def start_service_background(cls):
        """TTS 服务没起来时在后台拉起，不阻塞当前这一轮对话。"""
        if cls._starting_service:
            return
        cls._starting_service = True

        def _run():
            try:
                cls.ensure_service_running()
            finally:
                cls._starting_service = False

        import threading
        threading.Thread(target=_run, daemon=True).start()

    def synthesize_speech(self, text, emotion="tease"):
        """整段合成（先净化文本）。成功返回 (wav 路径, 情绪名)；失败返回 ("", 情绪名)，由调用方决定如何提示。"""
        cfg = EMOTION_CONFIG.get(emotion, EMOTION_CONFIG["tease"])
        clean_text = SpeechTextSanitizer.sanitize(text)
        return self.synthesize_clean(clean_text, emotion), cfg["title"]

    last_tts_call = 0.0

    def warm_up(self, text="你好，我在呢，今天想做点什么？"):
        """真正跑一次 GPT-SoVITS 推理（不走缓存、不保存）。启动时常用语都命中磁盘缓存，GPU 从没真正推理过，
        第一句新话要多花 10 秒左右；空闲很久后也会变慢。返回耗时毫秒，失败返回 None。"""
        if not self.is_service_up(timeout=0.5):
            return None
        cfg = EMOTION_CONFIG["pamper"]
        payload = {"text": text, "text_lang": "zh", "ref_audio_path": cfg["ref"], "prompt_text": cfg["prompt"],
                   "prompt_lang": "zh", "top_k": 8, "top_p": 0.85, "temperature": cfg.get("temp", 0.65),
                   "text_split_method": "cut5", "speed_factor": cfg.get("speed", 1.0), "repetition_penalty": 1.15}
        t0 = time.time()
        try:
            req = urllib.request.Request(TTS_URL, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
            _validate_wav(data)
        except Exception as e:
            print(f"[!] 语音预热失败: {e}")
            # 服务在听却回错误（不是超时）：算一次合成失败，空闲时也能让托盘及早重启坏掉的语音服务
            if isinstance(e, (urllib.error.HTTPError, ValueError)):
                TTSHealth.record_fail(e)
            return None
        TTSHealth.record_ok()
        TuantuanVoiceEngine.last_tts_call = time.time()
        return (time.time() - t0) * 1000

    @staticmethod
    def _cpu_fallback(clean_text):
        """CPU 系统语音（不缓存，音色与团团不同）；配置关闭或不可用时返回 ""（只显示文字）。"""
        if not getattr(sys.modules.get("canaan_config"), "TTS_CPU_FALLBACK", True):
            return ""
        import canaan_cpu_tts
        return canaan_cpu_tts.synthesize(clean_text)

    def synthesize_clean(self, clean_text, emotion="tease"):
        """合成已净化的一段文本，带磁盘缓存。失败返回 ""，绝不拿情绪参考音频顶替
        （参考音频是另一句台词，播出来会答非所问）。"""
        cfg = EMOTION_CONFIG.get(emotion, EMOTION_CONFIG["tease"])

        # 1. 命中音频缓存
        cached_wav = AudioCacheManager.get(clean_text, emotion)
        if cached_wav:
            return cached_wav

        # 2. 游戏模式：显卡让给游戏，用 CPU 的系统语音（不拉起 GPT-SoVITS）
        import canaan_gamemode
        if canaan_gamemode.active() or getattr(sys.modules.get("canaan_config"), "TTS_MODE", "gpu") == "cpu":
            return self._cpu_fallback(clean_text)

        # 3. 服务未监听：后台拉起，本句用 CPU 语音兜底（不在对话中阻塞等待 25 秒）
        if not self.is_service_up():
            print("[!] TTS 服务 (9880) 未监听，后台尝试拉起；本句改用系统语音")
            self.start_service_background()
            return self._cpu_fallback(clean_text)

        # 4. 服务活着但刚连续合成失败（显卡出错等）：先用系统语音，等托盘重启语音服务
        if TTSHealth.degraded():
            return self._cpu_fallback(clean_text)

        payload = {
            "text": clean_text,
            "text_lang": "zh",
            "ref_audio_path": cfg["ref"],
            "prompt_text": cfg["prompt"],
            "prompt_lang": "zh",
            "top_k": 8,
            "top_p": 0.85,
            "temperature": cfg.get("temp", 0.65),
            "text_split_method": "cut5",
            "speed_factor": cfg.get("speed", 1.0),
            "repetition_penalty": 1.15
        }
        req = urllib.request.Request(TTS_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        
        # 自动轮转历史临时回复音频，保留最近15条
        try:
            import glob
            old_replies = sorted(glob.glob(os.path.join(VOICEPACK_DIR, "reply_*.wav")), key=os.path.getmtime)
            for old_r in old_replies[:-15]:
                try:
                    os.remove(old_r)
                except Exception:
                    pass
        except Exception:
            pass

        out_file = os.path.join(VOICEPACK_DIR, f"reply_{int(time.time()*1000)}_{threading.get_ident() % 10000}.wav")
        last_err = None
        for retry in range(2):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                if len(data) < 1000:
                    raise RuntimeError(f"TTS 返回数据过短 ({len(data)} 字节)")
                _validate_wav(data)
                TuantuanVoiceEngine.last_tts_call = time.time()
                with open(out_file, "wb") as f:
                    f.write(data)

                # 修剪尾部冗余静音，分句播放时句间停顿更自然
                try:
                    import numpy as np
                    import soundfile as sf
                    d_audio, fs_audio = sf.read(out_file)
                    non_silent = np.where(np.abs(d_audio) > 0.008)[0]
                    if len(non_silent) > 0:
                        end_idx = min(len(d_audio), non_silent[-1] + int(fs_audio * 0.2))
                        sf.write(out_file, d_audio[:end_idx], fs_audio)
                except Exception:
                    pass

                AudioCacheManager.put(clean_text, emotion, out_file)
                TTSHealth.record_ok()
                return out_file
            except Exception as e:
                last_err = e
                print(f"[!] TTS 合成失败 ({retry+1}/2): {e}")
                # 配置/输入错误重复请求不会恢复；超时后再等完整一轮会拖住回答。
                timed_out = isinstance(e, TimeoutError) or (
                    isinstance(e, urllib.error.URLError) and isinstance(e.reason, TimeoutError))
                invalid_request = isinstance(e, urllib.error.HTTPError) and e.code in {400, 401, 403, 404, 405, 415, 422}
                if timed_out or invalid_request or isinstance(e, ValueError):
                    break
                if not self.is_service_up():
                    break

        # 合成失败：记一次失败（连续几次就让托盘重启语音服务），这句改用系统语音，不让主人只看到字听不到声音
        _log_tts_error(clean_text, emotion, last_err)
        if TTSHealth.record_fail(last_err):
            print("[!] 语音服务连续合成失败，先用系统语音，已请托盘重启语音服务")
        return self._cpu_fallback(clean_text)

    def open_stream(self, clean_text, emotion="tease"):
        """对没有缓存的句子发起流式合成，返回 TTSStream；命中缓存、游戏模式/CPU 语音、服务未开或请求失败时返回 None
        （调用方改用 synthesize_clean，行为与原来一致）。参数与整段合成相同（采样步数不变，音质不降）。"""
        if AudioCacheManager.get(clean_text, emotion):
            return None
        import canaan_gamemode
        if canaan_gamemode.active() or getattr(sys.modules.get("canaan_config"), "TTS_MODE", "gpu") == "cpu":
            return None
        if not self.is_service_up() or TTSHealth.degraded():
            return None
        cfg = EMOTION_CONFIG.get(emotion, EMOTION_CONFIG["tease"])
        payload = {"text": clean_text, "text_lang": "zh", "ref_audio_path": cfg["ref"], "prompt_text": cfg["prompt"],
                   "prompt_lang": "zh", "top_k": 8, "top_p": 0.85, "temperature": cfg.get("temp", 0.65),
                   "text_split_method": "cut5", "speed_factor": cfg.get("speed", 1.0), "repetition_penalty": 1.15,
                   "streaming_mode": True}
        out_file = os.path.join(VOICEPACK_DIR, f"reply_{int(time.time()*1000)}_{threading.get_ident() % 10000}.wav")
        try:
            req = urllib.request.Request(TTS_URL, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            print(f"[!] 流式合成请求失败，改用整段合成: {e}")
            return None
        return TTSStream(resp, clean_text, emotion, out_file)

    def interact(self, user_text, history):
        # 1. Check native soundboard
        native = self.check_native_soundboard(user_text)
        if native and os.path.exists(native["audio"]):
            return {
                "text": native["text"],
                "emotion": native["emotion"],
                "emotion_title": EMOTION_CONFIG[native["emotion"]]["title"],
                "audio_path": native["audio"],
                "is_native": True
            }

        # 2. LLM with Deep Persona
        emotion, reply_text = self.query_llm(user_text, history)

        # 3. Dynamic Emotional Voice Synthesis
        audio_path, emotion_title = self.synthesize_speech(reply_text, emotion)

        return {
            "text": reply_text,
            "emotion": emotion,
            "emotion_title": emotion_title,
            "audio_path": audio_path,
            "is_native": False
        }

if __name__ == "__main__":
    engine = TuantuanVoiceEngine()
    test_q = "团团，今天我们要去哪探险？"
    print(f"[*] 测试输入: {test_q}")
    res = engine.interact(test_q, [])
    print(f"[+] 情绪: {res['emotion_title']}")
    print(f"[+] 团团回复: {res['text']}")
    print(f"[+] 语音输出: {res['audio_path']}")
