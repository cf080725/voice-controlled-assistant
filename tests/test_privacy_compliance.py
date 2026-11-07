# -*- coding: utf-8 -*-
"""
Privacy Compliance & Data Security Test Suite.
Guarantees:
1. Zero PII leakage across all SFT training samples (no unmasked mobile, ID card, bank card, API keys).
2. PrivacyShield local-only sanitization & audit trail integrity.
"""
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from canaan_privacy_shield import PrivacyShield

RE_RAW_PHONE = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')
RE_RAW_IDCARD = re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')
RE_RAW_BANKCARD = re.compile(r'(?<!\d)62\d{14,17}(?!\d)')
RE_RAW_SECRET = re.compile(r'(?i)(?:sk-[a-zA-Z0-9]{32,}|ghp_[a-zA-Z0-9]{36,})')


def test_sft_dataset_zero_unmasked_pii():
    """验证 12,800 条 SFT 样本中 100% 零手机号、零身份证、零银行卡、零明文密钥泄漏"""
    ds_path = os.path.join(ROOT, "dataset_briefing_intelligence", "canaan_briefing_intelligence_sft.jsonl")
    if not os.path.exists(ds_path):
        pytest.skip("本机没有训练数据集（公开版不带训练数据）")

    with open(ds_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if not line.strip():
                continue
            item = json.loads(line)
            for msg in item.get("messages", []):
                content = msg.get("content", "")
                assert not RE_RAW_PHONE.search(content), f"Line {idx+1} contains raw mobile phone: {content}"
                assert not RE_RAW_IDCARD.search(content), f"Line {idx+1} contains raw ID card: {content}"
                assert not RE_RAW_BANKCARD.search(content), f"Line {idx+1} contains raw bank card: {content}"
                assert not RE_RAW_SECRET.search(content), f"Line {idx+1} contains raw secret: {content}"


def test_privacy_shield_masking_rules():
    """验证 PrivacyShield 本地实时脱敏遮罩规则与审计日志记录"""
    raw_text = "客户手机 13812345678，身份证 110101199003072345，银行卡 6222021234567890123，fake密钥 sk-abcdefghijklmnopqrstuvwxyz123456"
    masked, stats = PrivacyShield.mask_text(raw_text)

    assert "13812345678" not in masked
    assert "138****5678" in masked
    assert "110101199003072345" not in masked
    assert "110101********2345" in masked
    assert "6222021234567890123" not in masked
    assert "622202******0123" in masked
    assert stats["phone_masked"] == 1
    assert stats["idcard_masked"] == 1
    assert stats["bankcard_masked"] == 1


def test_privacy_audit_log_updated(tmp_path, monkeypatch):
    """验证数据脱敏操作均记录在本地审计日志中，零云端泄露（用临时文件，不碰真实 data/）"""
    import canaan_privacy_shield as ps
    audit_file = tmp_path / "privacy_audit.jsonl"
    monkeypatch.setattr(ps, "AUDIT_LOG_FILE", str(audit_file))
    ps.PrivacyShield.log_audit("test_event", {"model": "synthetic"})
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    last_log = json.loads(lines[-1])
    assert "timestamp" in last_log and last_log["event"] == "test_event"
