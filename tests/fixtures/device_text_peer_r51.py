"""Local test peer only: stdin/stdout JSON, fake turn/execution, no listener.

The owner-confirm/restart/stats commands are test controls, not a device API.
"""
from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
from types import SimpleNamespace
import urllib.request
import webbrowser
import os

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "plugins")]
blocked_calls = []


def forbidden(*args, **kwargs):
    blocked_calls.append({"port": "external_port", "stack": [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_stack(limit=7)[:-1]]})
    raise AssertionError("No external port is permitted in the device contract peer")


socket.socket.connect = socket.socket.connect_ex = socket.socket.bind = forbidden
urllib.request.urlopen = webbrowser.open = forbidden
subprocess.Popen._execute_child = forbidden
if hasattr(os, "startfile"):
    os.startfile = forbidden

import canaan_tiers
# Select a synthetic test profile before registry's configuration import. This
# avoids even attempting the configuration's automatic nvidia-smi probe.
canaan_tiers.detect_vram_mb = lambda: 8192

from canaan_api_clients import ClientRegistry
from canaan_device_binding import DeviceBindingStore, DeviceTextRequestBinder
from canaan_device_requests import DeviceExecutionResult, DeviceTextRequestBroker, PreparedDeviceTurn
from canaan_user_context import TurnContext


class Context:
    def __init__(self):
        self.current = TurnContext("owner", 1, 2, "fake-session", SimpleNamespace(incognito=False),
                                   None, threading.Event(), 3)

    def sync(self):
        return self.current

    def valid(self, bound):
        return bound is not None and not bound.cancelled.is_set() and bound.token == self.current.token


class FakePipeline:
    def __init__(self):
        self.prepared = []
        self.executed = []
        self.questions = []
        self.answers = []

    def prepare(self, bound):
        self.prepared.append(bound.text)
        if self.questions:
            return PreparedDeviceTurn({"text": bound.text, "test_adapter": True},
                                      {"original": bound.text, "answers": []}, question=self.questions[0])
        return PreparedDeviceTurn({"text": bound.text, "test_adapter": True}, bound.text)

    def follow_up(self, prepared, bound, text):
        self.answers.append({"request_id": bound.request_id, "original": bound.text, "answer": text})
        payload = {"original": prepared.payload["original"], "answers": [*prepared.payload["answers"], text]}
        question = self.questions[len(payload["answers"])] if len(payload["answers"]) < len(self.questions) else ""
        return PreparedDeviceTurn({"text": bound.text, "answers": payload["answers"]}, payload, question=question)

    def execute(self, prepared, bound):
        self.executed.append(prepared.payload)
        return DeviceExecutionResult("succeeded", "模拟电脑任务完成。")


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="canaan-device-contract-") as directory:
        path = Path(directory)
        owner_token = "local-fixture-" + "owner-only"
        clients = ClientRegistry(path / "clients", owner_token)
        client, device_token = clients.pair("vivo WATCH GT / Android / test")
        store = DeviceBindingStore(path / "grants.db")
        context = Context()
        binder = DeviceTextRequestBinder(clients, context, store, execution_host_id="fixture-desktop", max_handles=1)
        binder.register_local_binding(owner_token, client["id"])
        pipeline = FakePipeline()
        broker = DeviceTextRequestBroker(path / "requests.db", binder, pipeline)
        try:
            for line in sys.stdin:
                message = json.loads(line)
                try:
                    op = message["op"]
                    if op == "submit":
                        result = broker.submit(device_token, message["body"])
                    elif op == "get":
                        result = broker.get(device_token, message["task_id"])
                    elif op == "cancel":
                        result = broker.cancel(device_token, message["task_id"])
                    elif op == "follow_up":
                        result = broker.follow_up(device_token, message["task_id"], message["body"])
                    elif op == "questions":
                        assert not pipeline.prepared and all(type(q) is str and q.strip() for q in message["questions"])
                        pipeline.questions = message["questions"]
                        result = {"configured": True}
                    elif op == "owner_confirm":
                        view = broker.get_local(owner_token, message["task_id"])
                        result = broker.confirm_local(owner_token, message["task_id"],
                                                      expected_version=view["confirmation_version"])
                    elif op == "restart":
                        broker.close()
                        broker = DeviceTextRequestBroker(path / "requests.db", binder, pipeline)
                        result = {"reopened": True}
                    elif op == "revoke":
                        clients.revoke(client["id"])
                        result = {"revoked": True}
                    elif op == "change_session":
                        context.current = replace(context.current, session_id="new-fake-session")
                        result = {"changed": True}
                    elif op == "stats":
                        result = {"prepared": pipeline.prepared, "executed": pipeline.executed, "answers": pipeline.answers,
                                  "blocked_calls": blocked_calls}
                    else:
                        raise ValueError("Unknown test control")
                    response = {"id": message["id"], "ok": True, "result": result}
                except Exception as ex:
                    response = {"id": message["id"], "ok": False,
                                "error": getattr(ex, "code", type(ex).__name__)}
                print(json.dumps(response, ensure_ascii=False), flush=True)
        finally:
            broker.close()
            store.close()


if __name__ == "__main__":
    main()
