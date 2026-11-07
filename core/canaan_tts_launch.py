"""Build the same local TTS launch command for the tray and standalone engine."""
import os
from pathlib import Path


def tts_command(vendor_dir, config_path, *, port=9880, python_exe=None, reference_cache=None):
    vendor_dir = os.fspath(vendor_dir)
    if reference_cache is None:
        import canaan_config
        reference_cache = getattr(canaan_config, "TTS_REFERENCE_CACHE", False)
    executable = python_exe or os.path.join(vendor_dir, "runtime", "python.exe")
    args = ["-a", "127.0.0.1", "-p", str(port), "-c", os.fspath(config_path)]
    if reference_cache:
        wrapper = Path(__file__).resolve().parents[1] / "tools" / "serve_tuantuan_tts.py"
        return [executable, "-I", str(wrapper), "--vendor-dir", vendor_dir,
                "--enable-reference-cache", *args]
    return [executable, "-I", "api_v2.py", *args]
