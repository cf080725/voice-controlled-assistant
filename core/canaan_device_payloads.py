"""Versioned device-ledger payload encryption using an injected local Cipher.

Call after creating the two ledger tables, with a raw sqlite3.Row factory and
no active transaction. This module never locates, creates, or loads a key.
The output labels text/preview/reply/question are reserved for sensitive cells;
SQL queries must not alias unrelated expressions to one of those labels.
"""
import json
import sqlite3


class DevicePayloadError(ValueError):
    """Safe initialization/format failure; messages never contain payload data."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class DevicePayloadCodec:
    VERSION = "1"
    META_TABLE = "device_payload_meta"
    FINGERPRINT_AAD = "device-payloads:fingerprint:v1"
    _CHECK_AAD = "device-payloads:key-check:v1"
    _CHECK_VALUE = "device-payloads-key-check-v1"
    _FIELDS = {
        "device_text_requests": ("text", "preview", "reply", "question"),
        "device_text_answers": ("text",),
    }
    _LABELS = frozenset(("text", "preview", "reply", "question"))
    _META_KEYS = frozenset(("codec_version", "mode", "key_check", "cleanup_pending"))

    def __init__(self, db, cipher=None):
        if db.in_transaction:
            raise DevicePayloadError("transaction_active", "请先完成账本建表事务。")
        if db.row_factory is not sqlite3.Row:
            raise DevicePayloadError("invalid_factory", "账本初始化需要原始行读取方式。")
        if cipher is not None and not all(callable(getattr(cipher, name, None))
                                          for name in ("encrypt", "decrypt", "lookup")):
            raise DevicePayloadError("invalid_cipher", "本机加密接口不可用。")
        self.db, self.cipher = db, cipher
        self._initialize()
        # Construct real sqlite3.Row values; do not depend on crypto.Row internals.
        db.row_factory = self._row if cipher is not None else sqlite3.Row

    @staticmethod
    def _fail(code="corrupt_payload", message="设备任务账本内容损坏，暂时不能使用。"):
        raise DevicePayloadError(code, message) from None

    @staticmethod
    def _aad(column):
        return "device-payloads:" + column + ":v1"

    def encode(self, value, column):
        if type(column) is not str or column not in self._LABELS:
            self._fail("invalid_column", "设备任务字段名称不正确。")
        if type(value) is not str:
            self._fail("invalid_value", "设备任务内容必须是文字。")
        if self.cipher is None:
            return value
        try:
            # Cipher.encrypt treats enc1: as already encrypted. A JSON string
            # always begins with a quote, including literal enc1: user input.
            encoded = self.cipher.encrypt(json.dumps(value, ensure_ascii=False), self._aad(column))
            if type(encoded) is not str or not encoded.startswith("enc1:"):
                self._fail()
            return encoded
        except Exception:
            self._fail()

    def fingerprint(self, canonical):
        if type(canonical) is not str:
            self._fail("invalid_value", "设备任务指纹输入必须是文字。")
        if self.cipher is None:
            return canonical
        try:
            result = self.cipher.lookup(canonical, self.FINGERPRINT_AAD)
            if not self._is_lookup(result):
                self._fail()
            return result
        except Exception:
            self._fail()

    @staticmethod
    def _is_lookup(value):
        return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)

    def _decode(self, value, column):
        if type(value) is not str or not value.startswith("enc1:"):
            self._fail()
        try:
            decoded = json.loads(self.cipher.decrypt(value, self._aad(column)))
            if type(decoded) is not str:
                self._fail()
            return decoded
        except Exception:
            self._fail()

    def _row(self, cursor, values):
        decoded = tuple(self._decode(value, label.lower()) if label.lower() in self._LABELS else value
                        for (label, *_), value in zip(cursor.description, values))
        return sqlite3.Row(cursor, decoded)

    def _raw(self, sql, params=()):
        cursor = self.db.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute(sql, params)

    def _set_meta(self, values):
        self.db.executemany("INSERT OR REPLACE INTO device_payload_meta(key,value) VALUES(?,?)", values.items())

    def _metadata(self):
        meta = dict((r["key"], r["value"]) for r in self._raw("SELECT key,value FROM device_payload_meta"))
        if set(meta) != self._META_KEYS:
            self._fail("corrupt_metadata", "设备任务账本格式信息损坏。")
        if meta["codec_version"] != self.VERSION:
            self._fail("unsupported_version", "设备任务账本版本暂不支持。")
        if meta["mode"] not in ("plain", "encrypted") or meta["cleanup_pending"] not in ("0", "1"):
            self._fail("corrupt_metadata", "设备任务账本格式信息损坏。")
        if meta["mode"] == "plain":
            if meta["key_check"] != "" or meta["cleanup_pending"] != "0":
                self._fail("corrupt_metadata", "设备任务账本格式信息损坏。")
        else:
            if self.cipher is None:
                self._fail("key_required", "设备任务账本需要原来的本机密钥。")
            try:
                check = meta["key_check"]
                if type(check) is not str or not check.startswith("enc1:"):
                    self._fail()
                if json.loads(self.cipher.decrypt(check, self._CHECK_AAD)) != self._CHECK_VALUE:
                    self._fail()
            except Exception:
                self._fail("key_mismatch", "设备任务账本密钥不匹配或校验信息损坏。")
        return meta

    @staticmethod
    def _plain_fingerprint(value):
        # R51 stored the canonical JSON object itself. A lookup hash with lost
        # metadata must not be reinterpreted as a legacy plaintext fingerprint.
        def reject_constant(_):
            raise ValueError
        try:
            if type(value) is not str or type(json.loads(value, parse_constant=reject_constant)) is not dict:
                raise ValueError
        except Exception:
            DevicePayloadCodec._fail("corrupt_payload", "旧设备任务指纹格式损坏，不能自动迁移。")

    def _initialize(self):
        cleanup = False
        try:
            self.db.execute("PRAGMA secure_delete=ON")
            self.db.execute("PRAGMA journal_size_limit=0")
            self.db.execute("BEGIN IMMEDIATE")
            for table, fields in self._FIELDS.items():
                columns = {r["name"] for r in self._raw(f"PRAGMA table_info({table})")}
                required = set(fields) | ({"fingerprint", "id"} if table == "device_text_requests" else {"task_id"})
                if not required.issubset(columns):
                    self._fail("invalid_schema", "设备任务账本尚未完成建表。")
            exists = self._raw("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (self.META_TABLE,)).fetchone()
            if exists:
                meta = self._metadata()
            else:
                self.db.execute("CREATE TABLE device_payload_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                meta = {"codec_version": self.VERSION, "mode": "plain", "key_check": "", "cleanup_pending": "0"}
            encrypted = meta["mode"] == "encrypted"
            upgrade = not encrypted and self.cipher is not None
            if not encrypted and self._raw("""SELECT 1 FROM device_text_answers AS answer
                    WHERE NOT EXISTS(SELECT 1 FROM device_text_requests AS request
                                     WHERE request.id=answer.task_id) LIMIT 1""").fetchone():
                self._fail("corrupt_payload", "旧设备回答缺少对应任务，不能自动迁移。")
            for table, fields in self._FIELDS.items():
                for row in self._raw(f"SELECT rowid AS _codec_rowid,* FROM {table}").fetchall():
                    if encrypted:
                        for field in fields:
                            self._decode(row[field], field)
                        if table == "device_text_requests" and not self._is_lookup(row["fingerprint"]):
                            self._fail()
                    else:
                        if any(type(row[field]) is not str for field in fields):
                            self._fail()
                        if table == "device_text_requests":
                            self._plain_fingerprint(row["fingerprint"])
                        if upgrade:
                            changes = {field: self.encode(row[field], field) for field in fields}
                            if table == "device_text_requests":
                                changes["fingerprint"] = self.fingerprint(row["fingerprint"])
                            self.db.execute(f"UPDATE {table} SET " + ",".join(f"{field}=?" for field in changes)
                                            + " WHERE rowid=?", (*changes.values(), row["_codec_rowid"]))
            if upgrade:
                meta.update(mode="encrypted", cleanup_pending="1",
                            key_check=self.cipher.encrypt(json.dumps(self._CHECK_VALUE), self._CHECK_AAD))
            self._set_meta(meta)
            self.db.commit()
            cleanup = meta["cleanup_pending"] == "1"
        except DevicePayloadError:
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            self._fail()
        if cleanup:
            try:
                self._checkpoint()
                self.db.execute("VACUUM")
                self._checkpoint()
                self.db.execute("UPDATE device_payload_meta SET value='0' WHERE key='cleanup_pending'")
                self.db.commit()
            except Exception:
                self.db.rollback()
                self._fail("cleanup_failed", "设备任务账本清理尚未完成，请使用同一密钥重试。")

    def _checkpoint(self):
        result = self._raw("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or result[0] != 0:
            self._fail("cleanup_failed", "设备任务账本仍被占用，清理尚未完成。")
