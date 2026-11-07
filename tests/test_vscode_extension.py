"""VS Code 插件的纯函数测试（用 Node 跑 integrations/vscode-canaan/test），并检查清单里没写死本机以外的地址。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "integrations" / "vscode-canaan"


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 Node")
def test_extension_lib_with_node():
    out = subprocess.run(["node", "--test", "test/lib.test.js"], cwd=EXT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


def test_manifest_defaults_local_only():
    manifest = json.loads((EXT / "package.json").read_text(encoding="utf-8"))
    assert manifest["contributes"]["configuration"]["properties"]["canaan.apiUrl"]["default"].startswith("http://127.0.0.1")
    assert manifest["main"] == "./extension.js" and "dependencies" not in manifest
