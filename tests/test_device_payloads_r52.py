"""Device payloads reuse real local Cipher in temporary SQLite ledgers."""
import json
import sqlite3

import pytest

from canaan_crypto import Cipher
from canaan_device_payloads import DevicePayloadCodec, DevicePayloadError


def ledger(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE device_text_requests(
            id TEXT PRIMARY KEY, client_id TEXT NOT NULL, request_id TEXT NOT NULL,
            text TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
            preview TEXT NOT NULL, reply TEXT NOT NULL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            question TEXT NOT NULL DEFAULT '', UNIQUE(client_id,request_id));
        CREATE TABLE device_text_answers(
            client_id TEXT NOT NULL, answer_id TEXT NOT NULL, task_id TEXT NOT NULL,
            expected_version INTEGER NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY(client_id,answer_id), UNIQUE(task_id,expected_version));
    """)
    return db


def canonical(text):
    return json.dumps({"client_id": "watch", "request_id": "r1", "text": text},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def insert(db, codec, text, *, answer="补充原话", rid="t1"):
    fp = canonical(text)
    def enc(value, column):
        return codec.encode(value, column) if codec is not None else value
    db.execute("""INSERT INTO device_text_requests
        (id,client_id,request_id,text,fingerprint,state,preview,reply,created_at,updated_at,question)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (rid, "watch", rid, enc(text, "text"),
        codec.fingerprint(fp) if codec is not None else fp, "awaiting_input", enc('{"summary":"计划原话"}', "preview"),
        enc("回复原话", "reply"), 1.0, 1.0, enc("问题原话？", "question")))
    db.execute("INSERT INTO device_text_answers VALUES(?,?,?,?,?)",
               ("watch", rid + "-answer", rid, 1, enc(answer, "text")))
    db.commit()
    return fp


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "encrypted"])
@pytest.mark.parametrize("text", ["原话中文 100%\\_\n保留空白 ", "enc1:用户的字面原话"])
def test_roundtrip_uses_sqlite_row_without_exposing_private_fields(tmp_path, encrypted, text):
    db = ledger(tmp_path / "ledger.db")
    try:
        codec = DevicePayloadCodec(db, Cipher(b"P" * 32) if encrypted else None)
        fp = insert(db, codec, text)
        row = db.execute("SELECT * FROM device_text_requests").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["text"] == text and row[3] == text and row["TEXT"] == text
        assert dict(row)["preview"] == '{"summary":"计划原话"}'
        assert row["reply"] == "回复原话" and row["question"] == "问题原话？"
        assert row["fingerprint"] == codec.fingerprint(fp)
        if not encrypted:
            assert row["fingerprint"] == fp
            assert raw_rows(db, "device_text_requests")[0][3] == text
        assert db.execute("SELECT text FROM device_text_answers").fetchone()[0] == "补充原话"
    finally:
        db.close()


def test_missing_metadata_with_only_ciphertext_answers_cannot_be_double_encrypted(tmp_path):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    cipher = Cipher(b"P" * 32)
    codec = DevicePayloadCodec(db, cipher)
    insert(db, codec, "保密原话")
    db.execute("DELETE FROM device_text_requests")
    db.execute("DROP TABLE device_payload_meta")
    db.commit()
    db.close()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    before = db.execute("SELECT text FROM device_text_answers").fetchone()[0]
    try:
        with pytest.raises(DevicePayloadError):
            DevicePayloadCodec(db, cipher)
        assert db.execute("SELECT text FROM device_text_answers").fetchone()[0] == before
        assert db.row_factory is sqlite3.Row
    finally:
        db.close()


def raw_rows(db, table):
    cursor = db.cursor()
    cursor.row_factory = None
    return cursor.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def reopen(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def assert_no_plaintext_files(path, *secrets):
    for item in path.parent.iterdir():
        if item.is_file():
            content = item.read_bytes()
            assert all(secret.encode("utf-8") not in content for secret in secrets), item.name


def test_encrypted_disk_and_fingerprint_hide_all_sensitive_strings_and_reopen_stably(tmp_path):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    cipher = Cipher(b"P" * 32)
    codec = DevicePayloadCodec(db, cipher)
    fp = insert(db, codec, "原始私密请求", answer="私密补答")
    expected = cipher.lookup(fp, DevicePayloadCodec.FINGERPRINT_AAD)
    assert codec.fingerprint(fp) == expected and "原始私密请求" not in expected
    assert expected != cipher.lookup(fp, "some-other-domain")
    before = {name: raw_rows(db, name) for name in ("device_text_requests", "device_text_answers")}
    assert all(str(value).startswith("enc1:") for value in
               (before["device_text_requests"][0][3], before["device_text_requests"][0][6],
                before["device_text_requests"][0][7], before["device_text_requests"][0][11],
                before["device_text_answers"][0][4]))
    db.close()
    assert_no_plaintext_files(path, "原始私密请求", "私密补答", "计划原话", "回复原话", "问题原话")
    db = reopen(path)
    try:
        new_codec = DevicePayloadCodec(db, Cipher(b"P" * 32))
        assert new_codec.fingerprint(fp) == expected
        assert db.execute("SELECT text FROM device_text_requests").fetchone()[0] == "原始私密请求"
        assert db.execute("SELECT text FROM device_text_answers").fetchone()[0] == "私密补答"
        for name, rows in before.items():
            assert raw_rows(db, name) == rows  # no double encryption on reopen
    finally:
        db.close()


@pytest.mark.parametrize("journal", ["delete", "wal", "persist"])
@pytest.mark.parametrize("prior_meta", [False, True], ids=["legacy", "plain-codec"])
def test_plain_ledger_upgrades_atomically_and_scrubs_old_pages(tmp_path, journal, prior_meta):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    db.execute("PRAGMA journal_mode=" + journal)
    writer = DevicePayloadCodec(db) if prior_meta else None
    # R51 raw enc1: is user data, never an already-encrypted cell.
    fp = insert(db, writer, "enc1:旧版字面原话", answer="enc1:旧版字面回答")
    assert db.execute("SELECT text FROM device_text_requests").fetchone()[0] == "enc1:旧版字面原话"
    cipher = Cipher(b"P" * 32)
    try:
        codec = DevicePayloadCodec(db, cipher)
        assert db.execute("SELECT text FROM device_text_requests").fetchone()[0] == "enc1:旧版字面原话"
        assert db.execute("SELECT text FROM device_text_answers").fetchone()[0] == "enc1:旧版字面回答"
        assert db.execute("SELECT fingerprint FROM device_text_requests").fetchone()[0] == codec.fingerprint(fp)
        meta = dict((r[0], r[1]) for r in db.execute("SELECT key,value FROM device_payload_meta"))
        assert meta["codec_version"] == "1" and meta["mode"] == "encrypted"
        assert meta["cleanup_pending"] == "0" and meta["key_check"].startswith("enc1:")
        assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert_no_plaintext_files(path, "旧版字面原话", "旧版字面回答", "计划原话", "回复原话", "问题原话")
    finally:
        db.close()


@pytest.mark.parametrize("key", [None, b"Q" * 32], ids=["no-key", "wrong-key"])
def test_existing_encrypted_ledger_refuses_missing_or_wrong_key_without_downgrade(tmp_path, key):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    insert(db, DevicePayloadCodec(db, Cipher(b"P" * 32)), "保密原话")
    before = raw_rows(db, "device_text_requests")
    before_meta = raw_rows(db, "device_payload_meta")
    db.close()
    db = reopen(path)
    try:
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db, Cipher(key) if key else None)
        assert error.value.code == ("key_mismatch" if key else "key_required")
        assert "保密原话" not in str(error.value)
        assert db.row_factory is sqlite3.Row and not db.in_transaction
        assert raw_rows(db, "device_text_requests") == before
        assert raw_rows(db, "device_payload_meta") == before_meta
    finally:
        db.close()


@pytest.mark.parametrize("encrypted", [False, True])
def test_future_codec_version_is_not_rewritten_or_downgraded(tmp_path, encrypted):
    db = ledger(tmp_path / "ledger.db")
    cipher = Cipher(b"P" * 32) if encrypted else None
    insert(db, DevicePayloadCodec(db, cipher), "原始文字")
    db.execute("UPDATE device_payload_meta SET value='999' WHERE key='codec_version'")
    db.commit()
    db.row_factory = sqlite3.Row
    before = {name: raw_rows(db, name) for name in ("device_payload_meta", "device_text_requests", "device_text_answers")}
    try:
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db, cipher)
        assert error.value.code == "unsupported_version"
        assert db.row_factory is sqlite3.Row and not db.in_transaction
        for name, rows in before.items():
            assert raw_rows(db, name) == rows
    finally:
        db.close()


@pytest.mark.parametrize("bad_fingerprint", ["not-json", "a" * 64, "[]", '{"x":NaN}'])
def test_legacy_invalid_fingerprint_rolls_back_all_columns_and_meta(tmp_path, bad_fingerprint):
    db = ledger(tmp_path / "ledger.db")
    insert(db, None, "第一条合法原话")
    insert(db, None, "第二条将损坏", rid="t2")
    db.execute("UPDATE device_text_requests SET fingerprint=? WHERE id='t2'", (bad_fingerprint,))
    db.commit()
    before = {name: raw_rows(db, name) for name in ("device_text_requests", "device_text_answers")}
    try:
        with pytest.raises(DevicePayloadError):
            DevicePayloadCodec(db, Cipher(b"P" * 32))
        assert db.row_factory is sqlite3.Row and not db.in_transaction
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='device_payload_meta'").fetchone() is None
        for name, rows in before.items():
            assert raw_rows(db, name) == rows
    finally:
        db.close()


@pytest.mark.parametrize("table,column", [("device_text_requests", column) for column in
                                        ("text", "preview", "reply", "question")]
                         + [("device_text_answers", "text")])
@pytest.mark.parametrize("corruption", ["plain", "invalid-ciphertext", "wrong-aad", "json-number"])
def test_corrupt_encrypted_cells_fail_closed_on_reopen(tmp_path, table, column, corruption):
    db = ledger(tmp_path / "ledger.db")
    cipher = Cipher(b"P" * 32)
    insert(db, DevicePayloadCodec(db, cipher), "保密原话")
    invalid = {"plain": "不该存在的明文", "invalid-ciphertext": "enc1:bad",
               "wrong-aad": cipher.encrypt(json.dumps("其他字段"), "wrong-aad"),
               "json-number": cipher.encrypt("42", "device-payloads:" + column + ":v1")}[corruption]
    db.execute(f"UPDATE {table} SET {column}=?", (invalid,))
    db.commit()
    db.row_factory = sqlite3.Row
    before = raw_rows(db, table)
    try:
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db, cipher)
        assert error.value.code == "corrupt_payload"
        assert db.row_factory is sqlite3.Row and not db.in_transaction
        assert raw_rows(db, table) == before
    finally:
        db.close()


@pytest.mark.parametrize("encrypted", [False, True])
def test_row_keys_positions_slices_duplicate_labels_and_unmatched_columns_are_preserved(tmp_path, encrypted):
    db = ledger(tmp_path / "ledger.db")
    try:
        codec = DevicePayloadCodec(db, Cipher(b"P" * 32) if encrypted else None)
        insert(db, codec, "原话")
        row = db.execute("SELECT id,text,reply,text AS TEXT,state,version,text AS opaque,NULL AS extra "
                         "FROM device_text_requests").fetchone()
        assert row["TeXt"] == "原话" and row[1:4] == ("原话", "回复原话", "原话")
        assert row.keys() == ["id", "text", "reply", "TEXT", "state", "version", "opaque", "extra"]
        assert list(row)[:6] == ["t1", "原话", "回复原话", "原话", "awaiting_input", 1]
        assert dict(row)["extra"] is None and dict(row)["state"] == "awaiting_input"
        assert row["opaque"].startswith("enc1:") if encrypted else row["opaque"] == "原话"
    finally:
        db.close()


class VacuumFailure(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if sql == "VACUUM":
            raise sqlite3.OperationalError("injected VACUUM failure")
        return super().execute(sql, *args, **kwargs)


def test_committed_upgrade_cleanup_failure_stays_pending_until_same_key_reopen(tmp_path):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    insert(db, None, "旧原话需要清理", answer="旧补答需要清理")
    db.close()
    db = sqlite3.connect(path, factory=VacuumFailure)
    db.row_factory = sqlite3.Row
    cipher = Cipher(b"P" * 32)
    try:
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db, cipher)
        assert error.value.code == "cleanup_failed"
        assert db.row_factory is sqlite3.Row
        assert db.execute("SELECT value FROM device_payload_meta WHERE key='cleanup_pending'").fetchone()[0] == "1"
        ciphertext = db.execute("SELECT text FROM device_text_requests").fetchone()[0]
        assert ciphertext.startswith("enc1:")
    finally:
        db.close()
    db = reopen(path)
    try:
        DevicePayloadCodec(db, cipher)
        assert db.execute("SELECT text FROM device_text_requests").fetchone()[0] == "旧原话需要清理"
        assert db.execute("SELECT value FROM device_payload_meta WHERE key='cleanup_pending'").fetchone()[0] == "0"
        cursor = db.cursor(); cursor.row_factory = None
        assert cursor.execute("SELECT text FROM device_text_requests").fetchone()[0] == ciphertext
        assert_no_plaintext_files(path, "旧原话需要清理", "旧补答需要清理")
    finally:
        db.close()


def test_real_busy_wal_checkpoint_refuses_delivery_and_recovers_after_reader_closes(tmp_path):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=1")
    insert(db, None, "WAL中旧原话", answer="WAL中旧回答")
    reader = reopen(path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM device_text_requests").fetchall()
    cipher = Cipher(b"P" * 32)
    try:
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db, cipher)
        assert error.value.code == "cleanup_failed" and db.row_factory is sqlite3.Row
        assert db.execute("SELECT value FROM device_payload_meta WHERE key='cleanup_pending'").fetchone()[0] == "1"
    finally:
        reader.rollback()
        reader.close()
        db.close()
    db = reopen(path)
    try:
        DevicePayloadCodec(db, cipher)
        assert db.execute("SELECT text FROM device_text_requests").fetchone()[0] == "WAL中旧原话"
        assert db.execute("SELECT value FROM device_payload_meta WHERE key='cleanup_pending'").fetchone()[0] == "0"
        assert_no_plaintext_files(path, "WAL中旧原话", "WAL中旧回答")
    finally:
        db.close()


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("change", ["missing", "extra", "mode", "pending", "check"])
def test_corrupt_metadata_is_not_repaired_by_guessing(tmp_path, encrypted, change):
    db = ledger(tmp_path / "ledger.db")
    cipher = Cipher(b"P" * 32) if encrypted else None
    insert(db, DevicePayloadCodec(db, cipher), "保密原话")
    if change == "missing":
        db.execute("DELETE FROM device_payload_meta WHERE key='mode'")
    elif change == "extra":
        db.execute("INSERT INTO device_payload_meta VALUES('unexpected','value')")
    else:
        name, value = {"mode": ("mode", "maybe"), "pending": ("cleanup_pending", "maybe"),
                       "check": ("key_check", "enc1:broken")}[change]
        db.execute("UPDATE device_payload_meta SET value=? WHERE key=?", (value, name))
    db.commit()
    db.row_factory = sqlite3.Row
    before = raw_rows(db, "device_payload_meta")
    try:
        with pytest.raises(DevicePayloadError):
            DevicePayloadCodec(db, cipher)
        assert db.row_factory is sqlite3.Row and not db.in_transaction
        assert raw_rows(db, "device_payload_meta") == before
    finally:
        db.close()


@pytest.mark.parametrize("cipher_present", [False, True])
def test_missing_meta_with_lookup_fingerprints_cannot_be_reclassified_as_plain(tmp_path, cipher_present):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    insert(db, DevicePayloadCodec(db, Cipher(b"P" * 32)), "保密原话")
    db.execute("DROP TABLE device_payload_meta")
    db.commit()
    db.close()
    db = reopen(path)
    before = raw_rows(db, "device_text_requests")
    try:
        with pytest.raises(DevicePayloadError):
            DevicePayloadCodec(db, Cipher(b"P" * 32) if cipher_present else None)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='device_payload_meta'").fetchone() is None
        assert raw_rows(db, "device_text_requests") == before
    finally:
        db.close()


def test_invalid_legacy_answer_rolls_back_already_updated_requests(tmp_path):
    db = ledger(tmp_path / "ledger.db")
    insert(db, None, "先处理的合法原话")
    db.execute("UPDATE device_text_answers SET text=?", (sqlite3.Binary(b"invalid"),))
    db.commit()
    before = {name: raw_rows(db, name) for name in ("device_text_requests", "device_text_answers")}
    try:
        with pytest.raises(DevicePayloadError):
            DevicePayloadCodec(db, Cipher(b"P" * 32))
        assert not db.in_transaction and db.row_factory is sqlite3.Row
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='device_payload_meta'").fetchone() is None
        for name, rows in before.items():
            assert raw_rows(db, name) == rows
    finally:
        db.close()


@pytest.mark.parametrize("value,column", [(None, "text"), (123, "text"), ({"private": "独有秘密9af7"}, "text"),
                                        ("私密文字", "fingerprint"), ("私密文字", "state")])
def test_invalid_encode_arguments_fail_without_echoing_user_data(tmp_path, value, column):
    db = ledger(tmp_path / "ledger.db")
    try:
        codec = DevicePayloadCodec(db, Cipher(b"P" * 32))
        with pytest.raises(DevicePayloadError) as error:
            codec.encode(value, column)
        assert "私密文字" not in str(error.value) and "独有秘密9af7" not in str(error.value)
    finally:
        db.close()


def test_empty_strings_and_reserved_prefixes_never_skip_encryption(tmp_path):
    db = ledger(tmp_path / "ledger.db")
    try:
        codec = DevicePayloadCodec(db, Cipher(b"P" * 32))
        for value in ("", "enc1:", "enc1:not-base64", '"quoted"', "null", "\x00", "中文\n "):
            encoded = codec.encode(value, "text")
            assert encoded != value and encoded.startswith("enc1:")
            assert db.execute("SELECT ? AS text", (encoded,)).fetchone()[0] == value
    finally:
        db.close()


def test_initialization_requires_committed_tables_and_raw_rows(tmp_path):
    db = ledger(tmp_path / "ledger.db")
    try:
        db.execute("BEGIN")
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db)
        assert error.value.code == "transaction_active" and db.in_transaction
        db.rollback()
        db.row_factory = None
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db)
        assert error.value.code == "invalid_factory" and db.row_factory is None
        db.row_factory = sqlite3.Row
        db.execute("DROP TABLE device_text_answers")
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db)
        assert error.value.code == "invalid_schema" and not db.in_transaction
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='device_payload_meta'").fetchone() is None
    finally:
        db.close()


@pytest.mark.parametrize("where", ["update", "commit"])
def test_cleanup_flag_update_or_commit_failure_rolls_back_to_pending(tmp_path, where):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    insert(db, None, "旧内容要清理")
    seen_update = False
    def deny_cleanup(action, arg1, arg2, *_):
        nonlocal seen_update
        if action == sqlite3.SQLITE_UPDATE and arg1 == "device_payload_meta":
            seen_update = True
            if where == "update":
                return sqlite3.SQLITE_DENY
        if where == "commit" and seen_update and action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(deny_cleanup)
    cipher = Cipher(b"P" * 32)
    try:
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db, cipher)
        assert error.value.code == "cleanup_failed" and db.row_factory is sqlite3.Row
        assert not db.in_transaction
        assert db.execute("SELECT value FROM device_payload_meta WHERE key='cleanup_pending'").fetchone()[0] == "1"
    finally:
        db.set_authorizer(None)
        db.close()
    db = reopen(path)
    try:
        DevicePayloadCodec(db, cipher)
        assert db.execute("SELECT text FROM device_text_requests").fetchone()[0] == "旧内容要清理"
        assert db.execute("SELECT value FROM device_payload_meta WHERE key='cleanup_pending'").fetchone()[0] == "0"
    finally:
        db.close()


class ReaderAfterVacuum(sqlite3.Connection):
    reader = None
    path = None

    def execute(self, sql, *args, **kwargs):
        result = super().execute(sql, *args, **kwargs)
        if sql == "VACUUM":
            self.reader = reopen(self.path)
            self.reader.execute("BEGIN")
            self.reader.execute("SELECT * FROM device_text_requests").fetchall()
        return result


def test_second_checkpoint_busy_also_keeps_cleanup_pending(tmp_path):
    path = tmp_path / "ledger.db"
    db = ledger(path)
    db.execute("PRAGMA journal_mode=WAL")
    insert(db, None, "第二次清理原话")
    db.close()
    db = sqlite3.connect(path, factory=ReaderAfterVacuum)
    db.path, db.row_factory = path, sqlite3.Row
    db.execute("PRAGMA busy_timeout=1")
    cipher = Cipher(b"P" * 32)
    try:
        with pytest.raises(DevicePayloadError) as error:
            DevicePayloadCodec(db, cipher)
        assert error.value.code == "cleanup_failed" and db.reader is not None
        assert db.execute("SELECT value FROM device_payload_meta WHERE key='cleanup_pending'").fetchone()[0] == "1"
        assert db.row_factory is sqlite3.Row
    finally:
        if db.reader is not None:
            db.reader.rollback()
            db.reader.close()
        db.close()
    db = reopen(path)
    try:
        DevicePayloadCodec(db, cipher)
        assert db.execute("SELECT value FROM device_payload_meta WHERE key='cleanup_pending'").fetchone()[0] == "0"
        assert db.execute("SELECT text FROM device_text_requests").fetchone()[0] == "第二次清理原话"
    finally:
        db.close()


def test_codec_never_locates_or_generates_a_system_key(tmp_path, monkeypatch):
    import canaan_crypto
    def forbidden(*args, **kwargs):
        raise AssertionError("A codec must only use its injected cipher")
    monkeypatch.setattr(canaan_crypto, "default_cipher", forbidden)
    monkeypatch.setattr(canaan_crypto, "load_or_create_key", forbidden)
    db = ledger(tmp_path / "ledger.db")
    try:
        insert(db, DevicePayloadCodec(db), "仅使用注入密钥")
        DevicePayloadCodec(db, Cipher(b"P" * 32))
        assert db.execute("SELECT text FROM device_text_requests").fetchone()[0] == "仅使用注入密钥"
    finally:
        db.close()
