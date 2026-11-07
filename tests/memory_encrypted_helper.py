"""加密开着时重跑原有记忆库测试用：MemoryStore 没指定 cipher 时一律用测试密钥。"""
import os

import canaan_crypto
import canaan_memory as cm
import pytest

_TEST_CIPHER = canaan_crypto.Cipher(os.urandom(32))


@pytest.fixture(autouse=True)
def _force_encryption(monkeypatch):
    original = cm.MemoryStore.__init__

    def init(self, path=cm.DB_PATH, embedder=None, migrate=True, cipher=cm._AUTO):
        original(self, path, embedder, migrate, _TEST_CIPHER if cipher is cm._AUTO else cipher)

    monkeypatch.setattr(cm.MemoryStore, "__init__", init)