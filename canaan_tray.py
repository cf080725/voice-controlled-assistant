"""兼容垫片：托盘已搬到 shells/canaan_tray.py。
Windows 计划任务“CanaanDeskTray”和旧的开机自启仍运行这个路径，所以保留。直接运行 = 运行新位置；被 import 时换成真正的模块。"""
import importlib.util
import os
import runpy
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_REAL = os.path.join(_ROOT, "shells", "canaan_tray.py")
if _ROOT not in sys.path:
    sys.path.append(_ROOT)
import canaan  # noqa: E402,F401  把 core/shells/plugins 放到搜索路径最前

if __name__ == "__main__":
    sys.argv[0] = _REAL
    runpy.run_path(_REAL, run_name="__main__")
else:
    _spec = importlib.util.spec_from_file_location(__name__, _REAL)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[__name__] = _mod
    _spec.loader.exec_module(_mod)
