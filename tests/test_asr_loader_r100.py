"""Native checkpoint semantics through the installed FunASR build/load bodies.

Run with the bounded report runner; no full FunASR import, device, or audio.
"""
import ast
import copy
import hashlib
import io
import logging
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import weakref

import torch
import canaan_asr_loader as candidate

SDK = Path(torch.__file__).parents[1] / "funasr"
LOAD_FILE = SDK / "train_utils/load_pretrained_model.py"
BUILD_FILE = SDK / "auto/auto_model.py"
OUT = Path(os.environ["ASR_LOADER_TEST_DIR"]) if "ASR_LOADER_TEST_DIR" in os.environ else None


def function_from(path, name, namespace, parent=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body
    if parent:
        nodes = next(node.body for node in nodes if isinstance(node, ast.ClassDef) and node.name == parent)
    node = next(node for node in nodes if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class LoaderChecks(unittest.TestCase):
    def setUp(self):
        if OUT is None:
            temporary = tempfile.TemporaryDirectory(prefix="canaan_asr_loader_")
            self.addCleanup(temporary.cleanup)
            self.directory = Path(temporary.name)
        else:
            self.directory = OUT / self._testMethodName
            self.directory.mkdir()
        self.serial = 0

    def checkpoint(self, value, legacy=False):
        self.serial += 1
        path = self.directory / (str(self.serial) + ".pt")
        torch.save(value, path, _use_new_zipfile_serialization=not legacy)
        return path

    def make_base(self, width=4, version=None):
        trace = dict(loads=[], deepcopies=0, boundaries=[], references=[])

        def tensors(value):
            if isinstance(value, torch.Tensor):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from tensors(item)

        class TorchView:
            __version__ = version or torch.__version__

            def __getattr__(self, key):
                return getattr(torch, key)

            def load(self, *args, **kwargs):
                trace["loads"].append(dict(kwargs))
                value = torch.load(*args, **kwargs)
                trace["references"].extend(weakref.ref(item) for item in tensors(value))
                return value

        class CopyView:
            def __getattr__(self, key):
                return getattr(copy, key)

            def deepcopy(self, value):
                trace["deepcopies"] += 1
                result = copy.deepcopy(value)
                trace["references"].extend(weakref.ref(item) for item in tensors(result))
                return result

        class Target(torch.nn.Module):
            def __init__(self, **kwargs):
                super().__init__()
                self.weight = torch.nn.Parameter(torch.zeros(width, width), requires_grad=False)
                self.bias = torch.nn.Parameter(torch.full((width,), -.75))
                self.register_buffer("count", torch.tensor([9], dtype=torch.int64))
                self.loaded = []
                self.original_ids = {key: id(value) for key, value in self.named_parameters()}
                self.register_load_state_dict_pre_hook(self.boundary)

            def boundary(self, module, *args):
                items = [*self.parameters(), *self.buffers()]
                items += [ref() for ref in trace["references"] if ref() is not None]
                sizes = {item.untyped_storage().data_ptr(): item.untyped_storage().nbytes() for item in items}
                trace["boundaries"].append(sum(sizes.values()))

            def on_pretrained_model_loaded(self, keys):
                self.loaded.append(keys)

        load_globals = dict(torch=TorchView(), copy=CopyView(), logging=logging, BytesIO=io.BytesIO)
        original = function_from(LOAD_FILE, "load_pretrained_model", load_globals)
        build_globals = dict(torch=load_globals["torch"], logging=logging, os=os,
            tables=types.SimpleNamespace(model_classes={"Probe": Target}),
            deep_update=lambda dst, src: dst.update(src),
            set_all_random_seed=lambda seed: torch.manual_seed(seed),
            _resolve_ncpu=lambda options, fallback: options.get("ncpu", fallback),
            load_pretrained_model=original)
        build = function_from(BUILD_FILE, "build_model", build_globals, "AutoModel")

        class Base:
            build_model = staticmethod(build)

            def __init__(self, **kwargs):
                self.model, self.kwargs = self.build_model(**kwargs)

        return Base, original, trace

    def construct(self, base, path, **kwargs):
        return base(model="Probe", model_conf={}, init_param=str(path), device="cpu",
                    disable_update=True, trust_remote_code=True, **kwargs).model

    def assert_same_model(self, first, second):
        self.assertEqual(first.loaded, second.loaded)
        self.assertEqual(first.training, second.training)
        for key, value in first.state_dict().items():
            self.assertTrue(torch.equal(value, second.state_dict()[key]), key)
            self.assertEqual(value.dtype, second.state_dict()[key].dtype)
        for model in (first, second):
            self.assertEqual(model.original_ids, {key: id(value) for key, value in model.named_parameters()})
            self.assertFalse(model.weight.requires_grad)
            self.assertTrue(model.bias.requires_grad)

    def test_loader_boundary_has_at_most_two_live_weight_buffers(self):
        base, original, trace = self.make_base(width=1024)
        state = dict(weight=torch.full((1024, 1024), .125), bias=torch.full((1024,), .5), count=torch.tensor([4]))
        total = sum(item.untyped_storage().nbytes() for item in state.values())
        path = self.checkpoint(dict(state_dict=state))
        model = self.construct(candidate.scoped_auto_model_class(base), path)
        self.assertTrue(torch.equal(model.weight, state["weight"]))
        self.assertEqual(model.loaded, [frozenset(state)])
        self.assertLessEqual(max(trace["boundaries"]), 2 * total)
        self.assertEqual(trace["loads"], [dict(map_location="cpu", mmap=True)])
        self.assertEqual(trace["deepcopies"], 0)

    def test_exact_mapping_wrapper_exclusion_and_partial_load_semantics(self):
        state = dict(weight=torch.full((4, 4), .125), bias=torch.full((4,), .5), count=torch.tensor([4]))
        cases = [("plain", state, {}), ("state", dict(state_dict=state), {}),
            ("model_state", dict(model_state_dict=state), {}), ("model", dict(model=state), {}),
            ("nested", dict(state_dict=dict(model_state_dict=dict(model=state))), {}),
            ("module", {"module."+key: val for key, val in state.items()}, {}),
            ("remap_list", {"source."+key: val for key, val in state.items()}, dict(scope_map=["source.", "None"])),
            ("remap_text", {"source."+key: val for key, val in state.items()}, dict(scope_map="source.,None")),
            ("exclude_text", state, dict(excludes="weight")),
            ("exclude_list", state, dict(excludes=["weight", "count"])),
            ("missing", dict(weight=state["weight"]), {}),
            ("mismatch", dict(state, weight=torch.ones(2, 2)), {}),
            ("cast", dict(state, weight=state["weight"].to(torch.float64)), {})]
        for name, value, options in cases:
            with self.subTest(name=name):
                path = self.checkpoint(value)
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                base, _, _ = self.make_base()
                old = self.construct(base, path, **copy.deepcopy(options))
                actual = self.construct(candidate.scoped_auto_model_class(base), path, **copy.deepcopy(options))
                self.assert_same_model(old, actual)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_global_sdk_functions_and_torch_copy_objects_unchanged(self):
        base, original, trace = self.make_base()
        before = dict(base.build_model.__globals__)
        torch_load_before, deepcopy_before = torch.load, copy.deepcopy
        selected = candidate.scoped_auto_model_class(base)
        path = self.checkpoint(dict(weight=torch.ones(4, 4)))
        self.construct(selected, path)
        self.assertIs(base.build_model.__globals__["load_pretrained_model"], original)
        self.assertEqual(base.build_model.__globals__, before)
        self.assertIs(torch.load, torch_load_before)
        self.assertIs(copy.deepcopy, deepcopy_before)
        self.assertIsNot(selected, base)

    def test_shape_error_propagates_without_deserializing_twice(self):
        base, _, trace = self.make_base()
        path = self.checkpoint(dict(weight=torch.ones(2, 2)))
        with self.assertRaises(RuntimeError):
            self.construct(candidate.scoped_auto_model_class(base), path, ignore_init_mismatch=False)
        self.assertEqual(len(trace["loads"]), 1)

    def test_legacy_checkpoint_delegates_to_original_loader(self):
        base, _, trace = self.make_base()
        path = self.checkpoint(dict(weight=torch.ones(4, 4)), legacy=True)
        self.construct(candidate.scoped_auto_model_class(base), path)
        self.assertNotIn("mmap", trace["loads"][0])
        self.assertEqual(trace["deepcopies"], 1)

    def test_bytes_stream_and_oss_delegate_to_original_loader(self):
        path = self.checkpoint(dict(weight=torch.ones(4, 4)))
        for mode in ("bytes", "oss"):
            with self.subTest(mode=mode):
                base, _, trace = self.make_base()
                selected = candidate.scoped_auto_model_class(base)
                load = selected.build_model.__globals__["load_pretrained_model"]
                model = base.build_model.__globals__["tables"].model_classes["Probe"]()
                content = path.read_bytes()
                if mode == "bytes":
                    load(io.BytesIO(content), model, scope_map=[])
                else:
                    bucket = types.SimpleNamespace(get_object=lambda key: io.BytesIO(content))
                    load("synthetic-remote-object", model, oss_bucket=bucket, scope_map=[])
                self.assertNotIn("mmap", trace["loads"][0])
                self.assertEqual(trace["deepcopies"], 1)

    def test_custom_map_location_delegates_to_original_loader(self):
        base, _, trace = self.make_base()
        selected = candidate.scoped_auto_model_class(base)
        load = selected.build_model.__globals__["load_pretrained_model"]
        path = self.checkpoint(dict(weight=torch.ones(4, 4)))
        model = base.build_model.__globals__["tables"].model_classes["Probe"]()
        mapper = lambda storage, location: storage
        load(path, model, map_location=mapper, scope_map=[])
        self.assertEqual(trace["loads"], [dict(map_location=mapper)])
        self.assertEqual(trace["deepcopies"], 1)

    def test_unrecognized_torch_and_sdk_code_keep_original_construction(self):
        base, _, _ = self.make_base(version="unverified")
        self.assertIs(candidate.scoped_auto_model_class(base), base)
        base, _, _ = self.make_base()
        base.build_model = staticmethod(types.FunctionType(
            base.build_model.__code__.replace(co_filename=str(self.directory / "other.py")),
            base.build_model.__globals__))
        self.assertIs(candidate.scoped_auto_model_class(base), base)

    def test_broken_checkpoint_is_not_retried(self):
        base, _, trace = self.make_base()
        path = self.directory / "broken.pt"
        path.write_bytes(b"invalid checkpoint")
        with self.assertRaises(Exception):
            self.construct(candidate.scoped_auto_model_class(base), path)
        self.assertEqual(len(trace["loads"]), 1)

    def run_hub_loader(self, fail_primary=False):
        calls, ready, scheduled = [], [], []
        factory_calls = []
        original_factory = candidate.load_sensevoice
        selected_path = str(self.directory / "same-local-sensevoice")

        class AutoModel:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                if fail_primary:
                    raise RuntimeError("synthetic primary failure")

        def factory(path):
            factory_calls.append(path)
            return original_factory(path)

        class Thread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                if self.target.__name__ == "_load":
                    self.target()
                else:
                    scheduled.append(self.target)

        modules = {name: types.ModuleType(name) for name in
                   ("funasr", "modelscope", "modelscope.hub", "modelscope.hub.snapshot_download")}
        modules["funasr"].AutoModel = AutoModel
        modules["modelscope"].__path__ = []
        modules["modelscope.hub"].__path__ = []
        modules["modelscope.hub.snapshot_download"].snapshot_download = lambda name, **kwargs: selected_path
        absent = object()
        prior = {name: sys.modules.get(name, absent) for name in modules}
        sys.modules.update(modules)
        candidate.load_sensevoice = factory
        noop = lambda: None
        hub = types.SimpleNamespace(engine=object(), ui_queue=types.SimpleNamespace(put=lambda event: None),
            _asr_ready=types.SimpleNamespace(set=lambda: ready.append(True)),
            _release_asr_load_memory=noop, kws_audio_loop=noop, warmup_ollama=noop,
            prewarm_tts_cache=noop, _tts_keep_warm=noop, _start_proactive=noop)
        namespace = dict(threading=types.SimpleNamespace(Thread=Thread),
            os=types.SimpleNamespace(environ={}, path=types.SimpleNamespace(expanduser=lambda path: path, exists=lambda path: False)),
            canaan_config=types.SimpleNamespace(GAME_MODE_ENABLED=False, TTS_MODE="cpu"),
            canaan_gamemode=types.SimpleNamespace(active=lambda: False),
            CanaanWakeWordDetector=object, WhisperModel=lambda *args, **kwargs: object())
        try:
            hub_file = Path(__file__).resolve().parents[1] / "core/canaan_desk_os_hub.py"
            method = function_from(hub_file, "async_load_models", namespace, "CanaanDeskOSHub")
            method(hub)
        finally:
            candidate.load_sensevoice = original_factory
            for name, module in prior.items():
                if module is absent:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.assertEqual(factory_calls, [selected_path])
        self.assertEqual(calls, [dict(model=selected_path, trust_remote_code=True, device="cpu", disable_update=True)])
        self.assertIsNotNone(hub.asr_model)
        return hub, ready

    def test_hub_startup_uses_project_factory_with_original_model_options(self):
        hub, ready = self.run_hub_loader()
        self.assertIsNotNone(hub.sense_voice)
        self.assertEqual(len(ready), 2)

    def test_hub_keeps_whisper_fallback_when_primary_factory_raises(self):
        hub, ready = self.run_hub_loader(fail_primary=True)
        self.assertIsNone(hub.sense_voice)
        self.assertEqual(len(ready), 1)


if __name__ == "__main__":
    unittest.main()
