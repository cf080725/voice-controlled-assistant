"""A repeated local clock hour must not resurrect expired conversation context."""
import json
import time as real_time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import canaan_memory as memory
import canaan_retention as retention


TRANSITION = datetime(2026, 10, 25, 1, tzinfo=timezone.utc).timestamp()
NOW = datetime(2026, 10, 25, 2, 45, tzinfo=timezone.utc).timestamp()
EARLY = datetime(2026, 10, 25, 0, 15, tzinfo=timezone.utc).timestamp()
LATE = EARLY + 3600


@pytest.fixture
def berlin_clock(monkeypatch):
    def localtime(stamp):
        summer = int(stamp < TRANSITION)
        dt = datetime.fromtimestamp(stamp, timezone(timedelta(hours=1 + summer)))
        return real_time.struct_time((*dt.timetuple()[:8], summer))
    def mktime(parts):
        # Reproduce Windows choosing standard time for an ambiguous naive value.
        summer = int(parts[8] == 1)
        return datetime(*parts[:6], tzinfo=timezone(timedelta(hours=1 + summer))).timestamp()
    clock = SimpleNamespace(time=lambda: NOW, strptime=real_time.strptime,
                            localtime=localtime, mktime=mktime)
    monkeypatch.setattr(memory, 'time', clock)
    monkeypatch.setattr(retention, 'time', clock)
    return clock


def write_record(tmp_path, **fields):
    path = tmp_path / 'conversation.jsonl'
    row = {'time': '2026-10-25 02:15:00', 'user': 'fixture', 'action': 'chat', **fields}
    path.write_text(json.dumps(row) + '\n', encoding='utf-8')
    return str(path)


def test_ambiguous_legacy_hour_cannot_make_expired_context_recent(tmp_path, berlin_clock):
    path = write_record(tmp_path)
    assert memory.recent_conversation(path, now=NOW, within=7200) == []


def test_ambiguous_legacy_hour_cannot_cross_a_forget_boundary(tmp_path, berlin_clock):
    path = write_record(tmp_path)
    assert memory.recent_conversation(path, now=NOW, within=10800, not_before=EARLY + 1800) == []


@pytest.mark.parametrize('stamp, expected', [(EARLY, False), (LATE, True), (NOW + 60, False)])
def test_explicit_epoch_disambiguates_the_repeated_hour(tmp_path, berlin_clock, stamp, expected):
    path = write_record(tmp_path, timestamp=stamp)
    assert bool(memory.recent_conversation(path, now=NOW, within=7200)) is expected


@pytest.mark.parametrize('stamp', ['invalid', float('nan'), float('inf'), True])
def test_invalid_explicit_epoch_is_not_replaced_by_a_guessed_local_time(tmp_path, berlin_clock, stamp):
    path = write_record(tmp_path, time='2026-10-25 03:40:00', timestamp=stamp)
    assert memory.recent_conversation(path, now=NOW) == []


@pytest.mark.parametrize('text', ['2026-10-25T02:15:00+01:00', '2026-10-25T01:15:00Z'])
def test_explicit_iso_offset_is_read_without_local_time_guessing(tmp_path, berlin_clock, text):
    path = write_record(tmp_path, time=text)
    assert memory.recent_conversation(path, now=NOW, within=7200)


def test_ordinary_legacy_time_stays_readable(tmp_path, berlin_clock):
    path = write_record(tmp_path, time='2026-10-25 03:40:00')
    assert memory.recent_conversation(path, now=NOW, within=7200)


def test_retention_uses_the_explicit_epoch_too(tmp_path, berlin_clock):
    path = write_record(tmp_path, time='2026-10-25 03:40:00', timestamp=EARLY - 86400)
    assert retention.purge_conversations(path, days=1, now=NOW, cipher=None) == (0, 1)


@pytest.mark.parametrize('stamp', [10**500, 1e300])
def test_unrepresentable_epoch_cannot_break_retention(tmp_path, berlin_clock, stamp):
    path = write_record(tmp_path, timestamp=stamp)
    assert retention.purge_conversations(path, days=1, now=NOW, cipher=None) == (0, 1)
