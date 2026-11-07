"""授权签名密钥不写在源码里：从环境变量或 data 文件读；没配置时不崩、不签发。"""
import re
from pathlib import Path

import pytest

import canaan_licensing as L


def test_no_hardcoded_secret_in_source():
    src = Path(L.__file__).read_text(encoding="utf-8")
    assert not re.search(r"""SECRET\s*=\s*b?['"][A-Za-z0-9+/=_\-]{16,}['"]""", src)


def test_env_secret_used(monkeypatch):
    monkeypatch.setenv("CANAAN_LICENSE_SECRET", "unit-test-secret-not-real-0123456789")
    assert L._load_secret() == b"unit-test-secret-not-real-0123456789"


def test_missing_secret_is_open_and_cannot_sign(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "COMMERCIAL_HMAC_SECRET", None)
    ok, text, meta = L.LicenseManager(license_path=str(tmp_path / "x.key")).verify()
    assert ok and meta["tier"] == "OPEN" and not (tmp_path / "x.key").exists()
    with pytest.raises(RuntimeError):
        L.generate_license_key("machine")
