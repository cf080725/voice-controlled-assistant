"""Thread-owned Core Audio endpoint using only ctypes and the Windows COM ABI.

Import is inert: no COM, DLL loading, or third-party auto-initialization.
Vtable order and types follow the Windows SDK endpointvolume.h/mmdeviceapi.h:
https://learn.microsoft.com/windows/win32/api/endpointvolume/nn-endpointvolume-iaudioendpointvolume
https://learn.microsoft.com/windows/win32/api/mmdeviceapi/nn-mmdeviceapi-immdeviceenumerator
GetId storage belongs to the caller and is freed with CoTaskMemFree:
https://learn.microsoft.com/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-immdevice-getid
"""
from contextlib import contextmanager
import ctypes as ct
import math
import threading
from types import SimpleNamespace
import uuid


class AudioEndpointError(RuntimeError):
    def __init__(self, code, message, hresult=None):
        super().__init__(message)
        self.code, self.hresult = code, hresult


_HRESULT, _DWORD, _UINT, _BOOL = ct.c_int32, ct.c_uint32, ct.c_uint32, ct.c_int32
_CALL = getattr(ct, "WINFUNCTYPE", ct.CFUNCTYPE)
_VOID, _OUT = ct.c_void_p, ct.POINTER(ct.c_void_p)


class _GUID(ct.Structure):
    _fields_ = [("Data1", ct.c_uint32), ("Data2", ct.c_uint16), ("Data3", ct.c_uint16), ("Data4", ct.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


_GUID_PTR = ct.POINTER(_GUID)
_CLSID_ENUMERATOR = _GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_ENUMERATOR = _GUID.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_VOLUME = _GUID.parse("5CDF2C82-841E-4546-9722-0CF74078229A")


def _method(*args, result=_HRESULT):
    return _CALL(result, _VOID, *args)


_UNKNOWN_FIELDS = [("QueryInterface", _method(_GUID_PTR, _OUT)),
                   ("AddRef", _method(result=_UINT)), ("Release", _method(result=_UINT))]


class _EnumeratorVTable(ct.Structure):
    _fields_ = _UNKNOWN_FIELDS + [
        ("EnumAudioEndpoints", _method(ct.c_int32, _DWORD, _OUT)),
        ("GetDefaultAudioEndpoint", _method(ct.c_int32, ct.c_int32, _OUT)),
        ("GetDevice", _method(ct.c_wchar_p, _OUT)),
        ("RegisterEndpointNotificationCallback", _method(_VOID)),
        ("UnregisterEndpointNotificationCallback", _method(_VOID)),
    ]


class _DeviceVTable(ct.Structure):
    _fields_ = _UNKNOWN_FIELDS + [
        ("Activate", _method(_GUID_PTR, _DWORD, _VOID, _OUT)),
        ("OpenPropertyStore", _method(_DWORD, _OUT)),
        ("GetId", _method(_OUT)), ("GetState", _method(ct.POINTER(_DWORD))),
    ]


class _VolumeVTable(ct.Structure):
    _fields_ = _UNKNOWN_FIELDS + [
        ("RegisterControlChangeNotify", _method(_VOID)),
        ("UnregisterControlChangeNotify", _method(_VOID)),
        ("GetChannelCount", _method(ct.POINTER(_UINT))),
        ("SetMasterVolumeLevel", _method(ct.c_float, _GUID_PTR)),
        ("SetMasterVolumeLevelScalar", _method(ct.c_float, _GUID_PTR)),
        ("GetMasterVolumeLevel", _method(ct.POINTER(ct.c_float))),
        ("GetMasterVolumeLevelScalar", _method(ct.POINTER(ct.c_float))),
        ("SetChannelVolumeLevel", _method(_UINT, ct.c_float, _GUID_PTR)),
        ("SetChannelVolumeLevelScalar", _method(_UINT, ct.c_float, _GUID_PTR)),
        ("GetChannelVolumeLevel", _method(_UINT, ct.POINTER(ct.c_float))),
        ("GetChannelVolumeLevelScalar", _method(_UINT, ct.POINTER(ct.c_float))),
        ("SetMute", _method(_BOOL, _GUID_PTR)),
        ("GetMute", _method(ct.POINTER(_BOOL))),
        ("GetVolumeStepInfo", _method(ct.POINTER(_UINT), ct.POINTER(_UINT))),
        ("VolumeStepUp", _method(_GUID_PTR)), ("VolumeStepDown", _method(_GUID_PTR)),
        ("QueryHardwareSupport", _method(ct.POINTER(_DWORD))),
        ("GetVolumeRange", _method(ct.POINTER(ct.c_float), ct.POINTER(ct.c_float), ct.POINTER(ct.c_float))),
    ]


def _checked(hr):
    signed = ct.c_int32(hr).value
    if signed < 0:
        raise AudioEndpointError("AUDIO_NATIVE_ERROR", "Windows 音频接口调用失败", signed)
    return signed


class _ComPointer:
    """Own exactly one interface reference; release nulls every Python alias."""
    def __init__(self, address, vtable_type):
        self.address, self.vtable_type = ct.c_void_p(address), vtable_type
        self.thread = threading.get_ident()

    def call(self, name, *args):
        if threading.get_ident() != self.thread or not self.address.value:
            raise AudioEndpointError("AUDIO_ENDPOINT_CLOSED", "音频接口不在当前调用线程")
        table = ct.cast(self.address, ct.POINTER(ct.POINTER(self.vtable_type))).contents.contents
        return getattr(table, name)(self.address, *args)

    def release(self):
        if self.address.value:
            try:
                self.call("Release")
            finally:
                self.address.value = None


def _native_bindings():
    # WinDLL preserves raw HRESULTs, including S_FALSE and RPC_E_CHANGED_MODE.
    # Loading happens only inside an explicitly opened endpoint context.
    ole32 = ct.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes, ole32.CoInitializeEx.restype = [_VOID, _DWORD], _HRESULT
    ole32.CoUninitialize.argtypes, ole32.CoUninitialize.restype = [], None
    ole32.CoCreateInstance.argtypes = [_GUID_PTR, _VOID, _DWORD, _GUID_PTR, _OUT]
    ole32.CoCreateInstance.restype = _HRESULT
    ole32.CoTaskMemFree.argtypes, ole32.CoTaskMemFree.restype = [_VOID], None

    def initialize():
        hr = ct.c_int32(ole32.CoInitializeEx(None, 2)).value
        if (hr & 0xFFFFFFFF) == 0x80010106:
            return False  # Existing caller-owned apartment; no matching Uninitialize.
        _checked(hr)
        return True  # Both S_OK and S_FALSE must be balanced on this exact thread.

    def create_enumerator():
        address = ct.c_void_p()
        hr = ole32.CoCreateInstance(ct.byref(_CLSID_ENUMERATOR), None, 1, ct.byref(_IID_ENUMERATOR), ct.byref(address))
        interface = _ComPointer(address.value, _EnumeratorVTable)
        try:
            _checked(hr)
            if not address.value:
                raise AudioEndpointError("AUDIO_ENDPOINT_UNAVAILABLE", "没有音频端点枚举接口")
            return interface
        except Exception:
            interface.release()
            raise

    def default_device(enumerator):
        address = ct.c_void_p()
        hr = enumerator.call("GetDefaultAudioEndpoint", 0, 1, ct.byref(address))
        interface = _ComPointer(address.value, _DeviceVTable)
        try:
            _checked(hr)
            if not address.value:
                raise AudioEndpointError("AUDIO_ENDPOINT_UNAVAILABLE", "没有默认播放端点")
            return interface
        except Exception:
            interface.release()
            raise

    def device_id(device):
        address = ct.c_void_p()
        try:
            _checked(device.call("GetId", ct.byref(address)))
            if not address.value:
                raise AudioEndpointError("AUDIO_ENDPOINT_UNAVAILABLE", "音频端点没有标识")
            return ct.wstring_at(address.value)
        finally:
            if address.value:
                ole32.CoTaskMemFree(address)

    def activate(device):
        address = ct.c_void_p()
        hr = device.call("Activate", ct.byref(_IID_VOLUME), 23, None, ct.byref(address))
        interface = _ComPointer(address.value, _VolumeVTable)
        try:
            _checked(hr)
            if not address.value:
                raise AudioEndpointError("AUDIO_ENDPOINT_UNAVAILABLE", "音频端点不可用")
            return interface
        except Exception:
            interface.release()
            raise

    return SimpleNamespace(initialize=initialize, uninitialize=ole32.CoUninitialize,
        create_enumerator=create_enumerator, default_device=default_device, device_id=device_id, activate=activate)


class _Endpoint:
    def __init__(self, bindings, enumerator, device, volume):
        self._bindings, self._enumerator, self._device, self._volume = bindings, enumerator, device, volume
        self._thread = threading.get_ident()
        self._id = bindings.device_id(device)
        if not isinstance(self._id, str) or not self._id:
            raise AudioEndpointError("AUDIO_ENDPOINT_UNAVAILABLE", "音频端点标识无效")

    def _check(self):
        if threading.get_ident() != self._thread or self._volume is None:
            raise AudioEndpointError("AUDIO_ENDPOINT_CLOSED", "音频接口不在当前调用线程")
        current = self._bindings.default_device(self._enumerator)
        try:
            if self._bindings.device_id(current) != self._id:
                raise AudioEndpointError("AUDIO_ENDPOINT_CHANGED", "默认音频端点已变更")
        finally:
            current.release()

    def snapshot(self):
        self._check()
        level, muted = ct.c_float(), _BOOL()
        _checked(self._volume.call("GetMasterVolumeLevelScalar", ct.byref(level)))
        _checked(self._volume.call("GetMute", ct.byref(muted)))
        if not math.isfinite(level.value) or not 0 <= level.value <= 1 or muted.value not in (0, 1):
            raise AudioEndpointError("AUDIO_STATE_UNAVAILABLE", "音频状态无效")
        self._check()
        return {"endpoint_id": self._id, "level": float(level.value), "muted": bool(muted.value)}

    def set_level(self, value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Audio level must be within 0..1")
        self._check()
        _checked(self._volume.call("SetMasterVolumeLevelScalar", float(value), None))

    def set_mute(self, value):
        if type(value) is not bool:
            raise ValueError("Mute target must be bool")
        self._check()
        _checked(self._volume.call("SetMute", int(value), None))

    def close(self):
        self._volume = self._device = self._enumerator = None


@contextmanager
def open_default_endpoint():
    """Initialize, read/write/read, release interfaces, then balance owned COM."""
    bindings = _native_bindings()
    initialized = False
    port = enumerator = device = volume = None
    try:
        initialized = bindings.initialize()
        enumerator = bindings.create_enumerator()
        device = bindings.default_device(enumerator)
        volume = bindings.activate(device)
        port = _Endpoint(bindings, enumerator, device, volume)
        yield port
    finally:
        if port is not None:
            port.close()
        # Explicitly Release/null even if a traceback retains the wrapper.
        # There is no finalizer that can Release after CoUninitialize.
        try:
            try:
                if volume is not None: volume.release()
            finally:
                try:
                    if device is not None: device.release()
                finally:
                    if enumerator is not None: enumerator.release()
        finally:
            if initialized:
                bindings.uninitialize()
