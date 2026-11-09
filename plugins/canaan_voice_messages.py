"""Local asynchronous transcription. Chat audio never enters the action dispatcher."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from canaan_filelock import file_lock
from canaan_message_center import normalize_message
from canaan_message_priority import SOURCES, PRIORITIES

MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 600
RETENTION = 1800


def _audio_checksum(path):
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("音频超过20 MiB")
    return hashlib.sha256(content).hexdigest()


def decode_audio(path):
    import numpy as np
    import soundfile as sf
    from canaan_asr import SAMPLE_RATE, to_mono_f32
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("音频超过20 MiB")
    with path.open("rb") as src:
        header = src.read(10)
        silk = header.startswith((b"#!SILK_V3", b"\x02#!SILK_V3"))
        if silk:
            src.seek(0)
            content = src.read(MAX_BYTES + 1)
    if silk:
        if len(content) > MAX_BYTES:
            raise ValueError("音频超过20 MiB")
        offset, packets = (10 if content.startswith(b"\x02") else 9), 0
        while offset < len(content):
            if offset + 2 > len(content):
                raise ValueError("SILK 音频不完整")
            size = int.from_bytes(content[offset:offset + 2], "little", signed=True)
            offset += 2
            if size == -1 and offset == len(content):
                content = content[:-2]
                break
            # SDK MAX_ARITHM_BYTES 限制每个算术编码包为1024字节。
            if not 0 <= size <= 1024 or offset + size > len(content):
                raise ValueError("SILK 音频不完整")
            offset += size
            packets += 1
            if packets > MAX_SECONDS * 50:
                raise ValueError("音频超过十分钟")
        if not packets:
            raise ValueError("SILK 音频不完整")
        # 原生解码在独立进程中执行，损坏文件不能卡住或崩溃语音工作线程。
        worker = """import sys
try:
    from pysilk.backends.cffi import ffi, lib
except ImportError:
    sys.exit(3)
size = ffi.new("int32_t *")
if lib.SKP_Silk_SDK_Get_Decoder_Size(size):
    sys.exit(1)
state = ffi.new("uint8_t[]", size[0])
if lib.SKP_Silk_SDK_InitDecoder(state):
    sys.exit(1)
control = ffi.new("SKP_SILK_SDK_DecControlStruct *", {"API_sampleRate": 16000})
output, count = ffi.new("int16_t[480]"), ffi.new("int16_t *")
data = sys.stdin.buffer.read()
offset, total = (10 if data.startswith(b"\\x02") else 9), 0
while offset < len(data):
    length = int.from_bytes(data[offset:offset + 2], "little", signed=True)
    offset += 2
    packet = ffi.from_buffer("uint8_t[]", data[offset:offset + length])
    offset += length
    if not total:
        # SDK 首次重采样会覆盖错误码，先以24k内部上限校验首帧。
        control.API_sampleRate = 24000
        if lib.SKP_Silk_SDK_Decode(state, control, 0, packet, length, output, count):
            sys.exit(1)
        if lib.SKP_Silk_SDK_InitDecoder(state):
            sys.exit(1)
        control.API_sampleRate = 16000
    # 每包可有1–5个20ms帧；高级decode API只取第一帧。
    for _ in range(5):
        if lib.SKP_Silk_SDK_Decode(state, control, 0, packet, length, output, count):
            sys.exit(1)
        if not 0 < count[0] <= 320:
            sys.exit(1)
        total += count[0]
        if total > int(sys.argv[1]):
            sys.exit(4)
        sys.stdout.buffer.write(ffi.buffer(output, count[0] * 2))
        if not control.moreInternalDecoderFrames:
            break
    else:
        sys.exit(1)
"""
        try:
            result = subprocess.run([sys.executable, "-I", "-c", worker, str(SAMPLE_RATE * MAX_SECONDS)], input=content,
                                    capture_output=True, timeout=60,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as ex:
            raise ValueError("SILK 音频解码超时") from ex
        if result.returncode == 3:
            raise ValueError("SILK 音频需要本机 silk-python 解码库，请安装 requirements-integrations.txt")
        if result.returncode == 4:
            raise ValueError("音频超过十分钟")
        if result.returncode or not result.stdout or len(result.stdout) % 2:
            raise ValueError("SILK 音频解码失败，请检查文件是否完整")
        if len(result.stdout) > SAMPLE_RATE * MAX_SECONDS * 2:
            raise ValueError("音频超过十分钟")
        return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768
    try:
        with sf.SoundFile(path) as audio:
            if audio.frames / audio.samplerate > MAX_SECONDS or audio.channels > 8:
                raise ValueError("音频超过十分钟或声道数过多")
            if audio.samplerate == SAMPLE_RATE:
                return to_mono_f32(audio.read(dtype="float32", always_2d=True))
    except RuntimeError:
        pass
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        import canaan_config
        candidate = Path(canaan_config.GPT_SOVITS_DIR) / "ffmpeg.exe"
        ffmpeg = str(candidate) if candidate.is_file() else None
    if not ffmpeg:
        raise ValueError("当前格式需要本机 FFmpeg；未下载或调用云端服务")
    # 播放列表可引用允许目录外的文件，只解码独立音频容器。
    result = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
                             "-format_whitelist", "wav,mp3,ogg,flac,mov,aac,amr,aiff,matroska,webm", "-i", str(path),
                             "-t", str(MAX_SECONDS + .001), "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
                            capture_output=True, timeout=60, creationflags=0x08000000 if os.name == "nt" else 0)
    if result.returncode:
        raise ValueError("音频解码失败，请检查文件格式或文件是否完整")
    data = np.frombuffer(result.stdout, dtype="<f4").copy()
    if len(data) > SAMPLE_RATE * MAX_SECONDS:
        raise ValueError("音频超过十分钟")
    return data


class VoiceMessages:
    def __init__(self, center, transcriber=None, decoder=decode_audio):
        self.center, self.db, self.lock = center, center.db, center.lock
        self.directory = center.directory
        self.cache = self.directory / "audio" / "cache"
        self.samples = self.directory / "training" / "voice_samples"
        self.consent_lock = self.samples.parent / ".voice-consent"
        self.transcriber, self.decoder = transcriber, decoder
        self.stop_event = threading.Event()
        self._whisper = None
        with self.lock, self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS voice_jobs(
              id TEXT PRIMARY KEY, source TEXT, message_id TEXT, conversation TEXT, sender TEXT, timestamp REAL,
              purpose TEXT, session_id TEXT, audio_path TEXT, audio_hash TEXT, state TEXT, original_text TEXT DEFAULT '',
              corrected_text TEXT DEFAULT '', engine TEXT DEFAULT '', seconds REAL DEFAULT 0, latency_ms REAL DEFAULT 0,
              error TEXT DEFAULT '', consent INTEGER DEFAULT 0, label TEXT DEFAULT '{}', reason TEXT DEFAULT '',
              created_at REAL, updated_at REAL, expires_at REAL, UNIQUE(source,message_id));
            CREATE TABLE IF NOT EXISTS voice_audio_cleanup(audio_path TEXT PRIMARY KEY);
            """)
            self.db.execute("UPDATE voice_jobs SET state='queued' WHERE state='running'")

    def settings(self):
        path = self.directory / "voice_settings.json"
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return {"audio_roots": [str(self.directory / "audio" / "incoming"), *settings.get("audio_roots", [])]}

    def submit(self, raw):
        if not isinstance(raw, dict):
            raise ValueError("语音事件须为 JSON 对象")
        if "audio_path" in raw and raw["audio_path"] is not None and not isinstance(raw["audio_path"], str):
            raise ValueError("音频须为本机文件路径文本")
        meta = normalize_message({**raw, "text": "语音消息"})
        with self.lock:
            previous = self.db.execute("SELECT id,state,audio_hash,original_text,consent FROM voice_jobs WHERE source=? AND message_id=?", (meta["source"], meta["message_id"])).fetchone()
            recover = bool(previous and raw.get("audio_path") and previous["state"] == "failed" and not previous["audio_hash"] and not previous["original_text"] and not previous["consent"])
            if previous and not recover:
                return {"accepted": False, "id": previous["id"], "reason": "duplicate"}
            if self.db.execute("SELECT count(*) FROM voice_jobs WHERE state IN ('queued','running')").fetchone()[0] >= 100:
                raise ValueError("语音队列已满，请稍后重试")
            job_id = previous["id"] if recover else uuid.uuid4().hex
            cached, checksum, error = "", "", str(raw.get("audio_error") or "未取得音频：连接器只提供了语音占位信息")[:300]
            if raw.get("audio_path"):
                if not isinstance(raw["audio_path"], str) or str(raw["audio_path"]).startswith(("\\\\", "//")):
                    raise ValueError("音频须为可访问的本机文件路径")
                try:
                    path = Path(raw["audio_path"]).resolve(strict=True)
                except OSError as ex:
                    raise ValueError("音频文件不存在或不可访问") from ex
                roots = [Path(p).resolve(strict=True) for p in self.settings()["audio_roots"] if Path(p).is_dir()]
                if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
                    raise ValueError("音频必须位于配置允许的本机目录")
                cached, checksum = self._copy_audio(path, job_id)
                error = ""
            now = time.time()
            try:
                with self.db:
                    if recover:
                        self.db.execute("UPDATE voice_jobs SET audio_path=?,audio_hash=?,state='queued',error='',updated_at=?,expires_at=? WHERE id=?",
                                        (cached, checksum, now, now + RETENTION, job_id))
                    else:
                        self.db.execute("""INSERT INTO voice_jobs(id,source,message_id,conversation,sender,timestamp,purpose,session_id,
                          audio_path,audio_hash,state,error,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (job_id, meta["source"], meta["message_id"], meta["conversation"], meta["sender"], meta["timestamp"],
                           "message", str(raw.get("session_id") or meta["conversation"])[:200], cached, checksum,
                           "queued" if cached else "failed", error, now, now, now + RETENTION))
            except Exception:
                if cached:
                    Path(cached).unlink(missing_ok=True)
                raise
            return {"accepted": True, "id": job_id, "reason": "voice_audio_recovered" if recover else "voice_job"}

    def _copy_audio(self, path, job_id):
        self.cache.mkdir(parents=True, exist_ok=True)
        target = self.cache / (job_id + path.suffix[:12])
        return str(target), self._copy_audio_file(path, target)

    @staticmethod
    def _copy_audio_file(path, target, expected=None):
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("音频超过20 MiB")
        digest, size = hashlib.sha256(), 0
        created = False
        try:
            with path.open("rb") as src, target.open("xb") as dst:
                created = True
                while chunk := src.read(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("音频超过20 MiB")
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if not size:
                raise ValueError("音频为空")
            checksum = digest.hexdigest()
            if expected and expected != checksum:
                raise ValueError("音频内容已变化，请重新提供录音")
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise
        return checksum

    def _owned_audio(self, raw, *, approved=False):
        if not raw:
            return None
        path = Path(raw)
        try:
            if not path.is_absolute() or path.is_symlink():
                return None
            resolved = path.resolve()
            if resolved.is_relative_to(self.cache.resolve()) or (approved and resolved.is_relative_to(self.samples.resolve())):
                return resolved
        except (OSError, RuntimeError):
            return None
        return None

    def _audio_references(self):
        return {self._owned_audio(raw, approved=True) for (raw,) in self.db.execute("SELECT audio_path FROM voice_jobs WHERE audio_path<>''")}

    def _queue_audio_cleanup(self, raw):
        if self._owned_audio(raw, approved=True):
            self.db.execute("INSERT OR IGNORE INTO voice_audio_cleanup VALUES(?)", (raw,))

    def _delete_detached_audio(self):
        with self.db:
            pending = self.db.execute("SELECT audio_path FROM voice_audio_cleanup").fetchall()
            if not pending:
                return
            referenced = self._audio_references()
            for (raw,) in pending:
                path = self._owned_audio(raw, approved=True)
                if path is not None and path not in referenced:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue  # Retry once a player has released the file.
                self.db.execute("DELETE FROM voice_audio_cleanup WHERE audio_path=?", (raw,))

    def record_command(self, audio, text, engine):
        """Keep a short correction cache; this records no human-approved training label."""
        import soundfile as sf
        from canaan_asr import SAMPLE_RATE, to_mono_f32
        job_id, now = uuid.uuid4().hex, time.time()
        self.cache.mkdir(parents=True, exist_ok=True)
        target = self.cache / (job_id + ".wav")
        data = to_mono_f32(audio)
        if not 0 < len(data) <= SAMPLE_RATE * MAX_SECONDS:
            raise ValueError("录音长度无效")
        with self.lock:
            created = False
            try:
                with target.open("xb") as stream:
                    created = True
                    sf.write(stream, data, SAMPLE_RATE, format="WAV", subtype="PCM_16")
                    stream.flush()
                    os.fsync(stream.fileno())
                checksum = _audio_checksum(target)
                with self.db:
                    self.db.execute("""INSERT INTO voice_jobs(id,source,message_id,conversation,sender,timestamp,purpose,session_id,
                      audio_path,audio_hash,state,original_text,engine,seconds,created_at,updated_at,expires_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (job_id, "microphone", job_id, "本人语音指令", "本人", now, "command", time.strftime("%Y%m%d"),
                       str(target), checksum, "succeeded" if text.strip() else "failed", text[:16000], engine,
                       len(data) / SAMPLE_RATE, now, now, now + RETENTION))
                    if not text.strip():
                        self.db.execute("UPDATE voice_jobs SET error='未识别到文字，请核对录音' WHERE id=?", (job_id,))
            except Exception:
                if created:
                    target.unlink(missing_ok=True)
                raise
        return self.get(job_id)

    def get(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("语音任务不存在")
            result = dict(row)
        result["label"] = json.loads(result["label"])
        audio = self._owned_audio(result["audio_path"], approved=result["consent"])
        result["audio_available"] = bool(audio and audio.is_file())
        result["training_authorized"] = bool(result.pop("consent"))
        result.pop("audio_path")
        return result

    def list(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit 须为1–200")
        with self.lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM voice_jobs ORDER BY created_at DESC LIMIT ?", (limit,))]
        return [self.get(job_id) for job_id in ids]

    def status(self):
        from canaan_intent import COMPACT_ACTIONS, describe_action
        with self.lock:
            counts = dict(self.db.execute("SELECT state,count(*) FROM voice_jobs GROUP BY state"))
            reviewed = self.db.execute("SELECT count(*) FROM voice_jobs WHERE consent=1 AND reason<>''").fetchone()[0]
        return {"counts": counts, "authorized_samples": reviewed, "temporary_minutes": 30, "max_seconds": MAX_SECONDS,
                "max_bytes": MAX_BYTES, "local_only": True, "settings": self.settings(),
                "intent_actions": [{"value": a, "label": describe_action({"action": a, "param": ""})} for a in sorted(COMPACT_ACTIONS)]}

    def retry(self, job_id):
        with self.lock, self.db:
            row = self.db.execute("SELECT audio_path,expires_at,consent,state,purpose FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("语音任务不存在")
            path = self._owned_audio(row["audio_path"], approved=row["consent"])
            if row["purpose"] != "message" or row["state"] != "failed" or path is None or not path.is_file() or (not row["consent"] and row["expires_at"] < time.time()):
                raise ValueError("仅可重试仍有缓存音频的失败任务")
            self.db.execute("UPDATE voice_jobs SET state='queued',error='' WHERE id=?", (job_id,))
        return self.get(job_id)

    def _transcribe(self, audio):
        from canaan_asr import transcribe
        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel("tiny", device="cpu", compute_type="int8", local_files_only=True)
        return transcribe(audio, whisper=self._whisper, command=False)

    def process_once(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM voice_jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return False
            self.db.execute("UPDATE voice_jobs SET state='running',updated_at=? WHERE id=?", (time.time(), row["id"]))
        started = time.monotonic()
        try:
            import numpy as np
            path = self._owned_audio(row["audio_path"], approved=row["consent"])
            if path is None or path.stat().st_size > MAX_BYTES:
                raise ValueError("缓存音频路径或大小无效")
            if row["audio_hash"] and _audio_checksum(path) != row["audio_hash"]:
                raise ValueError("音频内容已变化，请重新提供录音")
            audio = self.decoder(path)
            if not 0 < len(audio) <= 16000 * MAX_SECONDS or not np.isfinite(audio).all() or np.max(np.abs(audio)) < 1e-5:
                raise ValueError("音频无有效语音或长度无效")
            text, engine = (self.transcriber or self._transcribe)(audio)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("未识别到文字，请核对音频或本机模型是否就绪")
            message = {k: row[k] for k in ("source", "message_id", "conversation", "sender", "timestamp")}
            message.update(text=text, capture="audio_asr")
            if self.stop_event.is_set():
                with self.lock, self.db:
                    self.db.execute("UPDATE voice_jobs SET state='queued' WHERE id=?", (row["id"],))
                return False
            self.center.ingest(message)
            with self.lock, self.db:
                self.db.execute("UPDATE voice_jobs SET state='succeeded',original_text=?,engine=?,seconds=?,latency_ms=?,error='',updated_at=?,expires_at=? WHERE id=?",
                                (text.strip(), engine, len(audio) / 16000, (time.monotonic() - started) * 1000, time.time(), time.time() + RETENTION, row["id"]))
        except Exception as ex:
            with self.lock, self.db:
                if self.stop_event.is_set():
                    self.db.execute("UPDATE voice_jobs SET state='queued',error='' WHERE id=?", (row["id"],))
                    return False
                error = str(ex)[:300] if isinstance(ex, ValueError) else f"本机语音处理失败：{type(ex).__name__}"
                self.db.execute("UPDATE voice_jobs SET state='failed',error=?,updated_at=?,expires_at=? WHERE id=?", (error, time.time(), time.time() + RETENTION, row["id"]))
            self.center.mark_health(row["source"], error=error)
        return True

    def review(self, job_id, body):
        if not isinstance(body, dict) or set(body) - {"text", "reason", "training_consent", "label"}:
            raise ValueError("未知语音修订字段")
        text, reason, consent = body.get("text"), body.get("reason"), body.get("training_consent", False)
        label = body.get("label", {})
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 16000 or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300 or type(consent) is not bool:
            raise ValueError("须填写核对文本、修订原因及布尔训练授权")
        if not isinstance(label, dict) or set(label) - {"a", "p", "priority", "todos"}:
            raise ValueError("未知理解标签")
        if "p" in label and (not isinstance(label["p"], str) or len(label["p"]) > 4000):
            raise ValueError("理解参数须为最多4000字符的文本")
        if "priority" in label and (not isinstance(label["priority"], str) or label["priority"] not in PRIORITIES):
            raise ValueError("优先级须为P0/P1/P2")
        if "a" in label:
            from canaan_intent import COMPACT_ACTIONS
            if not isinstance(label["a"], str) or label["a"] not in COMPACT_ACTIONS or not isinstance(label.get("p", ""), str) or len(label.get("p", "")) > 4000:
                raise ValueError("意图标签或参数无效")
        if "todos" in label and (not isinstance(label["todos"], list) or len(label["todos"]) > 20 or any(not isinstance(t, str) or len(t) > 500 for t in label["todos"])):
            raise ValueError("待办标签须为最多20条短文本")
        with self.lock, file_lock(self.consent_lock):
            row = self.db.execute("SELECT * FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("语音任务不存在")
            if row["state"] in ("queued", "running"):
                raise ValueError("请等待本次转写结束后修订")
            if row["purpose"] == "message" and label.get("a", "chat") != "chat":
                raise ValueError("聊天语音只能标为资料或闲聊，不能标成执行授权")
            old_audio = audio = row["audio_path"]
            copied = None
            if audio and not row["consent"] and row["expires_at"] < time.time():
                audio = ""
            if consent and audio:
                path = self._owned_audio(audio, approved=row["consent"])
                if path is None or not path.is_file():
                    raise ValueError("音频缓存已过期，不能授权声学训练；请重新提供录音")
                self.samples.mkdir(parents=True, exist_ok=True)
                target = self.samples / (job_id + path.suffix)
                if path != self._owned_audio(target, approved=True):
                    self._copy_audio_file(path, target, expected=row["audio_hash"])
                    copied = target
                audio = str(target)
            elif row["consent"] and audio:
                audio = ""
            try:
                with self.db:
                    self.db.execute("UPDATE voice_jobs SET corrected_text=?,reason=?,consent=?,label=?,audio_path=?,updated_at=? WHERE id=?",
                                    (text.strip(), reason.strip(), int(consent), json.dumps(label, ensure_ascii=False), audio, time.time(), job_id))
                    if audio != old_audio:
                        self._queue_audio_cleanup(old_audio)
                    if row["purpose"] == "message":
                        message = self.db.execute("SELECT id FROM messages WHERE source=? AND message_id=?", (row["source"], row["message_id"])).fetchone()
                        if not message:
                            raw = {k: row[k] for k in ("source", "message_id", "conversation", "sender", "timestamp")}
                            self.center.ingest({**raw, "text": text.strip(), "capture": "audio_human_review"})
                            message = self.db.execute("SELECT id FROM messages WHERE source=? AND message_id=?", (row["source"], row["message_id"])).fetchone()
                        if message:
                            self.db.execute("UPDATE messages SET text=? WHERE id=?", (text.strip(), message["id"]))
                            self.db.execute("INSERT INTO message_revisions VALUES(?,1) ON CONFLICT(message_id) DO UPDATE SET revision=revision+1", (message["id"],))
                            if "priority" in label:
                                self.db.execute("INSERT OR REPLACE INTO priority_reviews VALUES(?,?,?,?)", (message["id"], label["priority"], reason.strip(), time.time()))
            except Exception:
                if copied:
                    copied.unlink(missing_ok=True)
                raise
            try:
                self._delete_detached_audio()
            except sqlite3.Error:
                pass  # The committed cleanup queue is retried by the worker.
            return self.get(job_id)

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            with self.db:
                rows = self.db.execute("SELECT id,audio_path FROM voice_jobs WHERE consent=0 AND expires_at<? AND state<>'running' AND audio_path<>''", (now,)).fetchall()
                for row in rows:
                    self._queue_audio_cleanup(row["audio_path"])
                    self.db.execute("UPDATE voice_jobs SET audio_path='',state=CASE WHEN state='queued' THEN 'failed' ELSE state END,error=CASE WHEN state='queued' THEN '音频缓存已过期' ELSE error END WHERE id=?", (row["id"],))
                # Clean up interrupted, application-generated copies, never incoming files.
                referenced = self._audio_references()
                if self.cache.is_dir():
                    for path in self.cache.iterdir():
                        owned = self._owned_audio(path)
                        if (owned and owned not in referenced and re.fullmatch(r"[0-9a-f]{32}(?:\..{1,11})?", path.name)
                                and path.is_file() and path.stat().st_mtime < now - RETENTION):
                            self._queue_audio_cleanup(str(path))
            self._delete_detached_audio()
        from canaan_message_connectors import cleanup_export_working
        cleanup_export_working(self.directory, now)

    def start(self):
        def run():
            while not self.stop_event.is_set():
                try:
                    self.cleanup()
                    if not self.process_once():
                        self.stop_event.wait(.5)
                except Exception as ex:
                    self.center.mark_health("voice_worker", error=f"语音队列检查失败：{type(ex).__name__}")
                    self.stop_event.wait(2)
        self.thread = threading.Thread(target=run, name="canaan-voice-messages", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if hasattr(self, "thread"):
            self.thread.join(timeout=3)
