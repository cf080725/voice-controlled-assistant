"""Bind every turn and pending action to one local person, including async work."""
from dataclasses import dataclass
import json
import threading
import time
from contextvars import ContextVar
from contextlib import contextmanager

_execution_cancel = ContextVar('execution_cancel', default=None)


def execution_cancel_event():
    return _execution_cancel.get()


class BoundCancellation:
    def __init__(self, context, binding):
        self.context, self.binding = context, binding

    def is_set(self):
        return not self.context.valid(self.binding)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self.binding.cancelled.wait(.01 if deadline is None else min(.01, max(0, deadline-time.monotonic())))
        return True


@contextmanager
def execution_scope(context, binding):
    token = _execution_cancel.set(BoundCancellation(context, binding) if context else None)
    try:
        yield
    finally:
        _execution_cancel.reset(token)


@dataclass(frozen=True)
class TurnContext:
    profile_key: str
    revision: int
    privacy_revision: object
    session_id: str
    store: object
    memory: object
    cancelled: object
    owner_privacy_revision: object = None

    @property
    def token(self):
        return (self.profile_key, self.revision, self.privacy_revision, self.owner_privacy_revision)


class UserContext:
    def __init__(self, hub, database):
        self.hub, self.db = hub, database
        self.lock = threading.RLock()
        self.binding = None
        self.sync()

    def snapshot(self):
        people = getattr(self.hub, 'people', None)
        return people.snapshot() if people else dict(profile_key='owner', person_id=None, name='主人', revision=0)

    def token(self):
        snap = self.snapshot()
        store = self.hub.profile if snap['person_id'] is None else self.hub.people.store(snap['person_id'])
        return (snap['profile_key'], snap['revision'], getattr(store, 'privacy_revision', None),
                getattr(self.hub.profile, 'privacy_revision', None))

    def valid(self, binding):
        return binding is not None and not binding.cancelled.is_set() and binding.token == self.token()

    def _cancel_binding(self, old):
        old.cancelled.set()
        self.hub.interrupt_speech_flag = True
        self.hub.turn_cancelled = True
        self.hub._pending_voice_deferred = None
        self.hub._pending_voice_listener = None
        self.hub._pending_voice_item = None
        pending = getattr(self.hub, 'pending', None)
        if pending:
            item = pending.peek()
            if item:
                task = item['decision'].get('_context_task')
                if task:
                    self.db.transition_task(old.profile_key, task['id'], task['version'], 'cancelled')
                pending.resolve('取消', expected_item=item)
        for name in ('_speech_playback_session', '_ack_playback_session'):
            session = getattr(self.hub, name, None)
            if session:
                session['cancelled'] = True
        for name in ('chatapp_draft', 'clarify'):
            draft = getattr(self.hub, name, None)
            if draft is not None and hasattr(draft, 'reset_local'):
                draft.reset_local()
        self.hub._reply_payload = None
        self.db.close_session(old.profile_key, old.session_id)

    def sync(self):
        from canaan_intent import ConversationMemory
        with self.lock:
            snap = self.snapshot()
            store = self.hub.profile if snap['person_id'] is None else self.hub.people.store(snap['person_id'])
            if store is None:
                raise RuntimeError('当前使用者已不存在，请重新选择')
            token = (snap['profile_key'], snap['revision'], getattr(store, 'privacy_revision', None),
                     getattr(self.hub.profile, 'privacy_revision', None))
            if self.binding and self.binding.token == token:
                return self.binding
            old = self.binding
            if old:
                self._cancel_binding(old)
            key = snap['profile_key']
            memory = ConversationMemory(max_turns=4, idle_expire=300., revision=lambda: getattr(store, 'privacy_revision', None))
            session_id = None if getattr(store, 'incognito', False) else self.db.open_session(key)
            if not getattr(store, 'incognito', False):
                turns = self.db.recent_turns(key, max_turns=4)
                memory.seed([(r['user'], r['decision']) for r in turns], grace=1500.,
                            expected_revision=getattr(store, 'privacy_revision', None))
            self.binding = TurnContext(key, snap['revision'], getattr(store, 'privacy_revision', None),
                                       session_id, store, memory, threading.Event(),token[3])
            self.hub.memory = memory
            for name in ('chatapp_draft', 'clarify'):
                draft = getattr(self.hub, name, None)
                if draft is not None:
                    draft.bind(key, session_id)
            self.hub.followup_until = 0.
            return self.binding

    def switch(self, name=None):
        if getattr(self.hub, 'people', None) is None:
            raise RuntimeError('多人记忆不可用')
        with self.lock:
            if name is None:
                self.hub.people.switch_owner()
            else:
                self.hub.people.switch_to(name)
            self.sync()
            return self.snapshot()

    def record(self, binding, user, decision, spoken, result_note=''):
        with self.lock:
            if self.valid(binding) and not getattr(binding.store, 'incognito', False):
                self.db.append_turn(binding.profile_key, binding.session_id, user, decision,
                                    spoken=spoken, result_note=result_note)

    def prepare_pending(self, binding, decision, source, user_text):
        with self.lock:
            return self._prepare_pending(binding,decision,source,user_text)

    def _prepare_pending(self, binding, decision, source, user_text):
        if not self.valid(binding):
            raise RuntimeError('使用者已切换，原确认失效')
        decision = dict(decision)
        if getattr(binding.store, 'incognito', False):
            decision.pop('_context_task',None)
            return decision
        param = decision.get('param', '')
        try:
            slots = json.loads(param) if isinstance(param, str) else dict(param)
            if not isinstance(slots, dict):
                slots = {'param': param}
        except (ValueError, TypeError):
            slots = {'param': param}
        slots.update(_source=source, _user_text=user_text)
        task = decision.get('_context_task')
        task = self.db.save_task(binding.profile_key, binding.session_id, decision['action'], slots,
                                status='awaiting_confirmation', task_id=task['id'] if task else None,
                                expected_version=task['version'] if task else None)
        if not task:
            raise RuntimeError('草稿已变更，请重新核对')
        if not self.valid(binding):
            self.db.transition_task(binding.profile_key,task['id'],task['version'],'cancelled')
            raise RuntimeError('使用者已切换，原确认失效')
        decision['_context_task'] = task
        return decision

    def claim(self, binding, decision):
        with self.lock:
            if not self.valid(binding):
                return None
            task = decision.get('_context_task')
            if not task:
                return True  # Incognito still uses the in-memory identity-bound confirmation.
            claimed = self.db.claim_task(task['id'], binding.profile_key, task['version'])
            if claimed and not self.valid(binding):
                self.db.finish_task(claimed['id'],binding.profile_key,claimed['version'],'failed',result_note='使用者已切换，尚未执行')
                return None
            return claimed

    def finish(self, binding, claimed, result):
        if not isinstance(claimed, dict):
            return
        detail = result.get('detail') or {}
        detail = detail if isinstance(detail,dict) else {}
        unknown = (result.get('error_code') == 'DELIVERY_UNVERIFIED'
                   or (detail.get('send_attempted') is True or detail.get('delivery_attempted') is True)
                   and not result.get('success'))
        status = 'unknown' if unknown else 'succeeded' if result.get('success') else 'failed'
        self.db.finish_task(claimed['id'], binding.profile_key, claimed['version'], status,
                            result={'success': bool(result.get('success')), 'error_code': result.get('error_code')})

    def view(self):
        binding = self.sync()
        if getattr(binding.store,'incognito',False):
            return {'profile_key':binding.profile_key,'session_id':None,'recent_turns':[],'current_task':None,
                    'turns':[],'task':None,'incognito':True,'active':self.snapshot()}
        return {**self.db.view(binding.profile_key), 'incognito':False,'active': self.snapshot()}

    def clear_history(self):
        with self.lock:
            binding = self.sync()
            self._cancel_binding(binding)
            result = self.db.clear_history(binding.profile_key)
            people = getattr(self.hub,'people',None)
            if people is not None and hasattr(people,'invalidate_context'):
                people.invalidate_context()
            self.binding = None
            self.sync()
            return result
