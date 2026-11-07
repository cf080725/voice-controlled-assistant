"""Shared text turn decisions and a task-local device adapter.

No listener, model construction, desktop initialization or playback occurs on
import. The actual Hub supplies the existing policy/intent/executor ports.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field, replace
import copy
import json
import re
import threading
from pathlib import Path
from weakref import WeakSet

from canaan_device_requests import PreparedDeviceTurn, DeviceExecutionResult
from canaan_query_answers import QueryDocument, QueryFragment, get_query_document, merge_prefix
from canaan_device_attachments import capture_device_outputs


class TextTurnAborted(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class CompanionTextUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class IdentityCommandEffect:
    operation: str
    text: str
    name: str | None = None


@dataclass(frozen=True)
class MemoryDeletionTarget:
    store: object = field(repr=False)
    keyword: str
    profile_key: str
    person_id: int | None
    fact_ids: tuple
    episode_ids: tuple
    origins: tuple
    log_path: str | None
    context_store: object = field(repr=False)
    context_ids: object = field(repr=False)
    invalidate_identity: bool


@dataclass(frozen=True)
class MemoryForgetEffect:
    operation: str
    target: MemoryDeletionTarget


@dataclass(frozen=True)
class MemoryDeletionFacts:
    target: MemoryDeletionTarget = field(repr=False)
    facts_clear: bool
    episodes_clear: bool
    indexes_clear: bool
    log_clear: bool
    context_clear: bool


@dataclass(frozen=True, eq=False)
class TerminalControlPlan:
    kind: str
    operation: str
    effect: object
    target: object


@dataclass(frozen=True)
class ControlTarget:
    kind: str
    name: str | None = None
    incognito: bool | None = None
    profile_key: str | None = None
    identity_revision: int | None = None
    privacy_revision: object = None
    owner_privacy_revision: object = None
    memory: object = field(default=None, repr=False)

    def __post_init__(self):
        if self.kind not in {"owner", "person", "privacy", "memory"}:
            raise ValueError("Unknown approved control target")
        if self.kind == "person" and (not isinstance(self.name, str) or not self.name or self.name != " ".join(self.name.split())):
            raise ValueError("A person target requires the exact normalized name")
        if self.kind == "privacy" and (type(self.incognito) is not bool or not isinstance(self.profile_key, str)
                                       or type(self.identity_revision) is not int):
            raise ValueError("A privacy target requires the original identity and intended state")
        if self.kind == "memory" and (type(self.memory) not in {MemoryDeletionTarget, MemoryDeletionFacts}
                                      or type(self.incognito) is not bool or type(self.identity_revision) is not int):
            raise ValueError("A memory target requires exact store/deletion and original identity facts")


@dataclass(frozen=True, eq=False)
class CompletedControlTransition:
    plan: TerminalControlPlan
    effect: object
    before_token: tuple
    before_session_id: str
    after_token: tuple
    after_session_id: str | None
    expected_target: ControlTarget
    actual_target: ControlTarget
    completed: bool = True


_control_plans, _control_completions = WeakSet(), WeakSet()


def validate_control_completion(prepared, result):
    plan, receipt = prepared.control_transition, result.control_transition
    if (type(plan) is not TerminalControlPlan or plan not in _control_plans
            or type(receipt) is not CompletedControlTransition or receipt not in _control_completions
            or receipt.plan is not plan or receipt.effect is not plan.effect or receipt.completed is not True
            or receipt.expected_target is not plan.target or not _target_matches(plan.target, receipt.actual_target)
            or result.state != "succeeded" or result.continuation is not None
            or len(receipt.before_token) != 4 or len(receipt.after_token) != 4
            or (receipt.before_token == receipt.after_token and receipt.before_session_id == receipt.after_session_id)):
        raise ValueError("The terminal control completion is not an issued matching receipt")
    from canaan_companion import MemoryCommandEffect
    if plan.kind == "identity":
        valid = (type(plan.effect) is IdentityCommandEffect
                 and plan.operation in {"switch_person", "switch_owner"}
                 and plan.effect.operation == plan.operation)
    elif plan.kind == "privacy":
        valid = (type(plan.effect) is MemoryCommandEffect
                 and plan.operation in {"incognito_on", "incognito_off"}
                 and plan.effect.operation == plan.operation
                 and receipt.before_token[:2] == receipt.after_token[:2])
    elif plan.kind == "memory":
        valid = (type(plan.effect) is MemoryForgetEffect and plan.effect.target is plan.target.memory
                 and plan.operation in {"forget_memory", "forget_person_memory"}
                 and plan.effect.operation == plan.operation
                 and receipt.before_token[0] == receipt.after_token[0]
                 and receipt.after_token[1] == plan.target.identity_revision)
    else:
        valid = False
    if not valid:
        raise ValueError("This effect is not a terminal identity/privacy control")
    return receipt.before_token, receipt.after_token, receipt.after_session_id, receipt.expected_target


def _target_matches(expected, actual):
    if type(expected) is not ControlTarget or type(actual) is not ControlTarget or expected.kind != actual.kind:
        return False
    if expected.kind == "owner":
        return actual.profile_key == "owner"
    if expected.kind == "person":
        return expected.name == actual.name and isinstance(actual.profile_key, str) and actual.profile_key.startswith("person:")
    if expected.kind == "memory":
        facts = actual.memory
        return (type(expected.memory) is MemoryDeletionTarget and type(facts) is MemoryDeletionFacts
                and facts.target is expected.memory and facts.facts_clear is True and facts.episodes_clear is True
                and facts.indexes_clear is True and facts.log_clear is True
                and facts.context_clear is True and expected.name == actual.name
                and expected.profile_key == actual.profile_key and expected.identity_revision == actual.identity_revision
                and expected.incognito is actual.incognito and expected.privacy_revision == actual.privacy_revision
                and expected.owner_privacy_revision == actual.owner_privacy_revision)
    return (actual.profile_key == expected.profile_key and actual.identity_revision == expected.identity_revision
            and actual.incognito is expected.incognito)


def validate_control_target(context, binding, expected):
    """Read the actual approved target, cross-bound to the real context epoch."""
    if type(expected) is not ControlTarget or not context.valid(binding):
        raise ValueError("The control target context is invalid")
    if expected.kind == "memory":
        if context.db is not expected.memory.context_store:
            raise ValueError("The captured conversation context store changed")
        snapshot = context.hub.people.snapshot()
        if snapshot["profile_key"] != binding.profile_key or snapshot["revision"] != binding.revision:
            raise ValueError("The memory deletion identity changed")
        actual = ControlTarget("memory", incognito=binding.store.incognito,
            name=" ".join(snapshot["name"].split()) if snapshot["person_id"] is not None else None,
            profile_key=binding.profile_key, identity_revision=binding.revision,
            privacy_revision=binding.privacy_revision, owner_privacy_revision=binding.owner_privacy_revision,
            memory=_verify_memory_deletion(binding, expected.memory))
    elif expected.kind == "privacy":
        actual = ControlTarget("privacy", incognito=binding.store.incognito,
                               profile_key=binding.profile_key, identity_revision=binding.revision)
    else:
        snapshot = context.hub.people.snapshot()
        if snapshot["profile_key"] != binding.profile_key or snapshot["revision"] != binding.revision:
            raise ValueError("Identity facts changed while reading the terminal target")
        actual = ControlTarget("owner" if snapshot["person_id"] is None else "person",
                               name=" ".join(snapshot["name"].split()) if snapshot["person_id"] is not None else None,
                               profile_key=snapshot["profile_key"], identity_revision=snapshot["revision"])
    if not context.valid(binding) or not _target_matches(expected, actual):
        raise ValueError("The actual control target does not match the approved target")
    return actual


def _terminal_target_validator(context, binding, expected):
    validate_control_target(context, binding, expected)
    return True


def _control_plan(kind, effect, binding):
    if kind == "memory":
        if type(binding.privacy_revision) is not int or type(binding.owner_privacy_revision) is not int:
            raise ValueError("A memory deletion requires known privacy epochs")
        delta = 1 if effect.operation == "forget_memory" or effect.target.fact_ids else 0
        target = ControlTarget("memory", incognito=binding.store.incognito,
            name=" ".join(effect.target.store.name.split()) if effect.target.person_id is not None else None,
            profile_key=binding.profile_key, identity_revision=binding.revision + int(effect.target.invalidate_identity),
            privacy_revision=binding.privacy_revision + delta,
            owner_privacy_revision=binding.owner_privacy_revision + (delta if effect.operation == "forget_memory" else 0),
            memory=effect.target)
    else:
        target = (ControlTarget("owner", profile_key="owner") if effect.operation == "switch_owner" else
              ControlTarget("person", name=effect.name) if kind == "identity" else
              ControlTarget("privacy", incognito=effect.operation == "incognito_on",
                            profile_key=binding.profile_key, identity_revision=binding.revision))
    plan = TerminalControlPlan(kind, effect.operation, effect, target)
    _control_plans.add(plan)
    return plan


_ALL_MEMORY = {"所有", "全部", "一切", "所有事", "全部的事"}


def _literal_like(keyword):
    return "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _memory_effect(binding, decision, *, context_store):
    action = decision.get("action", "").strip().lower()
    action = "forget_memory" if action == "delete_memory" else action
    if action not in {"forget_memory", "forget_person_memory"}:
        return None
    import canaan_memory
    import canaan_people
    store, param = binding.store, decision.get("param", "")
    if action == "forget_memory":
        if isinstance(param, dict) and set(param) == {"value"}:
            param = param["value"]
        if not isinstance(param, str) or not param.strip() or binding.profile_key != "owner" or canaan_memory.get_store() is not store:
            raise ValueError("The approved owner-memory target is not this captured store")
        keyword, person_id = param.strip(), None
        log_path = str(Path(canaan_memory.CONV_LOG).resolve())
    else:
        if isinstance(param, str):
            param = json.loads(param)
        if (not isinstance(param, dict) or set(param) != {"person_id", "keyword"}
                or type(param["person_id"]) is not int or not isinstance(param["keyword"], str) or not param["keyword"].strip()
                or binding.profile_key != f"person:{param['person_id']}" or getattr(store, "person_id", None) != param["person_id"]
                or canaan_people.get_people() is not getattr(store, "people", None)):
            raise ValueError("The approved person-memory target is not this captured person")
        keyword, person_id, log_path = param["keyword"].strip(), param["person_id"], None
    fact_ids = tuple(row["id"] for row in store.matching(keyword))
    episodes = []
    if person_id is None:
        with store._lock:
            episodes = list(store.db.execute("SELECT id,origin FROM episodes" if keyword in _ALL_MEMORY else
                "SELECT id,origin FROM episodes WHERE dec(summary,'episodes.summary') LIKE ? ESCAPE '\\'",
                () if keyword in _ALL_MEMORY else (_literal_like(keyword),)))
    target = MemoryDeletionTarget(store, keyword, binding.profile_key, person_id, fact_ids,
        tuple(row["id"] for row in episodes), tuple(row["origin"] for row in episodes if row["origin"]), log_path,
        context_store, context_store.matching_content(binding.profile_key, keyword), person_id is not None and not fact_ids)
    return MemoryForgetEffect(action, target)


def _verify_memory_deletion(binding, target):
    import canaan_memory
    import canaan_people
    if type(target) is not MemoryDeletionTarget or binding.profile_key != target.profile_key:
        raise ValueError("The deleted memory belongs to a different captured profile")
    store = target.store
    if target.person_id is None:
        if binding.store is not store or canaan_memory.get_store() is not store or str(Path(canaan_memory.CONV_LOG).resolve()) != target.log_path:
            raise ValueError("The owner store or log target changed")
    elif (getattr(binding.store, "people", None) is not getattr(store, "people", None)
          or getattr(binding.store, "person_id", None) != target.person_id or canaan_people.get_people() is not store.people):
        raise ValueError("The person store target changed")
    if store.matching(target.keyword):
        raise ValueError("Approved memory facts remain after execution")
    remaining_context = target.context_store.matching_content(target.profile_key, target.keyword)
    if remaining_context["turn_ids"] or remaining_context["task_ids"]:
        raise ValueError("Forgotten conversation or task content remains readable")
    if target.person_id is not None:
        with store.people._lock:
            if any(store.db.execute("SELECT 1 FROM person_facts WHERE person_id=? AND id=?", (target.person_id, rid)).fetchone() for rid in target.fact_ids):
                raise ValueError("A targeted person fact survived deletion")
        return MemoryDeletionFacts(target, True, True, True, True, True)
    with store._lock:
        remaining = store.db.execute("SELECT 1 FROM episodes LIMIT 1" if target.keyword in _ALL_MEMORY else
            "SELECT 1 FROM episodes WHERE dec(summary,'episodes.summary') LIKE ? ESCAPE '\\' LIMIT 1",
            () if target.keyword in _ALL_MEMORY else (_literal_like(target.keyword),)).fetchone()
        if remaining is not None:
            raise ValueError("Approved memory episodes remain after execution")
        tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for base, ids in (("facts", target.fact_ids), ("episodes", target.episode_ids)):
            for table, column in ((base, "id"), (base + "_fts", "rowid"), (base + "_vec", "rowid"), (base + "_emb", "id")):
                if table in tables and any(store.db.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (rid,)).fetchone() for rid in ids):
                    raise ValueError("A targeted row or memory index survived deletion")
        if any(store.db.execute("SELECT 1 FROM forgotten_origins WHERE origin=?", (origin,)).fetchone() is None for origin in target.origins):
            raise ValueError("A deleted episode origin was not invalidated")
    path = Path(target.log_path)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        if target.keyword in _ALL_MEMORY and lines:
            raise ValueError("The approved conversation log was not cleared")
        if target.keyword not in _ALL_MEMORY:
            import canaan_crypto
            for line in lines:
                if target.keyword in line:
                    raise ValueError("A targeted conversation line survived deletion")
                decoded = canaan_crypto.open_record(json.loads(line))
                if target.keyword in json.dumps(decoded, ensure_ascii=False):
                    raise ValueError("A targeted encrypted conversation line survived deletion")
    return MemoryDeletionFacts(target, True, True, True, True, True)


@contextmanager
def turn_binding(hub, binding):
    if not hasattr(hub, "_turn_local"):
        hub._turn_local = threading.local()
    old = getattr(hub._turn_local, "binding", None)
    hub._turn_local.binding = binding
    try:
        yield
    finally:
        hub._turn_local.binding = old


@dataclass
class TextTurnSession:
    binding: object
    pending: object
    draft: object
    clarify: object
    device: bool = False
    effects_applied: bool = False
    facts: tuple = ()
    store: object = None
    task_context: object = None


@dataclass(frozen=True)
class _MessageSlotAnswer:
    session: object = field(repr=False)
    slots: dict = field(repr=False)
    task_key: tuple | None
    text: str = field(repr=False)

    def raw_for(self, session):
        part = session.draft.peek() if session.draft is not None else None
        task = session.draft.task_snapshot() if part else None
        key = (task["id"], task["version"], task["status"], task["missing_slot"]) if task else None
        if (session is not self.session or not session.device or part != self.slots
                or not part or not part.get("contact") or part.get("message")
                or key != self.task_key or session.clarify.snapshot() is not None):
            raise TextTurnAborted("clarification", "消息正文的问题已变化，请重新查看任务。")
        return self.text

    def completed_send(self, session, decision):
        if session is not self.session or not session.device or self.task_key is None:
            return None
        task = session.draft.task_snapshot()
        if (not task or task["id"] != self.task_key[0] or task["version"] != self.task_key[1] + 1
                or task["status"] != "awaiting_confirmation" or task["missing_slot"] != ""
                or task != session.store.get_task(session.binding.profile_key, task["id"])):
            return None
        pending = decision.get("_pending") if decision.get("action") == "chat" else None
        if not isinstance(pending, dict) or pending.get("action") != "send_chatapp" or pending.get("_context_task") != task:
            return None
        try:
            params = json.loads(pending["param"])
        except (KeyError, TypeError, ValueError):
            return None
        if (params != task["slots"] or params.get("contact") != self.slots["contact"]
                or set(params) != {"contact", "message"} or not isinstance(params["message"], str)):
            return None
        return pending


def text_session(hub, binding, *, device=False, client_id=None, request_id=None):
    if not device:
        return TextTurnSession(binding, hub.pending, getattr(hub, "chatapp_draft", None), getattr(hub, "clarify", None))
    from canaan_intent import PendingConfirmation, ChatAppDraft, ClarifyContext
    from canaan_device_context import device_task_namespace, ScopedTaskStore, TaskScopedUserContext
    store = ScopedTaskStore(hub.context_store, device_task_namespace(client_id, request_id))
    context = TaskScopedUserContext(hub.user_context, store)
    persist = lambda: not getattr(binding.store, "incognito", False)
    draft = ChatAppDraft(ttl=600., store=store, persist_enabled=persist)
    clarify = ClarifyContext(ttl=600., store=store, persist_enabled=persist)
    draft.bind(binding.profile_key, binding.session_id)
    clarify.bind(binding.profile_key, binding.session_id)
    return TextTurnSession(binding, PendingConfirmation(), draft, clarify, True, store=store, task_context=context)


@dataclass(eq=False)
class PreparedTextTurn:
    session: TextTurnSession
    text: str
    turn: dict
    ports: dict = field(repr=False)
    identity: object = None
    memory: object = None
    proactive: object = None
    policy: object = None
    facts: tuple = ()
    control: TerminalControlPlan | None = None
    question: str = ""
    resume_item: object = None
    retired: bool = False
    authority: tuple = ()
    recorded: bool = False
    consumed: bool = False
    readback: str = ""
    query_prefix: QueryDocument | None = None

    @property
    def binding(self):
        return self.session.binding


def prepare_identity(hub, text, binding, ports, *, deferred=False):
    if not deferred:
        decision = ports["people_command_of"](hub, text)
        if decision and decision.get("_guard") == "person_switch":
            binding = hub._capture_context()
        return decision, binding, None
    from canaan_people import parse_switch
    parsed = parse_switch(text) if getattr(hub, "people", None) is not None else None
    if parsed is None:
        return None, binding, None
    kind, name = parsed
    if kind == "switch":
        name = " ".join(name.split())
        if name == hub.people.owner_name():
            kind, name = "owner", None
    op = {"switch": "switch_person", "owner": "switch_owner", "whoami": "whoami"}[kind]
    effect = IdentityCommandEffect(op, text, name)
    decision = dict(action="chat", param="", emotion="pamper", _guard="person_switch",
                    reply="准备查询当前使用者。" if kind == "whoami" else "准备切换当前使用者。")
    return decision, binding, effect


def _prepared_proactive(hub, text, binding):
    if getattr(hub, "proactive_policy", None) is None:
        return None, None
    import canaan_proactive as proactive
    if not hub._context_valid(binding):
        raise RuntimeError("The local user context changed")
    policy = proactive.default_policy(binding.profile_key,
        directory=proactive.policy_directory(binding, context=getattr(hub, "user_context", None)))
    return proactive.prepare_command(text, policy), policy


def prepare_hub_turn(hub, text, *, binding, ports, session=None, identity_decision=None,
                     identity_effect=None, pending_kind=None, pending=None,
                     expected_pending=None, expected_clarification=Ellipsis,
                     tracer=None, cancellation=None, on_action=None, deferred=False, llm_override=None,
                     on_binding_change=None, message_answer=None):
    """The common full decision order; device effects remain uncommitted."""
    from canaan_companion import prepare_memory_command, apply_memory_command
    from canaan_turn_pipeline import decide
    session = session or text_session(hub, binding)
    if message_answer is not None and not isinstance(message_answer, _MessageSlotAnswer):
        raise ValueError("A server-issued message slot answer is required")
    session.binding = binding
    cancel = cancellation or (lambda: False)
    if cancel() or not hub._context_valid(binding):
        raise TextTurnAborted("cancelled", "这次回答已取消或会话已变更，尚未执行。")
    if pending_kind is None and pending is None and not identity_decision:
        if session.device:
            pending_kind, pending = session.pending.resolve(text, expected_item=expected_pending)
        else:
            pending_kind, pending = hub.resolve_pending(text, expected_item=expected_pending) if expected_pending is not None else hub.resolve_pending(text)
    if expected_pending is not None and pending_kind is None:
        raise TextTurnAborted("pending", "这项确认已过期或已变更，请重新确认。")
    facts = ()
    with turn_binding(hub, binding):
        if deferred and identity_decision:
            if session.clarify is not None:
                session.clarify.clear()
            if session.draft is not None:
                session.draft.clear()
        if pending_kind is None and not identity_decision:
            facts = tuple(ports["extract_facts"](text))
            if deferred:
                session.facts += tuple(item for item in facts if item not in session.facts)
                facts = session.facts
            if not deferred:
                store = ports["fact_store_of"](hub)
                for fact, key in facts:
                    kwargs = {"expected_revision": binding.privacy_revision} if hasattr(store, "privacy_revision") else {}
                    store.add(fact, key, **kwargs)
            if session.clarify is not None:
                fresh_memory = prepare_memory_command(text, session.binding.store)
                if fresh_memory is not None and (session.draft is None or session.draft.peek() is None):
                    session.clarify.clear()
                    merged = None
                else:
                    stale = object()
                    merged = ports["clarify_merge"](text, session.clarify, expected=expected_clarification, stale_result=stale)
                    if merged is stale:
                        raise TextTurnAborted("clarification", "追问已变更或过期，旧回答尚未执行。")
                if merged:
                    text = merged
                    if tracer:
                        tracer.set("clarify_merged", True)
        memory_plan, proactive_plan, policy = None, None, None
        def memory_channel(value):
            nonlocal memory_plan
            memory_plan = prepare_memory_command(value, session.binding.store,
                context_matches=lambda keyword: hub.context_store.matching_content(session.binding.profile_key, keyword))
            if memory_plan is None:
                return None
            if deferred:
                return dict(memory_plan.decision)
            result = apply_memory_command(memory_plan, session.binding.store)
            if not hub._context_valid(session.binding) and hub._context_token()[:2] == session.binding.token[:2]:
                session.binding = hub._capture_context()
                hub._turn_local.binding = session.binding
                hub.turn_cancelled = False
                if on_binding_change is not None:
                    on_binding_change(session.binding)
            return result
        def proactive_channel(value):
            nonlocal proactive_plan, policy
            if not deferred:
                return hub._proactive_command(value)
            proactive_plan, policy = _prepared_proactive(hub, value, session.binding)
            return dict(proactive_plan.decision) if proactive_plan is not None else None
        def llm(value):
            if cancel():
                raise TextTurnAborted("cancelled", "这次回答已取消或会话已变更，尚未执行。")
            if llm_override is not None:
                return llm_override(value)
            return hub.query_canaan_os(value, tracer=tracer or ports["NULL_TRACER"], on_action=on_action, cancellation=cancel)
        def planner(value, partial):
            return hub.query_canaan_os(value, tracer=tracer or ports["NULL_TRACER"], on_action=on_action,
                                       cancellation=cancel, initial_decision=partial)
        owned_draft = None
        def draft_channel(value):
            nonlocal owned_draft
            options = {"message_answer": message_answer.raw_for(session)} if message_answer is not None else {}
            decision = ports["draft_fill"](value, session.draft, **options)
            if (message_answer is not None and decision is not None
                    and message_answer.completed_send(session, decision) is not None):
                owned_draft = copy.deepcopy(decision)
            return decision
        def guard_channel(value, decision, strict):
            checked = ports["apply_command_guard"](value, decision, require_confidence=strict)
            if (owned_draft is None or decision != owned_draft or checked == decision
                    or not isinstance(checked, dict) or checked.get("action") != "chat" or checked.get("_pending")):
                return checked
            candidate = message_answer.completed_send(session, decision)
            if candidate is None:
                return checked
            params = json.loads(candidate["param"])
            # This envelope joins the server's issued recipient slot and this
            # answer's parsed body solely for the existing full validation.
            # The actual turn text, execution source and candidate stay intact.
            envelope = "给" + params["contact"] + "发聊天软件说〔数据〕"
            from canaan_message_source import _message_source_scope
            def source_valid():
                return decision == owned_draft and message_answer.completed_send(session, decision) == candidate
            # The typed slot is the literal source, rather than another command
            # string to parse. All other guard checks still see the same send.
            with _message_source_scope(envelope, params["contact"], params["message"], source_valid):
                validated = ports["apply_command_guard"](envelope, copy.deepcopy(candidate),
                    require_confidence=strict, _local_step=True)
            if (validated and validated.get("action") == candidate["action"]
                    and validated.get("param") == candidate["param"]
                    and decision == owned_draft and message_answer.completed_send(session, decision) == candidate):
                return decision
            return checked
        turn = decide(text, pending_kind, pending,
            (lambda _: identity_decision,
             lambda value: hub._context_rule(value, draft=session.draft, binding=session.binding),
             draft_channel,
             hub._notice_query, proactive_channel,
             memory_channel, lambda value: ports["resend_request"](value, session.binding.memory), hub.fast_match_intent),
            llm, lambda decision: ports["resolve_references"](decision, session.binding.memory),
            guard_channel,
            ports["cancel_reply"], planner=planner if getattr(getattr(hub, "intent_client", None), "supports_planning", False) else None)
        decision = turn["decision"]
        if cancel() or not hub._context_valid(session.binding):
            raise TextTurnAborted("cancelled", "这次回答已取消或会话已变更，尚未执行。")
        dropped = session.pending.last_dropped() if pending_kind is None else None
        if dropped is not None and ports["keeps_pending"](text, decision) and session.pending.restore(dropped):
            ask = ports["describe_action"](dropped["decision"]).rstrip("？?")
            decision = dict(action="chat", param="", emotion="confident", _guard="kept_pending",
                            reply=f"没听清。{ask}？说确认就执行，说取消就算了。")
            turn.update(decision=decision, exec_source="rule")
        remembered_question, restored_draft = False, False
        if not turn["confirmed"]:
            decision = ports["canaan_contact_match"].correct_decision(decision)
            if decision.get("_guard") == "context_restore" and session.draft is not None:
                part = session.draft.peek()
                restored_draft = bool(part and not (part.get("contact") and part.get("message")))
            # A restored slot draft already owns its next answer. Saving the
            # restore request as generic clarification would merge it into the
            # slot value before draft_fill gets the user's answer.
            if not restored_draft:
                remembered_question = ports["remember_clarify"](session.clarify, text, decision)
        if decision.get("_draft") and session.draft is not None:
            session.draft.set(decision["_draft"])
        turn["decision"] = decision
        turn["pending_kind"] = pending_kind
        control = None
        if identity_effect is not None and identity_effect.operation != "whoami":
            control = _control_plan("identity", identity_effect, binding)
        if memory_plan is not None and memory_plan.effect is not None and memory_plan.effect.operation in {"incognito_on", "incognito_off"}:
            control = _control_plan("privacy", memory_plan.effect, binding)
        forget = _memory_effect(session.binding, decision.get("_pending") or decision, context_store=getattr(hub, "context_store", None))
        if forget is not None:
            control = _control_plan("memory", forget, session.binding)
        question = ""
        if deferred and decision.get("action") == "chat" and not decision.get("_pending"):
            if remembered_question or restored_draft or decision.get("_draft"):
                question = str(decision.get("reply") or "请补充具体内容。")[:240]
        return PreparedTextTurn(session, text, turn, ports, identity_effect, memory_plan, proactive_plan,
                                policy, facts, control, question)


def _set_pending(hub, prepared, decision):
    session, turn = prepared.session, prepared.turn
    if not session.device:
        hub.set_pending(decision, turn["exec_source"], turn["exec_user_text"])
    else:
        context = session.task_context
        decision = context.prepare_pending(session.binding, decision, turn["exec_source"], turn["exec_user_text"])
        session.pending.set(decision, turn["exec_source"], turn["exec_user_text"])
        session.pending.peek()["_context_token"] = session.binding.token
    return session.pending.peek()


def execute_hub_turn(hub, prepared, *, on_step=None, cancellation=None, execution_context=None):
    """Common guarded executor/claim/tail handling, with no UI or playback."""
    from canaan_user_context import execution_scope
    from canaan_execution_guard import execution_allowed
    from canaan_turn_pipeline import decide
    session, ports = prepared.session, prepared.ports
    cancel = cancellation or (lambda: False)
    if prepared.retired or cancel() or not execution_allowed() or not hub._context_valid(session.binding):
        raise TextTurnAborted("cancelled", "这次回答已取消或会话已变更，尚未执行。")
    with turn_binding(hub, session.binding):
        if session.device and not session.effects_applied:
            before = session.binding
            if prepared.identity is not None:
                result = ports["people_command_of"](hub, prepared.identity.text)
            else:
                people = getattr(hub, "people", None)
                if people is not None:
                    people.touch()
                store = ports["fact_store_of"](hub)
                for fact, key in prepared.facts:
                    if not execution_allowed():
                        raise RuntimeError("Device authority was revoked")
                    kwargs = {"expected_revision": before.privacy_revision} if hasattr(store, "privacy_revision") else {}
                    store.add(fact, key, **kwargs)
                result = None
                if prepared.memory is not None:
                    from canaan_companion import apply_memory_command
                    if not execution_allowed():
                        raise RuntimeError("Device authority was revoked")
                    result = apply_memory_command(prepared.memory, before.store)
                if prepared.proactive is not None:
                    from canaan_proactive import apply_command
                    if not execution_allowed():
                        raise RuntimeError("Device authority was revoked")
                    try:
                        result = apply_command(prepared.proactive, prepared.policy)
                    except OSError:
                        result = dict(action="chat", param="", _guard="reminder_storage", reply="提醒暂时没有保存成功，请稍后再试。")
            session.effects_applied = True
            if result is not None:
                prepared.turn["decision"] = result
            if prepared.control is not None and prepared.control.kind != "memory":
                after = hub.user_context.sync()
                if before.token != after.token or before.session_id != after.session_id:
                    actual_target = validate_control_target(hub.user_context, after, prepared.control.target)
                    completion = CompletedControlTransition(prepared.control, prepared.control.effect,
                        tuple(before.token), before.session_id, tuple(after.token), after.session_id,
                        prepared.control.target, actual_target)
                    _control_completions.add(completion)
                    prepared.retired = True
                    return dict(result={"success": True, "message": result.get("reply", "处理完成。")},
                                turn=prepared.turn, completion=completion, pending=None)
        turn = prepared.turn
        if prepared.resume_item is not None:
            kind, item = session.pending.resolve("确认", expected_item=prepared.resume_item)
            if kind != "confirm" or item is not prepared.resume_item:
                task = prepared.resume_item["decision"].get("_context_task")
                if session.device and task is not None:
                    session.store.transition_task(task["profile_key"], task["id"], task["version"], "cancelled")
                raise TextTurnAborted("pending_expired", "本次确认已过期，待执行部分未执行。")
            turn = decide("确认", kind, item, (), lambda _: None, lambda d: d,
                          lambda *args: None, ports["cancel_reply"])
            prepared.turn = turn
        decision = turn["decision"]
        if decision.get("_pending"):
            item = _set_pending(hub, prepared, decision["_pending"])
            if session.device:
                kind, item = session.pending.resolve("确认", expected_item=item)
                turn = decide("确认", kind, item, (), lambda _: None, lambda d: d,
                              lambda *args: None, ports["cancel_reply"])
                prepared.turn = turn
                decision = turn["decision"]
        context, binding = session.task_context or getattr(hub, "user_context", None), session.binding
        claimed = context.claim(binding, decision) if turn["confirmed"] and context is not None else None
        if turn["confirmed"] and context is not None and not claimed:
            return dict(result={"success": False, "error_code": "CONTEXT_INVALIDATED", "message": "确认已变更或执行过，请重新核对。"},
                        turn=turn, completion=None, pending=None, claim_failed=True)
        if cancel() or not execution_allowed() or not hub._context_valid(binding):
            if context is not None and claimed:
                context.finish(binding, claimed, {"success": False, "error_code": "CONTEXT_INVALIDATED"})
            raise TextTurnAborted("cancelled", "这次回答已取消或会话已变更，尚未执行。")
        completion = None
        deleting = (prepared.control is not None and prepared.control.kind == "memory" and turn["confirmed"]
                    and decision.get("action", "").strip().lower() in {"forget_memory", "delete_memory", "forget_person_memory"})
        with ExitStack() as locks:
            if deleting:
                locks.enter_context(hub.user_context.lock)
                locks.enter_context(hub.people._lock)
                locks.enter_context(hub.profile._lock)
                current_effect = _memory_effect(binding, decision, context_store=hub.context_store)
                expected = prepared.control.effect.target
                if (current_effect is None or current_effect.target.store is not expected.store
                        or current_effect.target.fact_ids != expected.fact_ids or current_effect.target.episode_ids != expected.episode_ids):
                    raise RuntimeError("The exact memory deletion target changed before execution")
            with execution_scope(execution_context or context, binding):
                result = ports["CanaanOSExecutor"].execute(decision.get("action", "chat"), decision.get("param", ""),
                    on_step=on_step, source=turn["exec_source"], confirmed=turn["confirmed"], user_text=turn["exec_user_text"])
            if deleting:
                detail = result.get("detail") or {}
                executed = (result.get("success") is True or
                    result.get("error_code") == "EXECUTION_REVOKED" and detail.get("phase") == "after_execute"
                    and detail.get("prior_success") is True and detail.get("execution_started") is True)
                if not executed:
                    raise RuntimeError("Memory deletion did not return an authoritative successful execution")
                target = prepared.control.effect.target
                token_after_actor = hub._context_token()
                if (binding.store.incognito is not prepared.control.target.incognito
                        or token_after_actor[0] != binding.profile_key or token_after_actor[1] != binding.revision
                        or token_after_actor[2] != prepared.control.target.privacy_revision
                        or token_after_actor[3] != prepared.control.target.owner_privacy_revision
                        or target.person_id is not None and " ".join(hub.people.snapshot()["name"].split()) != prepared.control.target.name):
                    raise RuntimeError("The memory-control identity or privacy mode changed")
                cleared = target.context_store.forget_content(target.profile_key, target.keyword)
                if target.invalidate_identity:
                    if not (cleared["turns_removed"] or cleared["tasks_removed"]):
                        raise RuntimeError("The approved context-only deletion did not remove its target")
                    hub.people.invalidate_context()
                after = hub.user_context.sync()
                actual_target = validate_control_target(hub.user_context, after, prepared.control.target)
                count = len(prepared.control.effect.target.fact_ids)
                person = prepared.control.operation == "forget_person_memory"
                keyword = prepared.control.effect.target.keyword
                result = dict(result, success=True, error_code=None,
                    message=f"已删除 {count} 条记忆" + ("" if person else f"（关键词：{keyword}）"),
                    speech_override=(f"好，删掉了{count}条。" if person else f"好，删掉了{count}条，相关的聊天记录也清掉了。")
                        if count else "我这儿没记着相关的事。",
                    detail=dict(detail, expected_privacy_transition=True, deletion_target_verified=True),
                    verification={"passed": True, "verified": True, "gate_name": "MEMORY_DELETION_TARGET"})
                if not count and (cleared["turns_removed"] or cleared["tasks_removed"]):
                    result.update(message="已清除相关对话与任务记录", speech_override="好，相关对话与任务记录也清掉了。")
                if binding.token != after.token or binding.session_id != after.session_id:
                    completion = CompletedControlTransition(prepared.control, prepared.control.effect,
                        tuple(binding.token), binding.session_id, tuple(after.token), after.session_id,
                        prepared.control.target, actual_target)
                    _control_completions.add(completion)
        if context is not None and claimed:
            context.finish(binding, claimed, result)
        item = None
        if result.get("error_code") == "CONFIRM_REQUIRED":
            item = _set_pending(hub, prepared, result.get("pending_decision") or decision)
        return dict(result=result, turn=turn, completion=completion, pending=item, forget_verified=bool(deleting))


class _DeviceExecutionContext:
    def __init__(self, adapter, bound):
        self.adapter, self.bound = adapter, bound
        self.context = adapter.hub.user_context
        self.lock = self.context.lock

    def valid(self, binding):
        if self.adapter._closed.is_set() or not self.context.valid(binding):
            return False
        try:
            self.adapter.binder.validate_bound(self.bound.handle)
            return binding.profile_key == self.bound.profile_key and binding.session_id == self.bound.session_id
        except Exception:
            return False


def _pending_description(decision, describe):
    """Describe planned work without borrowing a generated completion reply."""
    steps = decision.get("param") if decision.get("action", "").strip().lower() == "multi_action" else None
    for _ in range(2):
        if not isinstance(steps, str):
            break
        try:
            steps = json.loads(steps)
        except ValueError:
            break
    planned = steps if isinstance(steps, list) and steps else [decision]
    return "待执行：" + "；".join(str(describe(step)) for step in planned)


def _public_execution(decision, describe):
    """Project executable content; private evidence remains in the live plan."""
    if not isinstance(decision, dict) or not isinstance(decision.get("action"), str):
        raise ValueError("An execution preview requires a named action")
    action, param = decision["action"], decision.get("param", "")
    if action.strip().lower() == "multi_action":
        steps = param
        for _ in range(2):
            if not isinstance(steps, str):
                break
            try:
                steps = json.loads(steps)
            except ValueError:
                break
        if isinstance(steps, list):
            projected = [_public_execution(step, describe) for step in steps]
            param = json.dumps(projected, ensure_ascii=False, allow_nan=False) if isinstance(param, str) else projected
    result = {"action": action, "param": copy.deepcopy(param)}
    for key in ("reply", "readback"):
        if key in decision:
            if not isinstance(decision[key], str):
                raise ValueError("An execution preview reply must be text")
            result[key] = decision[key]
    if "reply" in result and action.strip().lower() != "chat":
        result["reply"] = _pending_description(result, describe)
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


class DeviceTurnAdapter:
    """Use the real Hub text engine with task-local question/approval state."""
    def __init__(self, hub, binder):
        self.hub, self.binder = hub, binder
        binder.configure_terminal_validator(_terminal_target_validator)
        self._prepared = WeakSet()
        self._closed = threading.Event()
        self._lock = threading.RLock()

    def _open(self):
        if self._closed.is_set():
            raise RuntimeError("The device turn adapter is closed")

    def close(self):
        with self._lock:
            self._closed.set()
            for payload in tuple(self._prepared):
                payload.retired = True
            self._prepared.clear()

    def _binding(self, bound):
        self._open()
        self.binder.validate_bound(bound.handle)
        context = getattr(self.hub, "user_context", None)
        if context is None:
            raise RuntimeError("The real UserContext is required")
        binding = context.sync()
        if binding.profile_key != bound.profile_key or binding.session_id != bound.session_id or not context.valid(binding):
            raise RuntimeError("The request belongs to another local session")
        return binding

    @staticmethod
    def _authority(bound):
        return tuple(getattr(bound, name) for name in ("handle", "client_id", "request_id", "binding_id",
                     "grant_revision", "profile_key", "session_id", "execution_host_id"))

    def _belongs(self, payload, bound):
        if payload.authority != self._authority(bound):
            raise RuntimeError("This prepared turn belongs to another device request")

    def _wrap(self, payload):
        with self._lock:
            self._open()
            self._prepared.add(payload)
        pending = payload.resume_item
        decision = pending["decision"] if pending is not None else payload.turn["decision"]
        describe = payload.ports["describe_action"]
        preview = _public_execution(decision, describe)
        preview["source"] = pending["source"] if pending is not None else payload.turn["exec_source"]
        if payload.readback:
            preview["readback"] = payload.readback
        if payload.control is not None:
            control = payload.control
            public = {"kind": control.kind, "operation": control.operation}
            if control.operation == "switch_person":
                public["target_name"] = control.effect.name
                summary = f"将切换到使用者“{control.effect.name}”。"
            elif control.operation == "switch_owner":
                public["target_name"] = "主人"
                summary = "将切回主人。"
            elif control.operation == "incognito_on":
                summary = "将开启无痕模式。"
            elif control.operation == "incognito_off":
                summary = "将关闭无痕模式。"
            elif control.operation in {"forget_memory", "forget_person_memory"}:
                public["target_name"] = control.effect.target.keyword
                count = len(control.effect.target.fact_ids)
                subject = "主人" if control.operation == "forget_memory" else f"使用者“{control.target.name}”"
                summary = f"将从{subject}的记忆中彻底删除关于“{control.effect.target.keyword}”的{count}条记录。"
                if control.operation == "forget_memory":
                    summary += "同时清除相关摘要、索引、本地对话与任务内容。"
                else:
                    summary += "同时清除此使用者的相关对话与任务内容。"
            else:
                raise ValueError("Unknown terminal control preview")
            preview.update(summary=summary, reply=summary, control=public)
        else:
            is_action = decision["action"].strip().lower() != "chat"
            if payload.readback:
                preview["summary"] = payload.readback
            elif is_action:
                preview["summary"] = _pending_description(preview, describe)
            else:
                preview["summary"] = str(decision.get("reply") or describe(decision.get("_pending") or decision))
            if is_action and "reply" in preview:
                preview["reply"] = preview["summary"]
        if decision.get("_pending"):
            preview["pending"] = _public_execution(decision["_pending"], describe)
        return PreparedDeviceTurn(preview, payload, payload.question, payload.control)

    def _payload(self, prepared):
        self._open()
        payload = prepared.payload if type(prepared) is PreparedDeviceTurn else None
        if type(payload) is not PreparedTextTurn or payload not in self._prepared or payload.retired or payload.consumed:
            raise RuntimeError("This adapter did not issue this prepared turn")
        return payload

    def _record(self, payload, turn, result, reply, *, decision=None):
        from canaan_execution_guard import execution_allowed
        if payload.recorded or not self.hub._context_valid(payload.binding) or not execution_allowed():
            return
        decision = decision or turn["decision"]
        payload.binding.memory.add_turn(payload.text, decision,
            result_note=payload.ports["canaan_turn_pipeline"].result_note(result), expected_revision=payload.binding.privacy_revision)
        self.hub.user_context.record(payload.binding, payload.text, turn["decision"], reply)
        payload.recorded = True
        if result.get("success") and decision.get("action") not in payload.ports["TRIVIAL_ACTIONS"]:
            self.hub.remember_task(f"[任务] {turn['exec_user_text'][:40]} → {str(result.get('message', ''))[:60]}",
                expected_revision=payload.binding.privacy_revision, store=payload.binding.store,
                origin=f"device:{payload.authority[1]}:{payload.authority[2]}")

    def prepare(self, bound):
        binding = self._binding(bound)
        session = text_session(self.hub, binding, device=True, client_id=bound.client_id, request_id=bound.request_id)
        text = self.hub.clean_text_turn(bound.text)
        identity, binding, effect = self.hub.prepare_text_identity(text, binding, deferred=True)
        payload = self.hub.prepare_text_turn(text, binding=binding, session=session,
            identity_decision=identity, identity_effect=effect, deferred=True)
        payload.authority = self._authority(bound)
        self.binder.validate_bound(bound.handle)
        return self._wrap(payload)

    def follow_up(self, prepared, bound, answer_text):
        payload = self._payload(prepared)
        self._belongs(payload, bound)
        self._binding(bound)
        if not payload.question or not isinstance(answer_text, str) or not answer_text.strip():
            raise ValueError("An answer to this task's active question is required")
        snapshot = payload.session.clarify.snapshot()
        part = payload.session.draft.peek()
        if snapshot is None and part is None:
            raise RuntimeError("The question has expired")
        message_answer = None
        if snapshot is None and part and part.get("contact") and not part.get("message"):
            task = payload.session.draft.task_snapshot()
            if task is None or (task["status"] == "awaiting_slot" and task["missing_slot"] == "message"):
                key = (task["id"], task["version"], task["status"], task["missing_slot"]) if task else None
                message_answer = _MessageSlotAnswer(payload.session, dict(part), key, answer_text)
        with self._lock:
            if payload.consumed:
                raise RuntimeError("The prepared question was already consumed")
            payload.consumed = True
        if payload.session.effects_applied:
            payload.session.effects_applied = False
            payload.session.facts = ()
        text = self.hub.clean_text_turn(answer_text)
        identity, binding, effect = self.hub.prepare_text_identity(text, payload.binding, deferred=True)
        result = self.hub.prepare_text_turn(text, binding=binding, session=payload.session,
            identity_decision=identity, identity_effect=effect, expected_clarification=snapshot, deferred=True,
            message_answer=message_answer)
        result.authority = payload.authority
        result.query_prefix = payload.query_prefix
        payload.retired = True
        self.binder.validate_bound(bound.handle)
        return self._wrap(result)

    def execute(self, prepared, bound):
        payload = self._payload(prepared)
        self._belongs(payload, bound)
        self._binding(bound)
        if payload.question:
            raise RuntimeError("This task still needs an answer")
        from canaan_execution_guard import execution_guard
        from canaan_user_context import execution_scope
        context = _DeviceExecutionContext(self, bound)
        with execution_guard(lambda: not self._closed.is_set()), execution_scope(context, payload.binding), capture_device_outputs() as outputs:
            try:
                result = self._execute_payload(payload, bound, context)
            except TextTurnAborted as aborted:
                if aborted.kind != "pending_expired":
                    raise
                payload.retired = True
                result = DeviceExecutionResult("failed", str(aborted), query_document=payload.query_prefix)
            return replace(result, attachments=outputs.snapshots())

    def _execute_payload(self, payload, bound, context):
        with payload.ports["GLOBAL_TASK_MANAGER"].os_exec_lock:
            with self._lock:
                self._open()
                if payload.consumed:
                    raise RuntimeError("The prepared turn was already consumed")
                payload.consumed = True
            record = self.hub.execute_text_turn(payload, execution_context=context)
        self._open()
        result, turn = record["result"], record["turn"]
        decision = turn["decision"]
        fragment = get_query_document(result)
        if type(fragment) is QueryFragment:
            document = merge_prefix(payload.query_prefix, fragment)
        elif type(fragment) is QueryDocument and payload.query_prefix is None:
            document = fragment
        elif fragment is None and payload.query_prefix is None:
            document = None
        else:
            raise ValueError('Trusted query continuation omitted or replaced its fragment')
        display = document.summary_reply() if document is not None else None
        if record["completion"] is not None:
            return DeviceExecutionResult("succeeded", result.get("message", "处理完成。"), control_transition=record["completion"], query_document=document)
        if record["pending"] is not None:
            self._record(payload, turn, result, result.get("speech_override") or result.get("message", ""))
            continuation = PreparedTextTurn(payload.session, "确认", turn, payload.ports,
                                           resume_item=record["pending"], authority=payload.authority,
                                           readback=display or result.get("speech_override") or result.get("message", ""),
                                           query_prefix=document)
            payload.retired = True
            wrapped = self._wrap(continuation)
            return DeviceExecutionResult("awaiting_confirmation", result.get("message", "请确认待执行部分。"), wrapped, query_document=document)
        reply = payload.ports["canaan_turn_pipeline"].spoken_result(decision.get("action", "chat"), decision, result,
                    turn["exec_source"], describe=payload.ports["describe_action"])
        use_companion = (decision.get("action") == "chat" and turn["exec_source"] == "llm"
                         and not decision.get("_guard") and getattr(payload.ports["canaan_config"], "COMPANION_ENABLED", True)
                         and not payload.ports["canaan_gamemode"].active())
        if use_companion:
            try:
                reply, suggestion = self.hub.companion_text_response(payload.text, payload.binding.memory.messages(), binding=payload.binding)
            except CompanionTextUnavailable as unavailable:
                reply = str(unavailable)
                self._record(payload, turn, {"success": False, "message": reply}, reply,
                             decision=dict(action="chat", param="", reply=reply))
                payload.retired = True
                return DeviceExecutionResult("failed", reply, query_document=document)
            decision = dict(action="chat", param="", reply=reply)
            payload.ports["remember_clarify"](payload.session.clarify, payload.text, decision)
            self._record(payload, turn, result, reply, decision=decision)
            if suggestion:
                item = _set_pending(self.hub, payload, suggestion)
                continuation = PreparedTextTurn(payload.session, payload.text, turn, payload.ports,
                    resume_item=item, authority=payload.authority, readback=reply, query_prefix=document)
                payload.retired = True
                return DeviceExecutionResult("awaiting_confirmation", reply, self._wrap(continuation), query_document=document)
            if payload.session.clarify.peek() is not None:
                continuation = PreparedTextTurn(payload.session, payload.text, turn, payload.ports,
                    question=reply[:240], authority=payload.authority, query_prefix=document)
                payload.retired = True
                return DeviceExecutionResult("awaiting_input", reply, self._wrap(continuation), query_document=document)
        if decision.get("_guard") == "model_unavailable":
            result = dict(result, success=False, error_code="MODEL_UNAVAILABLE", message=reply)
        self._record(payload, turn, result, reply, decision=decision)
        payload.retired = True
        successful = bool(result.get("success")) and decision.get("_guard") != "model_unavailable"
        return DeviceExecutionResult("succeeded" if successful else "failed", (display or reply) if successful else reply, query_document=document)
