# -*- coding: utf-8 -*-
"""团团统一入口。

目录：core（语音链路、意图、执行器、权限、记忆、中枢）/ shells（桌宠、托盘、设置大屏、看板网页）/
plugins（消息、飞书、HTTP/MCP 接口、Muse/Dots，默认关闭）/ tools / training_suite / archive。
模块之间仍然用 `import canaan_xxx` 互相引用；import 本文件会把 core、shells、plugins 放到搜索路径最前面。

用法：python canaan.py [tray|hub|pet|api|mcp|board|cockpit|agents] [该组件自己的参数...]
不带参数等同 tray（托盘版，和以前双击“启动团团（托盘版）.bat”一样）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LAYERS = ("core", "shells", "plugins")


def ensure_paths():
    for layer in reversed(LAYERS):
        path = os.path.join(ROOT, layer)
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    if ROOT not in sys.path:
        sys.path.append(ROOT)  # message_tools 等根目录下的包


ensure_paths()

COMMANDS = {
    "tray": ("shells", "canaan_tray.py", "托盘版（默认）：管理主程序、语音服务和开机自启"),
    "hub": ("core", "canaan_desk_os_hub.py", "只启动主程序（语音、意图、执行）"),
    "pet": ("shells", "canaan_companion_pet.py", "只启动桌宠"),
    "island": ("shells", "canaan_island.py", "苹果式灵动岛（原生毛玻璃窗口，独立进程；起不来时主程序继续用旧版）"),
    "api": ("plugins", "canaan_api_server.py", "本机接口与看板；--pair / --revoke / --list-clients 管理外部客户端"),
    "mcp": ("plugins", "canaan_mcp_server.py", "MCP stdio 服务（给 AutoClaw 等客户端）"),
    "board": ("plugins", "canaan_agent_board.py", "Muse / Dots 任务看板"),
    "cockpit": ("shells", "canaan_settings_cockpit.py", "设置大屏"),
    "agents": ("plugins", "canaan_agent_service_manager.py", "启动 / 停止 Muse、Dots 服务"),
    "bootstrap": ("core", "canaan_bootstrap.py", "首次下载模型（断点续传 + SHA-256 校验）"),
}


def main(argv=None):
    import runpy
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        for key, (_, _, desc) in COMMANDS.items():
            print(f"  {key:<10}{desc}")
        return
    name = argv.pop(0) if argv and argv[0] in COMMANDS else "tray"
    layer, script, _ = COMMANDS[name]
    path = os.path.join(ROOT, layer, script)
    sys.argv = [path, *argv]
    runpy.run_path(path, run_name="__main__")


if __name__ == "__main__":
    main()
