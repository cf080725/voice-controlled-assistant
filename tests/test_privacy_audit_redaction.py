# -*- coding: utf-8 -*-
"""隐私审计只记允许字段：不落原话/窗口标题；历史原文能被收敛；未知事件不保存 details。"""
import json
import time

import canaan_privacy_shield as ps
import canaan_retention as r


def _read(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_computer_use_instruction_is_not_stored(tmp_path, monkeypatch):
    p = tmp_path / "privacy_audit.jsonl"
    monkeypatch.setattr(ps, "AUDIT_LOG_FILE", str(p))
    secret = "把桌面上的合成测试文件删掉，然后打开记事本输入 hello"
    ps.PrivacyShield.log_audit("computer_use_executed", {
        "instruction": secret, "plan_source": "llm", "steps_count": 2, "latency_ms": 12.5,
    })
    rec = _read(p)[0]
    assert "instruction" not in rec["details"]
    assert rec["details"]["instruction_length"] == len(secret)
    assert secret not in p.read_text(encoding="utf-8")


def test_window_title_is_not_stored(tmp_path, monkeypatch):
    p = tmp_path / "privacy_audit.jsonl"
    monkeypatch.setattr(ps, "AUDIT_LOG_FILE", str(p))
    ps.PrivacyShield.log_audit("computer_use_blocked_sensitive_window", {"window": "招商银行 账户 1234"})
    rec = _read(p)[0]
    assert "window" not in rec["details"]
    assert rec["details"]["window_title_length"] == 12
    assert "招商银行" not in p.read_text(encoding="utf-8")


def test_unknown_event_drops_details(tmp_path, monkeypatch):
    p = tmp_path / "privacy_audit.jsonl"
    monkeypatch.setattr(ps, "AUDIT_LOG_FILE", str(p))
    ps.PrivacyShield.log_audit("future_event", {"secret": "不该落盘"})
    assert _read(p)[0]["details"] == {}


def test_scrub_privacy_audit_removes_historical_instruction(tmp_path):
    p = tmp_path / "privacy_audit.jsonl"
    old = {"timestamp": time.time(), "datetime": "2026-10-20 10:00:00", "event": "computer_use_executed",
           "details": {"instruction": "删掉合成文件", "plan_source": "llm", "steps_count": 1}}
    p.write_text(json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
    assert r.scrub_privacy_audit(str(p)) == 1
    rec = _read(p)[0]
    assert "instruction" not in rec["details"]
    assert "删掉合成文件" not in p.read_text(encoding="utf-8")
    assert rec["details"]["plan_source"] == "llm"


def test_purge_privacy_audit_boundary(tmp_path):
    p = tmp_path / "privacy_audit.jsonl"
    now = time.time()
    p.write_text(json.dumps({"timestamp": now - 31 * 86400, "event": "old"}) + "\n" +
                 json.dumps({"timestamp": now - 10 * 86400, "event": "new"}) + "\n", encoding="utf-8")
    assert r.purge_privacy_audit(str(p), days=30, now=now) == (1, 1)
    assert [x["event"] for x in _read(p)] == ["new"]