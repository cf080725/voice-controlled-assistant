"""Process-local provenance for complete plans; payloads remain ordinary JSON.

The MAC authenticates provenance, not permission or correctness. Consumers must
still recheck local guards, confidence policy and complete source coverage.
"""
import hashlib
import hmac
import json
import math
import secrets

EVIDENCE_KEY = "_plan_evidence"
_PROCESS_KEY = secrets.token_bytes(32)
_DOMAIN = b"canaan-plan-evidence-v1\0"


def _json_value(value, depth=0):
    if depth > 32:
        raise ValueError("Evidence is too deeply nested")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise ValueError("Evidence must contain finite JSON values only")


def _bound(text, normalized_steps, records):
    if type(text) is not str or not text.strip():
        raise ValueError("A complete source is required")
    if type(normalized_steps) is not list or not 2 <= len(normalized_steps) <= 32:
        raise ValueError("A complete non-nested plan is required")
    steps = []
    for step in normalized_steps:
        if type(step) is not dict or type(step.get("action")) is not str:
            raise ValueError("Invalid step")
        action = step["action"]
        if not action or action != action.strip().lower() or action in ("chat", "multi_action"):
            raise ValueError("Steps must have canonical action names")
        steps.append({"action": action, "param": step.get("param", "")})
    if type(records) is not list or len(records) != len(steps):
        raise ValueError("Every step needs one provenance record")
    for index, record in enumerate(records):
        if type(record) is not dict or set(record) - {"index", "source_text", "source", "_model", "_conf", "_pconf"}:
            raise ValueError("Invalid record fields")
        if type(record.get("index")) is not int or record["index"] != index:
            raise ValueError("Record order does not match the plan")
        source = record.get("source_text")
        if type(source) is not str or not source.strip() or source not in text:
            raise ValueError("A record needs exact original source text")
        if record.get("source") not in ("rule", "model"):
            raise ValueError("Unknown provenance")
        if "_model" in record and type(record["_model"]) is not str:
            raise ValueError("Invalid model identity")
        for key in ("_conf", "_pconf"):
            if key == "_pconf" and record.get(key) is None:
                continue  # Parameter logprobs are observational, including None.
            if key in record and (type(record[key]) not in (int, float) or not math.isfinite(record[key]) or not 0 <= record[key] <= 1):
                raise ValueError("Invalid observed probability")
        if record["source"] == "model" and (not record.get("_model") or any(key not in record for key in ("_conf", "_pconf"))):
            raise ValueError("Model steps require actual model and probabilities")
    body = {"v": 1, "text": text, "steps": steps, "records": records}
    _json_value(body)
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def issue_evidence(text, normalized_steps, records):
    """Sign internally checked provenance, without manufacturing confidence."""
    encoded = _bound(text, normalized_steps, records)
    # JSON round trips detach all caller-owned mutable objects.
    copied = json.loads(encoded)["records"]
    return {"v": 1, "records": copied,
            "mac": hmac.new(_PROCESS_KEY, _DOMAIN + encoded, hashlib.sha256).hexdigest()}


def verify_evidence(text, normalized_steps, payload):
    """Return authenticated records or None; untrusted JSON never grants trust."""
    try:
        if type(payload) is not dict or set(payload) != {"v", "records", "mac"} or type(payload["v"]) is not int or payload["v"] != 1:
            return None
        if type(payload["mac"]) is not str or len(payload["mac"]) != 64:
            return None
        encoded = _bound(text, normalized_steps, payload["records"])
        expected = hmac.new(_PROCESS_KEY, _DOMAIN + encoded, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, payload["mac"]):
            return None
        return json.loads(encoded)["records"]
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        return None
