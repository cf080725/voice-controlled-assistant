# -*- coding: utf-8 -*-
"""灵动岛用的应用图标：按提示的来源名找到正在运行的程序，取它 exe 里的图标（64 像素），存成 PNG。

- 只取图标，不碰程序数据；文件名只有来源名（data/icon_cache/<来源>.png）。
- 找不到正在运行的程序就不存，岛那边用“首字 + 品牌色”的圆形头像。
- source_for_exe：前台程序的 exe 名 → 聊天 / 邮件来源名（主人切到这个应用时清掉它的“N 条新消息”）。
"""
import ctypes
import os
from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_DIR = os.path.join(BASE_DIR, "data", "icon_cache")
EXES = {
    "聊天软件": ("ChatApp.exe", "ChatApp.exe"), "ChatApp": ("ChatApp.exe", "ChatApp.exe"), "IMApp": ("IMApp.exe", "IMAppNT.exe"),
    "TIM": ("TIM.exe",), "飞书": ("Feishu.exe", "Lark.exe"), "Lark": ("Lark.exe", "Feishu.exe"), "钉钉": ("DingTalk.exe",),
    "企业聊天软件": ("WXWork.exe",), "VS Code": ("Code.exe",), "Visual Studio Code": ("Code.exe",), "Claude": ("claude.exe",),
    "ChatGPT": ("ChatGPT.exe",), "Outlook": ("olk.exe", "OUTLOOK.EXE"), "Outlook (new)": ("olk.exe",),
    "Microsoft Teams": ("ms-teams.exe",), "Teams": ("ms-teams.exe",), "Telegram": ("Telegram.exe",), "Discord": ("Discord.exe",),
    "Slack": ("slack.exe",), "Cursor": ("Cursor.exe",), "Microsoft Edge": ("msedge.exe",), "Google Chrome": ("chrome.exe",),
    "Foxmail": ("Foxmail.exe",), "网易邮箱大师": ("MailMaster.exe",),
}
CHAT_EXE = {"chatapp.exe": "聊天软件", "chatapp.exe": "聊天软件", "imapp.exe": "IMApp", "imappnt.exe": "IMApp", "tim.exe": "TIM",
            "feishu.exe": "飞书", "lark.exe": "飞书", "dingtalk.exe": "钉钉", "wxwork.exe": "企业聊天软件", "olk.exe": "Outlook",
            "outlook.exe": "Outlook", "ms-teams.exe": "Microsoft Teams", "telegram.exe": "Telegram", "discord.exe": "Discord",
            "slack.exe": "Slack", "foxmail.exe": "Foxmail", "mailmaster.exe": "网易邮箱大师"}


def safe_name(name):
    return "".join(ch if ch.isalnum() else "_" for ch in str(name))[:40] or "_"


def icon_path(source):
    return os.path.join(ICON_DIR, safe_name(source) + ".png")


def source_for_exe(exe):
    return CHAT_EXE.get(str(exe or "").lower())


def find_exe(source):
    """正在运行的、属于这个来源的程序的 exe 路径。"""
    names = {n.lower() for n in EXES.get(source, ())}
    if not names:
        return None
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            if (p.info.get("name") or "").lower() in names and p.info.get("exe"):
                return p.info["exe"]
    except Exception:
        return None
    return None


def extract_icon(exe, size=64):
    """从 exe 取一个 size×size 的图标，返回 PIL 图片（带透明）；失败返回 None。"""
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT), wintypes.UINT, wintypes.UINT]
    hicon = wintypes.HICON()
    got = user32.PrivateExtractIconsW(exe, 0, size, size, ctypes.byref(hicon), None, 1, 0)
    if got < 1 or not hicon:
        return None

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    dc = gdi32.CreateCompatibleDC(None)
    bits = ctypes.c_void_p()
    bmi = BMIH(ctypes.sizeof(BMIH), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    bmp = gdi32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    try:
        old = gdi32.SelectObject(dc, bmp)
        user32.DrawIconEx(dc, 0, 0, hicon, size, size, 0, None, 3)        # DI_NORMAL
        raw = ctypes.string_at(bits, size * size * 4)
        gdi32.SelectObject(dc, old)
        img = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1)
        if img.getchannel("A").getextrema()[1] == 0:                       # 老图标没有 alpha：当不透明
            img.putalpha(255)
        return img.copy()
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)
        user32.DestroyIcon(hicon)


def ensure_icon(source):
    """来源的图标已缓存就直接返回路径；否则试着从正在运行的程序取一个。"""
    path = icon_path(source)
    if os.path.exists(path):
        return path
    exe = find_exe(source)
    if not exe:
        return None
    try:
        img = extract_icon(exe)
        if img is None:
            return None
        os.makedirs(ICON_DIR, exist_ok=True)
        img.save(path)
        return path
    except Exception:
        return None
