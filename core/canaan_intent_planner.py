"""Bounded completion of partial Compact decisions using trusted source units.

The model's existing single-action schema is unchanged. No action is scheduled
here, and incomplete/late plans never expose an executable prefix.
"""
import json
import math
import re
import time

import canaan_intent as ci
from canaan_gui_task_contract import preflight_gui_task
from canaan_plan_evidence import EVIDENCE_KEY, issue_evidence

_JOIN = re.compile(r"^[\s，,。；;！!]*(?:(?:然后|再|接着|之后|最后|并且|以及|顺便|首先|第一步|第二步|先|请|麻烦|帮我|替我|给我)[\s，,。；;！!]*)*$")
_UI_ACTIONS = {"ufo_task", "computer_use", "openmuse_task", "opendots_task"}


def planning_units(text):
    """Return exact original source units, or [] when ownership is incomplete.

    This intentionally declines sources the existing semantic helpers cannot
    map back precisely, instead of inventing or dropping residual instructions.
    """
    if not isinstance(text, str) or not text.strip() or ci.mention_reason(text):
        return []
    reference = ci.fast_match_intent(text)
    owned = getattr(ci, "_plan_whole_owned_source", None)
    if callable(owned) and owned(text, reference=reference):
        return []
    if not callable(owned) and reference and reference.get("action") in ("send_chatapp", "set_reminder", "web_search", "diagnose_error", "ufo_task", "computer_use", "openmuse_task", "opendots_task"):
        return []
    clean = ci._space_clauses(ci._strip_prefix(text))
    if ci._correction_decision(clean) or ci._corrected_open_text(clean) or ci._literal_media_request(clean):
        return []
    units = ci._plan_source_units(text)
    if not 2 <= len(units) <= 32 or any(not ci._plan_explicit_request(source, known) for source, known in units):
        return []
    result, cursor = [], 0
    for source, known in units:
        if not isinstance(source, str) or not source.strip() or known and known.get("action") == "multi_action":
            return []
        extra_task = getattr(ci, "_plan_app_has_additional_task", None)
        if callable(extra_task) and extra_task(source):
            return []
        if known:
            local = ci.apply_command_guard(source, dict(known), require_confidence=False, _local_step=True)
            if not local or local.get("action") in (None, "chat", "multi_action") or local.get("_pending") or local.get("_draft"):
                return []
        start = text.find(source, cursor)
        if start < 0 or not _JOIN.fullmatch(text[cursor:start]):
            return []
        result.append(text[start:start + len(source)])
        cursor = start + len(source)
    return result if _JOIN.fullmatch(text[cursor:]) else []


def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def planning_trigger(text, decision):
    """Name the two supported recovery routes without broadening arbitrary chat."""
    if not isinstance(decision, dict) or decision.get("_pending") or decision.get("_draft"):
        return None
    if decision.get("_guard") == "multi_step_partial":
        return "multi_step_partial"
    if decision.get("action") == "ufo_task" and \
            ci._plan_task_text(decision.get("param")) == ci._plan_task_text(text) and \
            not preflight_gui_task(str(decision.get("param", ""))) and len(planning_units(text)) >= 2:
        return "unsupported_compound_ui"
    return None


class PlanningIntentClient:
    """An optional local-only wrapper; child classifications never recurse."""
    supports_planning = True

    def __init__(self, base, budget=None, *, monotonic=time.monotonic, cancellation=None,
                 planning_enabled=None):
        self.base = base
        if budget is not None and (not _probability_budget(budget)):
            raise ValueError("Planning budget must be positive and finite")
        self.budget, self._clock = budget, monotonic
        self._cancellation, self._planning_enabled = cancellation, planning_enabled

    def __getattr__(self, name):
        return getattr(self.base, name)

    @property
    def _query(self):
        return self.base._query

    @_query.setter
    def _query(self, value):
        self.base._query = value

    @property
    def model(self):
        return self.base.model

    @model.setter
    def model(self, value):
        self.base.model = value

    @property
    def working_model(self):
        return self.base.working_model

    @working_model.setter
    def working_model(self, value):
        self.base.working_model = value

    def warmup(self):
        return self.base.warmup()

    def ensure_ollama_alive(self):
        return self.base.ensure_ollama_alive()

    def _enabled(self):
        if getattr(self.base, "fallback", None) is not None:
            return False
        if self._planning_enabled is not None and not self._planning_enabled():
            return False
        if getattr(self.base, "_planning_local_only", False):
            return True
        try:
            from canaan_llm_provider import role_provider
            return not role_provider("intent")
        except Exception:
            return False

    def decide(self, user_text, history=None, on_action=None, tracer=None, *, cancellation=None):
        cancel = self._cancellation if cancellation is None else cancellation
        timeout = getattr(self.base, "timeout", 10)
        budget = self.budget if self.budget is not None else timeout
        if not _probability_budget(budget) or not _probability_budget(timeout):
            return self._stop("planning_timeout")
        deadline = self._clock() + budget
        if (stop := self._check_stopped(deadline, cancel)) is not None:
            return stop
        remaining = min(timeout, deadline - self._clock())
        if remaining <= 0:
            return self._stop("planning_timeout")
        try:
            initial = self.base.decide(user_text, history=history, on_action=None, tracer=tracer,
                                       query_timeout=remaining)
        except Exception as exc:
            return self._stop("planning_query_exception", exception=type(exc).__name__)
        if (stop := self._check_stopped(deadline, cancel)) is not None:
            return stop
        return self.complete_partial(user_text, initial, history=history, on_action=on_action, tracer=tracer,
                                     cancellation=cancel, _deadline=deadline)

    def complete_partial(self, user_text, initial_decision, history=None, on_action=None, tracer=None,
                         cancellation=None, *, _deadline=None):
        """Complete an already guarded partial rule/model decision exactly once.

        Rule-first callers do not classify the entire sentence again. Confirmed
        pending actions must bypass this entry point in the turn pipeline.
        """
        try:
            return self._complete_partial(user_text, initial_decision, history, on_action, tracer,
                                          cancellation, _deadline=_deadline)
        except Exception as exc:
            return self._stop("planning_guard_exception", exception=type(exc).__name__)

    def _complete_partial(self, user_text, initial_decision, history, on_action, tracer,
                          cancellation, *, _deadline):
        cancel = self._cancellation if cancellation is None else cancellation
        timeout = getattr(self.base, "timeout", 10)
        budget = self.budget if self.budget is not None else timeout
        if not _probability_budget(budget) or not _probability_budget(timeout):
            return self._stop("planning_timeout")
        deadline = self._clock() + budget if _deadline is None else _deadline

        def stopped():
            return self._check_stopped(deadline, cancel)

        def classify(source):
            if (stop := stopped()) is not None:
                return stop
            remaining = min(timeout, deadline - self._clock())
            if remaining <= 0:
                return self._stop("planning_timeout")
            return self.base.decide(source, history=history, on_action=None, tracer=tracer,
                                    query_timeout=remaining)

        initial = initial_decision
        if (stop := stopped()) is not None:
            return stop
        if not isinstance(initial, dict):
            return self._stop("planning_invalid_decision")
        trigger = planning_trigger(user_text, initial)
        if not trigger or not self._enabled() or \
                str(initial.get("_model", "")).startswith("provider:") or initial.get("_model") == "fallback":
            return self._deliver(initial, on_action)
        if tracer is not None:
            tracer.set("planning_trigger", trigger)
        units = planning_units(user_text)
        if not units:
            return initial
        steps, records = [], []
        for index, source in enumerate(units):
            if (stop := stopped()) is not None:
                return stop
            rule = ci.fast_match_intent(source)
            origin = "rule" if rule else "model"
            try:
                decision = rule if rule else classify(source)
            except Exception as exc:
                return self._stop("step_query_exception", exception=type(exc).__name__)
            if (stop := stopped()) is not None:
                return stop
            if not isinstance(decision, dict) or decision.get("action") in (None, "chat", "multi_action") or decision.get("_pending") or decision.get("_draft"):
                return self._stop("step_unavailable", model=decision.get("_model") if isinstance(decision, dict) else None)
            observed = {key: decision[key] for key in ("_model", "_conf", "_pconf") if key in decision}
            if origin == "model" and (observed.get("_model") != self.model or not _probability(observed.get("_conf")) or
                    "_pconf" not in observed or observed["_pconf"] is not None and not _probability(observed["_pconf"])):
                return self._stop("step_missing_model_evidence", model=observed.get("_model"))
            guarded = ci.apply_command_guard(source, dict(decision), require_confidence=False, _local_step=True)
            if not guarded or guarded.get("action") in (None, "chat", "multi_action") or guarded.get("_pending") or guarded.get("_draft"):
                return self._stop("step_guard_rejected", model=observed.get("_model"))
            normalized = ci._guard_multi_action_steps([{"action": guarded["action"], "param": guarded.get("param", "")}])
            if not normalized:
                return self._stop("step_invalid_parameter")
            step = normalized[0]
            if origin == "model":
                if observed["_conf"] < ci.confidence_threshold(guarded, source, self.base.min_confidence):
                    return self._stop("step_low_confidence", model=observed.get("_model"))
                if not ci._plan_local_step_grounded(step, source):
                    return self._stop("step_source_mismatch", model=observed.get("_model"))
            elif not ci._plan_step_equal(rule, step, source):
                return self._stop("step_source_mismatch")
            if step["action"] in _UI_ACTIONS and (step["action"] != "ufo_task" or
                    not preflight_gui_task(str(step.get("param", "")))):
                return self._stop("planning_no_plan", error_code="NO_PLAN")
            if step["action"] == "media_control" and not ci._plan_local_step_grounded(step, source):
                return self._stop("planning_no_plan", error_code="NO_PLAN")
            steps.append(step)
            records.append({"index": index, "source_text": source, "source": origin, **observed})
        if (stop := stopped()) is not None:
            return stop
        if not ci._multi_plan_matches_source(user_text, steps):
            return self._stop("planning_source_mismatch")
        result = {"action": "multi_action", "param": json.dumps(steps, ensure_ascii=False),
                  "emotion": "confident", "reply": "按完整步骤处理，需要确认的操作会先请你确认。",
                  "_model": self.model if any(record["source"] == "model" for record in records) else "planner:rules",
                  "_planning_trigger": trigger,
                  EVIDENCE_KEY: issue_evidence(user_text, steps, records)}
        result = ci.apply_command_guard(user_text, result, require_confidence=True)
        if (stop := stopped()) is not None:
            return stop
        return self._deliver(result, on_action)

    def _check_stopped(self, deadline, cancel):
        try:
            cancelled = bool(cancel() if callable(cancel) else cancel.is_set() if cancel is not None else False)
        except Exception:
            cancelled = True
        if cancelled:
            return self._stop("planning_cancelled")
        if self._clock() >= deadline:
            return self._stop("planning_timeout")
        return None

    def _stop(self, reason, *, model=None, exception=None, error_code=None):
        result = {"action": "chat", "param": "", "emotion": "confident", "_guard": reason,
                  "reply": "完整步骤还没核对齐，这次尚未执行。", "_model": model or self.model}
        if error_code:
            result["error_code"] = error_code
        if exception:
            result["_planning_exception"] = exception
        return result

    @staticmethod
    def _deliver(decision, on_action):
        if on_action is not None and decision.get("action") not in (None, "chat"):
            on_action(decision["action"])
        return decision


def _probability_budget(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0
