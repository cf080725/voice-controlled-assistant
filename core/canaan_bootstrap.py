# -*- coding: utf-8 -*-
"""
首次运行：按 models_manifest.json 下载开放模型（断点续传、多镜像、SHA-256 校验），并按显存档位导入 Ollama 模型。
已存在且校验通过的文件直接跳过，可反复运行。

用法: py -3.12 canaan_bootstrap.py [--check]   （--check 只检查不下载）
"""
import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后直接运行本文件时补全搜索路径
import os
import sys
import json
import shutil
import hashlib
import tarfile
import tempfile
import uuid
import argparse
import subprocess
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(BASE_DIR, "models_manifest.json")
CHUNK = 1 << 20


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def download(mirrors, dest, expected_sha=None, expected_size=None, retries=3, timeout=30, progress=print):
    """下载到 dest.part（已有部分用 HTTP Range 续传），完成后校验 SHA-256 再改名。返回 True/False。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    for url in mirrors:
        for attempt in range(retries):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if expected_size and have > expected_size:  # 坏的残片
                os.remove(part)
                have = 0
            req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    if have and r.status != 206:  # 服务器不支持续传：从头下
                        have = 0
                        mode = "wb"
                    else:
                        mode = "ab"
                    total = have + int(r.headers.get("Content-Length") or 0)
                    with open(part, mode) as f:
                        got = have
                        while True:
                            b = r.read(CHUNK)
                            if not b:
                                break
                            f.write(b)
                            got += len(b)
                            if total and got % (20 * CHUNK) < CHUNK:
                                progress(f"    {os.path.basename(dest)} {got / 1e6:.0f}/{total / 1e6:.0f} MB")
            except Exception as e:
                progress(f"    [{attempt + 1}/{retries}] {url[:60]}… 中断：{e}（下次续传）")
                continue
            if expected_size and os.path.getsize(part) != expected_size:
                progress(f"    大小不符（{os.path.getsize(part)} ≠ {expected_size}），重试")
                continue
            if expected_sha and sha256(part) != expected_sha:
                progress("    SHA-256 校验失败，删除重下")
                os.remove(part)
                continue
            os.replace(part, dest)
            return True
    return False


def _install_archive(item, arc, progress):
    """解压到 staging，全部校验通过后再合并到最终目录；失败恢复原文件。"""
    extract_root = os.path.join(BASE_DIR, item["extract_to"])
    staging_root = os.path.join(BASE_DIR, "models", "_staging")
    os.makedirs(staging_root, exist_ok=True)
    staging = tempfile.mkdtemp(prefix="canaan-", dir=staging_root)
    backups, created = {}, []
    try:
        with tarfile.open(arc) as t:
            t.extractall(staging, filter="data")
        bad = []
        for rel, expected in item["verify"].items():
            rel_to_root = os.path.relpath(os.path.join(BASE_DIR, rel), extract_root)
            staged = os.path.join(staging, rel_to_root)
            if not (os.path.exists(staged) and sha256(staged) == expected):
                bad.append(rel)
        if bad:
            progress(f"[!] 解压后校验失败：{bad}")
            return False
        for dirpath, _, filenames in os.walk(staging):
            rel_dir = os.path.relpath(dirpath, staging)
            dest_dir = extract_root if rel_dir == "." else os.path.join(extract_root, rel_dir)
            os.makedirs(dest_dir, exist_ok=True)
            for name in filenames:
                src = os.path.join(dirpath, name)
                dst = os.path.join(dest_dir, name)
                if os.path.exists(dst):
                    bak = f"{dst}.bak-{uuid.uuid4().hex[:8]}"
                    shutil.copy2(dst, bak)
                    backups[dst] = bak
                else:
                    created.append(dst)
                shutil.copy2(src, dst)
        bad = [p for p, s in ((os.path.join(BASE_DIR, p), s) for p, s in item["verify"].items())
               if not (os.path.exists(p) and sha256(p) == s)]
        if bad:
            raise RuntimeError(f"安装后校验失败：{bad}")
        for bak in backups.values():
            try:
                os.remove(bak)
            except OSError:
                pass
        return True
    except Exception as e:
        for dst, bak in backups.items():
            try:
                if os.path.exists(bak):
                    shutil.copy2(bak, dst)
                    os.remove(bak)
            except OSError:
                pass
        for dst in created:
            try:
                os.remove(dst)
            except OSError:
                pass
        progress(f"[!] 安装失败，已恢复原文件：{type(e).__name__}")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def ensure_files(manifest, check_only=False, progress=print):
    ok_all = True
    for item in manifest["files"]:
        if "archive" in item:
            targets = {os.path.join(BASE_DIR, p): s for p, s in item["verify"].items()}
            if all(os.path.exists(p) and sha256(p) == s for p, s in targets.items()):
                progress(f"[✓] {item['name']}")
            elif check_only:
                progress(f"[ ] {item['name']}（缺失或校验不通过）")
                ok_all = False
                continue
            else:
                progress(f"[↓] {item['name']}")
                arc = os.path.join(BASE_DIR, "models", "_downloads", item["archive"])
                if not download(item["mirrors"], arc, progress=progress):
                    ok_all = False
                    continue
                if not _install_archive(item, arc, progress):
                    ok_all = False
                    continue
                progress(f"[✓] {item['name']}")
            for src, dst in item.get("copy", {}).items():
                d = os.path.join(BASE_DIR, dst)
                if not os.path.exists(d):
                    shutil.copy2(os.path.join(BASE_DIR, src), d)
            continue
        dest = os.path.join(BASE_DIR, item["path"])
        if os.path.exists(dest) and os.path.getsize(dest) == item["size"] and sha256(dest) == item["sha256"]:
            progress(f"[✓] {item['name']}")
            continue
        if check_only:
            progress(f"[ ] {item['name']}（缺失或校验不通过）")
            ok_all = False
            continue
        progress(f"[↓] {item['name']}（{item['size'] / 1e6:.1f} MB）")
        if download(item["mirrors"], dest, item["sha256"], item["size"], progress=progress):
            progress(f"[✓] {item['name']}")
        else:
            progress(f"[!] {item['name']} 下载失败（可稍后重新运行，会续传）")
            ok_all = False
    return ok_all


def ollama_models():
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30).stdout
        return {line.split()[0] for line in out.splitlines()[1:] if line.strip()}
    except Exception:
        return None


def ensure_ollama(manifest, tier, check_only=False, progress=print):
    have = ollama_models()
    if have is None:
        progress("[!] 没找到 Ollama：请先安装 Ollama（https://ollama.com，或国内镜像）后重新运行")
        return False
    ok_all = True
    for item in manifest["ollama"]:
        if tier not in item["tiers"]:
            continue
        last = item["steps"][-1]
        target = last[-1] if last[1] == "cp" else last[2]
        if target in have:
            progress(f"[✓] {item['name']}")
            continue
        if check_only:
            progress(f"[ ] {item['name']}")
            ok_all = False
            continue
        req = item.get("requires_file")
        if req and not os.path.exists(os.path.join(BASE_DIR, req)):
            progress(f"[-] {item['name']}：缺少 {req}，跳过（意图识别会退回原版模型链）")
            continue
        progress(f"[↓] {item['name']}")
        for step in item["steps"]:
            r = subprocess.run(step, cwd=BASE_DIR)
            if r.returncode != 0:
                progress(f"[!] 执行失败：{' '.join(step)}")
                ok_all = False
                break
    return ok_all


# ----------------------------------------------------------------------------
# 大模型怎么跑：本机 / 云端 API / 只用规则（首次运行时问一次，写进 data/llm_providers.json 的 "mode"）
# ----------------------------------------------------------------------------

# 云端服务：名字 → (显示名, 接口类型, 默认模型)。地址见 canaan_llm_provider.KNOWN_BASE_URLS
CLOUD_CHOICES = [
    ("deepseek", "DeepSeek", "openai", "deepseek-chat"),
    ("qwen", "通义千问（阿里云百炼）", "openai", "qwen-plus"),
    ("kimi", "Kimi（月之暗面）", "openai", "moonshot-v1-8k"),
    ("zhipu", "智谱 GLM", "openai", "glm-4-flash"),
    ("siliconflow", "硅基流动", "openai", "Qwen/Qwen2.5-7B-Instruct"),
    ("openai", "OpenAI", "openai", "gpt-4o-mini"),
    ("claude", "Claude（Anthropic，需要 pip install anthropic）", "anthropic", "claude-haiku-5-5"),
    ("custom", "其他 OpenAI 兼容接口（自己填地址）", "openai", ""),
]
MODES = ("local", "api", "rules")


def recommend_mode(tier):
    """8GB / 6GB 显存推荐本机模型；更小或没有独显推荐云端 API。"""
    return "local" if tier in ("8gb", "6gb") else "api"


def describe_tier(tier, vram_mb):
    if tier == "8gb":
        return f"检测到显存 {vram_mb} MB：可以跑本机 3B 模型（约占 2.5 GB 显存）"
    if tier == "6gb":
        return f"检测到显存 {vram_mb} MB：可以跑本机 1.5B 模型（约占 1.5 GB 显存，聊天质量一般）"
    return "没检测到 6 GB 以上的独立显卡：本机大模型会很慢或跑不起来，建议用云端 API"


def ask_mode(tier, vram_mb, ask=input, out=print):
    rec = recommend_mode(tier)
    out(describe_tier(tier, vram_mb))
    out("大模型用哪种方式？（唤醒、语音识别、规则指令都在本机，和这里无关）")
    out("  1. 本机模型：免费、不联网；需要 6 GB 以上显存的 NVIDIA 显卡和 Ollama" + ("  ← 推荐" if rec == "local" else ""))
    out("  2. 云端 API：不占显存，按用量付费；你说的话会发给所选服务商" + ("  ← 推荐" if rec == "api" else ""))
    out("  3. 只用规则：不用任何大模型，只听固定说法的指令（开软件、音量、截图……），不能闲聊")
    default = "1" if rec == "local" else "2"
    while True:
        choice = (ask(f"请选择 [1/2/3]（直接回车 = {default}）：").strip() or default)
        if choice in ("1", "2", "3"):
            return MODES[int(choice) - 1]
        out("请输入 1、2 或 3")


def setup_cloud(ask=input, ask_secret=None, out=print, llm=None, test=True):
    """选服务商、填模型和密钥；陪聊和指令识别都交给它。返回服务名，放弃返回 None。"""
    import getpass
    ask_secret = ask_secret or getpass.getpass
    if llm is None:
        import canaan_llm_provider as llm
    for i, (_, label, _, model) in enumerate(CLOUD_CHOICES, 1):
        out(f"  {i}. {label}" + (f"（默认模型 {model}）" if model else ""))
    while True:
        raw = ask(f"选哪个服务 [1-{len(CLOUD_CHOICES)}]：").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(CLOUD_CHOICES):
            name, label, kind, default_model = CLOUD_CHOICES[int(raw) - 1]
            break
        out("请输入列表里的数字")
    entry = {"kind": kind}
    if name == "custom":
        base = ask("接口地址（例如 https://api.example.com/v1）：").strip()
        if not base.startswith(("http://", "https://")):
            out("[!] 地址要以 http:// 或 https:// 开头，已取消；之后可以用 python tools/llm_provider.py 配置")
            return None
        entry["base_url"] = base
    model = ask(f"模型名{f'（直接回车 = {default_model}）' if default_model else ''}：").strip() or default_model
    if not model:
        out("[!] 没填模型名，已取消；之后可以用 python tools/llm_provider.py 配置")
        return None
    entry["model"] = model
    cfg = llm.load_config()
    cfg.setdefault("providers", {})[name] = entry
    cfg.update({"chat": name, "intent": name, "mode": "api"})
    cfg.setdefault("send_memory", False)
    llm.save_config(cfg)
    key = ask_secret(f"{label} 的 API 密钥（输入时不显示；直接回车 = 稍后用环境变量 CANAAN_{name.upper()}_API_KEY）：").strip()
    if key:
        try:
            llm.SecretStore().set(name, key)
            out("[✓] 密钥已加密保存在本机 data/secrets.db")
        except Exception as e:
            out(f"[!] 本机加密存储不可用（{e}）：请改用环境变量 CANAAN_{name.upper()}_API_KEY")
    if test and (key or os.environ.get(f"CANAAN_{name.upper()}_API_KEY")):
        try:
            reply = llm.complete(llm.provider(name, cfg), [{"role": "user", "content": "用一句话回答：你好"}],
                                 max_tokens=40, timeout=30)
            out(f"[✓] {label} 接通了：{reply.strip()[:40]}")
        except Exception as e:
            out(f"[!] {label} 暂时不通（{e}）；检查密钥和模型名后可以运行 python tools/llm_provider.py --test {name}")
    out("默认不把长期记忆和本机资料发给云端；要发可以运行 python tools/llm_provider.py --send-memory on")
    return name


def save_mode(mode, llm=None):
    if llm is None:
        import canaan_llm_provider as llm
    cfg = llm.load_config()
    cfg["mode"] = mode
    if mode in ("local", "rules"):
        cfg["chat"] = cfg["intent"] = "local"
    llm.save_config(cfg)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="首次安装：下载模型，并选择大模型的运行方式")
    ap.add_argument("--check", action="store_true", help="只检查不下载")
    ap.add_argument("--llm", choices=("ask",) + MODES, default="ask",
                    help="大模型方式：local 本机 / api 云端 / rules 只用规则；默认第一次运行时问")
    args = ap.parse_args(argv)
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    import canaan_config
    import canaan_llm_provider as llm
    tier, vram = canaan_config.ACTIVE_TIER, canaan_config.DETECTED_VRAM_MB
    print(f"显存档位：{tier}（检测到 {vram} MB）")
    mode = args.llm
    if mode == "ask":
        mode = llm.load_config().get("mode")
        if mode not in MODES:
            mode = ask_mode(tier, vram) if (not args.check and sys.stdin and sys.stdin.isatty()) else recommend_mode(tier)
            if mode == "api" and (args.check or not (sys.stdin and sys.stdin.isatty())):
                mode = "rules" if tier == "4gb" else "local"   # 不能交互时不替用户填密钥
    if not args.check:
        if mode == "api":
            if llm.load_config().get("mode") != "api" or args.llm == "api":
                if setup_cloud() is None:
                    mode = "rules"
                    save_mode(mode)
        else:
            save_mode(mode)
    print({"local": "大模型：本机", "api": "大模型：云端 API", "rules": "大模型：不用（只听规则指令）"}[mode])
    a = ensure_files(manifest, args.check)   # 唤醒、端点检测、声纹、记忆检索：都是 CPU 小模型，三种方式都要
    b = True
    if mode == "local":
        if tier == "4gb":
            print("[!] 显存不够跑本机大模型：建议改用云端 API（python canaan.py bootstrap --llm api）")
        else:
            b = ensure_ollama(manifest, tier, args.check)
    print("全部就绪" if a and b else "还有未完成的项目，见上（可重新运行，会续传）")
    print("之后想换方式：python canaan.py bootstrap --llm local / api / rules；改完重启生效")
    return 0 if a and b else 1


if __name__ == "__main__":
    sys.exit(main())
