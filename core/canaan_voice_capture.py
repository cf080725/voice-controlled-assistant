"""Ownership for microphone recordings and local voice UI notifications."""
import threading


_STATE_INIT_LOCK = threading.Lock()


def voice_state_lock(hub):
    """One short state lock per hub; never hold it across model/audio work."""
    lock = getattr(hub, "_voice_state_lock", None)
    if lock is None:
        with _STATE_INIT_LOCK:
            lock = getattr(hub, "_voice_state_lock", None)
            if lock is None:
                lock = threading.RLock()
                hub._voice_state_lock = lock
    return lock


def clear_voice_processing(hub, owner):
    with voice_state_lock(hub):
        if owner is not getattr(hub, "_active_turn_owner", None):
            return False
        hub.is_processing = False
        return True


_ANY_SESSION = object()


class VoiceInterruption:
    """The output/turn stopped by one request; native stop may return much later."""

    def __init__(self, hub, pending):
        self.session = getattr(hub, '_speech_playback_session', None)
        self.ack = getattr(hub, '_ack_playback_session', None)
        self.owner = getattr(hub, '_active_turn_owner', None)
        self.binding = getattr(getattr(hub, 'user_context', None), 'binding', None)
        self.pending = pending
        self.was_speaking = bool(getattr(hub, 'is_speaking', False))

    def current(self, hub):
        with voice_state_lock(hub):
            return (self.session is getattr(hub, '_speech_playback_session', None)
                    and self.ack is getattr(hub, '_ack_playback_session', None)
                    and self.owner is getattr(hub, '_active_turn_owner', None)
                    and (self.binding is None or (
                        not self.binding.cancelled.is_set()
                        and self.binding is getattr(getattr(hub, 'user_context', None), 'binding', None)))
                    and (self.pending is None or self.pending is hub.pending.peek()))


def interrupt_voice_state(hub, *, require_speaking=False, cancel_processing=False,
                          clear_speaking=True, expected_session=_ANY_SESSION, expected_pending=None):
    """Cancel only the captured owner before any blocking audio call, never after it.

    Pending consent keeps the logical playing flag until the physical owner exits.
    This lock protects state only; callers must release it before native audio work.
    """
    with voice_state_lock(hub):
        if expected_session is not _ANY_SESSION and expected_session is not getattr(hub, '_speech_playback_session', None):
            return None
        if expected_pending is not None and expected_pending is not hub.pending.peek():
            return None
        speaking = bool(getattr(hub, 'is_speaking', False))
        if require_speaking and not speaking:
            return None
        if cancel_processing and not speaking and not getattr(hub, 'is_processing', False):
            return None
        request = VoiceInterruption(hub, expected_pending)
        for playback in (request.session, request.ack):
            if playback is not None:
                playback['cancelled'] = True
        hub.interrupt_speech_flag = True
        if clear_speaking:
            hub.is_speaking = False
        if cancel_processing and not speaking:
            hub.turn_cancelled = True
        return request


class VoiceCaptureContext:
    def __init__(self, hub):
        capture = getattr(hub, "_capture_context", None)
        self.binding = capture() if callable(capture) else None
        self._provider = getattr(hub, "_context_token", None)
        self.token = (self.binding.token if self.binding is not None else
                      self._provider() if callable(self._provider) else None)

    def current(self, *, final=False):
        cancelled = getattr(self.binding, "cancelled", None)
        if cancelled is not None and cancelled.is_set():
            return False
        # UserContext cancels its old binding when switching. Checking that event
        # is sufficient in the audio loop; final handoff also checks privacy/version.
        if self.binding is not None and not final:
            return True
        return not callable(self._provider) or self.token == self._provider()


class ContextVoiceText(str):
    """Keep the source identity with text until background playback claims it."""

    def __new__(cls, text, context):
        value = super().__new__(cls, text)
        value.voice_context = context
        return value


class BackgroundVoiceRequest:
    """An unsolicited utterance may start only in the idle context that queued it."""

    def __init__(self, hub, *, context=None):
        self.context = context if context is not None else VoiceCaptureContext(hub)
        with voice_state_lock(hub):
            self.owner = getattr(hub, '_active_turn_owner', None)
            self.previous_session = getattr(hub, '_speech_playback_session', None)
            self.ack = getattr(hub, '_ack_playback_session', None)
            self.session = None

    def current(self, hub, *, final=False):
        if not self.context.current(final=final):
            return False
        with voice_state_lock(hub):
            if (self.owner is not getattr(hub, '_active_turn_owner', None)
                    or getattr(hub, 'is_recording_command', False)
                    or getattr(hub, 'is_processing', False)
                    or self.ack is not getattr(hub, '_ack_playback_session', None)):
                return False
            pending = getattr(hub, 'pending', None)
            if pending is not None and pending.peek() is not None:
                return False
            current = getattr(hub, '_speech_playback_session', None)
            if self.session is not None:
                return self.session is current and not self.session.get('cancelled', False)
            return (self.previous_session is current and not getattr(hub, 'is_speaking', False)
                    and not getattr(hub, '_voice_output_busy', lambda: False)())


class VoiceUIEvent(tuple):
    """Keep the existing two-item GUI protocol while retaining local ownership."""

    def __new__(cls, kind, data, *, binding=None, session=None, turn_owner=None, request=None):
        item = super().__new__(cls, (kind, data))
        item.binding, item.session, item.turn_owner = binding, session, turn_owner
        item.request = request
        return item

    def current(self, hub):
        try:
            if self.request is not None and not self.request.current(hub):
                return False
            if self.turn_owner is not None and self.turn_owner is not getattr(hub, "_active_turn_owner", None):
                return False
            if self.session is not None and self.session is not getattr(hub, "_speech_playback_session", None):
                return False
            if self.binding is not None:
                valid = getattr(hub, "_context_valid", None)
                return callable(valid) and valid(self.binding)
            return True
        except Exception:
            return False
