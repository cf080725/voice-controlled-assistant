# -*- coding: utf-8 -*-
"""
本地数据库字段加密：AES-256-GCM，密钥用 Windows DPAPI（当前登录账号）保护后存在 data/secure.key。

- 密钥文件只能由同一台电脑上同一个 Windows 账号解开；拷走 data/ 目录到别的电脑或别的账号打不开。
- 防的是：数据库文件被拷走、备份泄露、别的账号读盘。防不了：以你的账号运行的恶意程序（它也能调 DPAPI）。
- 密文格式：enc1:<base64(12 字节随机数 + 密文 + 16 字节校验)>；附加数据（AAD）绑定“表.列”，挪到别的列解不开。
- 丢了 secure.key（或重装系统换了账号）数据就解不开，所以密钥解不开时直接报错，绝不重新生成覆盖。
- 环境变量 CANAAN_DB_ENCRYPTION=0 关掉（只用于排查问题）。
"""
import base64
import ctypes
import hashlib
import hmac
import os
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.path.join(BASE_DIR, "data", "secure.key")
PREFIX = "enc1:"
_ENTROPY = b"canaan-local-db-v1"


class KeyUnavailable(RuntimeError):
    """密钥文件在，但这个账号 / 这台电脑解不开。"""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data, protect):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _keep = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise KeyUnavailable(f"DPAPI {'加密' if protect else '解密'}失败（错误码 {ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data):
    return _dpapi(data, True)


def unprotect(data):
    return _dpapi(data, False)


def load_or_create_key(path=KEY_PATH, protector=protect, unprotector=unprotect):
    """读密钥；文件不存在才生成。文件在但解不开时抛 KeyUnavailable，不覆盖。"""
    if os.path.exists(path):
        with open(path, "rb") as f:
            blob = f.read()
        try:
            key = unprotector(blob)
        except Exception as e:
            raise KeyUnavailable(f"{path} 解不开：{e}") from e
        if len(key) != 32:
            raise KeyUnavailable(f"{path} 内容不对（{len(key)} 字节）")
        return key
    key = os.urandom(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(protector(key))
    os.replace(tmp, path)
    return key


class Cipher:
    """字段加解密。encrypt/decrypt 只处理字符串；None 原样返回。"""

    def __init__(self, key):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(key) != 32:
            raise ValueError("需要 32 字节密钥")
        self._aes = AESGCM(hashlib.sha256(b"canaan-field-enc" + key).digest())
        self._mac = hashlib.sha256(b"canaan-field-mac" + key).digest()

    @staticmethod
    def is_encrypted(value):
        return isinstance(value, str) and value.startswith(PREFIX)

    def encrypt(self, text, aad=""):
        if text is None or self.is_encrypted(text):
            return text
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, str(text).encode("utf-8"), aad.encode("utf-8"))
        return PREFIX + base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, value, aad=""):
        if not self.is_encrypted(value):
            return value
        raw = base64.b64decode(value[len(PREFIX):])
        return self._aes.decrypt(raw[:12], raw[12:], aad.encode("utf-8")).decode("utf-8")

    def encrypt_bytes(self, data, aad=""):
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, bytes(data), aad.encode("utf-8"))

    def decrypt_bytes(self, blob, aad=""):
        blob = bytes(blob)
        return self._aes.decrypt(blob[:12], blob[12:], aad.encode("utf-8"))

    def lookup(self, text, aad=""):
        """确定性的查找值（HMAC）：用来判断“有没有一模一样的”，本身看不出内容。"""
        return hmac.new(self._mac, (aad + "\0" + str(text)).encode("utf-8"), hashlib.sha256).hexdigest()


class Row(dict):
    """解密后的一行：既能 row["text"] 也能 row[0]、按值迭代（和 sqlite3.Row 一样），dict(row) 照常可用。"""
    __slots__ = ("_values",)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return dict.__getitem__(self, key)

    def __iter__(self):
        return iter(self._values)


def attach(db, cipher, columns):
    """给一个 sqlite3 连接装上：SQL 函数 dec(列, 'AAD')（cipher 为 None 时原样返回），
    以及按列名自动解密的行工厂（columns: 列名 → AAD；cipher 为 None 时用 sqlite3.Row）。"""
    import sqlite3

    def dec(value, aad):
        if cipher is None or not isinstance(value, str):
            return value
        return cipher.decrypt(value, aad)

    def row(cursor, values):
        names = [d[0] for d in cursor.description]
        values = tuple(dec(v, columns[n]) if n in columns else v for n, v in zip(names, values))
        r = Row(zip(names, values))
        r._values = values
        return r

    db.create_function("dec", 2, dec, deterministic=True)
    db.row_factory = row if cipher is not None else sqlite3.Row
    return dec


# logs/conversations.jsonl：时间、来源、动作名等留明文（保留期清理、排查要用），原话 / 参数 / 回复 / 建议加密进 enc 字段
LOG_FIELDS = ("user", "param", "spoken", "suggestion")
_LOG_AAD = "conversations.jsonl"


def seal_record(rec, cipher):
    """把一条对话记录里的敏感字段加密进 rec["enc"]；cipher 为 None 时原样返回。"""
    if cipher is None or "enc" in rec:
        return rec
    import json
    secret = {k: rec[k] for k in LOG_FIELDS if k in rec}
    out = {k: v for k, v in rec.items() if k not in LOG_FIELDS}
    out["enc"] = cipher.encrypt(json.dumps(secret, ensure_ascii=False), _LOG_AAD)
    return out


def open_record(rec, cipher=None):
    """seal_record 的反过程；没加密的记录原样返回。cipher 为 None 时用默认密钥（只在真有 enc 时才去取）。"""
    if not isinstance(rec, dict) or "enc" not in rec:
        return rec
    import json
    cipher = cipher or default_cipher()
    if cipher is None:
        raise KeyUnavailable("对话记录是加密的，但加密已关闭")
    out = {k: v for k, v in rec.items() if k != "enc"}
    out.update(json.loads(cipher.decrypt(rec["enc"], _LOG_AAD)))
    return out


_default = None
_default_lock = threading.Lock()


def enabled():
    return os.environ.get("CANAAN_DB_ENCRYPTION", "1") != "0" and os.name == "nt"


def default_cipher():
    """整个进程共用一个；关掉加密或不是 Windows 时返回 None。"""
    global _default
    if not enabled():
        return None
    with _default_lock:
        if _default is None:
            _default = Cipher(load_or_create_key())
        return _default
