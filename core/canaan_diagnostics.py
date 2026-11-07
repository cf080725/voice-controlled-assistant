# -*- coding: utf-8 -*-
"""导出诊断包（给内测收集问题用）：一个 zip，主人自己决定发不发给谁。

只放这些（白名单），不放任何对话原话、消息、记忆、联系人、截图、密钥：
- 版本：提交号、权限规则指纹、版本（自用 / 公开）、Python 和 Windows 版本、显存档位；
- 运行记录：主程序退出码记录（logs/hub_exits.jsonl）、崩溃调用栈（只有文件名:行号:函数名）、
  原生崩溃日志末尾、托盘监管日志末尾、每轮耗时（只留耗时字段）；
- 最近一次评测的总分（只有数字）；提示中心的开关设置（不含预览名单）。
所有文本里的用户目录、盘符路径换成 ~ / <路径>，邮箱和手机号打码。
"""
import json
import os
import platform
import re
import time
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(BASE_DIR, "logs")
REPORTS = os.path.join(BASE_DIR, "reports")
OUT_DIR = os.path.join(BASE_DIR, "data", "diagnostics")
LATENCY_KEYS = ("time", "source", "intent_source", "asr_engine", "endpointer", "record_ms", "endpoint_ms", "stages_ms")
TAIL_LINES = 300

_HOME = os.path.expanduser("~")
_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\\\s\"'<>|:*?]+\\)*[^\\\s\"'<>|:*?]*")
_MAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def redact(text):
    """去掉能认出人的东西：用户目录、本机路径（只留文件名）、邮箱、手机号。"""
    text = str(text).replace(_HOME, "~")
    text = _PATH_RE.sub(lambda m: "<路径>\\" + os.path.basename(m.group(0).rstrip("\\")), text)
    text = _MAIL_RE.sub("<邮箱>", text)
    return _PHONE_RE.sub("<手机号>", text)


def _tail(path, n=TAIL_LINES):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def _latency(path):
    out = []
    for line in _tail(path, 200):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        out.append(json.dumps({k: d.get(k) for k in LATENCY_KEYS if k in d}, ensure_ascii=False))
    return "\n".join(out) + ("\n" if out else "")


def _eval_scores():
    """reports 里每类评测最新一份的总分（只拿数字字段）。"""
    keep = ("tag", "generated_at", "n", "n_tasks", "n_scored", "done", "ok", "wrong_exec", "manual_review", "missed_stop", "false_stop")
    latest = {}
    try:
        names = sorted(os.listdir(REPORTS))
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        kind = "eval600" if name.startswith("eval600") else "injection" if name.startswith("injection") else \
            "enterprise" if name.startswith("enterprise_eval") else None
        if not kind:
            continue
        path = os.path.join(REPORTS, name)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        s = d.get("summary", d) if isinstance(d, dict) else {}
        if not isinstance(s, dict):
            continue
        mt = os.path.getmtime(path)
        if kind not in latest or mt > latest[kind][0]:
            latest[kind] = (mt, {k: s[k] for k in keep if k in s})
    return {k: v[1] for k, v in latest.items()}


def versions():
    info = {"python": platform.python_version(), "windows": platform.version(), "machine": platform.machine(),
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        from canaan_api_server import health_info
        h = health_info()
        info.update({k: h.get(k) for k in ("edition", "build", "policy_version", "config", "models")})
    except Exception:
        pass
    try:
        import canaan_config as c
        info.update({"tier": getattr(c, "ACTIVE_TIER", None), "vram_mb": getattr(c, "DETECTED_VRAM_MB", None),
                     "tts_mode": getattr(c, "TTS_MODE", None)})
    except Exception:
        pass
    return info


def build(out_dir=OUT_DIR, logs=None):
    """生成诊断包，返回 (zip 路径, 包里的文件名列表)。"""
    logs = logs or LOGS
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime("canaan_diag_%Y%m%d_%H%M%S.zip"))
    files = {"版本.json": json.dumps(versions(), ensure_ascii=False, indent=1),
             "评测总分.json": json.dumps(_eval_scores(), ensure_ascii=False, indent=1)}
    try:
        import canaan_notice_center as nc
        s = nc.load_settings()
        files["提示设置.json"] = json.dumps({k: s[k] for k in ("show_sender", *nc.FLAGS)} | {"preview_count": len(s["preview_allow"])},
                                          ensure_ascii=False)
    except Exception:
        pass
    for name, src in (("退出码记录.jsonl", "hub_exits.jsonl"), ("崩溃调用栈.log", "hub_crash.log"),
                      ("原生崩溃日志末尾.log", "hub_crash_native.log"), ("托盘监管日志末尾.log", "supervisor.log")):
        lines = _tail(os.path.join(logs, src))
        if lines:
            files[name] = redact("".join(lines))
    lat = _latency(os.path.join(logs, "latency.jsonl"))
    if lat:
        files["每轮耗时.jsonl"] = lat
    files["说明.txt"] = ("团团诊断包：只有版本、运行记录、崩溃调用栈、耗时和评测分数，"
                       "不含对话原话、消息、记忆、联系人、截图和密钥；路径、邮箱、手机号已打码。\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path, sorted(files)
