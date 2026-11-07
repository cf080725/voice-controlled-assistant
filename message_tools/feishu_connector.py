"""Optional official Feishu SDK listener; stores messages locally, never dispatches commands."""
import os as _os, sys as _sys; _r = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); _r in _sys.path or _sys.path.append(_r); import canaan  # noqa  目录拆分后补全 core/shells/plugins 搜索路径
import argparse
import json
import os
import sys
import threading
import time
import hashlib
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from canaan_message_center import normalize_message
from canaan_local_lock import acquire_file_lock
from canaan_voice_messages import MAX_BYTES

_write_lock = threading.RLock()


def load_credentials(config=None):
    app_id, secret = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
    if app_id and secret:
        return app_id, secret
    if config is None:
        raise ValueError("请设置 FEISHU_APP_ID / FEISHU_APP_SECRET，或指定自己的 --lark-config")
    apps = json.loads(Path(config).read_text(encoding="utf-8"))["apps"]
    if len(apps) != 1:
        raise ValueError("CLI 配置须只包含一个应用；多个应用请用环境变量明确选择")
    app = apps[0]
    if app.get("brand", "feishu") != "feishu" or not isinstance(app.get("appId"), str):
        raise ValueError("需要本人的飞书应用配置")
    secret = app.get("appSecret")
    if isinstance(secret, dict):
        if secret.get("source") != "keychain" or secret.get("id") != "appsecret:" + app["appId"]:
            raise ValueError("仅支持与当前应用一致的官方 CLI keychain 引用")
        secret = _lark_windows_secret(secret["id"])
    if not isinstance(secret, str) or not secret:
        raise ValueError("飞书应用密钥不可用，请在本机重新配置")
    return app["appId"], secret


def _lark_windows_secret(account):
    # Match official larksuite/cli Windows DPAPI storage; plaintext stays in memory.
    if sys.platform != "win32":
        raise ValueError("此 keychain 引用需要当前 Windows 用户；其他平台请使用环境变量")
    import base64
    import ctypes
    import winreg
    from ctypes import wintypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\LarkCli\keychain\lark-cli") as key:
        value = base64.urlsafe_b64encode(account.encode()).decode().rstrip("=")
        encrypted = base64.b64decode(winreg.QueryValueEx(key, value)[0], validate=True)
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    encoded, entropy = ctypes.create_string_buffer(encrypted), ctypes.create_string_buffer(("lark-cli\0" + account).encode())
    incoming = Blob(len(encrypted), ctypes.cast(encoded, ctypes.POINTER(ctypes.c_ubyte)))
    extra = Blob(len(entropy.raw) - 1, ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt.CryptUnprotectData(ctypes.byref(incoming), None, ctypes.byref(extra), None, None, 1, ctypes.byref(result)):
        raise ValueError("当前用户无法读取飞书 CLI 密钥，请在本机重新配置")
    kernel = ctypes.WinDLL("kernel32")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size).decode("utf-8")
    finally:
        kernel.LocalFree(result.data)


def text_event(payload):
    if not isinstance(payload, dict):
        raise ValueError("飞书事件须为对象")
    if payload.get("header", {}).get("event_type") != "im.message.receive_v1":
        return None
    event = payload.get("event", {})
    message = event.get("message", {})
    if message.get("message_type") != "text":
        return None
    sender = event.get("sender", {})
    if sender.get("sender_type") == "app":
        return None
    content = json.loads(message.get("content", "{}"))
    text = content.get("text")
    if not isinstance(text, str):
        raise ValueError("飞书文本内容无效")
    for mention in message.get("mentions") or []:
        key, name = mention.get("key"), mention.get("name")
        if isinstance(key, str) and key and isinstance(name, str):
            text = text.replace(key, "@" + name)
    return normalize_message({"source": "feishu", "message_id": message.get("message_id"),
                              "timestamp": float(message.get("create_time", 0)) / 1000,
                              "conversation": message.get("chat_id"),
                              "sender": sender.get("sender_id", {}).get("open_id", "未知发言人"),
                              "text": text, "capture": "feishu_sdk"})


def audio_event(payload, directory, download):
    message = payload.get("event", {}).get("message", {})
    if payload.get("header", {}).get("event_type") != "im.message.receive_v1" or message.get("message_type") != "audio":
        return None
    text_payload = json.loads(json.dumps(payload))
    text_message = text_payload["event"]["message"]
    text_message.update(message_type="text", content=json.dumps({"text": "语音消息"}))
    event = text_event(text_payload)
    if event is None:
        return None
    event.pop("text")
    file_key = json.loads(message.get("content", "{}")).get("file_key")
    event["audio_missing"] = True
    if not isinstance(file_key, str) or not file_key or len(file_key) > 500 or download is None:
        return event
    try:
        data = download(event["message_id"], file_key)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
            raise ValueError("飞书音频大小无效")
        folder = Path(directory) / "audio" / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (hashlib.sha256(event["message_id"].encode()).hexdigest() + ".opus")
        temp = target.with_suffix(".tmp")
        with _write_lock:
            with temp.open("wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            temp.replace(target)
        event["audio_path"] = str(target.resolve())
        event.pop("audio_missing")
    except Exception as ex:
        event["audio_error"] = f"飞书音频获取失败：{type(ex).__name__}"
    return event


def append_event(path, payload, download=None):
    event = text_event(payload)
    if event is None:
        event = audio_event(payload, Path(path).parent, download)
    if event is None:
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock, path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
    return True


class ResourceQueue:
    """Persist before acknowledging audio; downloading cannot stall the SDK receiver."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.directory / "feishu_resources.db"), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY,payload TEXT,state TEXT)")
        self.db.commit()

    def submit(self, payload):
        event = audio_event(payload, self.directory, None)
        if event is None:
            return False
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM resources WHERE id=?", (event["message_id"],)).fetchone():
                return True
            if self.db.execute("SELECT count(*) FROM resources WHERE state='queued'").fetchone()[0] >= 100:
                raise ValueError("飞书音频下载队列已满，等待重投递")
            self.db.execute("INSERT INTO resources VALUES(?,?,'queued')", (event["message_id"], json.dumps(payload, ensure_ascii=False)))
        return True

    def process_once(self, download):
        with self.lock:
            row = self.db.execute("SELECT id,payload FROM resources WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            return False
        append_event(self.directory / "feishu_events.jsonl", json.loads(row[1]), download)
        with self.lock, self.db:
            self.db.execute("UPDATE resources SET state='done' WHERE id=?", (row[0],))
        return True


def main():
    parser = argparse.ArgumentParser(description="用自己的飞书应用接收已获授权的文字和语音消息")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--lark-config", type=Path, help="本人官方飞书 CLI 的 config.json；不复制其中的密钥")
    args = parser.parse_args()
    try:
        app_id, secret = load_credentials(args.lark_config)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    try:
        import lark_oapi as lark
    except ImportError:
        parser.error("请先安装 requirements-feishu.txt 中的官方 SDK")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        lease = acquire_file_lock(args.data_dir / "feishu_listener.lock")
    except RuntimeError as ex:
        parser.error(str(ex))
    api = lark.Client.builder().app_id(app_id).app_secret(secret).timeout(30).build()
    resources = ResourceQueue(args.data_dir)
    def download(message_id, file_key):
        request = lark.api.im.v1.GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type("file").build()
        response = api.im.v1.message_resource.get(request)
        if not response.success() or response.file is None:
            raise ValueError("当前应用无法读取这条音频资源，请检查其已有权限")
        return response.file.read(MAX_BYTES + 1)
    last_event = 0
    def status(state):
        path = args.data_dir / "feishu_listener_status.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"state": state, "pid": os.getpid(), "updated_at": time.time(), "last_event_at": last_event}), encoding="utf-8")
        temp.replace(path)
    def receive(data):
        nonlocal last_event
        payload = json.loads(lark.JSON.marshal(data))
        if resources.submit(payload) or append_event(args.data_dir / "feishu_events.jsonl", payload):
            last_event = time.time()
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(receive).build()
    client = lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.ERROR)
    stopped = threading.Event()
    def observe():
        while not stopped.is_set():
            # shortcut: SDK 1.7 connection property; recheck when upgrading the SDK.
            connection = getattr(client, "_conn", None)
            status("connected" if connection is not None and not getattr(connection, "closed", True) else "connecting")
            stopped.wait(5)
    watcher = threading.Thread(target=observe, daemon=True)
    watcher.start()
    def fetch_resources():
        while not stopped.is_set():
            try:
                if not resources.process_once(download):
                    stopped.wait(.5)
            except Exception as ex:
                print(f"飞书音频队列写入失败：{type(ex).__name__}", flush=True)
                stopped.wait(5)
    fetcher = threading.Thread(target=fetch_resources, daemon=True)
    fetcher.start()
    try:
        client.start()
    finally:
        stopped.set()
        watcher.join(timeout=6)
        fetcher.join(timeout=1)
        if not fetcher.is_alive():
            resources.db.close()
        status("stopped")
        lease.close()


if __name__ == "__main__":
    main()
