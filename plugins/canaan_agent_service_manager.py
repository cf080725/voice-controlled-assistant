# -*- coding: utf-8 -*-
"""
Canaan Desk OS - External Agent Cluster Service Manager
Manages background lifecycles for:
  - OpenMuse API (port 8787)
  - OpenMuse Web (port 8081)
  - OpenDots API (port 4310)
  - OpenDots Web (port 5173)
"""
import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import os
import sys
import time
import socket
import subprocess
import logging
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from canaan_agent_board_data import MUSE_API, MUSE_WEB, DOTS_API, DOTS_WEB, LOCAL_AGENT_CONFIG, fetch_muse, fetch_dots

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("canaan_agent_manager")
MUSE_DIR = os.environ.get("CANAAN_MUSE_DIR", str(Path(__file__).resolve().parent.parent.parent / "muse"))
DOTS_DIR = os.environ.get("CANAAN_DOTS_DIR", str(Path(__file__).resolve().parent.parent.parent / "dots"))

SERVICES = {
    "muse_api": {
        "name": "OpenMuse 智能体 API",
        "port": 8787,
        "url": "http://127.0.0.1:8787",
        "cwd": MUSE_DIR,
        "cmd": ["cmd.exe", "/c", "node --env-file=.env --import tsx apps/server/src/index.ts"],
    },
    "muse_web": {
        "name": "OpenMuse Web 工作台",
        "port": 8081,
        "url": "http://127.0.0.1:8081",
        "cwd": MUSE_DIR,
        "cmd": ["cmd.exe", "/c", "pnpm --dir apps/mobile web"],
    },
    "dots_api": {
        "name": "OpenDots 虚拟同事 API",
        "port": 4310,
        "url": "http://127.0.0.1:4310",
        "cwd": DOTS_DIR,
        "cmd": ["cmd.exe", "/c", "node --env-file-if-exists=.env --import tsx src/server/index.ts"],
    },
    "dots_web": {
        "name": "OpenDots 知识空间 Web",
        "port": 5173,
        "url": "http://127.0.0.1:5173",
        "cwd": DOTS_DIR,
        "cmd": ["cmd.exe", "/c", "npx vite"],
    },
}
for key, url in {"muse_api": MUSE_API, "muse_web": MUSE_WEB, "dots_api": DOTS_API, "dots_web": DOTS_WEB}.items():
    SERVICES[key]["url"] = url
    SERVICES[key]["port"] = urlsplit(url).port or (443 if urlsplit(url).scheme == "https" else 80)


def is_port_listening(port, host="127.0.0.1", timeout=0.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False
    finally:
        s.close()


def service_ready(key):
    if key == "muse_api":
        return fetch_muse()["online"]
    if key == "dots_api":
        return fetch_dots()["online"]
    try:
        with urllib.request.urlopen(SERVICES[key]["url"], timeout=2.5) as response:
            return response.status == 200 and "text/html" in response.headers.get("Content-Type", "")
    except Exception:
        return False


def get_pids_for_port(port):
    try:
        import psutil
        return sorted({c.pid for c in psutil.net_connections(kind="tcp")
                       if c.status == psutil.CONN_LISTEN and c.laddr.port == port and c.pid})
    except Exception:
        return []


def start_service(key, wait_timeout=120.0):
    svc = SERVICES.get(key)
    if not svc:
        return False
    port = svc["port"]
    if is_port_listening(port):
        if not service_ready(key):
            logger.error(f"[!] 端口 {port} 被其他程序占用或 API 尚未就绪；不会启动重复服务")
            return False
        logger.info(f"[✓] {svc['name']} 已在运行 (端口 {port})")
        return True

    logger.info(f"[*] 正在拉起 {svc['name']} (端口 {port})...")
    address = urlsplit(svc["url"])
    if address.hostname not in ("localhost", "127.0.0.1"):
        logger.error("远端服务仅可检查状态，请在对应主机启动")
        return False
    try:
        log_path = Path(__file__).resolve().parent.parent / "logs" / "agent_services" / f"{key}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        # 所有 Node 服务只监听本机：Expo 开发服务器默认监听所有网卡，用预加载脚本收紧
        preload = (Path(__file__).resolve().parent.parent / "tools" / "node_localhost_only.cjs").as_posix()  # 反斜杠在 NODE_OPTIONS 里会被当转义
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + f' --require "{preload}"').strip()
        if key.endswith("_api"):
            env.update(PORT=str(port), HOST="127.0.0.1")
            model = LOCAL_AGENT_CONFIG.get("local_model")
            if model:
                env.update(OPENAI_BASE_URL="http://127.0.0.1:11434/v1", OPENAI_API_KEY="ollama",
                           OPENAI_MODEL=model, MODEL="openai/" + model, OPENAI_CHAT_COMPLETIONS="true")
        with log_path.open("ab") as output:
            process = subprocess.Popen(svc["cmd"], cwd=svc["cwd"], env=env,
                                       creationflags=0x08000000 if os.name == "nt" else 0,
                                       close_fds=True, stdout=output, stderr=output)
    except Exception as e:
        logger.error(f"[!] 启动 {svc['name']} 失败: {e}")
        return False

    t0 = time.monotonic()
    while time.monotonic() - t0 < wait_timeout:
        if service_ready(key):
            logger.info(f"[✓] {svc['name']} 启动就绪 (端口 {port}, 耗时 {time.monotonic()-t0:.1f}s)")
            return True
        if process.poll() is not None:
            logger.error(f"[!] {svc['name']} 提前退出，退出码 {process.returncode}；见 {log_path}")
            return False
        time.sleep(0.5)

    logger.warning(f"[!] 等待 {svc['name']} 端口 {port} 超时 ({wait_timeout}s)")
    return False


def stop_service(key):
    svc = SERVICES.get(key)
    if not svc:
        return
    port = svc["port"]
    if not service_ready(key):
        logger.warning(f"[!] {svc['name']} 未验证就绪，不会终止端口上的其他程序")
        return
    pids = get_pids_for_port(port)
    if not pids:
        logger.info(f"[i] {svc['name']} 未运行 (端口 {port})")
        return
    for pid in pids:
        try:
            logger.info(f"[*] 正在终止 {svc['name']} 进程 (PID {pid})...")
            result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            if result.returncode:
                logger.warning(f"[!] 终止 PID {pid} 失败，退出码 {result.returncode}")
                return False
        except Exception as e:
            logger.warning(f"[!] 终止 PID {pid} 失败: {e}")
            return False
    deadline = time.monotonic() + 10
    while is_port_listening(port):
        if time.monotonic() >= deadline:
            logger.warning(f"[!] {svc['name']} 端口仍未释放，请检查状态后重试")
            return False
        time.sleep(.2)
    return True


def ensure_all_running(only_installed=False):
    results = {}
    for key in ["muse_api", "dots_api", "dots_web", "muse_web"]:
        if only_installed and not Path(SERVICES[key]["cwd"]).is_dir():
            continue
        results[key] = start_service(key)
    return results


def stop_all():
    for key in ["muse_api", "dots_api", "dots_web", "muse_web"]:
        stop_service(key)


def status():
    print("=" * 78)
    print("          团团 · 智能体集群状态监视器 (OpenMuse & OpenDots Cluster)          ")
    print("=" * 78)
    all_ok = True
    for key, svc in SERVICES.items():
        online = service_ready(key)
        pids = get_pids_for_port(svc["port"]) if online else []
        pid_str = f"PID {pids[0]}" if pids else "-"
        st_str = "[ 在线 ONLINE ]" if online else "[ 离线 OFFLINE ]"
        if not online:
            all_ok = False
        print(f"{st_str:<16} {svc['name']:<22} 端口:{svc['port']:<6} {pid_str:<10} URL: {svc['url']}")
    print("=" * 78)
    return all_ok


if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if arg in ("ensure", "start"):
        results = ensure_all_running()
        status()
        sys.exit(0 if all(results.values()) else 1)
    elif arg == "stop":
        stop_all()
        status()
    else:
        status()
