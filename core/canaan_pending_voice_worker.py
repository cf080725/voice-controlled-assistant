"""One bounded ASR candidate, independent of the microphone's continuous reader."""
import threading
import time

import numpy as np


class PendingVoiceWorker:
    """Keep at most one running or completed candidate until the hub consumes it.

    A completed answer remains available while the previous playback unwinds.
    The hub owns identity checks and dispatch; this worker never executes actions.
    """

    def __init__(self, recognize, *, clock=time.monotonic):
        self.recognize = recognize
        self.clock = clock
        self._lock = threading.Lock()
        self._request = None
        self._result = None

    def submit(self, audio, *, item, context, session, stats="", audio_generation=0, captured_at=None,
               acoustic_timing=None):
        audio = np.asarray(audio, np.float32).reshape(-1)
        if not 0 < len(audio) <= 26 * 16000 or not np.isfinite(audio).all():
            return False
        timing = None
        if isinstance(acoustic_timing, dict):
            try:
                wait = float(acoustic_timing["endpoint_wait_sec"])
                frame = float(acoustic_timing["frame_sec"])
                if 0 <= wait <= 30 and 0 < frame <= 1:
                    timing = {"endpoint_wait_sec": wait, "frame_sec": frame}
            except (KeyError, TypeError, ValueError, OverflowError):
                pass  # Bad timing invalidates the measurement, never the user's audio.
        with self._lock:
            if self._request is not None:
                return False
            request = {"audio": audio.copy(),
                       "item": item, "context": context, "session": session,
                       "audio_generation": audio_generation,
                       "captured_at": self.clock() if captured_at is None else captured_at,
                       "acoustic_timing": timing, "stats": stats}
            self._request = request

        def run():
            request["asr_started_at"] = self.clock()
            try:
                result = {**request, **(self.recognize(request) or {})}
            except Exception as exc:
                result = {**request, "error": type(exc).__name__}
            result["recognized_at"] = self.clock()
            with self._lock:
                if self._request is request:
                    self._result = result

        try:
            threading.Thread(target=run, daemon=True, name="pending-voice-asr").start()
        except Exception:
            with self._lock:
                if self._request is request:
                    self._request = None
            raise
        return True

    def peek(self):
        with self._lock:
            return self._result

    def discard(self, expected):
        with self._lock:
            if self._result is not expected:
                return False
            self._request = self._result = None
            return True

    @property
    def busy(self):
        with self._lock:
            return self._request is not None
