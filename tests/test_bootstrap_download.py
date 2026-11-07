"""首次运行下载模型：断点续传、服务器不支持续传时从头下、大小 / SHA-256 不对就重下、换镜像；
清单里已校验通过的跳过；--check 只检查；压缩包解压后再校验；Ollama 不在时如实报告。全部用替身，不联网。"""
import hashlib
import io
import json
import tarfile

import canaan_bootstrap as bs
import pytest

DATA = b"0123456789" * 1000


class Resp(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def server(fail_first=0, no_range=False, corrupt=False, broken_mirrors=()):
    state = {"calls": [], "fails": fail_first}

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        rng = None if isinstance(req, str) else req.headers.get("Range")
        state["calls"].append((url, rng))
        if not url.startswith(("http://m", "http://bad/", "http://good/")):
            raise OSError("测试替身：不是本测试的地址")
        if url in broken_mirrors:
            raise OSError("镜像坏了")
        if state["fails"]:
            state["fails"] -= 1
            return Resp(DATA[:3000])                         # 下到一半断了（之后 read 返回空）
        body = b"x" * len(DATA) if corrupt else DATA
        if rng and not no_range:
            start = int(rng.split("=")[1].rstrip("-"))
            return Resp(body[start:], status=206)
        return Resp(body, status=200)
    return urlopen, state


def sha(b):
    return hashlib.sha256(b).hexdigest()


def test_resume_after_partial(tmp_path, monkeypatch):
    urlopen, st = server(fail_first=1)
    monkeypatch.setattr(bs.urllib.request, "urlopen", urlopen)
    dest = str(tmp_path / "m" / "a.bin")
    assert bs.download(["http://m1/a"], dest, sha(DATA), len(DATA), progress=lambda *_: None)
    assert open(dest, "rb").read() == DATA
    mine = [c for c in st["calls"] if c[0] == "http://m1/a"]     # 别的测试留下的后台线程也可能调到替身
    assert mine[1][1] == "bytes=3000-"                         # 第二次带 Range 续传


def test_server_without_range_restarts_from_zero(tmp_path, monkeypatch):
    urlopen, _ = server(fail_first=1, no_range=True)
    monkeypatch.setattr(bs.urllib.request, "urlopen", urlopen)
    dest = str(tmp_path / "a.bin")
    assert bs.download(["http://m1/a"], dest, sha(DATA), len(DATA), progress=lambda *_: None)
    assert open(dest, "rb").read() == DATA


def test_bad_hash_fails_and_leaves_no_file(tmp_path, monkeypatch):
    urlopen, _ = server(corrupt=True)
    monkeypatch.setattr(bs.urllib.request, "urlopen", urlopen)
    dest = tmp_path / "a.bin"
    assert not bs.download(["http://m1/a"], str(dest), sha(DATA), len(DATA), retries=2, progress=lambda *_: None)
    assert not dest.exists() and not (tmp_path / "a.bin.part").exists()


def test_oversized_partial_is_discarded_and_mirror_fallback(tmp_path, monkeypatch):
    urlopen, st = server(broken_mirrors=("http://bad/a",))
    monkeypatch.setattr(bs.urllib.request, "urlopen", urlopen)
    dest = tmp_path / "a.bin"
    (tmp_path / "a.bin.part").write_bytes(b"z" * (len(DATA) + 5))
    assert bs.download(["http://bad/a", "http://good/a"], str(dest), sha(DATA), len(DATA), retries=1, progress=lambda *_: None)
    mine = [c for c in st["calls"] if c[0].startswith(("http://bad/", "http://good/"))]
    assert dest.read_bytes() == DATA and mine[-1] == ("http://good/a", None)


def test_ensure_files_skip_check_and_download(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "BASE_DIR", str(tmp_path))
    (tmp_path / "have.bin").write_bytes(DATA)
    manifest = {"files": [{"name": "已有", "path": "have.bin", "size": len(DATA), "sha256": sha(DATA), "mirrors": []},
                          {"name": "缺的", "path": "need.bin", "size": len(DATA), "sha256": sha(DATA), "mirrors": ["http://m/x"]}]}
    out = []
    assert bs.ensure_files(manifest, check_only=True, progress=out.append) is False
    assert out == ["[✓] 已有", "[ ] 缺的（缺失或校验不通过）"]
    urlopen, _ = server()
    monkeypatch.setattr(bs.urllib.request, "urlopen", urlopen)
    assert bs.ensure_files(manifest, progress=lambda *_: None) is True and (tmp_path / "need.bin").read_bytes() == DATA


def _tar_bytes(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize("good", [True, False])
def test_archive_items_verified_after_extract(tmp_path, monkeypatch, good):
    monkeypatch.setattr(bs, "BASE_DIR", str(tmp_path))
    payload = b"model-weights"
    arc = _tar_bytes({"m/w.onnx": payload})
    monkeypatch.setattr(bs.urllib.request, "urlopen", lambda req, timeout=None: Resp(arc))
    manifest = {"files": [{"name": "包", "archive": "p.tar", "mirrors": ["http://m/p"], "extract_to": "models",
                           "verify": {"models/m/w.onnx": sha(payload if good else b"other")},
                           "copy": {"models/m/w.onnx": "models/w_copy.onnx"}}]}
    assert bs.ensure_files(manifest, progress=lambda *_: None) is good
    assert (tmp_path / "models" / "w_copy.onnx").exists() is good


def test_archive_cannot_write_outside_models(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(bs, "BASE_DIR", str(root))
    arc = _tar_bytes({"../../escaped.txt": b"pwn"})
    monkeypatch.setattr(bs.urllib.request, "urlopen", lambda req, timeout=None: Resp(arc))
    manifest = {"files": [{"name": "坏包", "archive": "p.tar", "mirrors": ["http://m/p"], "extract_to": "models",
                           "verify": {"models/w.onnx": "0" * 64}}]}
    assert bs.ensure_files(manifest, progress=lambda *_: None) is False
    assert not (tmp_path / "escaped.txt").exists() and not (root / "escaped.txt").exists()


def test_ollama_missing_and_tiers(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "BASE_DIR", str(tmp_path))
    out = []
    monkeypatch.setattr(bs, "ollama_models", lambda: None)
    assert bs.ensure_ollama({"ollama": []}, "8gb", progress=out.append) is False and "Ollama" in out[0]
    monkeypatch.setattr(bs, "ollama_models", lambda: {"have:1b"})
    ran = []
    monkeypatch.setattr(bs.subprocess, "run", lambda step, cwd=None: ran.append(step) or type("R", (), {"returncode": 0})())
    manifest = {"ollama": [
        {"name": "已有", "tiers": ["8gb"], "steps": [["ollama", "pull", "have:1b"]]},
        {"name": "别的档", "tiers": ["16gb"], "steps": [["ollama", "pull", "big"]]},
        {"name": "缺文件", "tiers": ["8gb"], "requires_file": "x.gguf", "steps": [["ollama", "create", "y"]]},
        {"name": "要建", "tiers": ["8gb"], "steps": [["ollama", "pull", "base"], ["ollama", "cp", "base", "new:1b"]]},
    ]}
    assert bs.ensure_ollama(manifest, "8gb", progress=lambda *_: None) is True
    assert ran == [["ollama", "pull", "base"], ["ollama", "cp", "base", "new:1b"]]
    assert bs.ensure_ollama(manifest, "8gb", check_only=True, progress=lambda *_: None) is False


def test_real_manifest_is_well_formed():
    m = json.load(open(bs.MANIFEST, encoding="utf-8"))
    for item in m["files"]:
        assert item["mirrors"] and ("archive" in item or (len(item["sha256"]) == 64 and item["size"] > 0))


def test_bad_archive_does_not_overwrite_existing_good_file(tmp_path, monkeypatch):
    """坏包必须先校验再合并：不能把已经好的旧模型文件覆盖掉。"""
    monkeypatch.setattr(bs, "BASE_DIR", str(tmp_path))
    good, bad = b"good-old", b"bad-new"
    (tmp_path / "models" / "m").mkdir(parents=True)
    (tmp_path / "models" / "m" / "keep.onnx").write_bytes(good)
    arc = _tar_bytes({"m/keep.onnx": bad, "m/new.onnx": bad})
    monkeypatch.setattr(bs.urllib.request, "urlopen", lambda req, timeout=None: Resp(arc))
    manifest = {"files": [{"name": "坏包", "archive": "p.tar", "mirrors": ["http://m/p"], "extract_to": "models",
                           "verify": {"models/m/keep.onnx": sha(good), "models/m/new.onnx": sha(b"expected")}}]}
    assert bs.ensure_files(manifest, progress=lambda *_: None) is False
    assert (tmp_path / "models" / "m" / "keep.onnx").read_bytes() == good
    assert not (tmp_path / "models" / "m" / "new.onnx").exists()
