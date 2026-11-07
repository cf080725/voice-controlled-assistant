"""Task-owned PNG snapshots and the attachment-v1 read contract.

Only a trusted device-execution scope may collect the built-in screenshot's
verified bytes. JSON, filenames in results and remote request bodies cannot
issue snapshots. This module opens no listener, loads no key and captures no
screen. Snapshot files are read only by capture_verified_screenshot(), called
by the built-in ScreenshotAction verification hook.

Read contract (all JSON, <=8192 UTF-8 bytes per response):
catalog: format_version/protocol_version/request_id/task_id/attachment_ids.
manifest: the first four identity fields plus attachment_id/content_type/
byte_count/width/height/sha256/chunk_bytes/chunk_count/expires_at.
chunk: the first four identity fields plus attachment_id/sha256/expires_at/
chunk_index/data/chunk_sha256. data is canonical standard base64. Index starts
at zero; chunks are 3072 bytes, except the last. Consumers must verify identity,
expiry, order, decoded size, each digest and the whole PNG before displaying it.

The broker authenticates current task ownership before AND after every read.
The store never grants authority. Existing Cipher byte encryption binds each
record to the immutable task binding, expiry, attachment identity and index.
Writes join the broker's result transaction; this store never commits it.
Attachments last 15 minutes, including across restarts. Capacity is bounded;
there is no public filesystem path or background purge/retry worker.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import base64
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
import uuid
import warnings

from canaan_execution_guard import execution_allowed

FILE_BYTES = 16 * 1024 * 1024
BUNDLE_BYTES = 32 * 1024 * 1024
LEDGER_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENTS = 32
MAX_PIXELS = 16 * 1024 * 1024
CHUNK_BYTES = 3072
RESPONSE_BYTES = 8192
TTL_MS = 15 * 60 * 1000
_CURRENT = ContextVar('canaan_device_screenshot_capture_v1', default=None)
_BASE = ('format_version', 'protocol_version', 'request_id', 'task_id')
_MANIFEST = (*_BASE, 'attachment_id', 'content_type', 'byte_count', 'width', 'height',
             'sha256', 'chunk_bytes', 'chunk_count', 'expires_at')


class AttachmentError(ValueError):
    def __init__(self):
        super().__init__('这项任务的附件暂时无法读取，请在电脑上核对。')


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _response(value):
    if len(_json(value).encode('utf-8')) > RESPONSE_BYTES:
        raise AttachmentError()
    return value


def _png_size(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= FILE_BYTES:
        raise AttachmentError()
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if (image.format != 'PNG' or type(width) is not int or type(height) is not int
                        or width < 1 or height < 1 or width * height > MAX_PIXELS):
                    raise AttachmentError()
                image.verify()
        return width, height
    except Exception:
        raise AttachmentError() from None


@dataclass(frozen=True)
class ScreenshotSnapshot:
    png: bytes = field(repr=False)
    width: int = field(init=False)
    height: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self):
        width, height = _png_size(self.png)
        object.__setattr__(self, 'width', width)
        object.__setattr__(self, 'height', height)
        object.__setattr__(self, 'sha256', hashlib.sha256(self.png).hexdigest())


class _Capture:
    def __init__(self):
        self.active, self.values, self.bytes = True, [], 0

    def add(self, value):
        if (not self.active or not execution_allowed() or type(value) is not ScreenshotSnapshot
                or len(self.values) >= MAX_ATTACHMENTS or self.bytes + len(value.png) > BUNDLE_BYTES):
            raise AttachmentError()
        self.values.append(value)
        self.bytes += len(value.png)

    def snapshots(self):
        if not self.active:
            raise AttachmentError()
        return tuple(self.values)


@contextmanager
def capture_device_outputs():
    previous = _CURRENT.get()
    if previous is not None:
        raise AttachmentError()
    capture = _Capture()
    token = _CURRENT.set(capture)
    try:
        yield capture
    finally:
        capture.active = False
        capture.values.clear()
        _CURRENT.reset(token)


def capture_verified_screenshot(path):
    capture = _CURRENT.get()
    if capture is None:
        return False  # The ordinary desktop flow keeps its existing behavior.
    if not capture.active or not execution_allowed():
        raise AttachmentError()
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(FILE_BYTES + 1)
        capture.add(ScreenshotSnapshot(raw))
    except Exception:
        raise AttachmentError() from None
    return True


class DeviceAttachmentStore:
    def __init__(self, db, cipher, *, now_ms=None):
        self.db, self.cipher = db, cipher
        self.now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        db.execute('''CREATE TABLE IF NOT EXISTS device_attachments(
            task_id TEXT NOT NULL, attachment_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, body_bytes INTEGER NOT NULL,
            manifest_blob BLOB NOT NULL, PRIMARY KEY(task_id,attachment_id), UNIQUE(task_id,ordinal))''')
        db.execute('''CREATE TABLE IF NOT EXISTS device_attachment_chunks(
            task_id TEXT NOT NULL, attachment_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
            body_blob BLOB NOT NULL, PRIMARY KEY(task_id,attachment_id,chunk_index))''')
        db.execute('CREATE INDEX IF NOT EXISTS device_attachment_expiry ON device_attachments(expires_at)')

    def _now(self):
        try:
            value = self.now_ms()
        except Exception:
            raise AttachmentError() from None
        if type(value) is not int or not 0 <= value <= 9007199254740991 - TTL_MS:
            raise AttachmentError()
        return value

    def _enabled(self):
        if self.cipher is None or any(not callable(getattr(self.cipher, name, None))
                                      for name in ('encrypt_bytes', 'decrypt_bytes')):
            raise AttachmentError()

    @staticmethod
    def _base(row):
        return dict(format_version=1, protocol_version=row['request_protocol'],
                    request_id=row['request_id'], task_id=row['id'])

    @staticmethod
    def _aad(row, header, part):
        return 'canaan.device.attachment.v1:' + _json([
            row['id'], row['client_id'], row['request_id'], row['request_protocol'], row['fingerprint'],
            header['attachment_id'], header['ordinal'], header['created_at'], header['expires_at'],
            header['body_bytes'], part])

    def _encrypt(self, raw, row, header, part):
        try:
            value = self.cipher.encrypt_bytes(raw, self._aad(row, header, part))
            if type(value) is not bytes or not 28 <= len(value) <= len(raw) + 256:
                raise AttachmentError()
            return value
        except Exception:
            raise AttachmentError() from None

    def _decrypt(self, value, row, header, part, maximum):
        self._enabled()
        if type(value) is not bytes or not 28 <= len(value) <= maximum + 256:
            raise AttachmentError()
        try:
            raw = self.cipher.decrypt_bytes(value, self._aad(row, header, part))
            if type(raw) is not bytes or len(raw) > maximum:
                raise AttachmentError()
            return raw
        except Exception:
            raise AttachmentError() from None

    def append(self, row, snapshots):
        if type(snapshots) is not tuple or any(type(item) is not ScreenshotSnapshot for item in snapshots):
            raise AttachmentError()
        if not snapshots:
            return
        self._enabled()
        now = self._now()
        if len(snapshots) > MAX_ATTACHMENTS or sum(len(item.png) for item in snapshots) > BUNDLE_BYTES:
            raise AttachmentError()
        self.db.execute('''DELETE FROM device_attachment_chunks WHERE (task_id,attachment_id) IN
            (SELECT task_id,attachment_id FROM device_attachments WHERE expires_at<=?)''', (now,))
        self.db.execute('DELETE FROM device_attachments WHERE expires_at<=?', (now,))
        existing = self.db.execute('SELECT COUNT(*),COALESCE(SUM(body_bytes),0),COALESCE(MAX(ordinal),-1) '
                                   'FROM device_attachments WHERE task_id=?', (row['id'],)).fetchone()
        total = self.db.execute('SELECT '
            '(SELECT COALESCE(SUM(length(body_blob)),0) FROM device_attachment_chunks) + '
            '(SELECT COALESCE(SUM(length(manifest_blob)),0) FROM device_attachments)').fetchone()[0]
        if (existing[0] + len(snapshots) > MAX_ATTACHMENTS
                or existing[1] + sum(len(v.png) for v in snapshots) > BUNDLE_BYTES
                or existing[2] + len(snapshots) > 9007199254740991):
            raise AttachmentError()
        upper = sum(len(v.png) + math.ceil(len(v.png) / CHUNK_BYTES) * 256 + 2048 for v in snapshots)
        if total + upper > LEDGER_BYTES:
            raise AttachmentError()
        for offset, snapshot in enumerate(snapshots, 1):
            if (not 0 < len(snapshot.png) <= FILE_BYTES
                    or hashlib.sha256(snapshot.png).hexdigest() != snapshot.sha256):
                raise AttachmentError()
            header = dict(attachment_id=uuid.uuid4().hex, ordinal=existing[2] + offset,
                          created_at=now, expires_at=now + TTL_MS, body_bytes=len(snapshot.png))
            manifest = self._base(row) | dict(attachment_id=header['attachment_id'], content_type='image/png',
                byte_count=len(snapshot.png), width=snapshot.width, height=snapshot.height,
                sha256=snapshot.sha256, chunk_bytes=CHUNK_BYTES,
                chunk_count=math.ceil(len(snapshot.png) / CHUNK_BYTES), expires_at=header['expires_at'])
            encoded = self._encrypt(_json(_response(manifest)).encode('utf-8'), row, header, 'manifest')
            self.db.execute('INSERT INTO device_attachments VALUES(?,?,?,?,?,?,?)',
                            (row['id'], header['attachment_id'], header['ordinal'], now, now + TTL_MS,
                             len(snapshot.png), encoded))
            for index, start in enumerate(range(0, len(snapshot.png), CHUNK_BYTES)):
                body = self._encrypt(snapshot.png[start:start + CHUNK_BYTES], row, header, index)
                self.db.execute('INSERT INTO device_attachment_chunks VALUES(?,?,?,?)',
                                (row['id'], header['attachment_id'], index, body))

    def _manifest(self, row, header):
        now = self._now()
        if (any(type(header[key]) is not int for key in ('ordinal', 'created_at', 'expires_at', 'body_bytes'))
                or not 0 <= header['ordinal'] <= 9007199254740991
                or not 0 < header['body_bytes'] <= FILE_BYTES
                or header['expires_at'] - header['created_at'] != TTL_MS
                or not header['created_at'] <= now < header['expires_at']):
            raise AttachmentError()
        try:
            raw = self._decrypt(header['manifest_blob'], row, header, 'manifest', 2048)
            manifest = json.loads(raw)
            base = self._base(row)
            if (type(manifest) is not dict or set(manifest) != set(_MANIFEST)
                    or any(type(manifest[k]) is not type(v) or manifest[k] != v for k, v in base.items())
                    or manifest['attachment_id'] != header['attachment_id'] or manifest['content_type'] != 'image/png'
                    or type(manifest['byte_count']) is not int or manifest['byte_count'] != header['body_bytes']
                    or type(manifest['expires_at']) is not int or manifest['expires_at'] != header['expires_at']
                    or type(manifest['chunk_bytes']) is not int or manifest['chunk_bytes'] != CHUNK_BYTES
                    or type(manifest['chunk_count']) is not int
                    or manifest['chunk_count'] != math.ceil(header['body_bytes'] / CHUNK_BYTES)
                    or any(type(manifest[k]) is not int or manifest[k] < 1 for k in ('width', 'height'))
                    or manifest['width'] * manifest['height'] > MAX_PIXELS
                    or type(manifest['sha256']) is not str or re.fullmatch('[a-f0-9]{64}', manifest['sha256']) is None):
                raise AttachmentError()
            if not header['created_at'] <= self._now() < header['expires_at']:
                raise AttachmentError()
            return _response(manifest)
        except Exception:
            raise AttachmentError() from None

    def _header(self, row, attachment_id):
        if type(attachment_id) is not str or re.fullmatch('[a-f0-9]{32}', attachment_id) is None:
            raise AttachmentError()
        header = self.db.execute('SELECT * FROM device_attachments WHERE task_id=? AND attachment_id=?',
                                 (row['id'], attachment_id)).fetchone()
        if header is None:
            raise AttachmentError()
        return header

    def catalog(self, row):
        now = self._now()
        headers = self.db.execute('SELECT * FROM device_attachments WHERE task_id=? AND expires_at>? ORDER BY ordinal',
                                  (row['id'], now)).fetchall()
        if len(headers) > MAX_ATTACHMENTS:
            raise AttachmentError()
        identifiers = [self._manifest(row, header)['attachment_id'] for header in headers]
        return _response(self._base(row) | dict(attachment_ids=identifiers))

    def manifest(self, row, attachment_id):
        return self._manifest(row, self._header(row, attachment_id))

    def chunk(self, row, attachment_id, index):
        header = self._header(row, attachment_id)
        manifest = self._manifest(row, header)
        if type(index) is not int or not 0 <= index < manifest['chunk_count']:
            raise AttachmentError()
        stored = self.db.execute('SELECT body_blob FROM device_attachment_chunks '
                                 'WHERE task_id=? AND attachment_id=? AND chunk_index=?',
                                 (row['id'], attachment_id, index)).fetchone()
        if stored is None:
            raise AttachmentError()
        raw = self._decrypt(stored['body_blob'], row, header, index, CHUNK_BYTES)
        if len(raw) != min(CHUNK_BYTES, header['body_bytes'] - index * CHUNK_BYTES):
            raise AttachmentError()
        if not header['created_at'] <= self._now() < header['expires_at']:
            raise AttachmentError()
        return _response(self._base(row) | dict(attachment_id=attachment_id, sha256=manifest['sha256'],
            expires_at=manifest['expires_at'], chunk_index=index,
            data=base64.b64encode(raw).decode('ascii'), chunk_sha256=hashlib.sha256(raw).hexdigest()))
