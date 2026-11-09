"""Exercise voice training and cache operations through the real local database."""

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import numpy as np
import pytest

from canaan_message_center import MessageCenter
from canaan_training_data import export_dataset, export_voice_dataset
from canaan_voice_messages import RETENTION, VoiceMessages


@pytest.fixture
def voices(tmp_path):
    center = MessageCenter(tmp_path / "local.sqlite")
    try:
        yield VoiceMessages(center)
    finally:
        center.db.close()


def _audio(voices, job_id):
    return Path(voices.db.execute("SELECT audio_path FROM voice_jobs WHERE id=?", (job_id,)).fetchone()[0])


def _record(voices, value=0.1):
    return voices.record_command(np.full(200, value, dtype=np.float32), "语音原文", "test")["id"]


def _review(voices, job_id, consent, text="核对文字"):
    return voices.review(job_id, {"text": text, "reason": "人工核对", "training_consent": consent})


def _submit(voices):
    source = voices.directory / "audio" / "incoming" / "import.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"imported audio")
    job_id = voices.submit({"source": "feishu", "conversation": "测试", "message_id": "1", "audio_path": str(source)})["id"]
    return source, job_id


def test_cache_collision_does_not_delete_an_existing_copy(voices, tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"new audio")
    voices.cache.mkdir(parents=True)
    target = voices.cache / "same.wav"
    target.write_bytes(b"keep this")

    with pytest.raises(FileExistsError):
        voices._copy_audio(source, "same")

    assert target.read_bytes() == b"keep this"


@pytest.mark.parametrize("submit", [_record, _submit], ids=["microphone", "import"])
def test_failed_job_insert_removes_its_new_cache_file(voices, submit):
    voices.db.execute("CREATE TRIGGER failed_insert BEFORE INSERT ON voice_jobs BEGIN SELECT RAISE(ABORT, 'write failed'); END")

    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        submit(voices)

    assert not list(voices.cache.iterdir())
    assert voices.list() == []


def test_failed_consent_updates_keep_the_current_audio_and_remove_staged_copies(voices):
    job = _record(voices)
    cached = _audio(voices, job)
    trigger = "CREATE TRIGGER failed_review BEFORE UPDATE OF consent ON voice_jobs BEGIN SELECT RAISE(ABORT, 'write failed'); END"
    voices.db.execute(trigger)
    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        _review(voices, job, True)
    assert cached.is_file()
    assert not list(voices.samples.iterdir())
    assert not voices.get(job)["training_authorized"]

    voices.db.execute("DROP TRIGGER failed_review")
    assert _review(voices, job, True)["training_authorized"]
    approved = _audio(voices, job)
    assert approved.parent == voices.samples and approved.is_file()
    assert not cached.exists()

    voices.db.execute(trigger)
    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        _review(voices, job, False)
    assert approved.is_file()
    assert voices.get(job)["training_authorized"]


def test_revocation_excludes_audio_immediately_and_retries_locked_file_cleanup(voices, monkeypatch):
    job = _record(voices)
    _review(voices, job, True)
    approved = _audio(voices, job)
    unlink = Path.unlink

    def busy(path, *args, **kwargs):
        if path == approved:
            raise PermissionError("player holds audio open")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", busy)
        revoked = _review(voices, job, False)
        assert not revoked["training_authorized"] and not revoked["audio_available"]
        assert approved.is_file()
        assert export_voice_dataset(voices, voices.directory, True)["input_count"] == 0

    VoiceMessages(voices.center).cleanup()
    assert not approved.exists()
    assert voices.db.execute("SELECT COUNT(*) FROM voice_audio_cleanup").fetchone()[0] == 0


def test_expired_queue_cleanup_preserves_incoming_and_running_audio(voices):
    incoming, job = _submit(voices)
    cached = _audio(voices, job)
    voices.db.execute("UPDATE voice_jobs SET state='running' WHERE id=?", (job,))
    voices.db.commit()
    future = time.time() + RETENTION + 1
    voices.cleanup(now=future)
    assert cached.is_file()

    voices.db.execute("UPDATE voice_jobs SET state='queued' WHERE id=?", (job,))
    voices.db.commit()
    voices.cleanup(now=future)
    assert incoming.is_file() and not cached.exists()
    assert voices.get(job)["state"] == "failed"


def test_review_supports_a_legacy_path_below_the_owned_cache(voices):
    job = _record(voices)
    cached = _audio(voices, job)
    nested = cached.parent / "legacy" / cached.name
    nested.parent.mkdir()
    cached.rename(nested)
    with voices.db:
        voices.db.execute("UPDATE voice_jobs SET audio_path=? WHERE id=?", (str(nested), job))

    assert _review(voices, job, True)["training_authorized"]
    assert _audio(voices, job).is_file()
    assert not nested.exists()


def test_changed_cache_is_rejected_before_transcription(voices):
    _, job = _submit(voices)
    _audio(voices, job).write_bytes(b"different audio")
    voices.decoder = lambda path: pytest.fail("unexpected decode of changed audio")

    assert voices.process_once()
    assert voices.get(job)["state"] == "failed"
    assert "内容已变化" in voices.get(job)["error"]


def test_export_moves_with_relative_audio_paths_and_checksums(voices):
    job = _record(voices)
    _review(voices, job, True)
    _review(voices, _record(voices, 0.2), False, "未授权样本")
    expected = _audio(voices, job).read_bytes()

    result = export_voice_dataset(voices, voices.directory, True)
    original = Path(result["directory"])
    moved = original.rename(voices.directory / "moved-export")
    manifest = json.loads((moved / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format_version"] == 2
    assert manifest["input_count"] == 1
    split = next(name for name, stats in manifest["splits"].items() if stats["asr"] == 1)
    root_row = json.loads((moved / f"{split}.jsonl").read_text(encoding="utf-8"))
    asr_row = json.loads((moved / "asr" / f"{split}.jsonl").read_text(encoding="utf-8"))
    assert root_row["audio"] == asr_row["audio"]
    assert root_row["audio"].startswith("audio/")
    assert (moved / asr_row["audio"]).read_bytes() == expected
    files = {path.relative_to(moved).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in moved.rglob("*") if path.is_file() and path.name != "manifest.json"}
    assert manifest["files"] == files


def test_export_and_revocation_are_serialized_across_connections(voices, monkeypatch):
    job = _record(voices)
    _review(voices, job, True)
    second_center = MessageCenter(voices.directory / "local.sqlite")
    second = VoiceMessages(second_center)
    copying, release_copy, revoking, revoked = (Event() for _ in range(4))
    copy = voices._copy_audio_file

    def wait_to_copy(*args, **kwargs):
        copying.set()
        assert release_copy.wait(4)
        return copy(*args, **kwargs)

    def revoke():
        revoking.set()
        try:
            return _review(second, job, False)
        finally:
            revoked.set()

    monkeypatch.setattr(voices, "_copy_audio_file", wait_to_copy)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            export = pool.submit(export_voice_dataset, voices, voices.directory, True)
            try:
                assert copying.wait(4)
                review = pool.submit(revoke)
                assert revoking.wait(4)
                assert not revoked.wait(0.15)
            finally:
                release_copy.set()
            assert export.result(timeout=5)["input_count"] == 1
            assert not review.result(timeout=5)["training_authorized"]
        assert export_voice_dataset(voices, voices.directory, True)["input_count"] == 0
    finally:
        second_center.db.close()


@pytest.mark.parametrize("failure", ["changed_audio", "publication"])
def test_failed_export_keeps_previous_dataset_and_registry(voices, monkeypatch, failure):
    _review(voices, _record(voices), True)
    good = Path(export_voice_dataset(voices, voices.directory, True)["directory"])
    registry = good.parent / "voice-split-registry.json"
    previous = registry.read_bytes()
    previous_manifest = (good / "manifest.json").read_bytes()
    new = _record(voices, 0.2)
    _review(voices, new, True, "第二条语音")
    if failure == "changed_audio":
        _audio(voices, new).write_bytes(b"changed since approval")
        error, message = ValueError, "内容已变化"
    else:
        rename = Path.rename

        def fail_publish(path, target):
            if path.name == "dataset":
                raise OSError("publication failed")
            return rename(path, target)

        monkeypatch.setattr(Path, "rename", fail_publish)
        error, message = OSError, "publication failed"

    with pytest.raises(error, match=message):
        export_voice_dataset(voices, voices.directory, True)

    assert list(good.parent.glob("voice-*/")) == [good]
    assert not list(good.parent.glob(".export-*/"))
    assert registry.read_bytes() == previous
    assert (good / "manifest.json").read_bytes() == previous_manifest


def test_priority_export_covers_its_prompt_in_manifest(voices):
    result = export_dataset(voices.center, voices.directory, True)
    assert set(result["files"]) == {"train.jsonl", "dev.jsonl", "holdout.jsonl", "system.txt"}
