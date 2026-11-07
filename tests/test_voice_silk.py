"""Real local SILK fixtures are generated from tones, never private recordings."""
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

import canaan_voice_messages as voice


@pytest.fixture
def silk_bytes():
    import io
    import pysilk
    rate = 16000
    pcm = (np.sin(np.arange(rate) / rate * 2 * np.pi * 440) * 10000).astype("<i2").tobytes()
    encoded = {}
    for tencent in (False, True):
        output = io.BytesIO()
        pysilk.encode(io.BytesIO(pcm), output, rate, 24000, tencent=tencent)
        encoded[tencent] = output.getvalue()
    return encoded


def multiframe_tone(packet_ms):
    from pysilk.backends.cffi import ffi, lib
    rate = 16000
    pcm = (np.sin(np.arange(19200) / rate * 2 * np.pi * 440) * 10000).astype("<i2").tobytes()
    size, status = ffi.new("int32_t *"), ffi.new("SKP_SILK_SDK_EncControlStruct *")
    assert lib.SKP_Silk_SDK_Get_Encoder_Size(size) == 0
    state = ffi.new("uint8_t[]", size[0])
    assert lib.SKP_Silk_SDK_InitEncoder(state, status) == 0
    control = ffi.new("SKP_SILK_SDK_EncControlStruct *", {"API_sampleRate": rate,
                      "maxInternalSampleRate": 24000, "packetSize": rate * packet_ms // 1000,
                      "bitRate": 24000, "complexity": 2})
    payload, length = ffi.new("uint8_t[1250]"), ffi.new("int16_t *")
    output, packets = bytearray(b"\x02#!SILK_V3"), 0
    for offset in range(0, len(pcm), 640):
        chunk = ffi.from_buffer("int16_t[]", pcm[offset:offset + 640])
        length[0] = 1250
        assert lib.SKP_Silk_SDK_Encode(state, control, chunk, 320, payload, length) == 0
        if length[0]:
            output.extend(length[0].to_bytes(2, "little", signed=True))
            output.extend(ffi.buffer(payload, length[0]))
            packets += 1
    assert packets == 1200 // packet_ms
    return bytes(output)


@pytest.mark.parametrize("packet_ms", [40, 60, 80, 100])
@pytest.mark.parametrize("tencent", [False, True])
def test_multiframe_silk_packet_keeps_every_frame(tmp_path, packet_ms, tencent):
    path = tmp_path / "multiple.amr"
    encoded = multiframe_tone(packet_ms)
    path.write_bytes(encoded if tencent else encoded[1:] + b"\xff\xff")
    audio = voice.decode_audio(path)
    assert audio.shape == (19200,)
    spectrum = np.abs(np.fft.rfft(audio[1600:]))
    assert abs(np.argmax(spectrum) * 16000 / len(audio[1600:]) - 440) < 3


def test_multiframe_duration_is_limited_by_decoded_frames(tmp_path, monkeypatch):
    path = tmp_path / "multiple.amr"
    path.write_bytes(multiframe_tone(100))
    monkeypatch.setattr(voice, "MAX_SECONDS", 1)
    with pytest.raises(ValueError, match="十分钟"):
        voice.decode_audio(path)


@pytest.mark.parametrize("rate", [8000, 24000, 48000])
def test_silk_source_sample_rate_is_normalized_to_16000(tmp_path, rate):
    import io
    import pysilk
    pcm = (np.sin(np.arange(rate) / rate * 2 * np.pi * 440) * 10000).astype("<i2").tobytes()
    output = io.BytesIO()
    pysilk.encode(io.BytesIO(pcm), output, rate, 24000)
    path = tmp_path / "rate.amr"
    path.write_bytes(output.getvalue())
    audio = voice.decode_audio(path)
    assert audio.shape == (16000,) and audio.dtype == np.float32


@pytest.mark.parametrize("tencent,extension", [(False, ".silk"), (True, ".amr"), (True, ".dat")])
def test_real_silk_tone_is_detected_by_header_and_decoded_to_mono_f32(tmp_path, silk_bytes, tencent, extension):
    path = tmp_path / ("tone" + extension)
    path.write_bytes(silk_bytes[tencent])
    audio = voice.decode_audio(path)
    assert audio.dtype == np.float32 and audio.shape == (16000,)
    assert np.isfinite(audio).all() and np.max(np.abs(audio)) <= 1
    assert np.sqrt(np.mean(audio ** 2)) > .1
    spectrum = np.abs(np.fft.rfft(audio[1600:]))
    assert abs(np.argmax(spectrum) * 16000 / len(audio[1600:]) - 440) < 3


def test_standard_silk_terminal_marker(tmp_path, silk_bytes):
    path = tmp_path / "standard.silk"
    path.write_bytes(silk_bytes[False] + b"\xff\xff")
    assert voice.decode_audio(path).shape == (16000,)


def test_pythonw_host_can_exchange_pcm_through_child_pipes(tmp_path, silk_bytes, monkeypatch):
    pythonw = Path(voice.sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("requires the Windows GUI interpreter used by the live hub")
    monkeypatch.setattr(voice.sys, "executable", str(pythonw))
    path = tmp_path / "gui-host.amr"
    path.write_bytes(silk_bytes[True])
    assert voice.decode_audio(path).shape == (16000,)


@pytest.mark.parametrize("data", [b"\x02#!SILK_V3", b"#!SILK_V3\x10\0broken", b"#!SILK_V3\xff\xffextra",
                                  b"\x02#!SILK_V3\xe3\x04" + b"\0" * 1251,
                                  b"\x02#!SILK_V3\x04\0\xff\xff\xff\xff"],
                         ids=["header-only", "truncated-frame", "invalid-terminal", "oversized-packet", "native-payload-error"])
def test_damaged_silk_fails_explicitly(tmp_path, data):
    path = tmp_path / "broken.amr"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="SILK.*完整"):
        voice.decode_audio(path)


def test_trailing_byte_is_not_silently_ignored(tmp_path, silk_bytes):
    path = tmp_path / "truncated.dat"
    path.write_bytes(silk_bytes[True] + b"\0")
    with pytest.raises(ValueError, match="SILK.*完整"):
        voice.decode_audio(path)


def test_silk_size_and_duration_are_bounded_before_native_decoder(tmp_path, silk_bytes, monkeypatch):
    path = tmp_path / "large.amr"
    with path.open("wb") as out:
        out.write(b"\x02#!SILK_V3")
        out.truncate(voice.MAX_BYTES + 1)
    monkeypatch.setattr(voice.subprocess, "run", lambda *a, **k: pytest.fail("invalid audio reached native decoder"))
    with pytest.raises(ValueError, match="20 MiB"):
        voice.decode_audio(path)
    path.write_bytes(silk_bytes[True] + silk_bytes[True][10:])
    monkeypatch.setattr(voice, "MAX_SECONDS", 1)
    with pytest.raises(ValueError, match="十分钟"):
        voice.decode_audio(path)


def test_silk_missing_dependency_has_actionable_error(tmp_path, silk_bytes, monkeypatch):
    path = tmp_path / "tone.amr"
    path.write_bytes(silk_bytes[True])
    run = subprocess.run

    def no_site_packages(command, **kwargs):
        assert command[1:3] == ["-I", "-c"]
        return run([command[0], "-I", "-S", *command[2:]], **kwargs)

    monkeypatch.setattr(voice.subprocess, "run", no_site_packages)
    with pytest.raises(ValueError, match="silk-python"):
        voice.decode_audio(path)


@pytest.mark.parametrize("failure", ["timeout", "crash"])
def test_native_silk_failure_is_isolated(tmp_path, silk_bytes, monkeypatch, failure):
    path = tmp_path / "tone.amr"
    path.write_bytes(silk_bytes[True])
    run = subprocess.run

    def fail_in_child(command, **kwargs):
        assert kwargs["timeout"] == 60
        assert kwargs["input"] == silk_bytes[True]
        command = list(command)
        command[3] = ("import time; time.sleep(5)\n" if failure == "timeout" else "import os; os._exit(99)\n") + command[3]
        kwargs["timeout"] = .05 if failure == "timeout" else 5
        return run(command, **kwargs)

    monkeypatch.setattr(voice.subprocess, "run", fail_in_child)
    start = time.monotonic()
    with pytest.raises(ValueError, match="超时" if failure == "timeout" else "解码失败"):
        voice.decode_audio(path)
    assert time.monotonic() - start < 3


def test_native_silk_pcm_length_is_checked(tmp_path, silk_bytes, monkeypatch):
    path = tmp_path / "tone.amr"
    path.write_bytes(silk_bytes[True])
    monkeypatch.setattr(voice, "MAX_SECONDS", 1)
    monkeypatch.setattr(voice.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, b"\0\0" * 16001, b""))
    with pytest.raises(ValueError, match="十分钟"):
        voice.decode_audio(path)
