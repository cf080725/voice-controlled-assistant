# -*- coding: utf-8 -*-
"""隐私审计并发写入：多线程同时写，不能丢行。"""
import threading

import canaan_privacy_shield as ps


def test_concurrent_privacy_audit_writes_are_not_lost(tmp_path, monkeypatch):
    audit = tmp_path / "privacy_audit.jsonl"
    monkeypatch.setattr(ps, "AUDIT_LOG_FILE", str(audit))

    def worker(worker_id):
        for step in range(20):
            ps.PrivacyShield.log_audit(
                "computer_use_executed",
                {"instruction_length": worker_id, "steps_count": step},
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 160