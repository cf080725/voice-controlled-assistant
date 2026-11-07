"""兼容垫片：MCP 服务已搬到 plugins/canaan_mcp_server.py。
AutoClaw 等客户端的配置里写的是这个路径，所以保留。直接运行 = 运行新位置；被 import 时换成真正的模块。
注意：stdio 协议数据走标准输出，这里不能打印任何东西。"""
import importlib.util
import os
import runpy
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_REAL = os.path.join(_ROOT, "plugins", "canaan_mcp_server.py")
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
