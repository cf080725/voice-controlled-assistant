# -*- coding: utf-8 -*-
"""
接外部大模型 API（10-24 起）：本机 Ollama 之外，可以用
  - OpenAI 兼容接口（kind="openai"）：DeepSeek、通义千问（DashScope 兼容模式）、Kimi、智谱、硅基流动、自建中转……
  - Claude（kind="anthropic"）：官方 anthropic SDK（pip install anthropic），没装时报错并退回本机模型。

配置：data/llm_providers.json（不含密钥，可以手改）：
    {"chat": "deepseek",            # 陪聊用哪个；"local" 或不写 = 本机 Ollama
     "providers": {"deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
                   "claude":   {"kind": "anthropic", "model": "claude-opus-5-5"}},
     "send_memory": false}          # 默认不把记忆、本地资料发给云端，只发人设和这几轮对话
密钥：加密存在 data/secrets.db（canaan_crypto），用 tools/llm_provider.py --set-key <名字> 写入；
      也可以用环境变量 CANAAN_<名字大写>_API_KEY（例如 CANAAN_DEEPSEEK_API_KEY）。
云端出错且还没出字时，陪聊自动退回本机模型（canaan_companion 里原有的主备切换）。
"""
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "data", "llm_providers.json")
SECRETS_PATH = os.path.join(BASE_DIR, "data", "secrets.db")
PREFIX = "provider:"        # CompanionChat 里用 "provider:名字" 当模型名，走这里

# 常见服务的默认地址（用户只写 kind/model 时补上）
KNOWN_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openai": "https://api.openai.com/v1",
}


class ProviderError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# 密钥（加密存储）
# ----------------------------------------------------------------------------

class SecretStore:
    """API 密钥：名字明文（只是服务名），值用 canaan_crypto 加密；附加数据绑定名字，挪到别的名字下解不开。"""

    def __init__(self, path=SECRETS_PATH, cipher=None):
        import canaan_crypto
        self.cipher = cipher or canaan_crypto.default_cipher()
        if self.cipher is None:
            raise ProviderError("本机加密不可用（不是 Windows 或 CANAAN_DB_ENCRYPTION=0），不保存密钥；请改用环境变量")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS secrets(name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL)")
        self.db.commit()

    def set(self, name, value):
        import time
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO secrets VALUES(?,?,?)",
                            (name, self.cipher.encrypt(value, "secret:" + name), time.time()))
            self.db.commit()

    def get(self, name):
        with self._lock:
            row = self.db.execute("SELECT value FROM secrets WHERE name=?", (name,)).fetchone()
        return self.cipher.decrypt(row[0], "secret:" + name) if row else None

    def delete(self, name):
        with self._lock:
            n = self.db.execute("DELETE FROM secrets WHERE name=?", (name,)).rowcount
            self.db.commit()
            self.db.execute("VACUUM")
        return bool(n)

    def names(self):
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT name FROM secrets ORDER BY name")]


def api_key(name, secrets=None):
    env = os.environ.get(f"CANAAN_{name.upper()}_API_KEY")
    if env:
        return env
    try:
        store = secrets or SecretStore()
        return store.get(name)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

def load_config(path=CONFIG_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg, path=CONFIG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def provider(name, cfg=None):
    """名字 → 完整的服务配置 dict（补上默认地址）；没配过返回 None。"""
    cfg = load_config() if cfg is None else cfg
    p = dict((cfg.get("providers") or {}).get(name) or {})
    if not p:
        return None
    p.setdefault("name", name)
    p.setdefault("kind", "openai")
    if p["kind"] == "openai" and not p.get("base_url"):
        p["base_url"] = KNOWN_BASE_URLS.get(name, "")
    return p


def role_provider(role, cfg=None):
    """某个用途（"chat"）配的服务名；本机 / 没配返回 None。"""
    cfg = load_config() if cfg is None else cfg
    name = cfg.get(role)
    if not name or name == "local":
        return None
    return name if provider(name, cfg) else None


def send_memory(cfg=None):
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("send_memory", False))


# ----------------------------------------------------------------------------
# 调用
# ----------------------------------------------------------------------------

def stream_chat(p, messages, *, max_tokens=400, temperature=None, stop=None, timeout=30,
                cancellation=None, key=None, opener=urllib.request.urlopen, anthropic_client=None):
    """逐块产出文字。messages 是 Ollama / OpenAI 格式 [{"role", "content"}]。"""
    key = key if key is not None else api_key(p["name"])
    if not key:
        raise ProviderError(f"{p['name']} 没有 API 密钥（tools/llm_provider.py --set-key {p['name']}）")
    if p["kind"] == "openai":
        yield from _openai_stream(p, messages, key, max_tokens, temperature, stop, timeout, cancellation, opener)
    elif p["kind"] == "anthropic":
        yield from _anthropic_stream(p, messages, key, max_tokens, stop, timeout, cancellation, anthropic_client)
    else:
        raise ProviderError(f"不认识的接口类型：{p['kind']}")


def _openai_stream(p, messages, key, max_tokens, temperature, stop, timeout, cancellation, opener):
    body = {"model": p["model"], "messages": messages, "stream": True, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    if stop:
        body["stop"] = list(stop)[:4]          # OpenAI 兼容接口大多最多 4 个
    req = urllib.request.Request(p["base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                                          "Accept": "text/event-stream"})
    try:
        resp = opener(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise ProviderError(f"{p['name']} 返回 HTTP {e.code}：{detail}") from e
    if cancellation is not None and not cancellation.register(resp):
        return
    try:
        with resp:
            for raw in resp:
                if cancellation is not None and cancellation.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise ProviderError(f"{p['name']}：{chunk['error']}")
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece
    finally:
        if cancellation is not None:
            cancellation.release(resp)


def _claude_messages(messages):
    """OpenAI 格式 → Claude：system 单独拿出来；连续同角色合并；开头必须是 user；
    结尾的 assistant（Ollama 式开场语预填）去掉——Claude 新模型不支持预填（400）。"""
    system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
    out = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant") or not m.get("content"):
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + m["content"]
        else:
            out.append({"role": role, "content": m["content"]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    while out and out[-1]["role"] == "assistant":
        out.pop()
    return system, out


def _anthropic_stream(p, messages, key, max_tokens, stop, timeout, cancellation, client=None):
    if client is None:
        try:
            import anthropic
        except ImportError as e:
            raise ProviderError("没装 anthropic SDK：python -m pip install anthropic") from e
        client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1,
                                     **({"base_url": p["base_url"]} if p.get("base_url") else {}))
    system, msgs = _claude_messages(messages)
    if not msgs:
        raise ProviderError("没有可发送的对话")
    kwargs = {"model": p.get("model") or "claude-opus-5-5", "max_tokens": max_tokens, "messages": msgs,
              # 语音陪聊要的是快：effort 默认 low（Claude Opus 5.5 的默认是 medium），可在配置里改
              "output_config": {"effort": p.get("effort", "low")}}
    if system:
        kwargs["system"] = system
    stops = [s for s in stop or [] if s.strip()]      # Claude 不收纯空白的停止串（如 "\n\n"）
    if stops:
        kwargs["stop_sequences"] = stops
    try:
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                if cancellation is not None and cancellation.is_set():
                    return
                yield text
            final = stream.get_final_message()
            if getattr(final, "stop_reason", None) == "refusal":
                raise ProviderError("Claude 拒绝回答这一句")
    except ProviderError:
        raise
    except Exception as e:     # SDK 的连接、限流、状态码错误都在这里转成统一的错误，交给上层退回本机模型
        raise ProviderError(f"{p['name']}：{type(e).__name__}：{str(e)[:200]}") from e


def complete(p, messages, **kwargs):
    """不流式的一次性回答（测试连通、后台小任务用）。"""
    return "".join(stream_chat(p, messages, **kwargs))


# ----------------------------------------------------------------------------
# 意图识别走云端（10-25 起，data/llm_providers.json 里 "intent" 配了才用）
# ----------------------------------------------------------------------------

INTENT_TIMEOUT = 6.0
_INTENT_RULES = ("只输出一个 JSON 对象，不要任何别的文字：{\"a\": 动作名, \"p\": 参数字符串}。"
                 "拿不准、只是提到、问做法、转述别人的话、闲聊，一律 {\"a\": \"chat\", \"p\": \"\"}。")


def parse_intent_json(raw):
    """从模型回复里取出 {"a","p"}；取不出返回 None。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("a"), str):
        return None
    p = obj.get("p", "")
    if isinstance(p, (dict, list)):
        p = json.dumps(p, ensure_ascii=False)
    return {"a": obj["a"], "p": "" if p is None else str(p)}


def cloud_intent(user_text, system_prompt, actions, cfg=None, chat=None):
    """配了 "intent" 时用云端模型判断意图，返回 {"a","p"}；没配、出错、超时、动作不认识都返回 None（交回本机模型）。
    原话会发给云端（主人 10-25 选了这一条）；结果在调用方照样过 apply_command_guard 和执行前确认。"""
    cfg = load_config() if cfg is None else cfg
    name = role_provider("intent", cfg)
    if not name:
        return None
    p = provider(name, cfg)
    messages = [{"role": "system", "content": system_prompt + "\n" + _INTENT_RULES},
                {"role": "user", "content": user_text}]
    try:
        raw = (chat or complete)(p, messages, max_tokens=120, temperature=0, timeout=INTENT_TIMEOUT)
    except Exception as e:
        print(f"[!] 云端意图识别不可用（{type(e).__name__}），改用本机模型")
        return None
    obj = parse_intent_json(raw)
    if obj is None or obj["a"] not in actions:
        return None
    return obj
