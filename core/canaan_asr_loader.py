"""Load the local CPU SenseVoice checkpoint without a redundant tensor copy.

The supported SDK only reads its source checkpoint. Private function globals
let that exact mapping/callback code use mmap and omit deepcopy, without
patching FunASR, torch, or other AutoModel instances. Unknown versions retain
their own loading behavior.
"""
import hashlib
import inspect
import os
from pathlib import Path
import types
import zipfile

_BUILD_SHA256 = "c0dc2a7b0f95196c74ec98a54de643f0a1b05d1a2ba8ede0e951eaa95e9ac79c"
_LOAD_SHA256 = "ff1cab2426e9b25057cb7ea6be8d9e5fdaf41d03e104702c1baa47394c49c982"
_TORCH_VERSION = "2.14.0+cpu"


def _matches_source(function, digest):
    if not isinstance(function, types.FunctionType):
        return False
    try:
        return hashlib.sha256(Path(function.__code__.co_filename).read_bytes()).hexdigest() == digest
    except (OSError, ValueError, TypeError):
        return False


def _clone(function, replacements):
    namespace = dict(function.__globals__)
    namespace.update(replacements)
    result = types.FunctionType(function.__code__, namespace, function.__name__,
                                function.__defaults__, function.__closure__)
    result.__kwdefaults__ = function.__kwdefaults__
    return result


class _MappedTorch:
    def __init__(self, original):
        self._original = original

    def __getattr__(self, name):
        return getattr(self._original, name)

    def load(self, *args, **kwargs):
        return self._original.load(*args, **dict(kwargs, mmap=True))


class _ReadOnlyCheckpoint:
    @staticmethod
    def deepcopy(checkpoint):
        return checkpoint


def _local_checkpoint_loader(original):
    mapped = _clone(original, dict(torch=_MappedTorch(original.__globals__["torch"]),
                                  copy=_ReadOnlyCheckpoint()))
    signature = inspect.signature(original)

    def load(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        # The SDK appends mapping defaults. Give omitted defaults a fresh list.
        bound.arguments.setdefault("scope_map", [])
        bound.apply_defaults()
        values = bound.arguments
        path = values["path"]
        eligible = (values["oss_bucket"] is None and values["map_location"] == "cpu"
                    and isinstance(path, (str, os.PathLike)))
        if eligible:
            try:
                eligible = zipfile.is_zipfile(path)
            except (OSError, ValueError, TypeError):
                eligible = False
        # Choose once: a genuine deserialize/shape error must propagate.
        return (mapped if eligible else original)(*bound.args, **bound.kwargs)

    return load


def scoped_auto_model_class(base):
    """Use a class-local loader only for the inspected SDK and CPU Torch."""
    descriptor = inspect.getattr_static(base, "build_model", None)
    if not isinstance(descriptor, staticmethod):
        return base
    build = descriptor.__func__
    if not _matches_source(build, _BUILD_SHA256):
        return base
    loader = build.__globals__.get("load_pretrained_model")
    if not _matches_source(loader, _LOAD_SHA256):
        return base
    torch = loader.__globals__.get("torch")
    if getattr(torch, "__version__", None) != _TORCH_VERSION:
        return base
    local_build = _clone(build, dict(load_pretrained_model=_local_checkpoint_loader(loader)))

    class LocalAutoModel(base):
        build_model = staticmethod(local_build)

    return LocalAutoModel


def load_sensevoice(model_path):
    from funasr import AutoModel

    return scoped_auto_model_class(AutoModel)(
        model=model_path, trust_remote_code=True, device="cpu", disable_update=True
    )
