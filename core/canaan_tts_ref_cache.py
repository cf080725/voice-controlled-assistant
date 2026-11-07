"""Optional, process-local reference caching for the installed GPT-SoVITS API.

No model weights, sampling parameters, or vendor files are changed. The ASGI
wrapper is required: the vendor pipeline itself has shared mutable state.
"""
import asyncio
import hashlib
import os
import sys
import threading
from collections import OrderedDict
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

MAX_ENTRIES = 5
MAX_BYTES = 32 * 1024 * 1024
REFERENCE_NAMES = ("pamper", "haughty", "confident", "dangerous", "tease")
_MODEL_FIELDS = ("t2s_model", "vits_model", "bert_model", "bert_tokenizer", "cnhuhbert_model", "text_preprocessor")
_CONFIG_FIELDS = ("version", "device", "is_half", "sampling_rate", "filter_length", "hop_length", "win_length", "use_vocoder")
_MUTATORS = ("init_t2s_weights", "init_vits_weights", "init_bert_weights", "init_cnhuhbert_weights",
             "enable_half_precision", "set_device")


def project_references(project_root):
    return [Path(project_root) / "tuantuan_voicepack" / "emotions" / f"{name}.wav" for name in REFERENCE_NAMES]


def _canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def _stat_signature(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino


def _file_stamp(path):
    if not path:
        return None
    try:
        return _canonical(path), _stat_signature(os.stat(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path)), None


def _reference_stamp(path):
    """References are small; hash bytes even if an editor preserves timestamps."""
    try:
        path_before = _stat_signature(os.stat(path))
        with open(path, "rb") as reference:
            before = _stat_signature(os.fstat(reference.fileno()))
            if before[0] > 8 * 1024 * 1024:
                return None
            digest = hashlib.sha256()
            for block in iter(lambda: reference.read(65536), b""):
                digest.update(block)
            after = _stat_signature(os.fstat(reference.fileno()))
        path_after = _stat_signature(os.stat(path))
        # Windows fstat/stat can report different ctime meanings. Compare each
        # API with itself; use file identity, size and mtime across APIs.
        same_file = after[:2] + after[3:] == path_after[:2] + path_after[3:]
        if before != after or path_before != path_after or not same_file:
            return None
        return _canonical(path), path_after, digest.digest()
    except OSError:
        return None


def _empty_prompt():
    return {"ref_audio_path": None, "prompt_semantic": None, "refer_spec": [], "prompt_text": None,
            "prompt_lang": None, "phones": None, "bert_features": None, "norm_text": None,
            "aux_ref_audio_paths": []}


def _copy_containers(value):
    """Vendor reads tensors; duplicate mutable containers without GPU transfers."""
    if isinstance(value, dict):
        return {key: _copy_containers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_containers(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_containers(item) for item in value)
    if isinstance(value, set):
        return {_copy_containers(item) for item in value}
    return value


def _retained_bytes(value, seen=None):
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(_retained_bytes(key, seen) + _retained_bytes(item, seen) for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return size + sum(_retained_bytes(item, seen) for item in value)
    if callable(getattr(value, "untyped_storage", None)):
        return size + value.untyped_storage().nbytes()  # A tiny tensor view can retain a much larger allocation.
    if callable(getattr(value, "numel", None)) and callable(getattr(value, "element_size", None)):
        return size + value.numel() * value.element_size()
    if hasattr(value, "nbytes"):
        if getattr(value, "base", None) is not None:
            return size + _retained_bytes(value.base, seen)
        return size + int(value.nbytes)
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return size
    raise TypeError("Unsupported reference cache value")


class _RequestScope:
    def __init__(self):
        self._lock = threading.Lock()
        self._iterators = set()
        self._closing = False

    def add(self, iterator):
        with self._lock:
            if self._closing:
                return False
            self._iterators.add(iterator)
            return True

    def discard(self, iterator):
        with self._lock:
            self._iterators.discard(iterator)

    def close_all(self):
        with self._lock:
            self._closing = True
            pending = list(self._iterators)
        errors = []
        for iterator in pending:
            try:
                iterator.close()
            except BaseException as error:
                errors.append(type(error).__name__)
        return errors


class _ManagedIterator:
    """Serialize next/close, including cleanup on another worker thread."""
    def __init__(self, source, scope):
        self._source, self._scope = source, scope
        self._lock = threading.Lock()
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        with self._lock:
            if self._closed:
                raise StopIteration
            try:
                return next(self._source)
            except BaseException:
                self._closed = True
                try:
                    self._source.close()
                finally:
                    if self._scope:
                        self._scope.discard(self)
                raise

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._source.close()
            finally:
                if self._scope:
                    self._scope.discard(self)


class ReferenceCache:
    """Install on one pipeline; serve it through SerializedTTSApp, not bare ASGI."""
    def __init__(self, pipeline, allowed_references, *, enabled=False, max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES):
        if not 1 <= max_entries <= MAX_ENTRIES or not 1 <= max_bytes <= MAX_BYTES:
            raise ValueError("Reference cache limits exceed the candidate budget")
        self.allowed = frozenset(_canonical(path) for path in allowed_references)
        if not 1 <= len(self.allowed) <= MAX_ENTRIES:
            raise ValueError("Only the five project references may be cached")
        if enabled and not _empty_prompt().keys() <= pipeline.prompt_cache.keys():
            raise ValueError("Unsupported vendor prompt cache schema")
        self.pipeline, self.enabled = pipeline, enabled
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entries, self._bytes = OrderedDict(), 0
        self._metadata_lock = threading.Lock()
        self._epoch, self._last_model_stamp = 0, None
        self._counts = dict(hits=0, misses=0, bypasses=0, invalidations=0, bookkeeping_errors=0)
        self._original_run = pipeline.run
        self._scope = ContextVar(f"tts_scope_{id(self)}", default=None)
        self._installed = False

    def install(self):
        if self._installed:
            return self
        if getattr(self.pipeline, "_canaan_reference_cache", None) is not None:
            raise ValueError("A reference cache is already installed")
        for name in _MUTATORS:
            original = getattr(self.pipeline, name, None)
            if callable(original):
                setattr(self.pipeline, name, self._mutator(original))
        self.pipeline.run = self.run
        self.pipeline._canaan_reference_cache = self
        self._installed = True
        return self

    def _mutator(self, original):
        @wraps(original)
        def changed(*args, **kwargs):
            self.clear()
            try:
                return original(*args, **kwargs)
            finally:
                self._epoch += 1
                self._last_model_stamp = None
                self.pipeline.prompt_cache = _empty_prompt()
        return changed

    def clear(self):
        with self._metadata_lock:
            self._entries.clear()
            self._bytes = 0
            self._counts["invalidations"] += 1

    def stats(self):
        with self._metadata_lock:
            return dict(self._counts, entries=len(self._entries), retained_bytes=self._bytes, enabled=self.enabled)

    def _model_stamp(self):
        config = self.pipeline.configs
        return (self._epoch, tuple(id(getattr(self.pipeline, name, None)) for name in _MODEL_FIELDS),
                tuple(str(getattr(config, name, None)) for name in _CONFIG_FIELDS),
                str(getattr(self.pipeline, "precision", None)),
                tuple(_file_stamp(getattr(config, name, None)) for name in ("t2s_weights_path", "vits_weights_path")))

    def _key(self, inputs):
        stamp = self._model_stamp()
        if self._last_model_stamp is not None and stamp != self._last_model_stamp:
            self.clear()
            self.pipeline.prompt_cache = _empty_prompt()
        self._last_model_stamp = stamp
        reference = _reference_stamp(inputs["ref_audio_path"])
        if reference is None:
            return None
        return stamp, reference, inputs["prompt_text"], inputs["prompt_lang"]

    def _eligible(self, inputs):
        return (isinstance(inputs, dict) and not inputs.get("aux_ref_audio_paths")
                and isinstance(inputs.get("ref_audio_path"), str)
                and _canonical(inputs["ref_audio_path"]) in self.allowed
                and isinstance(inputs.get("prompt_text"), str) and bool(inputs["prompt_text"])
                and isinstance(inputs.get("prompt_lang"), str) and bool(inputs["prompt_lang"]))

    def _remember(self, key, inputs):
        if key != self._key(inputs):
            return
        current = self.pipeline.prompt_cache
        if (current.get("prompt_semantic") is None or not current.get("refer_spec")
                or current.get("phones") is None or current.get("bert_features") is None
                or current.get("prompt_lang") != inputs["prompt_lang"]):
            return
        try:
            snapshot = _copy_containers(current)
            size = _retained_bytes(snapshot)
        except Exception:
            with self._metadata_lock:
                self._counts["bookkeeping_errors"] += 1
            return
        if size > self.max_bytes:
            return
        with self._metadata_lock:
            old = self._entries.pop(key, None)
            if old:
                self._bytes -= old[1]
            self._entries[key] = snapshot, size
            self._bytes += size
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                _, (_, removed_size) = self._entries.popitem(last=False)
                self._bytes -= removed_size

    def _cached_run(self, inputs):
        eligible = self.enabled and self._eligible(inputs)
        key = self._key(inputs) if eligible else None
        with self._metadata_lock:
            entry = self._entries.get(key) if key is not None else None
            self._counts["hits" if entry else "misses" if eligible else "bypasses"] += 1
            if entry:
                self._entries.move_to_end(key)
        if eligible:
            self.pipeline.prompt_cache = _copy_containers(entry[0]) if entry else _empty_prompt()
            if entry:
                self.pipeline.prompt_cache["ref_audio_path"] = inputs["ref_audio_path"]
        source = self._original_run(inputs)
        try:
            first = True
            for item in source:
                if first and key is not None:
                    self._remember(key, inputs)
                first = False
                yield item
        finally:
            source.close()

    def run(self, inputs):
        scope = self._scope.get()
        iterator = _ManagedIterator(self._cached_run(inputs), scope)
        if scope and not scope.add(iterator):
            iterator.close()
        return iterator


async def _wait_cleanup(scope):
    """A second cancellation must not release the gate while next is running."""
    cleanup = asyncio.create_task(asyncio.to_thread(scope.close_all))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    errors = cleanup.result()
    if errors:
        print("[TTS reference cleanup] " + ",".join(errors), flush=True)
    if cancelled:
        raise asyncio.CancelledError


async def _finish_cleanup(scope):
    try:
        import anyio
    except ImportError:
        return await _wait_cleanup(scope)
    # StreamingResponse cancels an AnyIO task group on disconnect. Shield its
    # parent scope as well as direct asyncio Task.cancel calls during cleanup.
    with anyio.CancelScope(shield=True):
        return await _wait_cleanup(scope)


class SerializedTTSApp:
    """Serialize inference/mutations until generators close, without blocking ASGI."""
    def __init__(self, app, cache):
        self.app, self.cache = app, cache
        self._gate = asyncio.Semaphore(1)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").rstrip("/")
        guarded = scope.get("type") == "http" and (path == "/tts" or path.startswith("/set_"))
        if not guarded:
            return await self.app(scope, receive, send)
        async with self._gate:
            request = _RequestScope()
            token = self.cache._scope.set(request)
            try:
                return await self.app(scope, receive, send)
            finally:
                try:
                    await _finish_cleanup(request)
                finally:
                    self.cache._scope.reset(token)
