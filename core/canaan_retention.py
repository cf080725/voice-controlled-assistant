# -*- coding: utf-8 -*-
"""本机数据保留（主人定：对话原话保留 14 天，旧审计记录里的参数清掉）。

- logs/conversations.jsonl：删掉 N 天以前的行；时间读不出来的行也删（判断不了新旧，按隐私优先处理）。
- logs/canaan_audit.jsonl：旧格式的行把参数换成个数和类型、message 清空，其余字段原样保留。
- data/privacy_audit.jsonl：历史里可能存过 instruction/window 等原文，启动时收敛成允许字段并清理过期记录。
- data/people.db：别人（非主人）没置顶的事实 90 天没被用到就删，没有事实、90 天没出现的人也删。
- 都是整文件原子重写；临时文件名带 pid/线程，避免并发清理互相覆盖。启动时跑一次，看板也能手动点。
"""
import json
import os
import threading
import time

from canaan_filelock import file_lock
from canaan_conversation_time import record_timestamps
from canaan_privacy_shield import PrivacyShield

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONV_LOG = os.path.join(BASE_DIR, "logs", "conversations.jsonl")
AUDIT_LOG = os.path.join(BASE_DIR, "logs", "canaan_audit.jsonl")
PRIVACY_AUDIT = os.path.join(BASE_DIR, "data", "privacy_audit.jsonl")
_LOCK = threading.RLock()


def _read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []


def _rewrite(path, lines):
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, path)


_AUTO = object()


def purge_conversations(path=CONV_LOG, days=14, now=None, cipher=_AUTO):
    """返回 (保留, 删掉) 的行数。留下的旧明文行顺便加密（10-24 起新写的行已经是加密的）。"""
    if cipher is _AUTO:
        import canaan_crypto
        cipher = canaan_crypto.default_cipher() if os.path.abspath(path) == os.path.abspath(CONV_LOG) else None
    with _LOCK, file_lock(path):
        now = time.time() if now is None else now
        cutoff = now - days * 86400
        lines = _read_lines(path)
        keep, sealed = [], 0
        for line in lines:
            try:
                rec = json.loads(line)
                stamps = record_timestamps(rec, clock=time)
            except (ValueError, KeyError, TypeError):
                continue
            if not stamps or min(stamps) < cutoff:
                continue
            if cipher is not None and isinstance(rec, dict) and "enc" not in rec:
                import canaan_crypto
                line = json.dumps(canaan_crypto.seal_record(rec, cipher), ensure_ascii=False) + "\n"
                sealed += 1
            keep.append(line if line.endswith("\n") else line + "\n")
        if len(keep) != len(lines) or sealed:
            _rewrite(path, keep)
        return len(keep), len(lines) - len(keep)


def _redacted_params(params):
    if isinstance(params, dict) and set(params) <= {"count", "types"} and isinstance(params.get("count"), int):
        return params, False
    values = list(params.values()) if isinstance(params, dict) else ([params] if params not in (None, "") else [])
    return {"count": len(values), "types": sorted({type(v).__name__ for v in values})}, True


def scrub_audit(path=AUDIT_LOG):
    """返回改写的行数。坏行原样保留。"""
    with _LOCK, file_lock(path):
        lines = _read_lines(path)
        out, changed = [], 0
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                out.append(line)
                continue
            if not isinstance(rec, dict):
                out.append(line)
                continue
            params, touched = _redacted_params(rec.get("params"))
            if touched or rec.get("message"):
                rec["params"], rec["message"], rec["content_redacted"] = params, "", True
                changed += 1
                out.append(json.dumps(rec, ensure_ascii=False) + "\n")
            else:
                out.append(line if line.endswith("\n") else line + "\n")
        if changed:
            _rewrite(path, out)
        return changed


def scrub_privacy_audit(path=PRIVACY_AUDIT):
    """把隐私审计历史里不允许的字段（instruction/window/正文）收敛掉。返回改写行数。"""
    with _LOCK, file_lock(path):
        lines = _read_lines(path)
        out, changed = [], 0
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                changed += 1
                continue
            safe = PrivacyShield.sanitize_record(rec) if isinstance(rec, dict) else {}
            new_line = json.dumps(safe, ensure_ascii=False) + "\n"
            if new_line != line:
                changed += 1
            out.append(new_line)
        if changed:
            _rewrite(path, out)
        return changed


def purge_privacy_audit(path=PRIVACY_AUDIT, days=30, now=None):
    """删除过期隐私审计记录，返回 (保留, 删掉)。"""
    with _LOCK, file_lock(path):
        now = time.time() if now is None else now
        cutoff = now - days * 86400
        lines = _read_lines(path)
        keep = []
        for line in lines:
            try:
                ts = float(json.loads(line).get("timestamp"))
            except (ValueError, TypeError, AttributeError):
                continue
            if ts >= cutoff:
                keep.append(line if line.endswith("\n") else line + "\n")
        if len(keep) != len(lines):
            _rewrite(path, keep)
        return len(keep), len(lines) - len(keep)


def run(days=None):
    """启动时调用：清对话原话、清旧审计参数、收敛隐私审计原文。返回只有数字的摘要。"""
    if days is None:
        try:
            import canaan_config
            days = int(getattr(canaan_config, "CONVERSATION_RETENTION_DAYS", 14))
        except Exception:
            days = 14
    try:
        import canaan_config
        privacy_days = int(getattr(canaan_config, "PRIVACY_AUDIT_RETENTION_DAYS", 30))
    except Exception:
        privacy_days = 30
    kept, removed = purge_conversations(days=days)
    p_kept, p_removed = purge_privacy_audit(days=privacy_days)
    return {"conversation_days": days, "conversation_kept": kept, "conversation_removed": removed,
            "audit_scrubbed": scrub_audit(), "privacy_audit_kept": p_kept,
            "privacy_audit_removed": p_removed, "privacy_audit_scrubbed": scrub_privacy_audit(),
            **purge_people()}


def purge_people(days=None, people=None):
    """多人记忆：别人没置顶、days 天没被用到的事实删掉（data/people.db 不存在就不建）。"""
    if days is None:
        try:
            import canaan_config
            days = int(getattr(canaan_config, "PEOPLE_MEMORY_RETENTION_DAYS", 90))
        except Exception:
            days = 90
    import canaan_people
    if people is None:
        if not os.path.exists(canaan_people.DB_PATH):
            return {"people_days": days, "people_facts_removed": 0, "people_removed": 0}
        people = canaan_people.get_people()
    return people.purge(days=days)
