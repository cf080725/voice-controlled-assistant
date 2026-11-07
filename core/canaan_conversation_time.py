"""Read conversation timestamps without guessing a repeated local clock hour."""
import math
import time
from datetime import datetime, timezone


def record_timestamps(record, *, clock=time):
    """Return every valid instant for a legacy wall time, or one explicit instant."""
    if not isinstance(record, dict):
        return ()
    if 'timestamp' in record:
        value = record['timestamp']
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ()
        try:
            stamp = float(value)
            if not math.isfinite(stamp):
                return ()
            datetime.fromtimestamp(stamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return ()
        return (stamp,)
    try:
        parsed = datetime.fromisoformat(record['time'])
        if parsed.tzinfo is not None:
            return (parsed.timestamp(),)
        wall = clock.strptime(record['time'], '%Y-%m-%d %H:%M:%S')
        candidates = set()
        for summer in (0, 1):
            try:
                stamp = clock.mktime((*wall[:8], summer))
                restored = clock.localtime(stamp)
            except (OverflowError, OSError, ValueError):
                continue
            if restored[:6] == wall[:6] and restored.tm_isdst == summer:
                candidates.add(stamp)
        return tuple(sorted(candidates))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return ()
