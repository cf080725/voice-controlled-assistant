"""Context-local read-only query selection from validated original rule sources.

Descriptors never authorize execution, confirmation or task completion. Existing
rule/guard ownership and the execution revocation guard remain authoritative.
No client/model metadata field is read by this module.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import re


_ORIGIN = object()
_CAPTURE = ContextVar('canaan_query_rule_capture', default=None)
_STEP = ContextVar('canaan_query_step', default=None)


class QueryContextError(ValueError):
    """An active query scope cannot be safely matched to this actor context."""


@dataclass(frozen=True, init=False)
class QueryBinding:
    original_text: str
    owned_text: str
    action: str
    param_key: str
    position: int
    source_position: int
    query: object
    identity: tuple
    _origin: object


@dataclass
class _Capture:
    normalized_text: str
    rows: object = None


@dataclass(frozen=True)
class _StepScope:
    binding: object
    step_index: int
    required: bool


def _legacy_disk_query(source):
    return bool(re.fullmatch(
        r'(?:请|麻烦|帮我|先|再)*(?:查(?:询)?(?:一下|下)?|看看|看一下)?'
        r'(?:硬盘|磁盘)(?:的)?(?:还剩|还有|剩余|剩)(?:多少|多大)?(?:容量|容亮|空间)?[啊呀吗吧]?[？?!！]?',
        source.strip()) and re.search(r'查|看|多少|多大', source))


def status_query_identity(source):
    """Pure status meaning used only for the existing adjacent-once policy."""
    if not isinstance(source, str):
        return None
    from canaan_status_query import query_kind
    query = query_kind(source)
    if query is not None:
        kind, targets = query
        return ('specific', kind, tuple(targets))
    return ('legacy_disk',) if _legacy_disk_query(source) else ('snapshot',)


def _key(step):
    import canaan_intent as intent
    action, param = intent._plan_step_identity(step)
    return action, json.dumps(param, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _record_rule_query_sources(normalized_text, decisions, decision_sources):
    """Private hook called only with the rule parser's local decision map."""
    capture = _CAPTURE.get()
    if capture is None or normalized_text != capture.normalized_text:
        return
    rows = []
    for decision in decisions:
        action, param_key = _key(decision)
        source = decision_sources.get(id(decision))
        rows.append((action, param_key, source if isinstance(source, str) else None))
    capture.rows = tuple(rows)


def _mint(text, clause, action, param_key, position, source_position, *, selection_text=None):
    from canaan_status_query import query_kind
    binding = object.__new__(QueryBinding)
    selected = clause if selection_text is None else selection_text
    query = query_kind(selected) if action == 'system_status' else None
    identity = status_query_identity(selected) if action == 'system_status' else ('command', param_key)
    values = (text, clause, action, param_key, position, source_position, query, identity, _ORIGIN)
    for name, value in zip(QueryBinding.__dataclass_fields__, values):
        object.__setattr__(binding, name, value)
    return binding


def _is_query(action, param_key):
    if action == 'system_status':
        return True
    if action != 'run_cmd':
        return False
    from canaan_command_reply import _family
    return bool(_family(json.loads(param_key))[0])


def _single_status_bindings(user_text, proposed):
    """Bind one guarded status intent; keep authority and original text intact."""
    import canaan_intent as intent
    from canaan_status_query import query_kind
    if len(proposed) != 1 or _key(proposed[0]) != ("system_status", '""'):
        return ()
    if intent._QUOTED_SPAN_RE.fullmatch(user_text.strip()):
        return ()
    if intent.mention_reason(user_text) in ("hypothetical", "reported", "quoted_report", "past_story"):
        return ()
    guarded = intent.apply_command_guard(user_text, {"action": "system_status", "param": ""}, require_confidence=False)
    if not guarded or guarded.get("_pending") or _key(guarded) != ("system_status", '""'):
        return ()
    rows = intent._status_source_requests(user_text)
    if not rows or any(row["known_action"] not in (None, "chat", "system_status") for row in rows):
        return ()
    queries = [row for row in rows if row["kind"] == "query"]
    identities = {row["identity"] for row in queries}
    if queries:
        if len(identities) != 1 or any(row["kind"] in ("request", "uncertain") for row in rows):
            return ()
        selected = queries[0]["selection_text"]
    else:
        if (intent._MORE_STEPS_RE.search(intent.mask_quotes(user_text))
                or sum(row["kind"] == "request" for row in rows) > 1
                or len(rows) > 1 and any(row["kind"] == "uncertain" for row in rows)
                or any(row["query"] is not None for row in rows)
                or query_kind(intent.mask_quotes(user_text)) is not None):
            return ()
        selected = ""
    return (_mint(user_text, user_text, "system_status", '""', 0, 0, selection_text=selected),)


def _literal_command_bindings(user_text, proposed):
    """Classify an already guarded single fixed command, granting no authority."""
    import canaan_intent as intent
    if len(proposed) != 1:
        return ()
    action, param_key = _key(proposed[0])
    command = json.loads(param_key)
    if action != "run_cmd" or not isinstance(command, str) or not _is_query(action, param_key):
        return ()
    if not intent._plan_local_action_intent(action, user_text):
        return ()
    # As in the rule-plan path, this is source/parameter revalidation only.
    # Confidence and execution/confirmation remain in the original caller and
    # actor; no synthetic confidence or authorization metadata is supplied.
    source_step = {"action": action, "param": command}
    guarded = intent.apply_command_guard(user_text, source_step, require_confidence=False)
    if not guarded or guarded.get("_pending") or _key(guarded) != (action, param_key):
        return ()
    # Match the original param_grounded whitespace/case normalization while
    # retaining the actual original span. Quotes around an explicit parameter
    # stay outside that span and retain their ordinary parser ownership.
    compact = re.sub(r"\s", "", command)
    pattern = r"\s*".join(re.escape(character) for character in compact)
    spans = list(re.finditer(pattern, user_text, re.I))
    if len(spans) != 1:
        return ()
    marker = "〔固定查询命令原文〕"
    if marker in user_text:
        return ()
    span = spans[0]
    outside = user_text[:span.start()] + marker + user_text[span.end():]
    # A fixed command owns its internal commas; all other source text still
    # passes through the existing source parser, including any extra request.
    units = intent._plan_source_units(outside, _reference={})
    if len(units) != 1:
        return ()
    clause, known = units[0]
    if marker not in clause or not intent._plan_local_action_intent(action, clause):
        return ()
    if known and _key(known)[0] != action:
        return ()
    return (_mint(user_text, user_text, action, param_key, 0, 0),)


def trusted_query_bindings(user_text, steps):
    """Bind a validated full plan or its exact remaining suffix to source units.

    Suffix support only selects query targets during existing confirmation
    recovery. It does not authorize skipping, resuming or executing a step.
    Returned positions are zero-based in the current dispatch list; the
    separate source_position is zero-based in the full original rule plan.
    Empty tuple means no safe source binding was obtained.
    """
    if not isinstance(user_text, str) or not user_text.strip():
        return ()
    import canaan_intent as intent
    try:
        proposed = intent._guard_multi_action_steps(steps)
        if not proposed:
            return ()
        normalized_text = intent._asr_fix(intent._space_clauses(user_text))
        capture = _Capture(normalized_text)
        token = _CAPTURE.set(capture)
        try:
            reference = intent.fast_match_intent(user_text)
            if reference is None:
                return _literal_command_bindings(user_text, proposed) or _single_status_bindings(user_text, proposed)
            if not reference or reference.get('action') == 'chat':
                return ()
            expected = (intent._guard_multi_action_steps(reference.get('param'))
                        if reference.get('action') == 'multi_action' else [reference])
            if not expected:
                return ()
            guarded = intent.apply_command_guard(user_text, reference, require_confidence=False)
            if not guarded or guarded.get('action') == 'chat' or guarded.get('_pending'):
                return ()
            accepted = (intent._guard_multi_action_steps(guarded.get('param'))
                        if guarded.get('action') == 'multi_action' else [guarded])
            if len(accepted) != len(expected) or any(_key(a) != _key(b) for a, b in zip(accepted, expected)):
                return ()
            if not intent._multi_plan_matches_source(user_text, expected):
                if len(expected) == 1 and _key(expected[0]) == ("system_status", '""'):
                    return _single_status_bindings(user_text, proposed)
                return ()
            captured = capture.rows
            if captured is not None and len(captured) == len(expected) and all(
                    _key(step) == row[:2] for step, row in zip(expected, captured)):
                sources = [row[2] for row in captured]
            else:
                units = intent._plan_source_units(user_text, _reference=reference)
                if len(units) != len(expected) or any(not known or not intent._plan_step_equal(
                        known, step, clause) for (clause, known), step in zip(units, expected)):
                    if len(expected) == 1 and _key(expected[0]) == ("system_status", '""'):
                        return _single_status_bindings(user_text, proposed)
                    return ()
                sources = [clause for clause, _known in units]
        finally:
            _CAPTURE.reset(token)
        if len(proposed) > len(expected):
            return ()
        offset = len(expected) - len(proposed)
        if any(not intent._plan_step_equal(want, got, source or user_text)
               for want, got, source in zip(expected[offset:], proposed, sources[offset:])):
            return ()
        bindings = []
        for index, (step, clause) in enumerate(zip(proposed, sources[offset:])):
            action, param_key = _key(step)
            if not _is_query(action, param_key):
                bindings.append(None)
            elif not clause:
                return ()
            else:
                bindings.append(_mint(user_text, clause, action, param_key, index, index + offset))
        return tuple(bindings)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        return ()


@contextmanager
def step_query_scope(binding, *, step_index, required=True):
    """Provide a local query descriptor; never change full ActionContext text."""
    if type(step_index) is not int or not 0 <= step_index < 32 or type(required) is not bool:
        raise QueryContextError('Invalid server step scope')
    if binding is not None and (type(binding) is not QueryBinding or getattr(binding, '_origin', None) is not _ORIGIN
                                or binding.position != step_index):
        raise QueryContextError('Descriptor does not belong to this step')
    token = _STEP.set(_StepScope(binding, step_index, required))
    try:
        yield
    finally:
        _STEP.reset(token)


def current_query(context):
    """None means no scope; an invalid active scope raises instead of guessing."""
    scope = _STEP.get()
    if scope is None:
        return None
    from canaan_execution_guard import execution_allowed
    if execution_allowed() is not True:
        raise QueryContextError('Query selection was revoked')
    binding = scope.binding
    if binding is None:
        if scope.required or getattr(context, 'user_text', ''):
            raise QueryContextError('No verified query source for this step')
        return None
    try:
        action, param_key = _key({'action': context.action_name, 'param': context.raw_param})
        matches = (type(binding) is QueryBinding and binding._origin is _ORIGIN
                   and binding.position == scope.step_index and context.user_text == binding.original_text
                   and (action, param_key) == (binding.action, binding.param_key))
    except (TypeError, ValueError, KeyError, AttributeError):
        matches = False
    if not matches:
        raise QueryContextError('Actor context does not match the verified query source')
    return binding
