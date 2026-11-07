# -*- coding: utf-8 -*-
"""
配置外部大模型 API（canaan_llm_provider）。改完要重启团团才生效。

    python tools/llm_provider.py --list
    python tools/llm_provider.py --add deepseek --kind openai --model deepseek-chat          # 常见服务自动补地址
    python tools/llm_provider.py --add myrelay --kind openai --base-url https://x.example/v1 --model gpt-x
    python tools/llm_provider.py --add claude --kind anthropic --model claude-opus-5-5
    python tools/llm_provider.py --set-key deepseek          # 输入时不回显，加密存进 data/secrets.db
    python tools/llm_provider.py --test deepseek             # 发一句“你好”，看能不能通（会产生一点费用）
    python tools/llm_provider.py --use-chat deepseek         # 陪聊用它；--use-chat local 改回本机
    python tools/llm_provider.py --send-memory off           # 默认 off：不把记忆发给云端
    python tools/llm_provider.py --delete-key deepseek
"""
import argparse
import getpass
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import canaan  # noqa: E402,F401  补全 core 搜索路径
import canaan_llm_provider as llm  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="配置团团的外部大模型 API")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--add")
    ap.add_argument("--kind", choices=["openai", "anthropic"], default="openai")
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--set-key")
    ap.add_argument("--delete-key")
    ap.add_argument("--test")
    ap.add_argument("--use-chat")
    ap.add_argument("--use-intent", help="指令识别用哪个服务（原话会发给它）；local 改回本机模型")
    ap.add_argument("--send-memory", choices=["on", "off"])
    a = ap.parse_args(argv)
    cfg = llm.load_config()
    changed = False

    if a.add:
        if not a.model:
            ap.error("--add 需要 --model")
        entry = {"kind": a.kind, "model": a.model}
        if a.base_url:
            entry["base_url"] = a.base_url
        elif a.kind == "openai" and a.add not in llm.KNOWN_BASE_URLS:
            ap.error(f"{a.add} 不是认识的服务，需要 --base-url（认识的：{', '.join(llm.KNOWN_BASE_URLS)}）")
        if a.effort:
            entry["effort"] = a.effort
        cfg.setdefault("providers", {})[a.add] = entry
        changed = True
        print(f"已添加 {a.add}：{entry}")
    if a.use_chat:
        if a.use_chat != "local" and not llm.provider(a.use_chat, cfg):
            ap.error(f"还没添加 {a.use_chat}")
        cfg["chat"] = a.use_chat
        changed = True
        print(f"陪聊改用：{a.use_chat}")
    if a.use_intent:
        if a.use_intent != "local" and not llm.provider(a.use_intent, cfg):
            ap.error(f"还没添加 {a.use_intent}")
        cfg["intent"] = a.use_intent
        changed = True
        print(f"指令识别改用：{a.use_intent}" + ("（你说的每句指令原话都会发给它；出错时退回本机模型）" if a.use_intent != "local" else ""))
    if a.send_memory:
        cfg["send_memory"] = a.send_memory == "on"
        changed = True
        print("会把记忆和本地资料发给云端" if cfg["send_memory"] else "不把记忆和本地资料发给云端")
    if changed:
        llm.save_config(cfg)
    if a.set_key:
        key = getpass.getpass(f"{a.set_key} 的 API 密钥（不回显）：").strip()
        if not key:
            ap.error("密钥为空")
        llm.SecretStore().set(a.set_key, key)
        print(f"已加密保存 {a.set_key} 的密钥")
    if a.delete_key:
        print("已删除" if llm.SecretStore().delete(a.delete_key) else "没有这个密钥")
    if a.test:
        p = llm.provider(a.test, cfg)
        if not p:
            ap.error(f"还没添加 {a.test}")
        try:
            reply = llm.complete(p, [{"role": "user", "content": "用一句话回答：你好"}], max_tokens=60, timeout=30)
            print(f"[OK] {a.test} 回复：{reply.strip()[:80]}")
        except Exception as e:
            print(f"[!] {a.test} 不通：{e}")
            return 1
    if a.list or not any(vars(a)[k] for k in ("add", "set_key", "delete_key", "test", "use_chat", "use_intent", "send_memory")):
        try:
            keys = set(llm.SecretStore().names())
        except Exception:
            keys = set()
        print(f"陪聊：{cfg.get('chat') or 'local'}；指令识别：{cfg.get('intent') or 'local'}；"
              f"发送记忆：{'是' if cfg.get('send_memory') else '否'}")
        for name in (cfg.get("providers") or {}):
            p = llm.provider(name, cfg)
            has_key = name in keys or bool(os.environ.get(f"CANAAN_{name.upper()}_API_KEY"))
            print(f"  {name}: {p['kind']} {p.get('model')} {p.get('base_url', '')} 密钥：{'有' if has_key else '没有'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
