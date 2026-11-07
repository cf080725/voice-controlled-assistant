"""Durable device text proposals with local approval and injected turn ports.

This module opens no network listener and does not import a model or actor.
One broker owns a ledger at a time. Process-local prepared turns are never
reconstructed from persisted JSON: interrupted work becomes unknown on restart.
"""
from dataclasses import dataclass
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from canaan_device_binding import DeviceTextRequest
from canaan_device_limits import DeviceAdmissionLimits
from canaan_execution_guard import execution_guard
from canaan_device_payloads import DevicePayloadCodec
from canaan_query_answers import QueryDocument, QueryPage, from_trusted_record, encode_page, utf16_units
from canaan_device_attachments import AttachmentError, DeviceAttachmentStore


class DeviceRequestError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedDeviceTurn:
    """The trusted adapter retains its guarded plan in payload, never in JSON."""
    preview: dict
    payload: object = None
    question: str = ""
    control_transition: object = None


@dataclass(frozen=True)
class DeviceExecutionResult:
    state: str
    reply: str
    continuation: PreparedDeviceTurn | None = None
    control_transition: object = None
    query_document: QueryDocument | None = None
    attachments: tuple = ()


_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "unknown"})
_RESULT_STATES = _TERMINAL | {"awaiting_confirmation", "awaiting_input"}


class _LedgerLease:
    """OS file locks disappear after a crash; never guess that a live job died."""
    def __init__(self, path):
        self.file = open(str(path) + ".lock", "a+b")
        try:
            self.file.seek(0, 2)
            if self.file.tell() == 0:
                self.file.write(b"\0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise DeviceRequestError("broker_in_use", "这份设备任务记录正在使用。") from None

    def close(self):
        # Closing the descriptor releases its lock on both supported platforms.
        self.file.close()


class DeviceTextRequestBroker:
    """Persist before prepare; all execution requires the local owner's token.

    pipeline.prepare(bound) must be side-effect-free and returns a guarded
    PreparedDeviceTurn. The current Hub's combined process_audio_data entry is
    not a prepare adapter: it also executes, writes memory and updates UI/audio.
    pipeline.execute(prepared, bound) returns
    DeviceExecutionResult and must revalidate bound before each side effect.
    An awaiting_confirmation result must contain ONLY the pending continuation,
    so approving it cannot replay already completed steps. No default adapter is
    provided: installing this class alone does not connect the desktop or watch.
    Local approval supplies the version displayed by get_local; retries of an
    older approval can never approve a later continuation.
    """
    def __init__(self, path, binder, pipeline, *, cipher=None, limits=None):
        if limits is not None and not isinstance(limits, DeviceAdmissionLimits):
            raise TypeError("limits must be a trusted DeviceAdmissionLimits policy")
        self.limits = limits if limits is not None else DeviceAdmissionLimits()
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.binder, self.pipeline = binder, pipeline
        self.lock = threading.RLock()
        self._idle = threading.Condition(self.lock)
        self._prepared = {}
        # Presentation bytes only, never an authorization decision. At most two
        # complete canonical records (<=4MiB total UTF8) plus <=4096 byte chunks.
        self._result_cache = OrderedDict()
        self._handles = {}
        self._active = 0
        self._prepare_active = 0
        self._closed = False
        if path.exists() and path.stat().st_nlink != 1:
            raise DeviceRequestError("ledger_alias", "设备任务记录不能使用硬链接副本。")
        self._lease = _LedgerLease(path)
        try:
            self.db = sqlite3.connect(str(path), check_same_thread=False)
            if path.stat().st_nlink != 1:
                raise DeviceRequestError("ledger_alias", "设备任务记录不能使用硬链接副本。")
            self.db.row_factory = sqlite3.Row
            self.db.execute("""CREATE TABLE IF NOT EXISTS device_text_requests(
                id TEXT PRIMARY KEY, client_id TEXT NOT NULL, request_id TEXT NOT NULL,
                text TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                preview TEXT NOT NULL, reply TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, question TEXT NOT NULL DEFAULT '',
                UNIQUE(client_id,request_id))""")
            if "version" not in {row[1] for row in self.db.execute("PRAGMA table_info(device_text_requests)")}:
                self.db.execute("ALTER TABLE device_text_requests ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "question" not in {row[1] for row in self.db.execute("PRAGMA table_info(device_text_requests)")}:
                self.db.execute("ALTER TABLE device_text_requests ADD COLUMN question TEXT NOT NULL DEFAULT ''")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(device_text_requests)")}
            for name, declaration in (("request_protocol", "INTEGER NOT NULL DEFAULT 1"),
                                      ("reply_format", "TEXT NOT NULL DEFAULT 'plain'"),
                                      ("result_revision", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE device_text_requests ADD COLUMN {name} {declaration}")
            self.db.execute("""CREATE TABLE IF NOT EXISTS device_text_answers(
                client_id TEXT NOT NULL, answer_id TEXT NOT NULL, task_id TEXT NOT NULL,
                expected_version INTEGER NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY(client_id,answer_id), UNIQUE(task_id,expected_version))""")
            self.db.execute("CREATE INDEX IF NOT EXISTS device_pending_state ON device_text_requests(state,client_id)")
            self.db.commit()
            self._codec = DevicePayloadCodec(self.db, cipher)
            self._attachments = DeviceAttachmentStore(self.db, cipher)
            interrupted = self.db.execute("SELECT id FROM device_text_requests WHERE state IN ('preparing','awaiting_confirmation','awaiting_input','running')").fetchall()
            for row in interrupted:
                self._set(row['id'], 'unknown', '上次任务的结果暂时无法确认，请先在电脑上核对。')
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._lease.close()
            raise

    def _open(self):
        if self._closed:
            raise DeviceRequestError("closed", "设备任务记录已经关闭。")

    def _fingerprint(self, bound):
        canonical = json.dumps({name: getattr(bound, name) for name in (
            "request_id", "text", "client_id", "binding_id", "grant_revision",
            "profile_key", "session_id", "execution_host_id")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._codec.fingerprint(canonical)

    @staticmethod
    def _stored_result(row):
        if row['reply_format'] == 'plain':
            if row['result_revision'] != 0:
                raise ValueError('Plain reply has unexpected result revision')
            return row['reply'], None
        if row['reply_format'] != 'query_document_v1':
            raise ValueError('Unsupported server reply format')
        record = json.loads(row['reply'])
        if (type(record) is not dict or set(record) != {'schema', 'reply', 'document', 'result_revision'}
                or record['schema'] != 'canaan.device.query-reply.v1'
                or type(record['reply']) is not str or type(record['result_revision']) is not int
                or not 1 <= record['result_revision'] <= 9007199254740991
                or record['result_revision'] != row['result_revision']):
            raise ValueError('Invalid decrypted server reply')
        # This row was decrypted by the existing authenticated server codec.
        return record['reply'], from_trusted_record(record['document'])

    def _receipt(self, row):
        reply, document = self._stored_result(row)
        protocol = row['request_protocol']
        if protocol == 1:
            reply = ('旧版摘要，完整结果请查看详情。' + reply)[:240] if document is not None else reply[:240]
        elif protocol != 2 or utf16_units(reply) > 4096:
            raise ValueError('Invalid stored request protocol or display reply')
        receipt = {"protocol_version": protocol, "request_id": row["request_id"],
                   "task_id": row["id"], "state": row["state"], "reply": reply}
        if row["state"] == "awaiting_input":
            receipt.update(question=row["question"], input_version=row["version"])
        return receipt

    def _row(self, task_id):
        self._open()
        if type(task_id) is not str or not task_id or len(task_id) > 128:
            raise DeviceRequestError("not_found", "没有找到这个设备任务。")
        row = self.db.execute("SELECT * FROM device_text_requests WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise DeviceRequestError("not_found", "没有找到这个设备任务。")
        return row

    def _set(self, task_id, state, reply, *, preview=None, question="", query_document=None, commit=True):
        # Drop before attempting a write so exceptions/rollback cannot retain a
        # speculative snapshot. Read access still checks current DB metadata.
        for key in tuple(self._result_cache):
            if key[0] == task_id:
                del self._result_cache[key]
        row = self._row(task_id)
        _, previous = self._stored_result(row)
        document = previous if query_document is None else query_document
        if document is not None and type(document) is not QueryDocument:
            raise ValueError('Only an exact server QueryDocument may be persisted')
        revision = row['result_revision']
        reply_format = 'plain'
        if document is not None:
            if previous is not None and (document.plan_steps != previous.plan_steps
                    or document.expected_query_steps != previous.expected_query_steps
                    or document.completed_steps < previous.completed_steps
                    or document.entries[:len(previous.entries)] != previous.entries):
                raise ValueError('Query result cannot replace or lose the trusted prefix')
            canonical = document.canonical_record()
            if previous is None or canonical != previous.canonical_record():
                revision += 1
            if not 1 <= revision <= 9007199254740991:
                raise ValueError('Result revision overflow')
            summary = document.summary_reply()
            if query_document is not None and summary:
                # Device display uses all bounded summaries, independently of speech.
                suffix = '' if state == 'succeeded' else f'（{reply}）'
                reply = summary + suffix
                if utf16_units(reply) > 4096:
                    reply = summary
            if utf16_units(reply) > 4096:
                raise ValueError('Display reply exceeds protocol2 budget; nothing truncated')
            reply = json.dumps(dict(schema='canaan.device.query-reply.v1', reply=reply,
                                    document=document.to_record(), result_revision=revision),
                               ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            reply_format = 'query_document_v1'
        elif row['request_protocol'] == 1:
            reply = reply[:240]
        elif utf16_units(reply) > 4096:
            raise ValueError('Display reply exceeds protocol2 budget; nothing truncated')
        fields = 'state=?,reply=?,updated_at=?,version=version+1,question=?,reply_format=?,result_revision=?'
        args = [state, self._codec.encode(reply, 'reply'), time.time(), self._codec.encode(question, 'question'), reply_format, revision]
        if preview is not None:
            fields += ',preview=?'
            args.append(self._codec.encode(preview, 'preview'))
        self.db.execute(f'UPDATE device_text_requests SET {fields} WHERE id=?', (*args, task_id))
        if commit:
            self.db.commit()

    @staticmethod
    def _preview(prepared):
        if not isinstance(prepared, PreparedDeviceTurn) or type(prepared.preview) is not dict:
            raise ValueError("The trusted pipeline must return a prepared turn")
        encoded = json.dumps(prepared.preview, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 16000:
            raise ValueError("Prepared preview too long")
        if (type(prepared.question) is not str or len(prepared.question) > 240 or "\x00" in prepared.question
                or prepared.question and not prepared.question.strip()):
            raise ValueError("Invalid task question")
        return encoded

    def _publish_prepared(self, task_id, handle, prepared):
        preview = self._preview(prepared)
        state = "awaiting_input" if prepared.question else "awaiting_confirmation"
        reply = "请补充这个任务的信息。" if prepared.question else "请在电脑上核对并确认这个任务。"
        self._set(task_id, state, reply, preview=preview, question=prepared.question)
        self._prepared[task_id] = (handle, prepared)

    def _release_task(self, task_id):
        entry = self._prepared.pop(task_id, None)
        handle = self._handles.pop(task_id, entry[0] if entry is not None else None)
        if handle is not None:
            self.binder.release_bound(handle)

    def _check_pending_capacity(self, client_id):
        # The caller holds our lock and BEGIN IMMEDIATE. Persistent states are
        # the reservation: transitions cannot drift from a second counter.
        row = self.db.execute("""SELECT COUNT(*),
            COALESCE(SUM(CASE WHEN client_id=? THEN 1 ELSE 0 END),0)
            FROM device_text_requests
            WHERE state IN ('preparing','awaiting_input','awaiting_confirmation','running')""",
            (client_id,)).fetchone()
        if row[1] >= self.limits.client_pending:
            raise DeviceRequestError("client_capacity", "设备待完成任务已满，请稍后保留原编号重试。")
        if row[0] >= self.limits.global_pending:
            raise DeviceRequestError("pending_capacity", "待完成任务已满，请稍后保留原编号重试。")

    def _check_prepare_capacity(self):
        if self._prepare_active >= self.limits.prepare_concurrency:
            raise DeviceRequestError("prepare_busy", "设备任务暂忙，请稍后保留原编号重试。")

    def submit(self, token, body):
        request = DeviceTextRequest.from_body(body)
        inspected = self.binder.inspect_request(token, request.request_id, request.text)
        fingerprint = self._fingerprint(inspected)
        bound = None
        keep_handle = False
        committed = False
        try:
            with self.lock:
                self._open()
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    row = self.db.execute("SELECT * FROM device_text_requests WHERE client_id=? AND request_id=?",
                                          (inspected.client_id, request.request_id)).fetchone()
                    if row is not None:
                        if row["fingerprint"] != fingerprint or row['request_protocol'] != request.protocol_version:
                            raise DeviceRequestError("request_conflict", "这个请求编号已用于其他内容或设备会话。")
                        self.db.commit()
                        return self._receipt(row)
                    self._check_pending_capacity(inspected.client_id)
                    self._check_prepare_capacity()
                    # Reserve a protected handle before writing any ledger row.
                    # The outer finally releases it if INSERT/commit fails.
                    bound = self.binder.bind(token, request.request_id, request.text, preserve_pending=True)
                    if self._fingerprint(bound) != fingerprint:
                        raise DeviceRequestError("binding_changed", "设备会话已变化。")
                    self.binder.validate_bound(bound.handle)
                    task_id = uuid.uuid4().hex
                    now = time.time()
                    self.db.execute("""INSERT INTO device_text_requests
                        (id,client_id,request_id,text,fingerprint,state,preview,reply,created_at,updated_at,question,request_protocol)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (task_id, inspected.client_id, request.request_id, self._codec.encode(request.text, 'text'),
                                     fingerprint, "preparing", self._codec.encode("{}", 'preview'),
                                     self._codec.encode("正在理解请求。", 'reply'), now, now, self._codec.encode('', 'question'), request.protocol_version))
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
                self._handles[task_id] = bound.handle
                committed = True
                self._active += 1
                self._prepare_active += 1
            try:
                self.binder.validate_bound(bound.handle)
                with execution_guard(lambda: False):
                    prepared = self.pipeline.prepare(bound)
                self._preview(prepared)
                self.binder.validate_bound(bound.handle)
                with self.lock:
                    row = self._row(task_id)
                    if row["state"] == "preparing":
                        self._publish_prepared(task_id, bound.handle, prepared)
                        keep_handle = True
                    return self._receipt(self._row(task_id))
            except BaseException as ex:
                with self.lock:
                    if self._row(task_id)["state"] == "preparing":
                        self._set(task_id, "failed", "暂时无法处理这个请求，请在电脑上核对。")
                    if not isinstance(ex, Exception):
                        raise
                    return self._receipt(self._row(task_id))
            finally:
                with self.lock:
                    self._active -= 1
                    self._prepare_active -= 1
                    self._idle.notify_all()
        finally:
            if bound is not None and not keep_handle:
                with self.lock:
                    if committed:
                        self._release_task(task_id)
                    else:
                        self.binder.release_bound(bound.handle)

    def _access(self, token, row):
        inspected = self.binder.inspect_request(token, row["request_id"], row["text"])
        if inspected.client_id != row["client_id"]:
            raise DeviceRequestError("not_found", "没有找到这个设备任务。")
        if self._fingerprint(inspected) != row["fingerprint"]:
            raise DeviceRequestError("binding_changed", "设备会话已变化，请先在电脑上核对原任务。")

    def get(self, token, task_id):
        with self.lock:
            row = self._row(task_id)
            self._access(token, row)
            return self._receipt(row)

    @staticmethod
    def capabilities():
        # Authentication belongs to the gateway; discovery creates no grant/row.
        return dict(protocol_versions=[1, 2], current_protocol_version=2, result_format_version=1,
                    reply_max_utf16=4096, document_max_utf8_bytes=2097152,
                    page_max_scalars=1024, page_max_json_bytes=8192, page_count_max=2048)

    def _result_row(self, task_id):
        self._open()
        if type(task_id) is not str or not task_id or len(task_id) > 128:
            raise DeviceRequestError('not_found', '没有找到这个设备任务。')
        # Do not decrypt a multi-MiB reply just to check current access/revision.
        row = self.db.execute('''SELECT id,client_id,request_id,text,fingerprint,
            request_protocol,reply_format,result_revision FROM device_text_requests WHERE id=?''', (task_id,)).fetchone()
        if row is None:
            raise DeviceRequestError('not_found', '没有找到这个设备任务。')
        return row

    @staticmethod
    def _snapshot_key(row):
        # Fingerprint commits the complete immutable binding identity (including
        # profile/session/grant/host); protocol is separately immutable metadata.
        return tuple(row[name] for name in ('id', 'client_id', 'request_id', 'fingerprint',
                                            'request_protocol', 'reply_format', 'result_revision'))

    def _result_snapshot(self, row):
        key = self._snapshot_key(row)
        if key in self._result_cache:
            self._result_cache.move_to_end(key)
            return self._result_cache[key]
        full_row = self._row(row['id'])
        if self._snapshot_key(full_row) != key:
            raise DeviceRequestError('stale_result', '查询结果已更新，请重新读取详情。')
        _, document = self._stored_result(full_row)
        if document is None:
            raise DeviceRequestError('result_unavailable', '这项任务没有可读取的完整查询结果。')
        canonical = document.canonical_record()
        encoded = canonical.encode('utf-8')
        pages = tuple(page.text.encode('utf-8') for page in document.pages())
        manifest = dict(protocol_version=row['request_protocol'], format_version=1,
                        request_id=row['request_id'], task_id=row['id'],
                        result_revision=row['result_revision'], content='canaan.query.document.v2',
                        document_digest=hashlib.sha256(encoded).hexdigest(), page_count=len(pages),
                        document_bytes=len(encoded), document_scalars=len(canonical))
        if len(encoded) > 2097152 or len(pages) > 2048 or sum(map(len, pages)) != len(encoded):
            raise ValueError('Result cache budget exceeded')
        # Byte chunks avoid retaining a whole widened Python Unicode string for
        # each page. Canonical/page transient data is released after this call.
        self._result_cache[key] = (manifest, pages)
        while len(self._result_cache) > 2:
            self._result_cache.popitem(last=False)
        return manifest, pages

    def result_manifest(self, token, task_id, *, local_owner=False):
        with self.lock:
            if local_owner:
                self.binder.require_local_owner(token)
            row = self._result_row(task_id)
            if not local_owner:
                self._access(token, row)
            return dict(self._result_snapshot(row)[0])

    def _attachment_read(self, token, task_id, method, *args, local_owner=False):
        with self.lock:
            row = self._result_row(task_id)
            def authorize():
                if local_owner:
                    self.binder.require_local_owner(token)
                else:
                    self._access(token, self._result_row(task_id))
            authorize()
            try:
                result = getattr(self._attachments, method)(row, *args)
            except AttachmentError:
                raise DeviceRequestError('attachment_unavailable', '这项任务的附件暂时无法读取，请在电脑上核对。') from None
            authorize()
            return result

    def attachment_catalog(self, token, task_id, *, local_owner=False):
        return self._attachment_read(token, task_id, 'catalog', local_owner=local_owner)

    def attachment_manifest(self, token, task_id, attachment_id, *, local_owner=False):
        return self._attachment_read(token, task_id, 'manifest', attachment_id, local_owner=local_owner)

    def attachment_chunk(self, token, task_id, attachment_id, index, *, local_owner=False):
        return self._attachment_read(token, task_id, 'chunk', attachment_id, index, local_owner=local_owner)

    def result_page(self, token, task_id, revision, index, *, local_owner=False):
        with self.lock:
            if local_owner:
                self.binder.require_local_owner(token)
            row = self._result_row(task_id)
            if not local_owner:
                self._access(token, row)
            if (type(revision) is not int or not 1 <= revision <= 9007199254740991
                    or type(index) is not int or not 0 <= index < 2048):
                raise DeviceRequestError('invalid_result_page', '结果页编号不正确。')
            manifest, pages = self._result_snapshot(row)
            if revision != manifest['result_revision']:
                raise DeviceRequestError('stale_result', '查询结果已更新，请重新读取详情。')
            if index >= len(pages):
                raise DeviceRequestError('invalid_result_page', '结果页编号不正确。')
            page = QueryPage(index, len(pages), pages[index].decode('utf-8'))
            metadata = {**manifest, 'page_index': index,
                        'page_digest': hashlib.sha256(page.text.encode('utf-8')).hexdigest()}
            # Helper checks the full JSON including its own index/total fields;
            # wire uses the already bound page_index/page_count names instead.
            encode_page(page, envelope=metadata)
            result = {**metadata, 'text': page.text}
            # Match Handler.reply spacing so actual HTTP response bytes fit.
            if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 8192:
                raise ValueError('Result page exceeds actual wire budget')
            return result

    def follow_up(self, token, task_id, body):
        """Persist one answer to this task/version before interpreting it once."""
        if (type(body) is not dict
                or set(body) != {"protocol_version", "answer_id", "expected_version", "text"}):
            raise ValueError("Task answer must contain only protocol_version, answer_id, expected_version and text")
        answer = DeviceTextRequest(body["protocol_version"], body["answer_id"], body["text"])
        version = body["expected_version"]
        if type(version) is not int or not 1 <= version <= 9007199254740991:
            raise ValueError("expected_version must be a positive safe integer")

        def receipt(row):
            return {**self._receipt(row), "answer_id": answer.request_id}

        with self.lock:
            row = self._row(task_id)
            self._access(token, row)
            if answer.protocol_version != row['request_protocol']:
                raise DeviceRequestError('request_conflict', '任务协议不能改变。')
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.db.execute("SELECT * FROM device_text_answers WHERE client_id=? AND answer_id=?",
                                      (row["client_id"], answer.request_id)).fetchone()
                if old is not None:
                    if (old["task_id"], old["expected_version"], old["text"]) != (task_id, version, answer.text):
                        raise DeviceRequestError("answer_conflict", "这个回答编号已用于其他内容或问题。")
                    self.db.commit()
                    return receipt(row)
                if row["state"] in _TERMINAL:
                    self.db.commit()
                    return receipt(row)
                if row["state"] != "awaiting_input" or row["version"] != version:
                    raise DeviceRequestError("stale_input_version", "问题已变化，请重新查看这个任务。")
                entry = self._prepared.get(task_id)
                if entry is None:
                    self.db.rollback()
                    self._set(task_id, "unknown", "原问题的准备信息已失效，请先在电脑上核对。")
                    self._release_task(task_id)
                    return receipt(self._row(task_id))
                handle, prepared = entry
                self.binder.validate_bound(handle)
                self._check_prepare_capacity()
                self.db.execute("INSERT INTO device_text_answers VALUES(?,?,?,?,?)",
                                (row["client_id"], answer.request_id, task_id, version, self._codec.encode(answer.text, 'text')))
                self._set(task_id, 'preparing', '正在理解补充信息。', commit=False)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            self._active += 1
            self._prepare_active += 1
        try:
            bound = self.binder.validate_bound(handle)
            with execution_guard(lambda: False):
                updated = self.pipeline.follow_up(prepared, bound, answer.text)
            self._preview(updated)
            self.binder.validate_bound(handle)
            with self.lock:
                if self._row(task_id)["state"] == "preparing":
                    self._publish_prepared(task_id, handle, updated)
                return receipt(self._row(task_id))
        except BaseException as ex:
            with self.lock:
                try:
                    if self._row(task_id)["state"] == "preparing":
                        self._set(task_id, "failed", "暂时无法处理这项补充信息，请在电脑上核对。")
                finally:
                    self._release_task(task_id)
                if not isinstance(ex, Exception):
                    raise
                return receipt(self._row(task_id))
        finally:
            with self.lock:
                self._active -= 1
                self._prepare_active -= 1
                self._idle.notify_all()

    def cancel(self, token, task_id):
        with self.lock:
            row = self._row(task_id)
            self._access(token, row)
            return self._cancel_row(row)

    def _cancel_row(self, row):
        if row["state"] == "running":
            raise DeviceRequestError("cannot_cancel_running", "任务已开始，请到电脑上查看。")
        if row["state"] not in _TERMINAL:
            self._set(row["id"], "cancelled", "已取消待执行部分。")
            self._release_task(row["id"])
        return self._receipt(self._row(row["id"]))

    def cancel_local(self, owner_token, task_id, *, expected_version):
        self.binder.require_local_owner(owner_token)
        if type(expected_version) is not int or expected_version < 1:
            raise DeviceRequestError("invalid_confirmation", "请重新核对这项任务。")
        with self.lock:
            row = self._row(task_id)
            if row["state"] not in _TERMINAL and row["version"] != expected_version:
                raise DeviceRequestError("stale_confirmation", "任务内容已变化，请重新核对。")
            return self._cancel_row(row)

    def get_local(self, owner_token, task_id):
        self.binder.require_local_owner(owner_token)
        with self.lock:
            row = self._row(task_id)
            return {**self._receipt(row), "text": row["text"], "preview": json.loads(row["preview"]),
                    "confirmation_version": row["version"], "answers": self._answer_history(task_id),
                    "client_id": row["client_id"]}

    def _answer_history(self, task_id):
        rows = self.db.execute("SELECT text,expected_version FROM device_text_answers WHERE task_id=? ORDER BY expected_version",
                               (task_id,)).fetchall()
        return [{"text": row["text"], "input_version": row["expected_version"]} for row in rows]

    def list_local(self, owner_token, *, limit=100):
        self.binder.require_local_owner(owner_token)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        with self.lock:
            self._open()
            rows = self.db.execute("""SELECT * FROM device_text_requests
                ORDER BY CASE WHEN state IN ('preparing','awaiting_input','awaiting_confirmation','running')
                              THEN 0 ELSE 1 END, updated_at DESC,id LIMIT ?""", (limit,)).fetchall()
            return [{**self._receipt(row), "text": row["text"], "preview": json.loads(row["preview"]),
                     "confirmation_version": row["version"], "client_id": row["client_id"],
                     "answers": self._answer_history(row["id"])} for row in rows]

    def confirm_local(self, owner_token, task_id, *, expected_version):
        self.binder.require_local_owner(owner_token)
        if type(expected_version) is not int or expected_version < 1:
            raise DeviceRequestError("invalid_confirmation", "请在电脑上重新核对待确认任务。")
        with self.lock:
            row = self._row(task_id)
            if row["state"] != "awaiting_confirmation":
                return self._receipt(row)
            if row["version"] != expected_version:
                raise DeviceRequestError("stale_confirmation", "待确认内容已变化，请重新核对。")
            entry = self._prepared.get(task_id)
            if entry is None:
                self._set(task_id, "unknown", "任务准备信息已失效，请先在电脑上核对。")
                self._release_task(task_id)
                return self._receipt(self._row(task_id))
            handle, prepared = entry
            try:
                bound = self.binder.validate_bound(handle)
                self.binder.require_local_owner(owner_token)
            except Exception:
                self._set(task_id, "failed", "设备或使用者会话已变化，本次待确认部分未执行。")
                self._release_task(task_id)
                return self._receipt(self._row(task_id))
            self._set(task_id, "running", "正在电脑上执行。")
            self._active += 1
        try:
            # Recheck after claiming, before entering the injected executor.
            bound = self.binder.validate_bound(handle)
            self.binder.require_local_owner(owner_token)
            def execution_valid():
                self.binder.validate_bound(handle)
                self.binder.require_local_owner(owner_token)
                return True
            with execution_guard(execution_valid):
                result = self.pipeline.execute(prepared, bound)
            if not isinstance(result, DeviceExecutionResult) or result.state not in _RESULT_STATES or type(result.reply) is not str:
                raise ValueError("Invalid execution receipt")
            if result.control_transition is None:
                self.binder.validate_bound(handle)
            else:
                from canaan_device_turn import validate_control_completion
                before, after, session, target = validate_control_completion(prepared, result)
                self.binder.validate_terminal_transition(handle, before, after, session, expected_target=target)
            preview = None
            if result.state in ("awaiting_confirmation", "awaiting_input"):
                preview = self._preview(result.continuation)
                if bool(result.continuation.question) != (result.state == "awaiting_input"):
                    raise ValueError("Continuation state does not match its question")
            with self.lock:
                try:
                    self._attachments.append(self._result_row(task_id), result.attachments)
                    self._set(task_id, result.state, result.reply, preview=preview,
                              question=result.continuation.question if preview is not None else "",
                              query_document=result.query_document, commit=False)
                    if result.attachments:
                        self.binder.validate_bound(handle)
                        self.binder.require_local_owner(owner_token)
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
                if result.state in ("awaiting_confirmation", "awaiting_input"):
                    self._prepared[task_id] = (handle, result.continuation)
                else:
                    self._release_task(task_id)
                return self._receipt(self._row(task_id))
        except BaseException as ex:
            with self.lock:
                # The executor may have made a partial change; never say failed
                # means nothing happened, and never retry it automatically.
                try:
                    self._set(task_id, "unknown", "执行结果暂时无法确认，请先在电脑上核对。")
                finally:
                    self._release_task(task_id)
                if not isinstance(ex, Exception):
                    raise
                return self._receipt(self._row(task_id))
        finally:
            with self.lock:
                self._active -= 1
                self._idle.notify_all()

    def wait_idle(self, timeout=None):
        """Wait for in-flight ports to return; never closes a DB under them."""
        with self._idle:
            return self._idle.wait_for(lambda: self._active == 0, timeout)

    def close(self):
        with self.lock:
            if self._closed:
                return
            if self._active:
                raise DeviceRequestError("busy", "设备任务仍在处理中。")
            for task_id in tuple(self._handles):
                self._release_task(task_id)
            self._result_cache.clear()
            self.db.close()
            self._lease.close()
            self._closed = True
