"""Serializable provenance cannot be asserted by public/model JSON."""
from copy import deepcopy
import json

import pytest

from canaan_plan_evidence import issue_evidence, verify_evidence


def sample():
    text = "打开计算器，然后搜火星"
    steps = [{"action": "open_app", "param": "calc"}, {"action": "web_search", "param": "火星"}]
    records = [{"index": 0, "source_text": "打开计算器", "source": "rule"},
               {"index": 1, "source_text": "搜火星", "source": "model", "_model": "selected:3b", "_conf": .4, "_pconf": .7}]
    return text, steps, records


def test_evidence_survives_json_without_inventing_rule_probability():
    text, steps, records = sample()
    payload = issue_evidence(text, steps, records)
    restored = json.loads(json.dumps(payload, ensure_ascii=False))
    assert verify_evidence(text, steps, restored) == records
    assert "_conf" not in restored["records"][0]
    result = verify_evidence(text, steps, restored)
    result[0]["source_text"] = "changed"
    assert verify_evidence(text, steps, restored) == records


@pytest.mark.parametrize("empty_param", [False, True])
def test_observed_none_parameter_probability_survives_roundtrip(empty_param):
    text, steps, records = sample()
    if empty_param:
        text = "打开计算器，然后截个图"
        steps[1] = {"action": "screenshot", "param": ""}
        records[1]["source_text"] = "截个图"
    records[1]["_pconf"] = None
    payload = json.loads(json.dumps(issue_evidence(text, steps, records), ensure_ascii=False))
    assert "_pconf" in payload["records"][1] and payload["records"][1]["_pconf"] is None
    assert verify_evidence(text, steps, payload) == records


def test_missing_parameter_probability_key_is_incomplete_evidence():
    text, steps, records = sample()
    del records[1]["_pconf"]
    with pytest.raises(ValueError): issue_evidence(text, steps, records)


@pytest.mark.parametrize("mutation", ["text", "order", "target", "source", "probability", "origin", "model", "mac", "checked"])
def test_any_dispatch_or_provenance_tampering_is_untrusted(mutation):
    text, steps, records = sample()
    payload = issue_evidence(text, steps, records)
    if mutation == "text": text += "，再截图"
    elif mutation == "order": steps.reverse()
    elif mutation == "target": steps[0]["param"] = "notepad"
    elif mutation == "source": payload["records"][1]["source_text"] = "火星"
    elif mutation == "probability": payload["records"][1]["_conf"] = .99
    elif mutation == "origin": payload["records"][1]["source"] = "rule"
    elif mutation == "model": payload["records"][1]["_model"] = "other:7b"
    elif mutation == "mac": payload["mac"] = "0" * 64
    else: payload["checked"] = True
    assert verify_evidence(text, steps, payload) is None


@pytest.mark.parametrize("value", [None, {}, {"checked": True}, {"v": True, "records": [], "mac": "0" * 64}])
def test_public_assertions_do_not_authenticate(value):
    text, steps, _ = sample()
    assert verify_evidence(text, steps, value) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, -.1, 1.1])
def test_invalid_observed_probabilities_cannot_be_signed(value):
    text, steps, records = sample()
    records[1]["_conf"] = value
    with pytest.raises(ValueError): issue_evidence(text, steps, records)


def test_caller_mutation_cannot_change_issued_evidence():
    text, steps, records = sample()
    expected = deepcopy(records)
    payload = issue_evidence(text, steps, records)
    records[0]["source_text"] = "changed"
    assert verify_evidence(text, steps, payload) == expected


def test_a_different_process_key_invalidates_saved_evidence(monkeypatch):
    import canaan_plan_evidence as evidence
    text, steps, records = sample()
    payload = issue_evidence(text, steps, records)
    monkeypatch.setattr(evidence, "_PROCESS_KEY", b"another-process-key")
    assert verify_evidence(text, steps, payload) is None
