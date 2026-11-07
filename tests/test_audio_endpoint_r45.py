"""Core Audio ABI exercised with in-memory vtables; no Windows COM activation."""
import ctypes as ct
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CALLBACK = getattr(ct, "WINFUNCTYPE", ct.CFUNCTYPE)
HRESULT, UINT, BOOL = ct.c_int32, ct.c_uint32, ct.c_int32


def load_source(name):
    spec = importlib.util.spec_from_file_location("_r45_" + name, ROOT / "core" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def native(monkeypatch):
    audio = load_source("canaan_audio_endpoint")
    events, keep, callback_errors = [], [], []
    state = SimpleNamespace(level=.72, muted=True, init_hr=0, setter_hr=0, create_hr=0)
    def callback(restype, argtypes, function):
        def checked(*args):
            try:
                return function(*args)
            except BaseException as exc:
                callback_errors.append(exc)
                return -2147467259
        wrapped = CALLBACK(restype, ct.c_void_p, *argtypes)(checked)
        keep.append(wrapped)
        return ct.cast(wrapped, ct.c_void_p).value
    def interface(size, slots):
        def unexpected(this):
            events.append("unexpected_slot")
            return -2147467259
        default = callback(HRESULT, [], unexpected)
        vtable = (ct.c_void_p * size)(*[default] * size)
        for slot, value in slots.items():
            vtable[slot] = value
        instance = (ct.c_void_p * 1)(ct.addressof(vtable))
        keep.extend((vtable, instance))
        return ct.addressof(instance)
    def release(name):
        return callback(UINT, [], lambda this: events.append(name + ".release") or 0)
    def get_level(this, output):
        events.append("get_level")
        output[0] = state.level
        return 0
    def get_mute(this, output):
        events.append("get_mute")
        output[0] = int(state.muted)
        return 0
    def set_level(this, value, event):
        assert not event
        state.level = value
        events.append(("set_level", value))
        return state.setter_hr
    def set_mute(this, value, event):
        assert not event
        state.muted = bool(value)
        events.append(("set_mute", value))
        return state.setter_hr
    # Slots below are hand-derived from endpointvolume.h, including IUnknown.
    volume = interface(21, {2: release("volume"),
        7: callback(HRESULT, [ct.c_float, ct.c_void_p], set_level),
        9: callback(HRESULT, [ct.POINTER(ct.c_float)], get_level),
        14: callback(HRESULT, [BOOL, ct.c_void_p], set_mute),
        15: callback(HRESULT, [ct.POINTER(BOOL)], get_mute)})
    identifier = ct.create_unicode_buffer("speaker-memory")
    keep.append(identifier)
    def get_id(this, output):
        output[0] = ct.addressof(identifier)
        return 0
    def activate(this, iid, clsctx, parameters, output):
        assert clsctx == 23 and not parameters
        assert ct.string_at(iid, 16) == bytes.fromhex("822cdf5c1e84464597220cf74078229a")
        output[0] = volume
        events.append("activate")
        return 0
    device = interface(7, {2: release("device"),
        3: callback(HRESULT, [ct.c_void_p, UINT, ct.c_void_p, ct.POINTER(ct.c_void_p)], activate),
        5: callback(HRESULT, [ct.POINTER(ct.c_void_p)], get_id)})
    def default(this, flow, role, output):
        assert (flow, role) == (0, 1)
        output[0] = device
        events.append("default")
        return 0
    enum = interface(8, {2: release("enum"),
        4: callback(HRESULT, [ct.c_int32, ct.c_int32, ct.POINTER(ct.c_void_p)], default)})
    def initialize(reserved, flags):
        assert reserved is None and flags == 2
        events.append("initialize")
        return state.init_hr
    def uninitialize():
        events.append("uninitialize")
    def create(clsid, outer, clsctx, iid, output):
        assert outer is None and clsctx == 1
        ct.cast(output, ct.POINTER(ct.c_void_p))[0] = enum
        events.append("create")
        return state.create_hr
    def free(address):
        assert (address.value if hasattr(address, "value") else address) == ct.addressof(identifier)
        events.append("free_id")
    dll = SimpleNamespace(CoInitializeEx=initialize, CoUninitialize=uninitialize,
                          CoCreateInstance=create, CoTaskMemFree=free)
    monkeypatch.setattr(ct, "WinDLL", lambda *a, **k: dll)
    def forbidden_loader(*args, **kwargs):
        raise AssertionError("An unmocked native library path must never be used")
    monkeypatch.setattr(ct, "OleDLL", forbidden_loader)
    yield SimpleNamespace(audio=audio, events=events, state=state, keep=keep, identifier=identifier)
    assert not callback_errors


@pytest.mark.parametrize("initialize_hr,uninitialize_count", [(0, 1), (1, 1), (-2147417850, 0)])
def test_native_abi_sets_and_reads_same_endpoint_and_balances_only_owned_com(native, initialize_hr, uninitialize_count):
    f = native
    f.state.init_hr = initialize_hr
    with f.audio.open_default_endpoint() as port:
        assert port.snapshot() == {"endpoint_id": "speaker-memory", "level": pytest.approx(.72), "muted": True}
        port.set_level(.3)
        port.set_mute(False)
        assert port.snapshot() == {"endpoint_id": "speaker-memory", "level": pytest.approx(.3), "muted": False}
    assert f.events.count("initialize") == 1 and f.events.count("uninitialize") == uninitialize_count
    tail = ["volume.release", "device.release", "enum.release"] + (["uninitialize"] if uninitialize_count else [])
    assert f.events[-len(tail):] == tail and "unexpected_slot" not in f.events
    with pytest.raises(f.audio.AudioEndpointError):
        port.snapshot()


def test_native_write_hresult_failure_still_releases_before_com_uninitialization(native):
    f = native
    f.state.setter_hr = -2147467259
    with pytest.raises(f.audio.AudioEndpointError):
        with f.audio.open_default_endpoint() as port:
            port.set_level(.3)
    assert f.state.level == pytest.approx(.3)
    assert f.events[-4:] == ["volume.release", "device.release", "enum.release", "uninitialize"]


def test_native_creation_failure_does_not_uninitialize_twice_or_leak_pointer(native):
    f = native
    f.state.create_hr = -2147467259
    with pytest.raises(f.audio.AudioEndpointError):
        with f.audio.open_default_endpoint():
            pytest.fail("Invalid COM instance must not reach caller")
    assert f.events[-2:] == ["enum.release", "uninitialize"]


def test_native_id_storage_is_freed_even_when_decoding_fails(native, monkeypatch):
    f = native
    def broken(address):
        raise ValueError("synthetic ID conversion failure")
    monkeypatch.setattr(ct, "wstring_at", broken)
    with pytest.raises(ValueError):
        with f.audio.open_default_endpoint():
            pytest.fail("Invalid endpoint identity must not reach caller")
    assert "free_id" in f.events
    assert f.events[-4:] == ["volume.release", "device.release", "enum.release", "uninitialize"]


def test_endpoint_rejects_cross_thread_use_before_native_work(native, monkeypatch):
    f = native
    with f.audio.open_default_endpoint() as port:
        before = len(f.events)
        with monkeypatch.context() as patch:
            patch.setattr(f.audio.threading, "get_ident", lambda: -1)
            with pytest.raises(f.audio.AudioEndpointError):
                port.snapshot()
        assert len(f.events) == before


def test_import_loads_no_native_libraries(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Import must not load a native library")
    monkeypatch.setattr(ct, "WinDLL", forbidden)
    monkeypatch.setattr(ct, "OleDLL", forbidden)
    load_source("canaan_audio_endpoint")


def test_failed_com_initialization_never_acquires_or_uninitializes(native):
    f = native
    f.state.init_hr = -2147467259
    with pytest.raises(f.audio.AudioEndpointError):
        with f.audio.open_default_endpoint():
            pytest.fail("Failed COM initialization must not reach caller")
    assert f.events == ["initialize"]


def test_released_pointer_is_null_and_cannot_be_released_twice(native):
    f = native
    with f.audio.open_default_endpoint() as port:
        owned = port._volume
    assert owned.address.value is None
    count = len(f.events)
    owned.release()
    assert len(f.events) == count


def test_changed_default_endpoint_blocks_setter(native):
    f = native
    with f.audio.open_default_endpoint() as port:
        f.identifier.value = "different"
        with pytest.raises(f.audio.AudioEndpointError) as error:
            port.set_level(.3)
        assert error.value.code == "AUDIO_ENDPOINT_CHANGED" and f.state.level == .72


@pytest.mark.parametrize("level", [float("nan"), float("inf"), -1, 2])
def test_invalid_native_level_readback_is_rejected(native, level):
    f = native
    f.state.level = level
    with f.audio.open_default_endpoint() as port:
        with pytest.raises(f.audio.AudioEndpointError) as error:
            port.snapshot()
        assert error.value.code == "AUDIO_STATE_UNAVAILABLE"


def test_s_false_setter_is_success_and_still_reads_target(native):
    f = native
    f.state.setter_hr = 1
    with f.audio.open_default_endpoint() as port:
        port.set_mute(False)
        assert port.snapshot()["muted"] is False


@pytest.fixture
def media(monkeypatch):
    module = load_source("canaan_media_session")
    data = {"requested": "pause", "before": "playing", "after": "paused", "accepted": True,
            "verified": True, "session_id": "one", "session_id_before": "one", "session_id_after": "one", "error_code": None}
    calls = []
    def run(script, timeout):
        calls.append((script, timeout))
        return SimpleNamespace(stdout=json.dumps(data), returncode=0)
    monkeypatch.setattr(module, "_run_bridge", run)
    return SimpleNamespace(module=module, data=data, calls=calls)


def test_media_parser_accepts_only_validated_same_session_target(media):
    assert media.module.control_media("pause")["after"] == "paused"
    assert media.calls[0][1] == 6.0


@pytest.mark.parametrize("key,value", [("requested", "play"), ("after", "playing"),
    ("accepted", False), ("session_id", ""), ("session_id_after", "two"), ("before", "unknown"), ("error_code", "MEDIA_TIMEOUT")])
def test_media_parser_rejects_false_positive_bridge_result(media, key, value):
    media.data[key] = value
    with pytest.raises(media.module.MediaSessionError):
        media.module.control_media("pause")


@pytest.mark.parametrize("mode", ["", "play_pause", "pause'; Stop-Process -Name example; '"])
def test_media_invalid_modes_never_reach_subprocess(media, mode):
    with pytest.raises(ValueError):
        media.module.control_media(mode)
    assert not media.calls


def test_media_timeout_is_unconfirmed_and_never_retried(media, monkeypatch):
    def timeout(*args):
        media.calls.append(args)
        raise subprocess.TimeoutExpired("fake", 6.0)
    monkeypatch.setattr(media.module, "_run_bridge", timeout)
    with pytest.raises(media.module.MediaSessionError, match="未核实") as error:
        media.module.control_media("pause")
    assert error.value.code == "MEDIA_TIMEOUT" and len(media.calls) == 1


@pytest.mark.parametrize("timeout", [-1, .1, 11, float("nan"), float("inf"), True, "6"])
def test_invalid_media_budget_never_reaches_subprocess(media, timeout):
    with pytest.raises(ValueError):
        media.module.control_media("pause", timeout)
    assert not media.calls


def test_bridge_runs_hidden_with_fixed_encoded_script_and_bounded_wait(monkeypatch):
    import base64
    module = load_source("canaan_media_session")
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return "synthetic child"
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._run_bridge("fixed script", 2.0) == "synthetic child"
    args, options = calls[0]
    command = args[0]
    assert command[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert base64.b64decode(command[4]).decode("utf-16le") == "fixed script"
    assert options["timeout"] == 2.0 and options["creationflags"] & 0x08000000
    assert not options.get("shell", False)


@pytest.mark.parametrize("stdout,returncode", [("not JSON", 0), ("[]", 0), ("x" * 65537, 0), (None, 0)],
                         ids=["invalid-json", "array", "oversized", "missing"])
def test_media_malformed_result_never_becomes_success(media, monkeypatch, stdout, returncode):
    monkeypatch.setattr(media.module, "_run_bridge", lambda *a: SimpleNamespace(stdout=stdout, returncode=returncode))
    with pytest.raises(media.module.MediaSessionError):
        media.module.control_media("pause")


def test_media_nonzero_child_exit_never_accepts_claimed_verified_state(media, monkeypatch):
    monkeypatch.setattr(media.module, "_run_bridge", lambda *a: SimpleNamespace(stdout=json.dumps(media.data), returncode=1))
    with pytest.raises(media.module.MediaSessionError):
        media.module.control_media("pause")


def test_media_stopped_to_playing_requires_accepted_same_session_readback(media):
    media.data.update(requested="play", before="stopped", after="playing")
    result = media.module.control_media("play")
    assert result["before"] == "stopped" and result["after"] == "playing"
    assert result["accepted"] is True and len(media.calls) == 1


@pytest.mark.parametrize("field,value", [("accepted", False), ("after", "stopped"),
    ("after", "paused"), ("session_id_after", "other"), ("error_code", "MEDIA_CONTROL_DISABLED")])
def test_media_stopped_play_without_accepted_target_readback_is_unconfirmed(media, field, value):
    media.data.update(requested="play", before="stopped", after="playing")
    media.data[field] = value
    with pytest.raises(media.module.MediaSessionError):
        media.module.control_media("play")
    assert len(media.calls) == 1


def test_media_stopped_does_not_count_as_paused(media):
    media.data.update(before="stopped", after="stopped")
    with pytest.raises(media.module.MediaSessionError):
        media.module.control_media("pause")
