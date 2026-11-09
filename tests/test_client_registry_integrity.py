import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from canaan_api_clients import ClientRegistry


def test_pair_cannot_restore_client_revoked_by_another_registry(tmp_path, monkeypatch):
    first = ClientRegistry(tmp_path, None)
    second = ClientRegistry(tmp_path, None)
    record, token = first.pair("旧客户端")
    pair_read = threading.Event()
    continue_pair = threading.Event()
    revoke_started = threading.Event()
    original_load = first._load

    def pause_pair_read(**kwargs):
        clients = original_load(**kwargs)
        pair_read.set()
        assert continue_pair.wait(5)
        return clients

    def revoke():
        revoke_started.set()
        return second.revoke(record["id"])

    monkeypatch.setattr(first, "_load", pause_pair_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        paired = pool.submit(first.pair, "新客户端")
        try:
            assert pair_read.wait(2)
            revoked = pool.submit(revoke)
            assert revoke_started.wait(2)
            with pytest.raises(TimeoutError):
                revoked.result(timeout=0.2)
        finally:
            continue_pair.set()
        new_record, new_token = paired.result(timeout=5)
        assert revoked.result(timeout=5)["revoked_at"] is not None

    assert second.authenticate(token) is None
    assert second.authenticate(new_token).id == new_record["id"]
    assert len(second.list()) == 2


def test_registry_detects_replacement_with_same_mtime(tmp_path):
    first = ClientRegistry(tmp_path, None)
    second = ClientRegistry(tmp_path, None)
    record, token = first.pair("旧客户端")
    assert second.authenticate(token) is not None
    stamp = first.path.stat()
    first.revoke(record["id"])
    os.utime(first.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    assert second.authenticate(token) is None


def test_returned_scope_changes_do_not_modify_registry_permissions(tmp_path):
    registry = ClientRegistry(tmp_path, None)
    _, token = registry.pair("只读客户端", ["read"], {"knowledge": {"documents": ["/docs/allowed"]}})
    record = registry.list()[0]
    record["resources"]["knowledge"]["documents"].append("/docs/private")
    assert not registry.authenticate(token).allows_document("/docs/private/one.txt")

    principal = registry.authenticate(token)
    principal.resources["knowledge"]["documents"].append("/docs/private")
    assert not registry.authenticate(token).allows_document("/docs/private/one.txt")
