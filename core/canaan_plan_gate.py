"""Recheck authenticated plan provenance against the current local policy.

This authenticates classifier observations, not user permission or task success.
The ordinary whole-request source guard and execution confirmation still apply.
"""
import math


def trusted_plan_confidence(text, decision):
    import canaan_config as cfg
    import canaan_intent as ci
    from canaan_gui_task_contract import preflight_gui_task
    from canaan_plan_evidence import EVIDENCE_KEY, verify_evidence

    steps = ci._guard_multi_action_steps(decision.get("param", ""))
    records = verify_evidence(text, steps, decision.get(EVIDENCE_KEY))
    if records is None:
        return False
    from canaan_intent_planner import planning_units
    units = planning_units(text)
    if len(units) != len(steps) or len(records) != len(steps):
        return False
    if not ci._multi_plan_matches_source(text, steps):
        return False
    for index, (unit, step, record) in enumerate(zip(units, steps, records)):
        if record["index"] != index or record["source_text"] != unit:
            return False
        if step["action"] in {"computer_use", "openmuse_task", "opendots_task"}:
            return False  # These backends do not share UFO's readable-input contract.
        if step["action"] == "ufo_task" and not preflight_gui_task(str(step.get("param", ""))):
            return False
        if record["source"] == "rule":
            observed = ci.fast_match_intent(unit)
            if not observed:
                return False
        else:
            confidence = record.get("_conf")
            if (type(confidence) not in (int, float) or not math.isfinite(confidence)
                    or record.get("_model") in (None, "", "fallback")
                    or record.get("_model") != decision.get("_model")):
                return False
            observed = {**step, **{key: record[key] for key in ("_model", "_conf", "_pconf") if key in record}}
            threshold = ci.confidence_threshold(observed, unit, getattr(cfg, "INTENT_MIN_CONFIDENCE", .6))
            if not threshold <= confidence <= 1:
                return False
        guarded = ci.apply_command_guard(unit, dict(observed), require_confidence=False, _local_step=True)
        if (not guarded or guarded.get("action") in (None, "chat", "multi_action")
                or guarded.get("_pending") or guarded.get("_draft")
                or not ci._plan_step_equal(guarded, step, unit)):
            return False
    return True
