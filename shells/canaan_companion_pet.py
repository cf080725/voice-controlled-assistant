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
Canaan Desk OS - Interactive Q-Version Desktop Companion Pet (Silky-Smooth 60FPS Fluid Edition)
Inspired by top open-source GitHub projects:
- LorisYounger/VPet: 72-state FSM, gravity physics, collar-drag pendulum, affection, feeding, minigames
- Open-LLM-Vtuber: Decoupled speech lip-sync, dynamic speech bubble, emotional state transitions
- ChaozhongLiu/DyberPet: Sedentary/hydration care, taskbar walking, focus work companion
- BongoCat / AgentPet: Keyboard typing reactivity, shake-to-dizzy physics, squash & stretch
- SeakMengs/WindowPet: Frameless transparent overlay with zero GPU VRAM overhead
"""
import os
import sys
import threading
import math
import random
import time
import json
import ctypes
from ctypes import wintypes
import tkinter as tk
from collections import OrderedDict
from PIL import Image, ImageTk
from canaan_desktop_geometry import work_areas, place, geometry
import canaan_motion as motion

# 语音与对口型（后台线程合成播放；语音模块不可用时伴侣照常运行，只显示气泡）
try:
    from canaan_pet_voice import PetVoice, make_mouth_frames
except Exception as _voice_err:  # noqa: F841
    PetVoice = None
    make_mouth_frames = None

try:
    from canaan_pet_chat import PetChat
except Exception as _chat_err:  # noqa: F841
    PetChat = None

# 对话：气泡打字机速度（字/秒）、每行字数、最多显示几行（流式时只显示最新几行）
TYPE_CPS = 18.0
BUBBLE_LINE_CHARS = 12
CHAT_BUBBLE_LINES = 4
# 动作标签 → 语音语气（团团语音的 5 种情绪参考音频）
POSE_TO_VOICE = {"pout": "haughty", "tsundere": "haughty", "blade": "dangerous", "action": "dangerous",
                 "cheer": "tease", "dance": "tease", "shock": "tease"}

try:
    from canaan_pet_needs import PetNeeds
except Exception as _needs_err:  # noqa: F841
    PetNeeds = None

# 养成提醒：提醒种类 → (可选姿态, 台词)
NEED_ALERTS = {
    "hungry": (["hug_screen"], "亲爱的，团团肚子咕咕叫了，投喂我一块点心嘛~ 🥟"),
    "sad": (["tsundere", "crying"], "哼……好久都不理我，人家才没有在等你呢……"),
    "tired": (["yawn", "sleepy_nod"], "连续忙了好久啦，我都困了……主人也该起来活动一下、休息休息了。"),
}

try:
    import canaan_pet_briefing as briefing
except Exception as _brief_err:  # noqa: F841
    briefing = None

try:
    from canaan_pet_sense import SenseWatcher
except Exception as _sense_err:  # noqa: F841
    SenseWatcher = None

# 桌面感知：可以被环境自动切换的“闲着”状态；环境姿态；同一个敏感窗口多久内只提醒一次；打字姿态至少保持多久
AMBIENT_OK_STATES = {"IDLE", "BLINK", "SLEEP", "WORK", "TYPING", "MUSIC_HEADSET"}
AMBIENT_POSES = {"code": "WORK", "music": "MUSIC_HEADSET"}
PRIVACY_WARN_COOLDOWN = 120.0
TYPING_HOLD_SEC = 1.5

# 说话时用口型帧的姿态（其余特殊姿态说话时保持原姿态，只随音节轻微弹动）
TALK_LIPSYNC_STATES = {"IDLE", "BLINK", "TALK", "SLEEP"}
MOUTH_FRAMES = {0: "talk_closed", 1: "talk_half", 2: "talk"}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets", "avatar")
CONFIG_POS_FILE = os.path.join(BASE_DIR, "data", "pet_position.json")
CONFIG_STATS_FILE = os.path.join(BASE_DIR, "data", "canaan_pet_stats.json")

# Voice lines libraries for all 72 states
VOICE_LINES = {
    # 基础交互 1-6
    "pat": [
        "嘻嘻，头发出油了吗？别乱揉嘛~", "亲爱的，今天本小姐陪你一起办公哦！",
        "只要你在，团团的飞索就有了归宿~ 💖", "唔... 摸得好舒服，好感度悄悄上升啦！"
    ],
    "talk": [
        "有什么烦恼，尽管和团团说说吧！", "本小姐今天可是状态拉满，随时待命！✨",
        "亲爱的，记得每工作一小时起来活动活动哦~"
    ],
    "blink": [
        "(眨巴眨巴灵动大眼睛~)", "嘻嘻，被本小姐的美貌吸引住了吗？😉"
    ],
    "shy": [
        "唔... 别这样盯着看人家嘛，脸都要红透了啦！😳", "呀！被你夸得怪不好意思的~ 💓"
    ],
    "drag": [
        "哇哇哇！放开我啦！小短腿在空中扑腾好丢人呀！🦵", "快放下快放下，裙子都要被吹乱了！"
    ],
    "sleep": [
        "呼……呼……西域的星光真美呀……Zzz... 🌙", "小憩片刻，养精蓄锐……不要吵醒本小姐哦……"
    ],

    # 伴工关怀 7-12
    "work": [
        "啪嗒啪嗒敲代码！本小姐是你的专属AI架构师~ 💻", "高强度伴工中，这一轮单元测试一定能全绿！"
    ],
    "tea": [
        "来，喝杯温热的红茶润润嗓子~ 🍵", "工作虽忙，健康第一，记得多喝水哦亲爱的！"
    ],
    "action": [
        "双匕首拔出！敏感窗口已被隐私护盾牢牢封锁，万无一失！🗡️", "谁敢窥探？本小姐的飞索绝不留情！"
    ],
    "cheer": [
        "太棒啦！搞定收工，亲爱的真厉害！🎉", "任务顺利完成，本小姐给你比个大大的心！🫶"
    ],
    "pout": [
        "哼~ 别一直狂戳啦，再戳团团就要鼓成包子脸咯！💢", "呜哇！不理你了，除非你投喂一块小糕点！"
    ],
    "walk": [
        "任务栏漫步巡逻中~ 巡视一下亲爱的桌面领地！🚶‍♀️", "走两步，身姿轻盈如风~"
    ],

    # 趣味拓展 13-18
    "boba": [
        "吸溜吸溜~ 嚼珍珠的感觉好满足呀！🧋", "大杯全糖珍珠奶茶，甜到心里去啦~"
    ],
    "gift": [
        "当当当！特意为你留的神秘心意礼盒，快打开看看！🎁", "愿亲爱的一整天都好运连连~ 💖"
    ],
    "audit": [
        "拿出放大镜！每一行日志和财报都要精细核对，零差错！🔍", "侦探模式开启，任何隐蔽bug都逃不过法眼！"
    ],
    "game": [
        "劳逸结合！盘腿搓手柄，今天要在天人城大杀四方！🎮", "连招接大招！看本小姐的丝滑操作~"
    ],
    "stealth": [
        "寂静暗刑——金沙狂暴！隐匿身形，为你扫平一切障碍！🌪️", "风沙掩护下，神出鬼没！"
    ],
    "climb": [
        "飞索命中屏幕边缘！看我轻松挂壁飞檐走壁！🧗‍♀️", "高处视野开阔，俯瞰整个桌面战场！"
    ],

    # 打工商务 19-24
    "typing": [
        "哒哒哒哒！疯狂码字速度突破 120 WPM！⌨️", "敲击键盘的节奏太上头啦，灵感如泉涌！"
    ],
    "phone": [
        "喂您好？我是团团商务代表，请问有何合作意向？📞", "正在沟通高优先级需求，请稍候~"
    ],
    "coffee": [
        "热美式提神醒脑，今天也要和亲爱的一起高效冲刺！☕", "来一杯香浓咖啡，困意全消啦~"
    ],
    "money": [
        "哇！金灿灿的大元宝！招财进宝，祝亲爱的财运亨通！💰", "数金币数到手抽筋，商业版图大扩张！"
    ],
    "reading": [
        "佩戴金丝眼镜~ 正在翻阅最新商业与前沿AI论文！📖", "腹有诗书气自华，本小姐不仅会打架，还很懂谋略！"
    ],
    "chart": [
        "请看业绩折线大屏！环比大涨 300%，蒸蒸日上！📈", "数据向好，亲爱的决策简直太英明了！"
    ],

    # 情绪互动 25-30
    "dizzy": [
        "哎呀呀~ 晃得团团头晕目眩，小星星在眼前转圈圈啦！💫", "转得太快啦，容本小姐扶墙歇歇~"
    ],
    "crying": [
        "呜呜呜... 刚才出了点小意外，抱抱团团嘛~ 🥺", "眼泪止不住啦，需要亲爱的一颗奶糖安慰~"
    ],
    "shock": [
        "天呐！这数据太惊人了，下巴都要掉到地上了！😱", "不可思议！亲爱的竟然达成了这么高的成就！"
    ],
    "dance": [
        "欢快摇摆~ 跟着西域节拍跳起曼妙的沙漠之舞！💃", "叮叮当当，金铃作响，好心情就要舞动起来！"
    ],
    "eating": [
        "大口啃烤肉！西域秘制孜然香喷喷，超级大满足！🍖", "好香好脆呀，吃饱了才有力气当刺客守护你！"
    ],
    "yawn": [
        "嗷呜~ 伸个长长的懒腰打个大哈欠……有点犯困啦~ 🥱", "眼皮在打架，但本小姐还想再多陪你一会儿……"
    ],

    # 绝技四季 31-36
    "grapple": [
        "飞索凌空破空飞跃！咻——！精准命中目标！🏹", "穿梭自如，身姿如燕！"
    ],
    "umbrella": [
        "外面下雨了吗？为你撑起这把小花伞，风雨无阻~ ☂️", "晴天遮阳，雨天避雨，有团团在绝不淋湿！"
    ],
    "swim": [
        "套上可爱小黄鸭泳圈！夏天就是要去沙漠绿洲清凉戏水！🦆", "扑腾扑腾，冰冰凉凉好舒服呀！"
    ],
    "scarf": [
        "天冷啦，围上暖和的红围巾，搓搓小手给你哈气取暖~ 🧣", "冬天虽冷，但有亲爱的在身边，心里暖烘洋洋的！"
    ],
    "mask": [
        "戴上金丝神秘面纱，顶级刺客出任务，闲人勿近~ 🧕", "若隐若现的神秘，才是西域女刺客的魅力~"
    ],
    "crown": [
        "加冕为王！戴上女皇王冠，手握权杖，你是我唯一的誓约！👑", "君临巅峰，四海臣服，感谢你一路的守护！"
    ],

    # 武学 37-42
    "parry": [
        "当——！金光振刀！想偷袭本小姐？没门！🛡️", "完美格挡，反手就是一套致命反击！✨"
    ],
    "blade": [
        "太刀蓄力百裂斩！剑气如虹，阻挡在前方的bug统统斩断！⚔️", "这记百裂，可还顺眼？"
    ],
    "spear": [
        "长枪龙王破！一点寒芒先到，随后枪出如龙！🐉", "直捣黄龙，任务难关瞬间穿透！"
    ],
    "slide": [
        "地面潇洒滑铲——咻！闪避障碍，极速奔向你！💨", "滑铲赶路，身法轻盈如沙丘掠影~"
    ],
    "crouch": [
        "嘘……暗杀潜行中，屏住呼吸，暗中观察哦~ 🤫", "蹲伏在屋檐上，任何风吹草动都逃不过我的眼睛！"
    ],
    "resurrect": [
        "返魂回煞！涅槃重生，本小姐满血归来啦！🔥", "金沙重聚，越挫越勇才是顶级刺客！"
    ],

    # 现代职场百态 43-48
    "mask_spa": [
        "熬夜爆肝敷眼膜，沙漠明珠也要好好保养呢~ 🧖‍♀️", "贴贴眼膜，黑眼圈统统退散！"
    ],
    "headset": [
        "喂喂，麦克风测试中，本小姐准备上线主讲跨国商务会啦！🎧", "线上视频会议开始，请各位准备好汇报！"
    ],
    "takeout": [
        "开饭啦！热腾腾的小笼包，咬一口汤汁爆棚好治愈~ 🥟", "外卖到了！快放手里的活，先好好干饭！"
    ],
    "coffee_spill": [
        "唔噗！冰美式怎么这么苦哇，苦得本小姐舌头打结！😖", "呜……被苦到了，要亲爱的夸一句才能甜回来！"
    ],
    "overtime": [
        "绑上必胜头巾！死线将至，今晚本小姐陪你一起冲刺到底！🔥", "全力以赴，不破楼兰终不还！"
    ],
    "headache": [
        "啊啊啊！方案又改第十版？本小姐头都要炸裂啦！🤯", "抓狂抓狂！容我先深呼吸三秒钟……呼！"
    ],

    # 深度生活温馨 49-54
    "cat": [
        "小猫咪呼噜呼噜~ 软乎乎的小肉垫太治愈啦！🐱", "嘘，别吵醒怀里的小猫，一起享受温柔时光~"
    ],
    "bubble_gum": [
        "吹一个大大的泡泡糖……啪！哎呀粘在鼻尖上了啦！🫧", "甜甜的葡萄味泡泡糖，分你一个呀~"
    ],
    "fitness": [
        "一二三四，举哑铃练线条！做刺客不仅要敏捷，还要力量！💪", "运动出汗真畅快，亲爱的也要起来活动一下筋骨哦！"
    ],
    "music_headset": [
        "沉浸在节奏律动里，跟着低音节拍轻轻晃动~ 🎵", "音乐是沙漠旅途最好的慰藉，一起来听这首歌！"
    ],
    "watering": [
        "给桌面多肉喷喷水~ 绿油油的生长茁壮，心情也变好啦！🌱", "细水长流，就像本小姐对亲爱的陪伴一样~"
    ],
    "selfie": [
        "看镜头，三、二、一，咔嚓！比个耶，把这个瞬间永远珍藏！📸", "今天的自拍美美哒，存进我们专属相册！"
    ],

    # 传统节日欢庆 55-60
    "firecracker": [
        "噼里啪啦！捂住耳朵点爆竹，红红火火新年到！🧨", "岁岁平安，万事顺意，金币滚滚来！"
    ],
    "zongzi": [
        "清香粽叶裹着大肉粽，咸蛋黄糯叽叽，太美味啦！🌿", "端午安康！本小姐亲手包的粽子送给你！"
    ],
    "mooncake": [
        "坐在弯弯月牙上啃月饼，千里共婵娟，有你才圆满~ 🥮", "月圆之夜，西域的月光也洒在你身边~"
    ],
    "pumpkin": [
        "万圣奇妙夜！提着南瓜小桔灯，不给糖就捣蛋哦！🎃", "嘻嘻，今晚本小姐是魅影小魔女~"
    ],
    "dumpling": [
        "冬至到，热腾腾水饺出锅！皮薄馅大热气腾腾！🥟", "吃下这碗热饺子，一整个冬天都不冻耳朵~"
    ],
    "lantern": [
        "元宵佳节，挑起玉兔琉璃花灯，灯火阑珊处只看你~ 🏮", "花市灯如昼，愿年年岁岁皆胜意！"
    ],

    # 趣味傲娇彩蛋 61-66
    "tsundere": [
        "哼！才不是因为想你才转过来的呢……别自作多情啦！😤", "抱胸扭头！明明是你先冷落本小姐的好不好！"
    ],
    "heart_eyes": [
        "哇啊啊！双眼冒大红心星！亲爱的刚才那一瞬间帅呆啦！😍", "好喜欢好喜欢！心跳砰砰快要跳出来啦！💖"
    ],
    "worship": [
        "膜拜！给大佬跪下了！这行云流水的代码简直神仙手笔！🙇‍♀️", "太强了吧！本小姐对你的敬仰如滔滔江水连绵不绝！"
    ],
    "detective": [
        "戴上猎鹿帽，叼起烟斗~ 真相只有一个，任何bug逃不过法眼！🕵️‍♀️", "蛛丝马迹全在掌控之中，案件侦破！"
    ],
    "hug_screen": [
        "贴贴玻璃！想穿过屏幕抱抱你，感受你的温度~ 🫂", "双手贴在屏幕前，给亲爱的一个隔空超大抱抱！"
    ],
    "box_hide": [
        "纸箱特工出击！只露出一双眼睛，暗中观察亲爱的在干嘛~ 📦", "你看不见我、你看不见我……噗嗤！"
    ],

    # 终极幻化 67-72
    "wedding": [
        "红绡曳地，金凤衔珠。此生遇君，纵风沙万里亦无悔……👰", "西域绝色红嫁衣，只为等待与你立下终生誓约之名。💍"
    ],
    "angel_wings": [
        "璀璨金色光翼展开！无论风雨险阻，本小姐永恒庇护你的安宁。🪽", "光羽倾落，为你驱散一切疲惫与阴霾。✨"
    ],
    "magician": [
        "当当！神奇魔术时间~ 礼帽里飞出和平白鸽，惊喜连连！🎩🕊️", "见证奇迹的时刻！本小姐的魔法只为你绽放！"
    ],
    "motorcycle": [
        "轰鸣引擎咆哮！机车女骑极速飞驰，带你冲破所有死线绝境！🏍️💨", "戴好头盔抓紧我，去追逐沙漠落日的风！"
    ],
    "sleepy_nod": [
        "小鸡啄米……呼……困得眼睛睁不开，但还是舍不得去睡嘛……💤", "小脑袋一点一点……亲爱的，我只眯五分钟哦……"
    ],
    "rainbow_star": [
        "双手捧起璀璨七彩许愿星！愿你心想事成，愿我们永不分离！⭐🌈", "七彩星光闪耀，72态大圆满终极达成，你是最好的奇迹！✨"
    ]
}

NOTICE_POSES = {"chat": "phone", "p0": "shock", "progress": "typing", "done": "cheer", "failed": "headache",
                "waiting": "audit", "summary": "reading", "info": None}


def notice_reaction(notice, missed=0):
    """提示 → (桌宠姿态, 气泡里的一句话)。标题已经是提示中心脱敏、截短过的；聊天只有来源和条数。纯函数，方便测试。"""
    v = notice.get("variant") or ("chat" if notice.get("kind") == "message" else notice.get("kind"))
    src = str(notice.get("source") or "")[:10]
    title = str(notice.get("title") or "")[:14]
    count = int(notice.get("count") or 1)
    lines = {
        "chat": f"{src} 来了 {count} 条消息～",
        "p0": f"{src} 有条重要消息，等你核对哦。",
        "progress": f"我盯着 {src} 的「{title}」。" if title else f"我盯着 {src} 呢。",
        "done": f"{src}「{title}」好啦！" if title else f"{src} 办好啦！",
        "failed": f"{src}「{title}」没成，要不要看看？" if title else f"{src} 没办成，要不要看看？",
        "waiting": f"{src} 在等你处理呢。",
        "summary": f"{title}，我帮你记着。" if title else "刚才有几条提示，我帮你记着。",
        "info": f"{src}：{title}" if title else src,
    }
    text = lines.get(v, f"{src}：{title}")
    if missed:
        text += f"（之前还有 {missed} 条）"
    return NOTICE_POSES.get(v), text[:40]


def load_pet_position():
    if os.path.exists(CONFIG_POS_FILE):
        try:
            with open(CONFIG_POS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("x", 1600), d.get("y", 820)
        except Exception:
            pass
    return 1600, 820

def save_pet_position(x, y):
    try:
        os.makedirs(os.path.dirname(CONFIG_POS_FILE), exist_ok=True)
        with open(CONFIG_POS_FILE, "w", encoding="utf-8") as f:
            json.dump({"x": int(x), "y": int(y)}, f)
    except Exception:
        pass

def load_pet_stats():
    default_stats = {
        "affection_pts": 100,
        "level": 2,
        "title": "熟络",
        "pats_count": 0,
        "feeds_count": 0,
        "pomodoro_count": 0,
        "total_interact_sec": 0
    }
    if os.path.exists(CONFIG_STATS_FILE):
        try:
            with open(CONFIG_STATS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                default_stats.update(d)
                return default_stats
        except Exception:
            pass
    return default_stats

def save_pet_stats(stats):
    try:
        os.makedirs(os.path.dirname(CONFIG_STATS_FILE), exist_ok=True)
        with open(CONFIG_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class CanaanPetCore:
    def _init_pet_core(self, is_speaking_provider=None, on_interrupt=None, on_exit=None, companion=None):
        self.is_speaking_provider = is_speaking_provider
        self.on_interrupt_callback = on_interrupt
        self.on_exit_callback = on_exit

        # Action Step Progress HUD (实时步骤看板)
        self.action_hud_task_id = ""
        self.action_hud_step = 0
        self.action_hud_total = 0
        self.action_hud_desc = ""
        self.action_hud_status = "idle"
        self.action_hud_message = ""
        self.action_hud_until = 0.0

        # 1. Window Attributes: Zero GPU Overhead (Pure GDI Software Raster)
        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        
        self.TRANS_COLOR = "#010203"
        self.configure(bg=self.TRANS_COLOR)
        self.wm_attributes("-transparentcolor", self.TRANS_COLOR)

        self.screen_width = self.winfo_screenwidth()
        self.screen_height = self.winfo_screenheight()

        # Canvas Dimensions
        self.CANVAS_W = 280
        self.CANVAS_H = 300
        
        # Ground Level (resting right on top of Windows taskbar)
        self._work_areas = work_areas((0, 0, self.screen_width, self.screen_height))
        self._screen_check_at = 0.0
        init_x, init_y = load_pet_position()
        self.pos_x, self.pos_y, self._work_area = place(
            init_x, init_y, self.CANVAS_W, self.CANVAS_H, self._work_areas)
        self.ground_y = max(self._work_area[1], self._work_area[3] - self.CANVAS_H)
        
        self.geometry(f"{self.CANVAS_W}x{self.CANVAS_H}+{int(self.pos_x)}+{int(self.pos_y)}")

        self.canvas = tk.Canvas(
            self, width=self.CANVAS_W, height=self.CANVAS_H,
            bg=self.TRANS_COLOR, highlightthickness=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 2. Load and Pre-cache all 72 base PIL images
        self.base_images = self._load_all_72_base_images()
        self._render_cache = OrderedDict()
        self._avatar_item = None
        self.stats = load_pet_stats()
        # 形象包（共创平台 pet.json 格式）：设置里有就加载，没有用原来的 72 态；注视状态
        self.pack = None
        self._gaze_dir = None
        self._gaze_cursor = None
        self._gaze_moved_at = 0.0
        self._status_last = None
        self._load_pack_from_settings()
        self.voice = PetVoice() if PetVoice is not None else None
        self._last_mouth = 0
        # 大模型对话（后台线程；事件队列每帧取一次）
        self.chat = PetChat(valid_poses=set(self.base_images), companion=companion) if PetChat is not None else None
        self.chat_active = False
        self.chat_full = ""
        self.chat_shown = 0.0
        self.chat_voice = "pamper"
        self.input_win = None
        if self.voice is not None:
            self.voice.warm()
        if self.chat is not None:
            self.chat.warm()
        # 后台线程把要在界面上做的事放进这个队列，主循环每帧执行（Tk 只能在主线程操作）
        import queue as _queue
        self._ui_q = _queue.Queue()
        # 每日早报：早上 9 点，或当天早上 6 点后启动时；推送只发文件传输助手、只在电脑空闲时
        self.briefing_settings = briefing.load_settings() if briefing is not None else {}
        self._started_at = time.time()
        self._briefing_check_at = 0.0
        self._briefing_running = False
        self._push_pending = None      # (文字, 截止时间, 失败次数)
        self._push_try_at = 0.0
        # 智能体任务（Muse / Dots）：后台只读轮询，脚下显示进行中/等你处理的数量
        self.agent_counts = None
        self._agent_waiting_told = set()
        self._agent_watch_lock = threading.RLock()
        self._agent_watch_thread = None
        self._agent_watch_stop = None
        self._agent_watch_destroyed = False
        self._start_agent_watch()
        # 养成数值：饱腹度 / 心情 / 精力（关着的时间也按最多 8 小时衰减）
        self.needs = PetNeeds.from_dict(self.stats.get("needs")) if PetNeeds is not None else None
        self._needs_tick_at = 0.0
        self._needs_saved_at = time.time()
        # 桌面感知（后台线程每秒看一次前台窗口；只读标题和进程名，不存不传）
        self.sense = None
        self.ambient_pose = None
        self._privacy_warned = {}
        self._typing_until = 0.0
        if SenseWatcher is not None:
            self.sense = SenseWatcher()
            self.sense.enabled = self.stats.get("sense_enabled", True)
            self.sense.start()
        # 气泡字体提前加载（第一次用微软雅黑画字时 Tk 要加载字体，会让界面卡约 0.25 秒）
        try:
            import tkinter.font as tkfont
            for spec in (("Microsoft YaHei UI", 9, "bold"), ("Microsoft YaHei UI", 10, "bold"), ("Segoe UI", 9, "bold")):
                tkfont.Font(family=spec[0], size=spec[1], weight=spec[2]).measure("团团预热 Zzz 💖")
        except Exception:
            pass

        # 3. State Machine Variables
        self.state = "IDLE"
        self.state_lock_until = 0
        self.is_dragging = False
        self.is_falling = False
        self.is_focus_work_mode = False
        self.pomodoro_active = False
        self.pomodoro_end_time = 0
        
        self.last_interaction_time = time.time()
        self.last_blink_time = time.time()
        self.last_pat_time = 0
        self.rapid_click_count = 0
        self.shake_counter = 0

        # 4. SILKY PHYSICS ENGINE (60 FPS Spring-Damper & Pendulum Dynamics)
        # Squash & Stretch Spring (果冻弹性形变 Q 弹系统)
        self.wobble_y = 0.0       # Relative scale delta in Y (-0.2 = -20% squash)
        self.wobble_vy = 0.0      # Velocity of squash deformation
        self.spring_k = 260.0     # Spring stiffness (high = responsive)
        self.spring_d = 16.0      # Spring damping (viscous decay)

        # Pendulum Drag Swing (单摆惯性摆动物理)
        self.angle = 0.0          # Current rotation angle in degrees
        self.angle_v = 0.0        # Angular velocity
        self.angle_k = 180.0      # Angular spring restoring stiffness
        self.angle_d = 14.0       # Angular damping

        # Sub-pixel Drag & Lerp Follow
        self.drag_mouse_prev_x = 0
        self.drag_mouse_prev_y = 0
        self.drag_vx = 0.0
        self.drag_vy = 0.0
        self.target_win_x = self.pos_x
        self.target_win_y = self.pos_y

        # Gravity & Floor Bounce
        self.fall_vy = 0.0
        self.gravity = 1800.0

        # 甩出去的惯性、边缘吸附（canaan_motion）
        self.fall_vx = 0.0
        self.vel_tracker = motion.VelocityTracker()
        self.dock = None                       # None / "left" / "right" / "top"
        self.dock_base = (0, 0)                # 吸附后窗口左上角（可以在屏幕外）
        self.dock_out = False                  # 光标靠近时滑出来一点
        self.dock_spring_x = motion.Spring(self.pos_x)
        self.dock_spring_y = motion.Spring(self.pos_y)
        self._avatar_size = (120, 180)         # 上一帧实际画出来的形象大小（吸附按它算）
        self._typing_since = None
        self._quiet_cache = (0.0, False)

        # Walking Dynamics (起伏步态、侧倾与脚步微尘)
        self.walk_vx = 0.0
        self.walk_steps_left = 0
        self.walk_phase = 0.0
        self.facing_left = False

        # Cursor Gaze Lean (视线感应微倾角)
        self.gaze_lean = 0.0

        # Speech Bubble Animation
        self.bubble_text = ""
        self.bubble_until = 0
        self.bubble_scale = 0.0
        self.custom_bubble_text = None
        self.custom_bubble_until = 0

        # Particle Dynamics
        self.floating_particles = []

        # Current photo image reference
        self.cur_photo_img = None
        self.last_time = time.time()

        # 5. Bind Events
        self.canvas.bind("<Button-1>", self._on_left_click_down)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_click_up)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.tag_bind("bubble", "<ButtonRelease-1>", self._on_bubble_click)   # 提示气泡可以点
        self.bind_all("<Escape>", lambda e: self.interrupt())

        # 上次是吸附在边缘关掉的：恢复吸附（不冒气泡）
        if self.stats.get("dock") in ("left", "right", "top"):
            target = motion.dock_position(self.stats["dock"], self.pos_x, self.pos_y, self.CANVAS_W, self.CANVAS_H,
                                          *self._avatar_size, self._work_area)
            if target:
                self._dock_to(*target, quiet=True)
                self.dock_spring_x.snap(target[1])
                self.dock_spring_y.snap(target[2])

        # 6. Start 60 FPS Buttery-Smooth Loop (~16ms)
        self.after(16, self._animation_loop)

    def _load_all_72_base_images(self):
        """Pre-cache high quality base PIL images for all 72 poses"""
        base_imgs = {}
        target_heights = {
            # 1-6 基础交互
            "idle": 210, "talk": 210, "blink": 210, "shy": 210, "drag": 200, "sleep": 175,
            # 7-12 伴工关怀
            "work": 200, "tea": 210, "action": 205, "cheer": 210, "pout": 190, "walk": 210,
            # 13-18 趣味拓展
            "boba": 210, "gift": 210, "audit": 205, "game": 195, "stealth": 220, "climb": 215,
            # 19-24 打工商务
            "typing": 200, "phone": 210, "coffee": 210, "money": 200, "reading": 200, "chart": 210,
            # 25-30 情绪互动
            "dizzy": 215, "crying": 210, "shock": 210, "dance": 215, "eating": 210, "yawn": 215,
            # 31-36 绝技四季
            "grapple": 195, "umbrella": 225, "swim": 210, "scarf": 210, "mask": 210, "crown": 225,
            # 37-42 武学
            "parry": 215, "blade": 215, "spear": 210, "slide": 195, "crouch": 185, "resurrect": 220,
            # 43-48 职场百态
            "mask_spa": 210, "headset": 215, "takeout": 205, "coffee_spill": 210, "overtime": 210, "headache": 210,
            # 49-54 温馨居家
            "cat": 210, "bubble_gum": 210, "fitness": 210, "music_headset": 215, "watering": 210, "selfie": 210,
            # 55-60 传统节日
            "firecracker": 215, "zongzi": 210, "mooncake": 210, "pumpkin": 215, "dumpling": 210, "lantern": 215,
            # 61-66 趣味傲娇
            "tsundere": 215, "heart_eyes": 215, "worship": 195, "detective": 220, "hug_screen": 205, "box_hide": 185,
            # 67-72 终极幻化
            "wedding": 225, "angel_wings": 215, "magician": 215, "motorcycle": 205, "sleepy_nod": 210, "rainbow_star": 215
        }
        
        for k, th in target_heights.items():
            p = os.path.join(ASSETS_DIR, f"canaan_q_{k}.png")
            if os.path.exists(p):
                try:
                    img = Image.open(p).convert("RGBA")
                    aspect = img.size[0] / img.size[1]
                    tw = max(10, int(th * aspect))
                    scaled = img.resize((tw, th), Image.Resampling.LANCZOS)
                    base_imgs[k] = scaled
                except Exception as e:
                    print(f"Failed to load frame {k}: {e}")

        # 口型帧：从说话图生成闭嘴、半张嘴两帧，缩放到与说话图相同高度
        if make_mouth_frames is not None and "talk" in base_imgs:
            try:
                raw = Image.open(os.path.join(ASSETS_DIR, "canaan_q_talk.png")).convert("RGBA")
                tw, th = base_imgs["talk"].size
                for key, im in make_mouth_frames(raw).items():
                    base_imgs[key] = im.resize((tw, th), Image.Resampling.LANCZOS)
            except Exception as e:
                print(f"口型帧生成失败: {e}")

        # Fallback
        if "idle" not in base_imgs:
            base_imgs["idle"] = Image.new("RGBA", (130, 210), (99, 102, 241, 180))

        return base_imgs

    def is_speaking(self):
        """检查团团是否在说话（包括 PetVoice 与 Hub 主程序播音）。"""
        if self.voice is not None and self.voice.is_speaking():
            return True
        if callable(self.is_speaking_provider):
            try:
                if self.is_speaking_provider():
                    return True
            except Exception:
                pass
        return False

    def interrupt(self, *, notify=True):
        """0毫秒即刻掐断当前说话与动作：声音闭嘴、气泡收拢、口型复位、回退到idle。"""
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception:
                pass
        if notify and callable(self.on_interrupt_callback):
            try:
                self.on_interrupt_callback()
            except Exception as e:
                print(f"[!] 打断回调执行异常: {e}")
        self.custom_bubble_text = None
        self.custom_bubble_until = 0.0
        self.bubble_text = ""
        self.bubble_until = 0.0
        self.bubble_scale = 0.0
        self._last_mouth = 0
        self.chat_active = False
        if self.state in ("TALK", "SHY") or self.state.startswith("TALK_"):
            self.state = "IDLE"
            self.state_lock_until = 0.0
        print("[⚡ 即时打断] 团团桌面伴侣已闭嘴归位")

    def set_action_step(self, task_id, step, total, desc, status="running"):
        """主控调度下发执行步骤：在桌宠头顶展示实时步骤微看板 [step/total] desc"""
        def _apply():
            self.action_hud_task_id = str(task_id)
            self.action_hud_step = int(step)
            self.action_hud_total = int(total)
            self.action_hud_desc = str(desc)
            self.action_hud_status = status
            self.action_hud_until = time.time() + 8.0
            if not self.is_focus_work_mode:
                self.set_pose("work", duration_sec=4.0, quiet=True)
        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            self._ui_q.put(_apply)

    def set_action_finished(self, task_id, success, message=""):
        """主控调度任务完成或失败通知：更新步骤看板状态并作出姿态反馈"""
        def _apply():
            self.action_hud_task_id = str(task_id)
            self.action_hud_status = "success" if success else "failed"
            self.action_hud_message = str(message)
            self.action_hud_until = time.time() + 5.0
            if success:
                self.cheer()
                self._spawn_burst_particles("✨", count=3, color="#22c55e")
            else:
                self.set_pose("crying", duration_sec=4.0, quiet=True)
                self._spawn_burst_particles("❌", count=2, color="#ef4444")
                self.say(f"呜，步骤受阻：{message}", duration_sec=4.5, speak=False)
        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            self._ui_q.put(_apply)

    def on_status_event(self, event):
        """订阅统一状态事件（core/canaan_events）。事件只有动作名、步骤号和结果类别，在后台线程送达，这里切回界面线程。"""
        def _apply():
            import canaan_events
            kind, text = event["kind"], canaan_events.label(event)
            if kind == "agent":
                # Muse / Dots 进度：平静模式只显示等你处理、办好了、没办成；完整模式也显示第 k/n 步
                result = event.get("result", "")
                if result == "waiting":
                    self.action_hud_status, self.action_hud_message = "waiting", text
                    self.action_hud_until = time.time() + 30.0
                elif result in ("ok", "failed", "unknown"):
                    self.action_hud_status, self.action_hud_message = ("success" if result == "ok" else "failed"), text
                    self.action_hud_until = time.time() + 8.0
                elif not self.calm:
                    self.set_action_step("", event["step"], event["total"], text)
                return
            self._status_last = dict(event)
            if self.calm:
                # 平静模式：只在需要主人的时候出现（等确认、没办成），不换姿态、不放粒子、不冒气泡
                if kind == "awaiting_confirm":
                    self.action_hud_status, self.action_hud_message = "waiting", text
                    self.action_hud_until = time.time() + 30.0
                elif kind == "failure":
                    self.action_hud_status, self.action_hud_message = "failed", text
                    self.action_hud_until = time.time() + 8.0
                elif self.action_hud_status in ("waiting", "failed") and kind in ("step", "success", "idle"):
                    self.action_hud_until = min(self.action_hud_until, time.time() + 1.5)
                return
            if kind == "step":
                self.set_action_step("", event["step"], event["total"], text)
            elif kind == "awaiting_confirm":
                self.action_hud_status, self.action_hud_message = "waiting", text
                self.action_hud_until = time.time() + 30.0
                self.set_pose("audit", duration_sec=4.0, quiet=True)
            elif kind in ("success", "failure"):
                self.set_action_finished("", kind == "success", text)
            elif kind in ("listening", "thinking"):
                self.action_hud_status, self.action_hud_message = kind, text
                self.action_hud_until = time.time() + 8.0
            elif kind == "idle" and self.action_hud_status in ("running", "listening", "thinking"):
                self.action_hud_until = min(self.action_hud_until, time.time() + 1.5)
        self._ui_q.put(_apply)

    # ---------- 形象包 ----------
    @staticmethod
    def has_builtin_images():
        """本机有没有内置 72 态图片（只在自用版本机；公开版不带）。"""
        return os.path.exists(os.path.join(ASSETS_DIR, "canaan_q_idle.png"))

    def _load_default_pack(self):
        """没有 72 态图片时用原创默认形象“团团”（代码生成，第一次用时画好放进 data/packs/）。"""
        try:
            from canaan_default_pack import ensure_default_pack
            from canaan_pet_pack import load_pack
            self.pack = load_pack(ensure_default_pack())
            self._render_cache.clear()
        except Exception as e:
            print(f"[!] 默认形象生成失败: {e}")
            self.pack = None

    def _load_pack_from_settings(self):
        path = self.stats.get("pet_pack")
        if not path:
            if not self.has_builtin_images():
                self._load_default_pack()
            return
        try:
            from canaan_pet_pack import load_pack
            self.pack = load_pack(path)
            self._render_cache.clear()
            print(f"[✓] 已加载形象包：{self.pack.name}（{self.pack.kind}）")
        except Exception as e:
            print(f"[!] 形象包加载失败，改用默认形象：{e}")
            self.pack = None
            if not self.has_builtin_images():
                self._load_default_pack()

    def import_pet_pack(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title="导入形象包（pet.json 所在的 zip，或文件夹里的 pet.json）",
                                          filetypes=[("形象包", "*.zip pet.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            from canaan_pet_pack import install_pack
            installed, pack = install_pack(path)   # 校验通过才复制进 data/packs/，原文件挪走也不影响
        except Exception as e:
            self.say(f"这个形象包用不了：{e}", 6.0, speak=False)
            return
        self.use_pet_pack(installed, pack)

    def use_pet_pack(self, path, pack=None):
        try:
            if pack is None:
                from canaan_pet_pack import load_pack
                pack = load_pack(path)
        except Exception as e:
            self.say(f"这个形象包用不了：{e}", 6.0, speak=False)
            return
        self.pack = pack
        self._render_cache.clear()
        self.stats["pet_pack"] = path
        save_pet_stats(self.stats)
        self.say(f"换好啦：{pack.name}", 3.0, speak=False)

    def reset_pet_pack(self):
        self.pack = None
        self._render_cache.clear()
        self.stats.pop("pet_pack", None)
        save_pet_stats(self.stats)
        if self.has_builtin_images():
            self.say("回到默认形象", 3.0, speak=False)
        else:
            self._load_default_pack()
            self.say("回到默认形象：团团", 3.0, speak=False)

    def toggle_gaze(self):
        self.stats["gaze_enabled"] = not self.stats.get("gaze_enabled", False)
        save_pet_stats(self.stats)
        self._gaze_dir = None

    @property
    def calm(self):
        """平静模式（默认开）：不呼吸、不眨眼、不随光标转、不按桌面状态换姿态、不冒需求提醒和庆祝粒子；
        只有“等你确认”和“没办成”才出现提示。周边视觉对运动很敏感，工作时角色应该安静地待在一边。"""
        return self.stats.get("calm_mode", True)

    def toggle_calm(self):
        self.stats["calm_mode"] = not self.calm
        save_pet_stats(self.stats)
        if self.calm:
            self.ambient_pose = None
            self.state, self.state_lock_until = "IDLE", 0
            self._gaze_dir = None

    def _update_gaze(self, now):
        """光标注视（只对 V2 形象包、只在待机时）：光标动时跟着看，停下 1.2 秒后回正。"""
        if not (self.pack and self.pack.has_gaze and self.stats.get("gaze_enabled", False) and not self.calm
                and self.state in ("IDLE", "BLINK") and not self.is_dragging):
            self._gaze_dir = None
            return
        try:
            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            cursor = (pt.x, pt.y)
        except Exception:
            self._gaze_dir = None
            return
        from canaan_pet_pack import gaze_direction
        if cursor != self._gaze_cursor:
            self._gaze_cursor, self._gaze_moved_at = cursor, now
            anchor_x = self.pos_x + self.CANVAS_W // 2
            anchor_y = self.pos_y + self.CANVAS_H // 2 + 120
            self._gaze_dir = gaze_direction(cursor[0] - anchor_x, cursor[1] - anchor_y)
        elif now - self._gaze_moved_at > 1.2:
            self._gaze_dir = None

    def _pack_image(self, k, now):
        """有形象包时：把 72 态姿态换成包里的动作帧（或注视方向帧）。返回 (图, 缓存键)。"""
        if self._gaze_dir is not None:
            from canaan_pet_pack import mirror_direction
            d = mirror_direction(self._gaze_dir) if self.facing_left else self._gaze_dir
            return self.pack.gaze_frame(d), f"pack:{self.pack.id}:gaze:{d}"
        action = self.pack.for_canaan_state(k)
        img, idx = self.pack.frame_at(action, now)
        return img, f"pack:{self.pack.id}:{action}:{idx}"

    def toggle_hub_capsule(self):
        hub = getattr(self.master, "hub_instance", None) or getattr(self, "hub", None)
        if hub:
            hub.toggle_top_bar()

    def hide_hub(self):
        hub = getattr(self.master, "hub_instance", None) or getattr(self, "hub", None)
        if hub:
            hub.hide_top_bar()

    def show_hub(self):
        hub = getattr(self.master, "hub_instance", None) or getattr(self, "hub", None)
        if hub:
            hub.show_top_bar()

    def _draw_rounded_pill(self, x1, y1, x2, y2, r=12, **kwargs):
        """绘制高质感 Apple 悬浮胶囊 / 动态圆角多边形"""
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
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _draw_action_hud(self):
        """在桌宠头顶绘制半透明科技感步骤 HUD 卡片"""
        now = time.time()
        status = self.action_hud_status
        if now >= self.action_hud_until or status == "idle":
            # 进行中的状态 90 秒没有新事件显示“待确认”，10 分钟后清掉（与 canaan_events 一致）
            last = self._status_last
            if not (last and last["kind"] in ("step", "awaiting_confirm", "thinking", "listening")
                    and 90 <= now - last["ts"] < 600):
                return
            status = "pending"

        hw = 216
        hh = 32
        hx = (self.CANVAS_W - hw) // 2
        hy = 12

        # 步骤卡不显示任务编号
        if status == "running":
            border_col = "#38bdf8"
            badge_text = f"⚡ 第 {self.action_hud_step}/{self.action_hud_total} 步"
            detail_text = self.action_hud_desc
        elif status == "success":
            border_col = "#22c55e"
            badge_text = "✓ 办好了"
            detail_text = self.action_hud_message or "指令已顺利搞定"
        elif status == "waiting":
            border_col = "#f59e0b"
            badge_text = "⏸ 等你确认"
            detail_text = self.action_hud_message or "说“确认”执行，“取消”放弃"
        elif status in ("listening", "thinking"):
            border_col = "#a78bfa"
            badge_text = "👂 在听" if status == "listening" else "💭 在想"
            detail_text = ""
        elif status == "pending":
            border_col = "#94a3b8"
            badge_text = "⌛ 待确认"
            detail_text = "很久没有进展了"
        else:
            border_col = "#ef4444"
            badge_text = "❌ 没办成"
            detail_text = self.action_hud_message or "遇到异常已终止"

        self._draw_rounded_pill(
            hx, hy, hx + hw, hy + hh,
            r=10, fill="#121424", outline=border_col, width=1.2,
            tags="action_hud"
        )
        self.canvas.create_text(
            hx + 8, hy + 10,
            text=badge_text, anchor="w",
            fill=border_col, font=("Microsoft YaHei UI", 8, "bold"),
            tags="action_hud"
        )
        if len(detail_text) > 20:
            detail_text = detail_text[:19] + "…"
        self.canvas.create_text(
            hx + 8, hy + 23,
            text=detail_text, anchor="w",
            fill="#e2e8f0", font=("Microsoft YaHei UI", 8),
            tags="action_hud"
        )

    def say(self, text, duration_sec=4.5, speak=None, emotion="pamper"):
        """外部调用：让团团在头顶冒出气泡并说话。
        speak=True 时同时用本地 GPT-SoVITS 念出来；未指定且已挂载到 Hub 时不二次发声。"""
        self.custom_bubble_text = text
        self.custom_bubble_until = time.time() + duration_sec
        self.bubble_scale = 0.0
        self.last_interaction_time = time.time()
        if self.state == "SLEEP":
            self.state = "IDLE"
        if speak is None:
            speak = (self.is_speaking_provider is None)
        if speak and self.voice is not None and self.stats.get("voice_enabled", True):
            self.voice.speak(text, emotion)

    def toggle_voice(self):
        """右键菜单：开/关语音（只关声音，气泡照常）。"""
        on = not self.stats.get("voice_enabled", True)
        self.stats["voice_enabled"] = on
        save_pet_stats(self.stats)
        if not on and self.voice is not None:
            self.voice.stop()
        self.say("好，我开口说话啦~" if on else "好，我先不出声，只冒气泡~", 3.0, speak=on)

    def set_pose(self, pose_name, duration_sec=3.5, dialogue="", quiet=False):
        """统一方法：切换到 72 种姿态中的任意一种（带果冻形变缓冲 Anticipation）。
        quiet=True 只换姿态，不冒预设台词（对话里由大模型的动作标签触发时用）。"""
        p = pose_name.lower().replace("canaan_q_", "").replace(".png", "")
        if p in self.base_images:
            self.state = p.upper()
            self.state_lock_until = time.time() + duration_sec
            
            # Juicy Anticipation Squash (蓄力切换形变)
            self.wobble_vy = -3.2

            if dialogue:
                self.say(dialogue, duration_sec)
            elif p in VOICE_LINES and not quiet:
                self.say(random.choice(VOICE_LINES[p]), duration_sec)

    def cheer(self, dialogue=None):
        """任务完成或获得好评时的庆祝大比心"""
        msg = dialogue or "搞定啦！主人太棒了，团团给你大大的赞！✨"
        self.set_pose("cheer", 4.0, msg)
        self.add_affection(2)
        self._spawn_burst_particles("💖", count=5, color="#f43f5e")

    def alert(self, warning_text):
        """系统预警或安全警报时的刺客警戒"""
        self.set_pose("action", 4.5)
        self.say(warning_text, 4.5, emotion="dangerous")
        self.wobble_vy = -4.0

    def set_work_mode(self, active=True):
        """进入或退出专注伴读敲电脑模式"""
        self.is_focus_work_mode = active
        if active:
            self.set_pose("work", 999999, "团团与你一起专注冲刺！开始办公~ 💻")
        else:
            self.state = "IDLE"
            self.state_lock_until = 0
            self.say("伴读模式结束，休息一下吧~ 🍵")

    def wake_up(self):
        """唤醒正在小憩的团团"""
        if self.state == "SLEEP":
            self.state = "IDLE"
            self.last_interaction_time = time.time()
            self.wobble_vy = 3.5  # stretch up
            self.say("嗯……亲爱的？本小姐醒啦，这就为你效劳~ ✨")

    def add_affection(self, pts):
        """增加亲密度好感点数，并实时提升阶梯等级"""
        self.stats["affection_pts"] += pts
        old_lvl = self.stats["level"]
        cur_pts = self.stats["affection_pts"]
        
        if cur_pts >= 1000:
            self.stats["level"] = 4
            self.stats["title"] = "誓约之巅"
        elif cur_pts >= 400:
            self.stats["level"] = 3
            self.stats["title"] = "知心红颜"
        elif cur_pts >= 150:
            self.stats["level"] = 2
            self.stats["title"] = "亲昵无间"
        else:
            self.stats["level"] = 1
            self.stats["title"] = "初见熟络"
            
        save_pet_stats(self.stats)
        self._spawn_floating_particle(f"+ {pts} 💖", "#f43f5e")

        if self.stats["level"] > old_lvl:
            title = self.stats["title"]
            lvl = self.stats["level"]
            self.set_pose("crown", 5.0, f"羁绊阶梯跃升至 Lv.{lvl}【{title}】！感谢你一直以来的守护与陪伴！👑")
            self._spawn_burst_particles("👑", count=8, color="#facc15")

    def _spawn_floating_particle(self, text, color="#f43f5e"):
        self.floating_particles.append({
            "text": text,
            "color": color,
            "x": self.CANVAS_W // 2 + random.uniform(-15, 15),
            "y": 65,
            "vx": random.uniform(-0.5, 0.5),
            "vy": -1.8,
            "alpha": 1.0,
            "life": 45
        })

    def _spawn_burst_particles(self, symbol, count=4, color="#f43f5e"):
        for _ in range(count):
            self.floating_particles.append({
                "text": symbol,
                "color": color,
                "x": self.CANVAS_W // 2 + random.uniform(-25, 25),
                "y": 140 + random.uniform(-20, 20),
                "vx": random.uniform(-2.5, 2.5),
                "vy": random.uniform(-3.5, -1.0),
                "alpha": 1.0,
                "life": 35
            })

    def flip_coin(self):
        """纠结症帮选功能：团团为你抛出命运金币"""
        res = random.choice(["正面！果断出击，听从直觉！✨", "反面！稳扎稳打，静观其变~ 🛡️"])
        self.wobble_vy = 4.0
        self.set_pose("cheer", 3.5, f"🪙 命运金币结果：{res}")

    def clean_memory(self):
        """一键释放系统内存，并释放金沙暴大招特效"""
        try:
            ctypes.windll.psapi.EmptyWorkingSet(-1)
            msg = "寂静暗刑！系统无用内存已如飞沙般被本小姐扫清！🌪️"
            self.set_pose("stealth", 4.0, msg)
            self._spawn_burst_particles("✨", count=6, color="#38bdf8")
            self._spawn_floating_particle("🧹 内存已释放", "#38bdf8")
        except Exception:
            self.say("内存释放完成！系统飞速运转~ 🚀")

    def start_pomodoro(self, minutes=25):
        """开启番茄钟"""
        self.pomodoro_active = True
        self.pomodoro_end_time = time.time() + minutes * 60
        self.stats["pomodoro_count"] += 1
        save_pet_stats(self.stats)
        self.set_work_mode(True)
        self.say(f"🍅 专注番茄钟开启 ({minutes}分钟)！团团为你静音守护！")

    def stop_pomodoro(self):
        """提前停止番茄钟"""
        self.pomodoro_active = False
        self.set_work_mode(False)
        self.say("番茄钟已停止，辛苦啦~ 喝杯茶休息一下！")

    def _on_left_click_down(self, event):
        # 说话中途点击桌宠：0毫秒即刻闭嘴打断 (Barge-in Cutoff)
        if self.is_speaking():
            self.interrupt()
            self._spawn_floating_particle("🤫 闭嘴", color="#94a3b8")
            return

        self.drag_mouse_prev_x = event.x_root
        self.drag_mouse_prev_y = event.y_root
        self.drag_grab_offset_x = event.x
        self.drag_grab_offset_y = event.y
        self.click_down_time = time.time()
        self.is_drag_moved = False
        self.last_interaction_time = time.time()
        self.wake_up()

    def _on_drag_motion(self, event):
        dx = event.x_root - self.drag_mouse_prev_x
        dy = event.y_root - self.drag_mouse_prev_y
        self.drag_vx = float(dx)
        self.drag_vy = float(dy)
        self.vel_tracker.add(time.time(), event.x_root, event.y_root)
        if self.dock and (abs(dx) > 1 or abs(dy) > 1):
            self._undock()   # 从边缘拖出来：解除吸附，跟着手走
        self.drag_mouse_prev_x = event.x_root
        self.drag_mouse_prev_y = event.y_root

        if abs(dx) > 1 or abs(dy) > 1 or self.is_dragging:
            self.is_drag_moved = True
            
            # Smooth window target follow
            self.target_win_x = event.x_root - self.drag_grab_offset_x
            self.target_win_y = event.y_root - self.drag_grab_offset_y

            # Immediately update position with Lerp
            self.pos_x += (self.target_win_x - self.pos_x) * 0.45
            self.pos_y += (self.target_win_y - self.pos_y) * 0.45
            self._place_pet()

            if not self.is_dragging:
                self.is_dragging = True
                self.is_falling = False
                self.state = "DRAG"
                self.wobble_y = 0.12  # collar pull stretch
                self.say("哇呀！抓脖领悬空啦！小短腿扑腾扑腾~ 🙀", 2.0)

            # Rapid shake detection
            if abs(self.drag_vx) > 18:
                self.shake_counter += 1
                if self.shake_counter > 6:
                    self.set_pose("dizzy", 4.0, "哎呀呀头好晕，小星星转圈圈啦！💫")
                    self.shake_counter = 0

    def _on_left_click_up(self, event):
        if self.is_dragging:
            self.is_dragging = False
            vx, vy = self.vel_tracker.velocity(time.time())
            self.vel_tracker.clear()
            aw, ah = self._avatar_size
            target = motion.dock_target(self.pos_x, self.pos_y, self.CANVAS_W, self.CANVAS_H, aw, ah, self._work_area)
            if target and math.hypot(vx, vy) < 600:
                # 离边缘够近、又不是用力甩：弹簧吸过去，露出 60%（探头 / 倒挂）
                self._dock_to(*target)
                return
            self._place_pet(snap=18)
            save_pet_position(self.pos_x, self.pos_y)
            if math.hypot(vx, vy) > 220:
                # 甩出去：带着松手时的速度飞，撞墙反弹、落地压扁；用力甩向墙就直接吸附
                self.is_falling = True
                self.fall_vx, self.fall_vy = vx, max(-1400.0, min(1400.0, vy))
                self.wobble_y = 0.08
                self.say("呼哇！飞起来啦~ 🪂", 1.4, speak=False)
                return
            if self.pos_y < self.ground_y - 20:
                # FREE FALL GRAVITY INITIATED!
                self.is_falling = True
                self.fall_vx = 0.0
                self.fall_vy = max(-200.0, min(600.0, vy))
                self.wobble_y = 0.08  # airborne trailing stretch
                self.say("呼哇！失重下落啦~ 迎风滑翔！🪂", 1.8)
            else:
                self.state = "IDLE"
                self.wobble_vy = -3.5  # landing squash
                save_pet_position(self.pos_x, self.pos_y)
                self.say("呼，终于平稳着陆啦，小脚踩到地面的感觉真好~ 🦶")

        elif not self.is_drag_moved:
            # Click event (摸头果冻回弹 Q-Bounce)
            now = time.time()
            if now - self.last_pat_time < 0.5:
                self.rapid_click_count += 1
            else:
                self.rapid_click_count = 1
            self.last_pat_time = now

            if self.rapid_click_count >= 3:
                # Rapid poke -> Pout with anger wobble
                self.set_pose("pout", 3.5, random.choice(VOICE_LINES["pout"]))
                self.wobble_vy = 3.0
                self._spawn_burst_particles("💢", count=2, color="#f97316")
                self.rapid_click_count = 0
            else:
                # Tender Petting -> Juicy Jelly Wobble
                self.stats["pats_count"] += 1
                self.add_affection(1)
                if self.needs is not None:
                    self.needs.pat()
                self.wobble_vy = -3.8  # squashes down under hand, then rebounds!
                self.set_pose("shy", 3.0, random.choice(VOICE_LINES["pat"]))

    def _on_double_click(self, event):
        """双击：弹出对话输入框（跳舞挪到右键菜单“趣味与情绪”里）。没有对话模块时保持原来的跳舞。
        吸附在边缘时，双击是把她完整拉回屏幕里，落到地面。"""
        self.wobble_vy = -4.0
        if self.dock:
            self._undock()
            self._place_pet()
            self.is_falling, self.fall_vx, self.fall_vy = True, 0.0, 0.0
            return
        if self.chat is None:
            self.set_pose("dance", 4.0, random.choice(VOICE_LINES["dance"]))
            self.add_affection(2)
            self._spawn_burst_particles("🌸", count=6, color="#ec4899")
            return
        self.open_chat_input()

    def open_chat_input(self):
        """在团团头顶弹出一个输入框：回车发送，Esc 关闭。"""
        if self.chat is None:
            return
        if self.input_win is not None:
            try:
                self.input_win.lift()
                self.input_win.focus_force()
                return
            except Exception:
                self.input_win = None
        w, h = 260, 34
        x = int(self.pos_x + (self.CANVAS_W - w) // 2)
        y = int(self.pos_y - h - 4) if self.pos_y > h + 10 else int(self.pos_y + self.CANVAS_H + 4)
        x, y, _ = place(x, y, w, h, self._work_areas)
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.wm_attributes("-topmost", True)
        win.configure(bg="#6366f1")
        win.geometry(f"{w}x{h}+{x}+{y}")
        entry = tk.Entry(win, font=("Microsoft YaHei UI", 10), bg="#13141f", fg="#ffffff",
                         insertbackground="#ffffff", relief=tk.FLAT)
        entry.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        entry.insert(0, "")
        entry.focus_force()

        def _send(_e=None):
            text = entry.get().strip()
            self._close_chat_input()
            if text:
                self.chat_ask(text)

        entry.bind("<Return>", _send)
        entry.bind("<Escape>", lambda _e: self._close_chat_input())
        self.input_win = win
        self.say("想跟我说什么呀？打字后按回车~", 4.0, speak=False)

    def _close_chat_input(self):
        if self.input_win is not None:
            try:
                self.input_win.destroy()
            except Exception:
                pass
        self.input_win = None

    def chat_ask(self, text):
        """把一句话发给大模型（后台线程），回复流式显示在气泡里，同时边生成边说。"""
        if self.chat is None:
            return
        self.chat.cancel()
        self.chat_full, self.chat_shown, self.chat_active = "", 0.0, True
        self.chat_voice = "pamper"
        sink = None
        if self.voice is not None and self.stats.get("voice_enabled", True):
            sink = self.voice.start_stream(lambda: self.chat_voice)
        self.chat.ask(text, speech_sink=sink)
        self.set_pose("reading", 30.0, quiet=True)  # 思考中
        self.last_interaction_time = time.time()

    def _pump_chat(self, dt):
        """每帧取出对话事件：动作标签换姿态，文字按打字机速度逐字显示。"""
        if self.chat is None or not self.chat_active:
            return
        now = time.time()
        while True:
            try:
                kind, val = self.chat.events.get_nowait()
            except Exception:
                break
            if kind == "pose":
                if not self.chat_full:
                    self.chat_voice = POSE_TO_VOICE.get(val, "pamper")  # 第一个动作决定语气
                self.set_pose(val, 6.0, quiet=True)
            elif kind == "text":
                self.chat_full += val
            elif kind == "done":
                self.chat_full = val
            elif kind == "error":
                self.chat_active = False
                self.set_pose("crying", 3.5, quiet=True)
                self.say(f"呜……{val}", 5.0, speak=False)
                return
        self.last_interaction_time = now
        if not self.chat_full:
            dots = "……"[: int(now * 3) % 3]
            self.custom_bubble_text = "嗯，让我想想" + dots
            self.custom_bubble_until = now + 1.0
            return
        if self.state == "READING":
            self.state_lock_until = now + 6.0
        self.chat_shown = min(len(self.chat_full), self.chat_shown + dt * TYPE_CPS)
        self.custom_bubble_text = self._wrap_tail(self.chat_full[:int(self.chat_shown)], CHAT_BUBBLE_LINES)
        self.custom_bubble_until = now + 6.0
        if not self.chat.busy and int(self.chat_shown) >= len(self.chat_full):
            self.chat_active = False
            self.add_affection(1)
            if self.needs is not None:
                self.needs.chatted()

    def feed(self, food, pose, pts):
        """右键投喂：换吃东西的姿态、加好感、补饱腹度/心情/精力。"""
        self.set_pose(pose, 4.0)
        self.add_affection(pts)
        self.stats["feeds_count"] = self.stats.get("feeds_count", 0) + 1
        if self.needs is not None:
            self.needs.feed(food)
            self._save_needs()

    def _save_needs(self):
        if self.needs is not None:
            self.stats["needs"] = self.needs.to_dict()
            save_pet_stats(self.stats)
            self._needs_saved_at = time.time()

    def _update_needs(self, now):
        """每秒更新一次数值；空闲时按需提醒；每分钟存一次盘。"""
        if self.needs is None or now - self._needs_tick_at < 1.0:
            return
        self._needs_tick_at = now
        working = self.state in ("WORK", "TYPING") or self.is_focus_work_mode or self.pomodoro_active
        self.needs.tick(now, working=working, sleeping=self.state == "SLEEP")
        busy = self.is_dragging or self.is_falling or self.chat_active or \
            (self.state_lock_until > 0 and self.state != self.ambient_pose) or \
            (self.voice is not None and self.voice.is_busy())
        if not busy and not self.calm:  # 平静模式不冒饿了/累了这类提醒
            for kind in self.needs.due_alerts(now, working=working, limit=1):  # 一次只提醒一件事
                poses, line = NEED_ALERTS[kind]
                self.ambient_pose = None
                self.set_pose(random.choice(poses), 5.0, quiet=True)
                self.say(line, 5.0)
        if now - self._needs_saved_at > 60:
            self._save_needs()

    # ------------------------------ 每日早报 ------------------------------
    def _run_ui_queue(self):
        while True:
            try:
                fn = self._ui_q.get_nowait()
            except Exception:
                break
            try:
                fn()
            except Exception as e:
                print(f"界面任务出错: {e}")

    def run_briefing(self, auto=False):
        """生成并播报今日早报（后台线程生成，界面线程播报）。auto=True 时按设置排队推送到文件传输助手。"""
        if briefing is None or self._briefing_running:
            return
        self._briefing_running = True
        self.set_pose("reading", 10.0, quiet=True)
        self.say("我翻翻今天的安排……", 3.0, speak=False)
        needs = self.needs.to_dict() if self.needs is not None else None

        def _work():
            try:
                br = briefing.make_briefing(needs=needs, settings=self.briefing_settings)
            except Exception as e:
                br = None
                err = str(e)
            def _show():
                self._briefing_running = False
                if br is None:
                    self.say(f"早报没整理出来（{err}）", 4.0, speak=False)
                    return
                self.stats["briefing_day"] = time.strftime("%Y-%m-%d")
                save_pet_stats(self.stats)
                self.state_lock_until = 0
                self.set_pose(br["pose"], 8.0, quiet=True)
                self.say(br["speech"], 10.0)
                self.last_briefing = br
                if auto and self.briefing_settings.get("push_chatapp", True):
                    self._push_pending = (br["push"], time.time() + 3 * 3600, 0)
            self._ui_q.put(_show)
        import threading as _th
        _th.Thread(target=_work, daemon=True).start()

    def push_briefing_now(self):
        """右键菜单：马上把今日早报发到手机（文件传输助手）。主人自己点的，不等空闲。"""
        if briefing is None:
            return
        br = getattr(self, "last_briefing", None)
        needs = self.needs.to_dict() if self.needs is not None else None
        self.say("好，我把早报发到你的文件传输助手，发送时会动一下鼠标键盘，稍等几秒别碰哦~", 5.0, speak=False)

        def _work():
            b = br or briefing.make_briefing(needs=needs, settings=self.briefing_settings)
            res = briefing.push_to_chatapp(b["push"], require_idle=False)
            self._ui_q.put(lambda: self._report_push(res))
        import threading as _th
        _th.Thread(target=_work, daemon=True).start()

    def _report_push(self, res):
        if res == "sent":
            self.say("早报已经发到你的文件传输助手啦，手机上也能看到~ 📲", 4.0, speak=False)
        elif res != "deferred":
            self.say(f"早报没发出去：{res[7:] if res.startswith('failed:') else res}", 5.0, speak=False)

    def _check_briefing(self, now):
        """每 30 秒看一次：今天还没播早报，且已到设定时间（默认 9 点）或今天早上 6 点后刚启动 → 播报。
        有待推送的早报时，电脑空闲 2 分钟以上才发（发送会操作鼠标键盘），否则推迟，3 小时内有效，失败最多重试 3 次。"""
        if briefing is None:
            return
        if now - self._briefing_check_at >= 30 or self._briefing_check_at == 0:
            self._briefing_check_at = now
            lt = time.localtime(now)
            today = time.strftime("%Y-%m-%d", lt)
            just_started = now - self._started_at < 90 and lt.tm_hour >= 6
            if self.stats.get("briefing_day") != today and (lt.tm_hour >= self.briefing_settings.get("hour", 9) or just_started) \
                    and not (self.is_dragging or self.chat_active):
                self.run_briefing(auto=True)
        if self._push_pending and now >= self._push_try_at:
            text, deadline, fails = self._push_pending
            if now > deadline or fails >= 3:
                self._push_pending = None
                return
            self._push_try_at = now + 60
            self._push_pending = None  # 发送期间不重复发起

            def _work():
                res = briefing.push_to_chatapp(text, require_idle=True)
                def _after():
                    if res == "deferred":
                        self._push_pending = (text, deadline, fails)
                    elif res != "sent" and "登录" not in res:
                        # 聊天软件没登录时不再重试（每次尝试都会把聊天软件窗口唤到前台，打扰主人）
                        self._push_pending = (text, deadline, fails + 1)
                    self._report_push(res)
                self._ui_q.put(_after)
            import threading as _th
            _th.Thread(target=_work, daemon=True).start()

    # ------------------------------ 智能体任务 ------------------------------
    def open_agent_board(self):
        """打开智能体任务看板（无控制台窗口）。"""
        import subprocess
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        subprocess.Popen([exe, os.path.join(BASE_DIR, "plugins", "canaan_agent_board.py")], cwd=BASE_DIR)
        self.say("打开智能体任务看板啦，Muse 和 Dots 在忙什么一目了然~", 3.0, speak=False)

    def _start_agent_watch(self):
        """每实例一个只读轮询；重启换代，旧请求与旧界面回调不能复用。"""
        with self._agent_watch_lock:
            if self._agent_watch_destroyed:
                return
            if not getattr(self, "_agent_watch_bound", False):
                self.bind("<Destroy>", self._on_agent_watch_destroy, add="+")
                self._agent_watch_bound = True
            worker = self._agent_watch_thread
            if worker is not None and worker.is_alive():
                if self._agent_watch_stop.is_set():
                    # 旧 I/O 尚在返回时沿用线程；下一轮读取新的停止信号。
                    self._agent_watch_stop = threading.Event()
                return worker
            try:
                import canaan_agent_board_data as abd
            except Exception:
                return
            self._agent_watch_stop = threading.Event()

            def _current(stop):
                with self._agent_watch_lock:
                    return (not self._agent_watch_destroyed and
                            self._agent_watch_stop is stop and not stop.is_set())

            def _deliver(m, d, stop):
                with self._agent_watch_lock:
                    if _current(stop):
                        self._on_agents(m, d)

            def _loop():
                while True:
                    with self._agent_watch_lock:
                        stop = self._agent_watch_stop
                        if not _current(stop):
                            self._agent_watch_thread = None
                            return
                    try:
                        m = abd.fetch_muse()
                        if not _current(stop):
                            continue
                        d = abd.fetch_dots()
                        with self._agent_watch_lock:
                            if _current(stop):
                                self._ui_q.put(lambda m=m, d=d, stop=stop: _deliver(m, d, stop))
                    except Exception:
                        # 数据层通常返回离线快照；意外异常也不能杀死后续刷新。
                        pass
                    stop.wait(10)

            worker = threading.Thread(target=_loop, daemon=True)
            self._agent_watch_thread = worker
            try:
                worker.start()
            except Exception:
                self._agent_watch_stop.set()
                self._agent_watch_thread = None
                raise
            return worker

    def _stop_agent_watch(self):
        """只发停止信号，不在 Tk 线程等待已有的限时请求。"""
        with self._agent_watch_lock:
            stop = getattr(self, "_agent_watch_stop", None)
            if stop is not None:
                stop.set()

    def _on_agent_watch_destroy(self, event):
        # <Destroy> 也会收到子控件事件；只响应桌宠窗口本身。
        if event.widget is self:
            with self._agent_watch_lock:
                self._agent_watch_destroyed = True
                self._stop_agent_watch()

    def open_muse_web(self):
        import webbrowser
        from canaan_agent_board_data import MUSE_WEB
        webbrowser.open(MUSE_WEB)
        self.say("正在为你打开 OpenMuse 智能体工作台~", 3.0, speak=False)

    def open_dots_web(self):
        import webbrowser
        from canaan_agent_board_data import DOTS_WEB
        webbrowser.open(DOTS_WEB)
        self.say("正在为你打开 OpenDots 知识库空间~", 3.0, speak=False)

    def _on_agents(self, muse, dots):
        tasks = [("Muse", t) for t in muse.get("tasks", [])] + [("Dots", t) for t in dots.get("tasks", [])]
        self.agent_counts = {
            "running": sum(t["status"] == "running" for _, t in tasks),
            "waiting": sum(t["status"] == "waiting" for _, t in tasks),
            "muse_online": muse.get("online", False),
            "dots_online": dots.get("online", False),
        }
        for who, t in tasks:
            key = (who, t["id"])
            if t["status"] == "waiting" and key not in self._agent_waiting_told:
                self._agent_waiting_told.add(key)
                if not (self.chat_active or self.is_dragging):
                    self.set_pose("audit", 4.0, quiet=True)
                    self.say(f"{who} 的任务「{t['title'][:12]}」在等你处理，右键打开任务看板看看~", 5.0)
            elif t["status"] != "waiting":
                self._agent_waiting_told.discard(key)

    def _draw_agent_hud(self):
        c = getattr(self, "agent_counts", None)
        if not c or self.pomodoro_active:
            return
        if c.get("running") or c.get("waiting"):
            text = "🤖 " + " · ".join(s for s in (f"进行中 {c['running']}" if c.get('running') else "",
                                                 f"等你处理 {c['waiting']}" if c.get('waiting') else "") if s)
            fill_col = "#f59e0b" if c.get("waiting") else "#38bdf8"
        elif c.get("muse_online") and c.get("dots_online"):
            text = "✦ Muse · Dots 就绪"
            fill_col = "#34d399"
        elif c.get("muse_online"):
            text = "✦ Muse 就绪"
            fill_col = "#38bdf8"
        elif c.get("dots_online"):
            text = "✦ Dots 就绪"
            fill_col = "#a78bfa"
        else:
            return

        self.canvas.create_text(self.CANVAS_W // 2, self.CANVAS_H - 18, text=text,
                                fill=fill_col, font=("Microsoft YaHei UI", 9, "bold"),
                                tags="agents")

    def toggle_sense(self):
        """右键菜单：开/关桌面感知。"""
        on = not self.stats.get("sense_enabled", True)
        self.stats["sense_enabled"] = on
        save_pet_stats(self.stats)
        if self.sense is not None:
            self.sense.enabled = on
        if not on and self.ambient_pose and self.state == self.ambient_pose:
            self.state, self.ambient_pose = "IDLE", None
        self.say("好，我会留意你在忙什么，陪你一起~" if on else "好，我不看你的窗口啦，安心做事~", 3.0, speak=False)

    def _apply_sense(self, now):
        """根据桌面感知切换姿态：敏感窗口拔匕首/戴面纱并提醒；写代码敲电脑/疯狂打字；听歌戴耳机。
        正在做主人点出来的动作、聊天、被拖动、番茄钟/伴读模式时不打断。"""
        if self.sense is None or not self.sense.enabled:
            return
        st = self.sense.latest
        busy = self.is_dragging or self.is_falling or self.chat_active or self.is_focus_work_mode or self.pomodoro_active
        # 1. 敏感窗口：同一个窗口 2 分钟内只提醒一次（提醒里不出现窗口标题）
        if st.mode == "privacy":
            last = self._privacy_warned.get(st.window_key, 0)
            if now - last > PRIVACY_WARN_COOLDOWN and not self.is_dragging:
                self._privacy_warned = {k: v for k, v in self._privacy_warned.items() if now - v < 3600}
                self._privacy_warned[st.window_key] = now
                self.ambient_pose = None
                if st.recorder:
                    self.set_pose("mask", 5.0, quiet=True)
                    self.say("有录屏软件开着，又是密码或支付页面，小心别把敏感信息录进去！", 5.0, emotion="dangerous")
                else:
                    self.set_pose("action", 5.0, quiet=True)
                    self.say("这是密码或支付页面，我替你守着，不看也不记~", 4.5, emotion="dangerous")
            return
        # 2. 写代码 / 听歌：只在“闲着”的状态下自动切换（平静模式不跟着换姿态，敏感窗口提醒照常）
        desired = None if self.calm else AMBIENT_POSES.get(st.mode)
        if desired == "WORK":
            if st.typing:
                self._typing_until = now + TYPING_HOLD_SEC
            if now < self._typing_until:
                desired = "TYPING"
        allowed = not busy and self.state in AMBIENT_OK_STATES and \
            (self.state_lock_until == 0 or self.state == self.ambient_pose)
        if desired and allowed:
            if self.state != desired:
                if self.state not in ("WORK", "TYPING") or desired not in ("WORK", "TYPING"):
                    self.wobble_vy = -2.2  # 换姿态时的小蓄力
                self.state = desired
                self.state_lock_until = 0
            self.ambient_pose = desired
            self.last_interaction_time = now  # 陪着干活，不自己睡着
        elif not desired and self.ambient_pose:
            if self.state == self.ambient_pose:
                self.state = "IDLE"
            self.ambient_pose = None

    @staticmethod
    def _wrap_lines(text, width=BUBBLE_LINE_CHARS, font=None, max_pixel_w=190):
        """均衡黄金排版折行：支持标点智能断句、均分双行与物理像素测距，彻底杜绝气泡溢出出界"""
        if not text:
            return []
        if font is not None:
            import math
            lines = []
            delims = set("，。！？、；…,~!?; ")
            for paragraph in str(text).split("\n"):
                if not paragraph.strip():
                    continue
                total_w = font.measure(paragraph)
                if total_w <= max_pixel_w:
                    lines.append(paragraph.strip())
                    continue

                num_lines = max(2, math.ceil(total_w / (max_pixel_w - 20)))
                target_len = math.ceil(len(paragraph) / num_lines)
                cur_para = paragraph
                while cur_para:
                    if font.measure(cur_para) <= max_pixel_w:
                        lines.append(cur_para.strip())
                        break

                    best_split = min(len(cur_para), target_len)
                    split_found = False
                    # 优先在目标字数附近的标点处自然折行
                    for offset in [0, 1, -1, 2, -2, 3, -3, 4, -4]:
                        idx = target_len + offset
                        if 2 <= idx < len(cur_para) and cur_para[idx - 1] in delims:
                            if font.measure(cur_para[:idx]) <= max_pixel_w:
                                best_split = idx
                                split_found = True
                                break

                    if not split_found:
                        # 确保不超最大像素宽
                        for idx in range(min(len(cur_para), target_len + 3), 2, -1):
                            if font.measure(cur_para[:idx]) <= max_pixel_w:
                                best_split = idx
                                break

                    lines.append(cur_para[:best_split].strip())
                    cur_para = cur_para[best_split:].strip()

            return lines or [str(text)]

        lines = []
        for para in (text or "").split("\n"):
            while len(para) > width:
                lines.append(para[:width])
                para = para[width:]
            lines.append(para)
        return lines

    def _wrap_tail(self, text, max_lines):
        """自动换行，只保留最后几行（流式回复较长时气泡不会一直变高）。"""
        return "\n".join(self._wrap_lines(text)[-max_lines:])

    def _on_right_click(self, event):
        """右键 72 态全景交互分类控制菜单"""
        self.last_interaction_time = time.time()
        lvl = self.stats.get("level", 1)
        title = self.stats.get("title", "熟络")
        pts = self.stats.get("affection_pts", 0)

        menu = tk.Menu(self, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#6366f1", font=("Microsoft YaHei UI", 9))
        menu.add_command(label=f"💖 团团好感度: Lv.{lvl}【{title}】({pts}分)", command=self._show_affection_card)
        if self.chat is not None:
            src = "DeepSeek" if self.chat.provider == "deepseek" else "本地模型"
            menu.add_command(label=f"💬 和团团聊天（双击也可以，{src}）", command=self.open_chat_input)
        menu.add_separator()

        # 1. 🍱 美食与投喂
        sub_food = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#ec4899")
        sub_food.add_command(label="🍖 啃西域大烤肉 (+5好感 饱腹+35)", command=lambda: self.feed("eating", "eating", 5))
        sub_food.add_command(label="🍰 投喂西域点心 (+3好感 饱腹+20)", command=lambda: self.feed("dessert", "cheer", 3))
        sub_food.add_command(label="🧋 嚼大杯珍珠奶茶 (+2好感 饱腹+15)", command=lambda: self.feed("boba", "boba", 2))
        sub_food.add_command(label="🥟 拆小笼包干饭 (+2好感 饱腹+30)", command=lambda: self.feed("takeout", "takeout", 2))
        sub_food.add_command(label="🌿 捧端午大肉粽 (+3好感 饱腹+25)", command=lambda: self.feed("zongzi", "zongzi", 3))
        sub_food.add_command(label="🥮 月牙啃大月饼 (+3好感 饱腹+25)", command=lambda: self.feed("mooncake", "mooncake", 3))
        sub_food.add_command(label="🍲 捧热腾腾水饺 (+2好感 饱腹+25)", command=lambda: self.feed("dumpling", "dumpling", 2))
        sub_food.add_command(label="☕ 喝提神热美式 (精力+20)", command=lambda: self.feed("coffee", "coffee", 0))
        sub_food.add_command(label="🍵 捧温茶喝水小憩 (精力+15)", command=lambda: self.feed("tea", "tea", 0))
        menu.add_cascade(label="🍱 美食与投喂 (8态)", menu=sub_food)

        # 2. 💼 伴工与商务
        sub_work = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#6366f1")
        sub_work.add_command(label="💻 伴工敲小电脑", command=lambda: self.set_pose("work", 4.0))
        sub_work.add_command(label="⌨️ 疯狂敲键盘 (BongoCat)", command=lambda: self.set_pose("typing", 4.0))
        sub_work.add_command(label="📞 接听商务洽谈电话", command=lambda: self.set_pose("phone", 4.0))
        sub_work.add_command(label="📈 业绩增长大屏汇报", command=lambda: self.set_pose("chart", 4.0))
        sub_work.add_command(label="🔍 放大镜逐行查账报表", command=lambda: self.set_pose("audit", 4.0))
        sub_work.add_command(label="📖 佩戴眼镜认真读书", command=lambda: self.set_pose("reading", 4.0))
        sub_work.add_command(label="💰 招财进宝数大元宝", command=lambda: (self.set_pose("money", 4.0), self._spawn_floating_particle("💰 财源滚滚", "#facc15")))
        menu.add_cascade(label="💼 伴工与商务 (7态)", menu=sub_work)

        # 3. 💖 趣味与情绪
        sub_fun = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#f59e0b")
        sub_fun.add_command(label="🪙 团团命运金币 (纠结帮选)", command=self.flip_coin)
        sub_fun.add_command(label="🎮 摸鱼电竞打游戏", command=lambda: self.set_pose("game", 4.0))
        sub_fun.add_command(label="💃 西域风情欢快跳舞", command=lambda: self.set_pose("dance", 4.0))
        sub_fun.add_command(label="🎁 领取今日心意礼物", command=lambda: (self.set_pose("gift", 4.0), self.add_affection(3)))
        sub_fun.add_command(label="💫 蚊香眼晕头转向", command=lambda: self.set_pose("dizzy", 3.5))
        sub_fun.add_command(label="🥺 委屈抹眼泪求安慰", command=lambda: self.set_pose("crying", 3.5))
        sub_fun.add_command(label="😱 搞笑惊呆大跌眼镜", command=lambda: self.set_pose("shock", 3.5))
        sub_fun.add_command(label="🥱 伸懒腰打大哈欠", command=lambda: self.set_pose("yawn", 3.5))
        sub_fun.add_command(label="😤 傲娇抱胸背对别扭", command=lambda: self.set_pose("tsundere", 3.5))
        sub_fun.add_command(label="😍 双眼冒大红心花痴", command=lambda: self.set_pose("heart_eyes", 3.5))
        sub_fun.add_command(label="🙇‍♀️ 跪坐膜拜代码大佬", command=lambda: self.set_pose("worship", 3.5))
        menu.add_cascade(label="💖 趣味与情绪 (11态)", menu=sub_fun)

        # 4. 🥋 武学姿态
        sub_wushu = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#eab308")
        sub_wushu.add_command(label="🛡️ 当！金光振刀反击", command=lambda: self.set_pose("parry", 4.0))
        sub_wushu.add_command(label="⚔️ 太刀蓄力百裂斩", command=lambda: self.set_pose("blade", 4.0))
        sub_wushu.add_command(label="🐉 长枪龙王破突进", command=lambda: self.set_pose("spear", 4.0))
        sub_wushu.add_command(label="💨 潇洒地面极速滑铲", command=lambda: self.set_pose("slide", 3.5))
        sub_wushu.add_command(label="🤫 屋檐暗杀潜行侦查", command=lambda: self.set_pose("crouch", 4.0))
        sub_wushu.add_command(label="🔥 返魂回煞满血重生", command=lambda: self.set_pose("resurrect", 4.0))
        sub_wushu.add_command(label="🗡️ 双匕首出鞘警戒", command=lambda: self.set_pose("action", 4.0))
        sub_wushu.add_command(label="🌪️ 寂静暗刑金沙暴大招", command=lambda: self.set_pose("stealth", 4.0))
        sub_wushu.add_command(label="🏹 金色飞索破空穿梭", command=lambda: self.set_pose("grapple", 3.5))
        sub_wushu.add_command(label="🧗‍♀️ 飞索挂壁吸附攀爬", command=lambda: self.set_pose("climb", 4.0))
        menu.add_cascade(label="🥋 武学姿态 (10态)", menu=sub_wushu)

        # 5. 🏢 现代职场百态
        sub_office = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#06b6d4")
        sub_office.add_command(label="🧖‍♀️ 熬夜敷眼膜贴养生", command=lambda: self.set_pose("mask_spa", 4.0))
        sub_office.add_command(label="🎧 戴耳麦视频例会", command=lambda: self.set_pose("headset", 4.0))
        sub_office.add_command(label="😖 冰美式太苦皱眉吐舌", command=lambda: self.set_pose("coffee_spill", 3.5))
        sub_office.add_command(label="🔥 必胜头巾爆肝冲刺", command=lambda: self.set_pose("overtime", 4.0))
        sub_office.add_command(label="🤯 方案改十版抓狂", command=lambda: self.set_pose("headache", 3.5))
        sub_office.add_command(label="🕵️‍♀️ 猎鹿帽大侦探破案", command=lambda: self.set_pose("detective", 4.0))
        menu.add_cascade(label="🏢 现代职场百态 (6态)", menu=sub_office)

        # 6. 🌿 深度生活温馨
        sub_home = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#10b981")
        sub_home.add_command(label="🐱 怀抱小黑猫抚摸", command=lambda: self.set_pose("cat", 4.0))
        sub_home.add_command(label="🫧 吹泡泡糖爆在鼻尖", command=lambda: self.set_pose("bubble_gum", 3.5))
        sub_home.add_command(label="💪 举粉色小哑铃健身", command=lambda: self.set_pose("fitness", 4.0))
        sub_home.add_command(label="🎵 戴头戴耳机闭眼晃", command=lambda: self.set_pose("music_headset", 4.0))
        sub_home.add_command(label="🌱 喷水壶浇多肉绿植", command=lambda: self.set_pose("watering", 4.0))
        sub_home.add_command(label="📸 手机自拍比耶留念", command=lambda: self.set_pose("selfie", 4.0))
        sub_home.add_command(label="📦 纸箱特工暗中观察", command=lambda: self.set_pose("box_hide", 4.0))
        sub_home.add_command(label="🫂 隔空贴贴屏幕玻璃", command=lambda: self.set_pose("hug_screen", 4.0))
        menu.add_cascade(label="🌿 深度生活温馨 (8态)", menu=sub_home)

        # 7. 🏮 传统节日欢庆
        sub_fest = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#f43f5e")
        sub_fest.add_command(label="🧨 新春捂耳点大爆竹", command=lambda: self.set_pose("firecracker", 4.0))
        sub_fest.add_command(label="🎃 万圣南瓜提灯搞怪", command=lambda: self.set_pose("pumpkin", 4.0))
        sub_fest.add_command(label="🏮 元宵挑玉兔琉璃花灯", command=lambda: self.set_pose("lantern", 4.0))
        sub_fest.add_command(label="☂️ 雨天撑小折叠伞", command=lambda: self.set_pose("umbrella", 4.0))
        sub_fest.add_command(label="🦆 夏日小鸭泳圈度假", command=lambda: self.set_pose("swim", 4.0))
        sub_fest.add_command(label="🧣 冬日红大围巾暖心", command=lambda: self.set_pose("scarf", 4.0))
        menu.add_cascade(label="🏮 传统节日欢庆 (6态)", menu=sub_fest)

        # 8. 👑 终极高光幻化
        sub_trans = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#a855f7")
        sub_trans.add_command(label="👰 西域红绡绝色嫁衣", command=lambda: self.set_pose("wedding", 5.0))
        sub_trans.add_command(label="🪽 璀璨金光天使神翼", command=lambda: self.set_pose("angel_wings", 5.0))
        sub_trans.add_command(label="🎩 礼帽变和平白鸽魔术", command=lambda: self.set_pose("magician", 4.5))
        sub_trans.add_command(label="🏍️ 酷飒暗黑机车女骑", command=lambda: self.set_pose("motorcycle", 4.5))
        sub_trans.add_command(label="💤 小鸡啄米打瞌睡微困", command=lambda: self.set_pose("sleepy_nod", 4.0))
        sub_trans.add_command(label="⭐ 双手捧七彩许愿流星", command=lambda: self.set_pose("rainbow_star", 5.0))
        sub_trans.add_command(label="🧕 西域神秘金丝面纱", command=lambda: self.set_pose("mask", 4.0))
        sub_trans.add_command(label="👑 商业女皇权杖巅峰", command=lambda: self.set_pose("crown", 5.0))
        menu.add_cascade(label="👑 终极高光幻化 (8态)", menu=sub_trans)

        menu.add_separator()
        pom_label = "🍅 停止专注番茄钟" if self.pomodoro_active else "🍅 开启25分钟专注番茄钟"
        menu.add_command(label=pom_label, command=lambda: self.stop_pomodoro() if self.pomodoro_active else self.start_pomodoro(25))
        menu.add_command(label="🧹 一键释放系统内存优化", command=self.clean_memory)
        menu.add_command(label="🚶 任务栏漫步巡逻", command=self._trigger_walk)

        menu.add_separator()
        if briefing is not None:
            # 本地早报（不上传）；云端早报只在 data/canaan_pet_briefing.json 里打开 use_cloud_briefing 才会用
            menu.add_command(label="☀️ 今日早报（团团播报）", command=lambda: self.run_briefing(auto=False))
            menu.add_command(label="📲 把今日早报发到手机（文件传输助手）", command=self.push_briefing_now)
        menu.add_command(label="退出团团（Ctrl+Alt+Q）", command=self.request_exit)
        menu.add_command(label="⚙️ 打开设置大屏 (Cockpit)", command=lambda: os.system("start py -3.12 shells\\canaan_settings_cockpit.py"))
        menu.add_command(label="🧭 智能体任务看板（Muse / Dots）", command=self.open_agent_board)
        menu.add_command(label="🖥️ 电脑自主控制 (Computer Use) 诊断与测试", command=self.test_computer_use)
        hub = getattr(self.master, "hub_instance", None) or getattr(self, "hub", None)
        if hub is not None:
            menu.add_command(label="团团消息与任务看板 / 知识库 / 反馈", command=hub.open_control_center)
            if getattr(hub, "is_hidden", False):
                menu.add_command(label="🐵 显示顶部灵动胶囊", command=self.show_hub)
            else:
                menu.add_command(label="✨ 切换顶部灵动微岛（展开/折叠）", command=self.toggle_hub_capsule)
                menu.add_command(label="🙈 隐藏顶部状态栏（仅保留桌面伴侣）", command=self.hide_hub)
        menu.add_command(label="🌐 打开 OpenMuse 智能体工作台", command=self.open_muse_web)
        menu.add_command(label="🌐 打开 OpenDots 知识库空间", command=self.open_dots_web)
        menu.add_separator()
        if self.voice is not None:
            menu.add_command(label="🔇 关闭语音（只冒气泡）" if self.stats.get("voice_enabled", True) else "🔊 开启语音",
                             command=self.toggle_voice)
        if self.sense is not None:
            menu.add_command(label="👀 关闭桌面感知（不再看前台窗口）" if self.stats.get("sense_enabled", True)
                             else "👀 开启桌面感知（写代码/听歌/敏感页面）", command=self.toggle_sense)
        sub_pack = tk.Menu(menu, tearoff=0, bg="#13141f", fg="#ffffff", activebackground="#6366f1")
        try:
            from canaan_pet_pack import list_installed
            installed = list_installed()
        except Exception:
            installed = []
        current = self.stats.get("pet_pack")
        for _pid, pname, ppath in installed:
            sub_pack.add_command(label=("● " if ppath == current else "○ ") + pname,
                                 command=lambda p=ppath: self.use_pet_pack(p))
        if installed:
            sub_pack.add_separator()
        sub_pack.add_command(label="📦 导入形象包（pet.json，只导入你有权使用的形象）…", command=self.import_pet_pack)
        if current:
            sub_pack.add_command(label=f"↩ 恢复默认形象（当前：{self.pack.name if self.pack else '?'}）", command=self.reset_pet_pack)
        sub_pack.add_command(label="🙈 关闭光标注视" if self.stats.get("gaze_enabled", False) else "👀 开启光标注视（V2 形象包，平静模式下不生效）",
                             command=self.toggle_gaze)
        menu.add_command(label="🌙 关闭平静模式（恢复动画和提醒）" if self.calm else "🌙 开启平静模式（工作时安静，只提示要确认和失败的事）",
                         command=self.toggle_calm)
        menu.add_cascade(label="🎭 形象", menu=sub_pack)
        menu.add_command(label="💤 让团团去小憩一下", command=self._force_sleep)
        menu.add_command(label="👻 隐藏挂件", command=self.withdraw)
        menu.post(event.x_root, event.y_root)

    def test_computer_use(self):
        self._spawn_floating_particle("🖥️ 1ms WinMM / 贝塞尔", color="#38bdf8")
        self.say("亲爱的，电脑自主控制引擎运转流畅，支持 1 毫秒高精定时与贝塞尔平滑光标运动~", 4.0)

    def _show_affection_card(self):
        lvl = self.stats.get("level", 1)
        title = self.stats.get("title", "熟络")
        pts = self.stats.get("affection_pts", 0)
        pats = self.stats.get("pats_count", 0)
        feeds = self.stats.get("feeds_count", 0)
        poms = self.stats.get("pomodoro_count", 0)

        card_text = (
            f"👑 团团具身伴侣 · 72态大圆满全息状态\n"
            f"-----------------------------------\n"
            f"💖 羁绊阶梯: Lv.{lvl}【{title}】\n"
            f"🌟 亲密度点数: {pts} / 1000\n"
            f"💆 累计摸头心动: {pats} 次\n"
            f"🍱 美食投喂次数: {feeds} 次\n"
            f"🍅 深度伴读番茄钟: {poms} 次\n"
            + (f"🥟 饱腹 {self.needs.hunger:.0f}  😊 心情 {self.needs.mood:.0f}  ⚡ 精力 {self.needs.energy:.0f}\n"
               if self.needs is not None else "") +
            f"⚡ GPU VRAM 占用: 0.0 MB (60FPS 流体光栅)"
        )
        self.say(card_text, 6.0, speak=False)  # 多行数值卡片只显示，不念

    def _force_sleep(self):
        self.state = "SLEEP"
        self.say("本小姐先眯一会儿……有事敲敲我哦……Zzz... 🌙", 3.5)

    def _trigger_walk(self):
        self.state = "WALK"
        self.walk_vx = random.choice([-1.5, 1.5])
        self.facing_left = (self.walk_vx < 0)
        self.walk_steps_left = 120
        self.say("在任务栏漫步巡逻，呼吸新鲜空气~ 🌸", 3.0)

    def _update_speech_bubble(self):
        now = time.time()
        if self.custom_bubble_text and self.is_speaking():
            self.custom_bubble_until = max(self.custom_bubble_until, now + 0.8)  # 话没说完气泡不消失
        if self.custom_bubble_text and now < self.custom_bubble_until:
            active_text = self.custom_bubble_text
        elif now < self.bubble_until:
            active_text = self.bubble_text
        else:
            active_text = None
            self.custom_bubble_text = None
            self.bubble_scale = 0.0

        if active_text:
            # Smooth scale up pop-in (弹簧弹性展开)
            self.bubble_scale += (1.0 - self.bubble_scale) * 0.22

            import tkinter.font as tkfont
            font = getattr(self, "bubble_font", None)
            if font is None:
                font = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
                self.bubble_font = font

            lines = self._wrap_lines(active_text, font=font, max_pixel_w=175)
            line_widths = [font.measure(l) for l in lines]
            max_line_w = max(line_widths) if line_widths else 80
            line_height = 20
            total_text_h = len(lines) * line_height

            # Apple 悬浮灵动胶囊动态气泡自适应尺寸（双侧预留充足 13px+ 内边距）
            full_w = min(self.CANVAS_W - 16, max(100, max_line_w + 26))
            full_h = total_text_h + 16

            bubble_w = max(10, int(full_w * self.bubble_scale))
            bubble_h = max(10, int(full_h * self.bubble_scale))

            bx = (self.CANVAS_W - bubble_w) // 2
            if now < self.action_hud_until and self.action_hud_task_id:
                by = 56 + int((1.0 - self.bubble_scale) * 8)
            else:
                by = 12 + int((1.0 - self.bubble_scale) * 8)

            # Apple 极简深色圆润悬浮胶囊气泡底色
            self._draw_rounded_pill(
                bx, by, bx + bubble_w, by + bubble_h,
                r=min(14, bubble_h // 2),
                fill="#101220", outline="#6366f1", width=1.5,
                tags="bubble"
            )

            # 向下指向团团头顶的小巧灵动指示尾巴
            if self.bubble_scale > 0.85:
                tail_cx = bx + bubble_w // 2
                tail_top_y = by + bubble_h
                self.canvas.create_polygon(
                    tail_cx - 6, tail_top_y - 2,
                    tail_cx + 6, tail_top_y - 2,
                    tail_cx, tail_top_y + 7,
                    fill="#101220", outline="#6366f1", width=1.5,
                    tags="bubble"
                )
                start_y = by + (bubble_h - total_text_h) // 2 + line_height // 2
                for i, line in enumerate(lines):
                    ly = start_y + i * line_height
                    self.canvas.create_text(
                        bx + bubble_w // 2, ly,
                        text=line, fill="#ffffff",
                        font=font,
                        justify=tk.CENTER, tags="bubble"
                    )

    def _update_pomodoro_hud(self):
        if self.pomodoro_active:
            rem = max(0, int(self.pomodoro_end_time - time.time()))
            m = rem // 60
            s = rem % 60
            self.canvas.create_text(
                self.CANVAS_W // 2, self.CANVAS_H - 18,
                text=f"🍅 专注中 {m:02d}:{s:02d}",
                fill="#f43f5e", font=("Segoe UI", 9, "bold"),
                tags="pomodoro"
            )
            if rem == 0:
                self.pomodoro_active = False
                self.set_pose("cheer", 5.0, "🎉 恭喜！番茄钟顺利完成！亲爱的辛苦啦，快起来喝杯茶！")

    def request_exit(self):
        # 退出回调可能先排队销毁窗口；此时也不能被并发 start 换代复活。
        if getattr(self, "_agent_watch_lock", None) is None:
            self._agent_watch_lock = threading.RLock()
        with self._agent_watch_lock:
            self._agent_watch_destroyed = True
            self._stop_agent_watch()
        save_pet_position(self.pos_x, self.pos_y)
        save_pet_stats(self.stats)
        if self.on_exit_callback:
            self.on_exit_callback()
        else:
            if self.voice is not None:
                self.voice.interrupt()
            if self.chat is not None:
                self.chat.cancel()
            self.destroy()
            hidden = getattr(self, "_hidden_root", None)
            if hidden is not None:
                hidden.destroy()

    # ---------- 统一提示中心：桌宠配合灵动岛 ----------
    NOTICE_REACT_EVERY = 60.0

    def on_notice(self, event, notice):
        """提示中心的新提示（show）：和灵动岛一起反应——转头看岛、做对应的动作、冒一句短气泡（不含聊天正文）。
        60 秒内只反应一次，错过的条数下次合并说；主人专心打字 / 全屏 / 游戏时不动（岛照样计数）。
        吸附在屏幕边时先探出头，说完再缩回去。气泡可以点：打开对应的应用 / 看板。"""
        if event != "show" or not notice:
            return

        def _apply():
            now = time.time()
            if self._quiet_now(now) or now - getattr(self, "_last_notice_react", 0.0) < self.NOTICE_REACT_EVERY:
                self._missed_notices = getattr(self, "_missed_notices", 0) + 1
                return
            missed, self._missed_notices = getattr(self, "_missed_notices", 0), 0
            self._last_notice_react = now
            pose, text = notice_reaction(notice, missed)
            try:   # 转头看向屏幕顶部正中的灵动岛
                self.facing_left = (self.pos_x + self.CANVAS_W / 2) > self.winfo_screenwidth() / 2
            except Exception:
                pass
            if getattr(self, "dock", None):
                self.dock_out, self._dock_out_until = True, now + 3.5     # 从边上探出头，3.5 秒后缩回
            self.wobble_vy -= 2.2
            if pose:
                self.set_pose(pose, 3.0, quiet=True)
            speak = bool(getattr(self, "speak_important", False)) and notice.get("variant") in ("waiting", "failed", "p0")
            self._bubble_target = (notice.get("id"), notice.get("variant"))
            self.say(text, 3.5, speak=speak)
            if notice.get("variant") == "waiting":
                self._tap_screen(notice.get("id"), 4)
        self._ui_q.put(_apply)

    def _tap_screen(self, notice_id, left):
        """等你处理：每 30 秒轻轻敲一下（只动不出声），最多 4 次；事情处理掉或安静时停。"""
        if left <= 0:
            return

        def _tap():
            if getattr(self, "_bubble_target", (None,))[0] != notice_id or self._quiet_now(time.time()):
                return
            self.wobble_vy -= 1.6
            self._tap_screen(notice_id, left - 1)
        try:
            self.after(30000, _tap)
        except Exception:
            pass

    def notice_done(self, notice_id):
        """提示被处理掉了：停止敲屏幕、气泡不再指向它。"""
        if getattr(self, "_bubble_target", (None,))[0] == notice_id:
            self._bubble_target = (None, None)

    def _on_bubble_click(self, _event=None):
        target = getattr(self, "_bubble_target", (None, None))
        cb = getattr(self, "on_notice_click", None)
        if target[0] and cb is not None and time.time() < self.custom_bubble_until:
            try:
                cb(target[0])
            except Exception:
                pass
            self._bubble_target = (None, None)
            self.custom_bubble_until = 0

    # ---------- 边缘吸附 ----------
    def _dock_to(self, side, x, y, quiet=False):
        """弹簧吸到边缘：窗口可以一部分在屏幕外（不走 _place_pet 的夹紧）。"""
        self.is_falling = False
        self.fall_vx = self.fall_vy = 0.0
        self.dock, self.dock_base, self.dock_out = side, (x, y), False
        self.dock_spring_x.snap(self.pos_x)
        self.dock_spring_y.snap(self.pos_y)
        self.dock_spring_x.set(x)
        self.dock_spring_y.set(y)
        self.wobble_vy = -2.5                         # 贴上去时轻轻一弹
        self.stats["dock"] = side
        save_pet_stats(self.stats)
        if not quiet:
            self.say("贴边待着，有事叫我~" if side != "top" else "倒挂着看你干活~", 1.8, speak=False)

    def _undock(self):
        self.dock = None
        self.dock_out = False
        self.stats.pop("dock", None)
        save_pet_stats(self.stats)

    def _update_dock(self, dt):
        """吸附中：光标靠近就滑出来一点，离开 1.5 秒后缩回去；位置由弹簧逐帧逼近。"""
        try:
            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            aw, ah = self._avatar_size
            cx = self.pos_x + self.CANVAS_W / 2
            cy = self.pos_y + self.CANVAS_H - ah / 2 if self.dock != "top" else self.pos_y + ah / 2
            near = math.hypot(pt.x - cx, pt.y - cy) < max(aw, ah) * 0.9
        except Exception:
            near = False
        now = time.time()
        if near:
            self.dock_out, self._dock_out_until = True, now + 1.5
        elif self.dock_out and now >= getattr(self, "_dock_out_until", 0):
            self.dock_out = False
        aw, ah = self._avatar_size
        off = motion.peek_offset(self.dock, aw, ah, self.dock_out)
        bx, by = self.dock_base
        tx = bx + off if self.dock == "left" else bx - off if self.dock == "right" else bx
        ty = by + off if self.dock == "top" else by
        self.dock_spring_x.set(tx)
        self.dock_spring_y.set(ty)
        self.pos_x = int(round(self.dock_spring_x.step(dt)))
        self.pos_y = int(round(self.dock_spring_y.step(dt)))
        self.geometry(geometry(self.CANVAS_W, self.CANVAS_H, self.pos_x, self.pos_y))

    def _quiet_now(self, now):
        """全屏 / 演示 / 勿扰、游戏、连续打字超过 2 分钟：不放待机动画（每 2 秒判断一次）。"""
        at, value = self._quiet_cache
        if now - at < 2.0:
            return value
        typing = bool(getattr(getattr(self.sense, "latest", None), "typing", False)) if self.sense is not None else False
        if typing:
            self._typing_since = self._typing_since or now
        else:
            self._typing_since = None
        try:
            import canaan_proactive
            busy_screen = not canaan_proactive.notification_ok()
        except Exception:
            busy_screen = False
        try:
            import canaan_gamemode
            game = canaan_gamemode.active()
        except Exception:
            game = False
        value = motion.quiet(fullscreen=busy_screen, game=game,
                             typing_streak=(now - self._typing_since) if self._typing_since else 0.0)
        self._quiet_cache = (now, value)
        return value

    def _place_pet(self, snap=0):
        self.pos_x, self.pos_y, self._work_area = place(
            self.pos_x, self.pos_y, self.CANVAS_W, self.CANVAS_H, self._work_areas, snap)
        self.ground_y = max(self._work_area[1], self._work_area[3] - self.CANVAS_H)
        self.geometry(geometry(self.CANVAS_W, self.CANVAS_H, self.pos_x, self.pos_y))

    def _render_avatar(self, base_img, k, nw, nh, draw_angle, flip_v=False):
        render_key = (k, nw, nh, self.facing_left, flip_v, round(draw_angle * 2) / 2)
        cached = self._render_cache.get(render_key)
        if cached is None:
            transformed = base_img.resize((nw, nh), Image.Resampling.BILINEAR)
            if self.facing_left:
                transformed = transformed.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if flip_v:
                transformed = transformed.transpose(Image.Transpose.FLIP_TOP_BOTTOM)   # 倒挂在屏幕顶部
            if abs(render_key[-1]) > 0.4:
                transformed = transformed.rotate(render_key[-1], resample=Image.Resampling.BILINEAR, expand=True)
            transformed.thumbnail((self.CANVAS_W - 8, self.CANVAS_H - 40), Image.Resampling.BILINEAR)
            cached = (ImageTk.PhotoImage(transformed), transformed.size)
            self._render_cache[render_key] = cached
            if len(self._render_cache) > 96:
                self._render_cache.popitem(last=False)
        else:
            self._render_cache.move_to_end(render_key)
        return cached


    def _animation_loop(self):
        now = time.time()
        dt = max(0.005, min(0.05, now - self.last_time))
        self.last_time = now

        for tag in ("particle", "bubble", "action_hud", "pomodoro", "agents", "need"):
            self.canvas.delete(tag)
        if now >= self._screen_check_at and not self.is_dragging:
            self._screen_check_at = now + 2.0
            self._work_areas = work_areas((0, 0, self.winfo_screenwidth(), self.winfo_screenheight()))
            if not self.dock:
                self._place_pet()   # 吸附在边缘时窗口本来就有一部分在屏幕外，不能夹回来

        # -------------------------------------------------------------
        # 1. PHYSICAL SIMULATION PIPELINE (60 FPS)
        # -------------------------------------------------------------

        # A. Squash & Stretch Spring Damper (果冻弹性衰减振荡)
        acc_y = -self.spring_k * self.wobble_y - self.spring_d * self.wobble_vy
        self.wobble_vy += acc_y * dt
        self.wobble_y += self.wobble_vy * dt

        # B. Pendulum Drag Angle Simulation
        target_angle = 0.0
        if self.is_dragging:
            target_angle = max(-22.0, min(22.0, -self.drag_vx * 0.45))
        elif self.state == "WALK":
            target_angle = math.sin(self.walk_phase) * 4.5 * (-1.0 if self.facing_left else 1.0)
        else:
            # Subtle cursor gaze lean (视线跟随微倾)
            target_angle = self.gaze_lean

        angle_acc = -self.angle_k * (self.angle - target_angle) - self.angle_d * self.angle_v
        self.angle_v += angle_acc * dt
        self.angle += self.angle_v * dt

        # C. 吸附在边缘：光标靠近滑出来，弹簧逐帧逼近
        if self.dock and not self.is_dragging:
            self._update_dock(dt)

        # C'. 甩出 / 下落：重力 + 空气阻力 + 撞墙反弹衰减；用力甩向墙直接吸附
        if self.is_falling and not self.dock:
            left, top, right, _ = self._work_area
            bounds = (left, top, right - self.CANVAS_W, self.ground_y)
            self.pos_x, self.pos_y, self.fall_vx, self.fall_vy, events = motion.projectile_step(
                self.pos_x, self.pos_y, self.fall_vx, self.fall_vy, dt, bounds, gravity=self.gravity)
            for ev in events:
                if ev[0] == "wall":
                    if ev[2] > motion.WALL_SNAP_SPEED:
                        aw, ah = self._avatar_size
                        self._dock_to(*motion.dock_position(ev[1], self.pos_x, self.pos_y, self.CANVAS_W,
                                                            self.CANVAS_H, aw, ah, self._work_area))
                        break
                    self.wobble_vy = -min(4.0, ev[2] * 0.003 + 1.0)   # 撞墙压扁
                elif ev[0] == "landed" and ev[1] > 160:
                    self.wobble_vy = -min(5.5, ev[1] * 0.008 + 2.0)   # 落地 Q 弹
                    self._spawn_burst_particles("💨", count=3, color="#94a3b8")
                elif ev[0] == "rest":
                    self.is_falling = False
                    self.fall_vx = self.fall_vy = 0.0
                    self.state = "IDLE"
                    save_pet_position(self.pos_x, self.pos_y)
            if not self.dock:
                self._place_pet()

        # D. Walking State Cycle（吸附在边缘时不自己走开）
        elif self.state == "WALK" and not self.is_dragging and not self.dock:
            if self.walk_steps_left > 0:
                self.walk_phase += dt * 8.5
                self.pos_x += self.walk_vx * dt * 45.0
                self.walk_steps_left -= 1
                
                # Edge bounce
                if self.pos_x < self._work_area[0] + 25:
                    self.walk_vx = abs(self.walk_vx)
                    self.facing_left = False
                elif self.pos_x > self._work_area[2] - self.CANVAS_W - 25:
                    self.walk_vx = -abs(self.walk_vx)
                    self.facing_left = True

                self._place_pet()
            else:
                self.state = "IDLE"
                self.facing_left = False
                save_pet_position(self.pos_x, self.pos_y)

        # E. Idle State Timers
        elif not self.is_dragging:
            # Auto sleep after 120s inactivity
            if self.calm and self.state in ("IDLE", "BLINK", "SLEEP"):
                # 平静模式（主人选：互动时生动、干活时安静）：不随光标倾斜、不自己睡着冒气泡；
                # 不在全屏 / 游戏 / 连续打字时，保留自然眨眼（3–7 秒一次）
                self.gaze_lean = 0.0
                if self.state == "SLEEP":
                    self.state = "IDLE"
                if self.state == "IDLE" and not self._quiet_now(now) and now - self.last_blink_time > random.uniform(3.0, 7.0):
                    self.last_blink_time = now
                    self.state = "BLINK"
                    self.after(180, lambda: setattr(self, "state", "IDLE") if self.state == "BLINK" else None)
            elif self.state not in ["SLEEP", "WORK", "TYPING", "MUSIC_HEADSET"] and now - self.last_interaction_time > 120:
                self.state = "SLEEP"
                # 没人操作时自动睡着：只冒气泡不出声，免得打扰专心做事的主人
                self.say("好安静呀……亲爱的也在专心致志吧……Zzz... 🌙", 4.0, speak=False)

            # Revert locked states after duration
            elif self.state_lock_until > 0 and now >= self.state_lock_until:
                self.state_lock_until = 0
                self.state = "IDLE"

            # Natural blinking in IDLE
            elif self.state == "IDLE":
                if now - self.last_blink_time > random.uniform(3.5, 6.0):
                    self.last_blink_time = now
                    self.state = "BLINK"
                    self.after(220, lambda: setattr(self, "state", "IDLE"))

                # Cursor Proximity Gaze Lean
                try:
                    pt = POINT()
                    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                    pet_center_x = self.pos_x + self.CANVAS_W // 2
                    cursor_dist_x = pt.x - pet_center_x
                    if abs(cursor_dist_x) < 320:
                        self.gaze_lean = max(-4.5, min(4.5, cursor_dist_x * 0.015))
                    else:
                        self.gaze_lean = 0.0
                except Exception:
                    self.gaze_lean = 0.0

        self._run_ui_queue()
        self._pump_chat(dt)
        self._apply_sense(now)
        self._update_needs(now)
        self._check_briefing(now)

        # -------------------------------------------------------------
        # 2. FLUID PROCEDURAL AVATAR TRANSFORMATION & DRAWING
        # -------------------------------------------------------------
        k = self.state.lower()
        speaking = self.is_speaking()
        if speaking:
            self.last_interaction_time = now  # 说话期间不会自动睡着
            if self.voice is not None and self.voice.is_speaking():
                mouth = self.voice.mouth_level()
            else:
                mouth = 1 if int(now * 7) % 2 == 0 else 2
            if self.state in TALK_LIPSYNC_STATES and not self.is_dragging:
                # 对口型：按当前播放的音量在闭嘴/半张/张嘴三帧之间切换
                k = MOUTH_FRAMES[mouth] if MOUTH_FRAMES[mouth] in self.base_images else "talk"
            elif mouth == 2 and self._last_mouth < 2:
                self.wobble_vy -= 0.6  # 特殊姿态说话：保持姿态，随音节轻轻一弹
            self._last_mouth = mouth
        if self.pack is not None:
            self._update_gaze(now)
            base_img, k = self._pack_image(k, now)
        else:
            base_img = self.base_images.get(k, self.base_images.get("idle"))

        # Compute dynamic scale factors
        # Natural breathing sinusoidal oscillation (conservation of volume)
        breath_y = 0.0
        breath_x = 0.0
        walk_bob_y = 0.0

        if (self.state in ["IDLE", "TALK", "BLINK", "WORK", "TEA"] or k.startswith("talk")) and not self._quiet_now(now):
            # 呼吸：平静模式很浅（±1.5%，4 秒一次），完整模式稍明显；全屏 / 游戏 / 连续打字时不动
            if self.calm:
                bx, by = motion.breathing(now)
                breath_x, breath_y = bx - 1.0, by - 1.0
            else:
                breath_y = math.sin(now * 3.2) * 0.025
                breath_x = -breath_y * 0.55
        stretch_x, stretch_y = motion.stretch(self.fall_vx, self.fall_vy) if self.is_falling else (1.0, 1.0)

        if self.state == "WALK":
            # Chibi cute step-bobbing
            walk_bob_y = -abs(math.sin(self.walk_phase)) * 7.0

        # Total combined scale factor
        total_scale_y = (1.0 + self.wobble_y + breath_y) * stretch_y
        total_scale_x = (1.0 - (self.wobble_y * 0.55) + breath_x) * stretch_x

        # Clamping
        total_scale_y = max(0.65, min(1.35, total_scale_y))
        total_scale_x = max(0.65, min(1.35, total_scale_x))

        bw, bh = base_img.size
        nw = max(10, int(bw * total_scale_x))
        nh = max(10, int(bh * total_scale_y))

        # 1. Resize for Squash & Stretch
        draw_angle = max(-25.0, min(25.0, self.angle))
        hanging = self.dock == "top"
        self.cur_photo_img, rendered_size = self._render_avatar(base_img, k, nw, nh, draw_angle, flip_v=hanging)
        self._avatar_size = rendered_size

        # Anchor at bottom-center so squashing preserves ground foot contact!（倒挂时贴窗口顶部）
        cx = self.CANVAS_W // 2
        cy = 4 if hanging else self.CANVAS_H // 2 + 120 + int(walk_bob_y)
        anchor = tk.N if hanging else tk.S
        if self._avatar_item is None:
            self._avatar_item = self.canvas.create_image(cx, cy, image=self.cur_photo_img, anchor=anchor, tags="avatar")
        else:
            self.canvas.coords(self._avatar_item, cx, cy)
            self.canvas.itemconfigure(self._avatar_item, image=self.cur_photo_img, anchor=anchor)
        self.canvas.tag_lower(self._avatar_item)

        # 饿了：头顶右侧冒一个上下浮动的小笼包
        if self.needs is not None and self.needs.hungry:
            top = cy - rendered_size[1]
            self.canvas.create_text(cx + 58, top + 28 + int(math.sin(now * 3.0) * 4), text="🥟",
                                    font=("Segoe UI Emoji", 16), tags="need")

        # -------------------------------------------------------------
        # 3. PARTICLE ENGINE (Floating Hearts, Dust Puffs, Sparkles)
        # -------------------------------------------------------------
        survived_particles = []
        for p in self.floating_particles:
            p["x"] += p.get("vx", 0.0)
            p["y"] += p.get("vy", -1.5)
            # Gentle horizontal drift
            p["x"] += math.sin(now * 8.0 + p["y"] * 0.05) * 0.4
            p["life"] -= 1
            if p["life"] > 0:
                self.canvas.create_text(
                    int(p["x"]), int(p["y"]), text=p["text"],
                    fill=p["color"], font=("Microsoft YaHei UI", 10, "bold"),
                    tags="particle"
                )
                survived_particles.append(p)
        self.floating_particles = survived_particles

        # -------------------------------------------------------------
        # 4. DYNAMIC SPEECH BUBBLE & HUD
        # -------------------------------------------------------------
        self._update_speech_bubble()
        self._draw_action_hud()
        self._update_pomodoro_hud()
        self._draw_agent_hud()

        # 被“显示桌面”之类最小化了：恢复（实测会变成屏幕底部一个小标题条，自己回不来）。每秒查一次
        if now - getattr(self, "_iconic_check_at", 0.0) > 1.0:
            self._iconic_check_at = now
            try:
                if self.wm_state() == "iconic":          # 注意：self.state 是姿态名，不能用 state()
                    self.deiconify()
            except Exception:
                pass

        # 下一帧：扣掉本帧绘制用掉的时间，按 60 帧/秒的节拍调度（原来固定再等 16ms，实测只有约 49 帧/秒）
        spent_ms = (time.time() - now) * 1000.0
        self.after(max(1, int(16.7 - spent_ms)), self._animation_loop)


class CanaanCompanionPet(tk.Toplevel, CanaanPetCore):
    def __init__(self, master=None, is_speaking_provider=None, on_interrupt=None, on_exit=None, companion=None):
        if master is None:
            self._hidden_root = tk.Tk()
            self._hidden_root.withdraw()
            master = self._hidden_root
        super().__init__(master)
        self._init_pet_core(is_speaking_provider=is_speaking_provider, on_interrupt=on_interrupt, on_exit=on_exit, companion=companion)


class CanaanQChibiPet(tk.Tk, CanaanPetCore):
    def __new__(cls, master=None, *args, **kwargs):
        if master is not None and cls is CanaanQChibiPet:
            return CanaanCompanionPet(master, *args, **kwargs)
        return super().__new__(cls)

    def __init__(self, master=None, is_speaking_provider=None, on_interrupt=None, on_exit=None, companion=None):
        super().__init__()
        self._init_pet_core(is_speaking_provider=is_speaking_provider, on_interrupt=on_interrupt, on_exit=on_exit, companion=companion)


if __name__ == "__main__":
    app = CanaanQChibiPet()
    app.mainloop()
