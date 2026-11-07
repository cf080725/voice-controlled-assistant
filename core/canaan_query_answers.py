"""Server-only presentation snapshots. No execution or authority port.

The exact Python carrier is deliberately invisible to JSON. Restoring records is
only for a server-decrypted document; a record never grants execution authority.
"""
from contextvars import ContextVar
from dataclasses import dataclass
import json


DOCUMENT_BYTES = 2 * 1024 * 1024
PAGE_SCALARS = 1024
PAGE_JSON_BYTES = 8192
MAX_PAGES = 2048
REPLY_UTF16 = 4096
RECORD_SCHEMA = 'canaan.query.document.v2'
_SOURCES = frozenset(('run_cmd', 'system_status'))
_STOPS = frozenset(('completed', 'confirmation_required', 'failed', 'cancelled'))
_CURRENT_STEP = ContextVar('canaan_query_answer_step_v2', default=None)


class AnswerError(ValueError):
    pass


class IncompleteAnswers(AnswerError):
    pass


def canonical_json(value):
    _json_value(value)
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(',', ':'))
    except (TypeError, ValueError, UnicodeEncodeError):
        raise AnswerError('Finite Unicode JSON required') from None


def utf16_units(value):
    if type(value) is not str:
        raise AnswerError('Text required')
    try:
        return len(value.encode('utf-16-le')) // 2
    except UnicodeEncodeError:
        raise AnswerError('Invalid Unicode') from None


def _text(value, maximum, name, *, empty=False):
    if (type(value) is not str or (not empty and not value) or len(value) > maximum
            or '\x00' in value):
        raise AnswerError('Invalid ' + name)
    utf16_units(value)
    return value


def _json_value(value, depth=0, ancestors=None):
    if depth > 16:
        raise AnswerError('Payload depth exceeded')
    if value is None or type(value) in (bool, int, float):
        return
    if type(value) is str:
        _text(value, DOCUMENT_BYTES, 'JSON string', empty=True)
        return
    if type(value) not in (list, tuple, dict):
        raise AnswerError('Plain JSON values only')
    ancestors = set() if ancestors is None else ancestors
    marker = id(value)
    if marker in ancestors:
        raise AnswerError('Cyclic JSON')
    ancestors.add(marker)
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise AnswerError('String JSON keys required')
            for key in value:
                _text(key, 256, 'JSON key')
            values = value.values()
        else:
            values = value
        for item in values:
            _json_value(item, depth + 1, ancestors)
    finally:
        ancestors.remove(marker)


@dataclass(frozen=True)
class QueryAnswer:
    source: str
    kind: str
    target: str
    summary: str
    full_text: str
    payload_json: str
    summary_is_complete: bool


def issue_answer(*, source, kind, target, summary, full_text, payload, summary_is_complete):
    """Built-in formatter supplies parsed data, never reparsed message numbers."""
    if type(source) is not str or source not in _SOURCES:
        raise AnswerError('Built-in source required')
    if type(summary_is_complete) is not bool:
        raise AnswerError('Explicit summary coverage required')
    text = canonical_json(payload)
    if len(text.encode('utf-8')) > DOCUMENT_BYTES:
        raise AnswerError('Payload budget exceeded')
    return QueryAnswer(source, _text(kind, 80, 'kind'), _text(target, 256, 'target'),
                       _text(summary, 24, 'summary'),
                       _text(full_text, 65536, 'full_text'), text, summary_is_complete)


def _validate_answer(answer):
    if type(answer) is not QueryAnswer:
        raise AnswerError('Exact server answer required')
    try:
        payload = json.loads(answer.payload_json)
    except (TypeError, ValueError):
        raise AnswerError('Invalid snapshot payload') from None
    validated = issue_answer(source=answer.source, kind=answer.kind, target=answer.target,
                             summary=answer.summary, full_text=answer.full_text,
                             payload=payload, summary_is_complete=answer.summary_is_complete)
    if answer != validated:
        raise AnswerError('Noncanonical snapshot')


@dataclass(frozen=True)
class AnswerEntry:
    step: int
    answer: QueryAnswer


def _positions(values, maximum):
    if type(values) is not tuple or any(type(step) is not int or not 1 <= step <= maximum for step in values):
        raise AnswerError('Immutable 1-based positions required')
    if tuple(sorted(set(values))) != values:
        raise AnswerError('Positions must be strictly ordered')


def _validate_progress(steps, completed, expected, entries, stop):
    if type(steps) is not int or not 1 <= steps <= 32:
        raise AnswerError('Existing 32-step bound required')
    if type(completed) is not int or not 0 <= completed <= steps:
        raise AnswerError('Invalid contiguous completed prefix')
    _positions(expected, steps)
    if type(entries) is not tuple:
        raise AnswerError('Immutable answer entries required')
    positions = []
    for entry in entries:
        if type(entry) is not AnswerEntry:
            raise AnswerError('Exact answer entry required')
        if type(entry.step) is not int or not 1 <= entry.step <= steps:
            raise AnswerError('Invalid answer step')
        _validate_answer(entry.answer)
        positions.append(entry.step)
    if tuple(positions) != tuple(step for step in expected if step <= completed):
        raise IncompleteAnswers('Every completed query must retain exactly its ordered answer')
    if type(stop) is not str or stop not in _STOPS:
        raise AnswerError('Unknown presentation stop reason')
    if (stop == 'completed') != (completed == steps):
        raise AnswerError('Stop reason must match actual completed prefix')


@dataclass(frozen=True)
class QueryFragment:
    segment_steps: int
    completed_steps: int
    expected_query_steps: tuple
    entries: tuple
    stop_reason: str

    def __post_init__(self):
        _validate_progress(self.segment_steps, self.completed_steps, self.expected_query_steps,
                           self.entries, self.stop_reason)


@dataclass(frozen=True)
class QueryPage:
    index: int
    total: int
    text: str

    def to_record(self):
        return {'index': self.index, 'total': self.total, 'text': self.text}


@dataclass(frozen=True)
class QueryDocument:
    plan_steps: int
    completed_steps: int
    expected_query_steps: tuple
    entries: tuple
    stop_reason: str

    def __post_init__(self):
        _validate_progress(self.plan_steps, self.completed_steps, self.expected_query_steps,
                           self.entries, self.stop_reason)
        if not self.expected_query_steps:
            raise AnswerError('No-query plan has no query document')
        if len(self.canonical_record().encode('utf-8')) > DOCUMENT_BYTES:
            raise AnswerError('Document exceeds 2MiB; nothing truncated')

    def to_record(self):
        return {'schema': RECORD_SCHEMA, 'plan_steps': self.plan_steps,
                'completed_steps': self.completed_steps,
                'expected_query_steps': list(self.expected_query_steps),
                'stop_reason': self.stop_reason,
                'answers': [{'step': entry.step, 'source': entry.answer.source,
                             'kind': entry.answer.kind, 'target': entry.answer.target,
                             'summary': entry.answer.summary, 'full_text': entry.answer.full_text,
                             'payload': json.loads(entry.answer.payload_json),
                             'summary_is_complete': entry.answer.summary_is_complete}
                            for entry in self.entries]}

    as_data = to_record

    def canonical_record(self):
        return canonical_json(self.to_record())

    def summary_reply(self, maximum_utf16=REPLY_UTF16):
        if type(maximum_utf16) is not int or not 1 <= maximum_utf16 <= REPLY_UTF16:
            raise AnswerError('Invalid summary UTF16 budget')
        if not self.entries:
            return None
        text = '，'.join(entry.answer.summary.rstrip('。！？!?') for entry in self.entries) + '。'
        return text if utf16_units(text) <= maximum_utf16 else None

    def compact_reply(self, maximum_utf16=24):
        if any(not entry.answer.summary_is_complete for entry in self.entries):
            return None
        return self.summary_reply(maximum_utf16)

    def full_text(self):
        return '\n'.join(f'第{entry.step}项（{entry.answer.target}）：{entry.answer.full_text}'
                         for entry in self.entries)

    def pages(self, *, content='record'):
        """Read-only data chunks. Transport supplies all authenticated identities.

        Default pages reconstruct the complete canonical typed record. Text pages
        are optional presentation only and must not be confused with typed pages.
        """
        if content not in ('record', 'text'):
            raise AnswerError('Unknown page content')
        text = self.canonical_record() if content == 'record' else self.full_text()
        chunks = tuple(text[index:index + PAGE_SCALARS] for index in range(0, len(text), PAGE_SCALARS))
        if len(chunks) > MAX_PAGES:
            raise AnswerError('Page-count bound exceeded; nothing truncated')
        pages = tuple(QueryPage(index, len(chunks), chunk) for index, chunk in enumerate(chunks))
        for page in pages:
            encode_page(page)
        return pages


def encode_page(page, *, envelope=None):
    """Encode actual JSON and enforce bytes, including caller-supplied metadata.

    This helper validates size only. It never authenticates the envelope and must
    not be used instead of transport owner/client/task/revision/digest checks.
    """
    if (type(page) is not QueryPage or type(page.index) is not int or type(page.total) is not int
            or not 0 <= page.index < page.total <= MAX_PAGES):
        raise AnswerError('Invalid page position')
    _text(page.text, PAGE_SCALARS, 'page text')
    if envelope is None:
        record = page.to_record()
    else:
        if type(envelope) is not dict or set(envelope) & {'index', 'total', 'text'}:
            raise AnswerError('Plain non-overlapping page metadata required')
        record = {**envelope, **page.to_record()}
    raw = canonical_json(record).encode('utf-8')
    if len(raw) > PAGE_JSON_BYTES:
        raise AnswerError('Encoded page JSON exceeds 8192 bytes')
    return raw


def from_trusted_record(record):
    """ONLY after authenticated server decryption; never for request JSON."""
    keys = {'schema', 'plan_steps', 'completed_steps', 'expected_query_steps', 'stop_reason', 'answers'}
    if type(record) is not dict or set(record) != keys or record.get('schema') != RECORD_SCHEMA:
        raise AnswerError('Invalid trusted document record')
    if len(canonical_json(record).encode('utf-8')) > DOCUMENT_BYTES:
        raise AnswerError('Trusted record exceeds budget')
    if type(record['expected_query_steps']) is not list or type(record['answers']) is not list:
        raise AnswerError('Invalid trusted record arrays')
    entries = []
    for row in record['answers']:
        if type(row) is not dict or set(row) != {'step', 'source', 'kind', 'target', 'summary', 'full_text', 'payload', 'summary_is_complete'}:
            raise AnswerError('Invalid trusted answer record')
        values = {key: value for key, value in row.items() if key != 'step'}
        entries.append(AnswerEntry(row['step'], issue_answer(**values)))
    return QueryDocument(record['plan_steps'], record['completed_steps'],
                         tuple(record['expected_query_steps']), tuple(entries), record['stop_reason'])


restore_document = from_trusted_record


def merge_prefix(prefix, fragment):
    if type(fragment) is not QueryFragment:
        raise AnswerError('Exact current execution fragment required')
    if prefix is None:
        if not fragment.expected_query_steps:
            return None
        return QueryDocument(fragment.segment_steps, fragment.completed_steps,
                             fragment.expected_query_steps, fragment.entries, fragment.stop_reason)
    if type(prefix) is not QueryDocument or prefix.stop_reason != 'confirmation_required':
        raise AnswerError('Exact paused server prefix required')
    offset = prefix.completed_steps
    if fragment.segment_steps != prefix.plan_steps - offset:
        raise AnswerError('Continuation must cover the exact remaining suffix')
    shifted = tuple(step + offset for step in fragment.expected_query_steps)
    if shifted != tuple(step for step in prefix.expected_query_steps if step > offset):
        raise AnswerError('Continuation query positions changed')
    entries = prefix.entries + tuple(AnswerEntry(entry.step + offset, entry.answer) for entry in fragment.entries)
    return QueryDocument(prefix.plan_steps, offset + fragment.completed_steps,
                         prefix.expected_query_steps, entries, fragment.stop_reason)


class QueryExecutionResult(dict):
    __slots__ = ('_query_document',)

    def __init__(self, result, document):
        if type(document) not in (QueryFragment, QueryDocument):
            raise AnswerError('Exact server presentation object required')
        super().__init__(result)
        self._query_document = document


def get_query_document(result):
    if type(result) is not QueryExecutionResult:
        return None
    document = result._query_document
    return document if type(document) in (QueryFragment, QueryDocument) else None


def clone_execution_result(result, **overrides):
    data = {**result, **overrides}
    document = get_query_document(result)
    return QueryExecutionResult(data, document) if document is not None else data


class AnswerCollector:
    def __init__(self, *, segment_steps, expected_query_steps):
        expected = tuple(expected_query_steps)
        _validate_progress(segment_steps, 0, expected, (), 'confirmation_required')
        self.segment_steps, self.expected = segment_steps, expected
        self._completed, self._entries, self._active, self._closed = 0, [], False, False

    def step(self, step, canonical_action):
        if (self._closed or self._active or type(step) is not int
                or step != self._completed + 1 or not 1 <= step <= self.segment_steps):
            raise AnswerError('Invalid/repeated/out-of-order step')
        return _Step(self, step, canonical_action)

    def finish(self, stop_reason):
        if self._closed or self._active:
            raise AnswerError('Collector is active or closed')
        fragment = QueryFragment(self.segment_steps, self._completed, self.expected,
                                 tuple(self._entries), stop_reason)
        # Check a maximum-size full record before publishing even a partial result.
        if fragment.expected_query_steps:
            merge_prefix(None, fragment)
        self._closed = True
        return fragment


class _Step:
    def __init__(self, collector, step, action):
        self.collector, self.step, self.action = collector, step, action
        self.answer, self.committed, self.closed, self.token = None, False, False, None

    def __enter__(self):
        if (self.closed or self.token is not None or self.collector._closed or self.collector._active
                or self.step != self.collector._completed + 1):
            raise AnswerError('Step scope cannot be reused')
        self.collector._active = True
        self.token = _CURRENT_STEP.set(self)
        return self

    def publish(self, answer):
        if self.closed or self.committed or self.answer is not None:
            raise AnswerError('Late/duplicate snapshot')
        _validate_answer(answer)
        if answer.source != self.action or self.step not in self.collector.expected:
            raise AnswerError('Snapshot source/step mismatch')
        self.answer = answer

    def commit(self):
        if self.closed or self.committed or self.token is None:
            raise AnswerError('Active uncommitted step required')
        if self.step in self.collector.expected and self.answer is None:
            raise IncompleteAnswers('Successful query omitted its answer')
        self.committed = True

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None and self.committed:
                self.collector._completed += 1
                if self.answer is not None:
                    self.collector._entries.append(AnswerEntry(self.step, self.answer))
        finally:
            self.closed = True
            self.collector._active = False
            _CURRENT_STEP.reset(self.token)
        return False


def publish_answer(answer):
    if type(answer) is not QueryAnswer:
        return False
    scope = _CURRENT_STEP.get()
    if scope is None:
        return False
    scope.publish(answer)
    return True


class QuerySpeechError(AnswerError):
    """A typed native summary cannot be spoken without losing its provenance."""


_QUERY_SPEECH_SEAL = object()
_QUERY_SPEECH_REJECTED = '查询摘要暂时无法完整播报，完整文字已保留。'


class _QuerySpeech(str):
    __slots__ = ('_parts', '_prefix', '_tail', '_seal', '_rejected')

    def __new__(cls, *args, **kwargs):
        raise TypeError('Only the native query-speech factory may issue this value')

    def __setattr__(self, name, value):
        raise AttributeError('Native query-speech snapshots are immutable')

    def __delattr__(self, name):
        raise AttributeError('Native query-speech snapshots are immutable')


def _query_speech_value(display, parts, prefix, tail, *, rejected=False):
    value = str.__new__(_QuerySpeech, display)
    for name, item in (('_parts', parts), ('_prefix', prefix), ('_tail', tail),
                       ('_seal', _QUERY_SPEECH_SEAL), ('_rejected', rejected)):
        object.__setattr__(value, name, item)
    return value


def _query_speech_part(value):
    _text(value, 24, 'query-speech summary')
    if any(ord(char) < 32 for char in value) or not any(char.isalnum() for char in value):
        raise QuerySpeechError('A query-speech summary has no speakable content')
    return value


def native_query_speech(result, speech):
    """Wrap desktop speech only; JSON/shared replies remain ordinary strings.

    A rejected exact typed result stays a sealed value, so the native pipeline
    can fail inside its existing on_error/full-show ownership boundary. No
    document, payload, page or authority is retained in the speech value.
    """
    if type(result) is not QueryExecutionResult:
        return speech
    display = _QUERY_SPEECH_REJECTED
    try:
        _text(speech, REPLY_UTF16, 'native query display')
        if utf16_units(speech) > REPLY_UTF16:
            raise QuerySpeechError('Native query display exceeds UTF16 budget')
        display = speech
        typed = get_query_document(result)
        if typed is None:
            raise QuerySpeechError('Exact typed result omitted its query fragment')
        document = merge_prefix(None, typed) if type(typed) is QueryFragment else typed
        if document is None or not document.entries:
            return speech
        if type(document) is not QueryDocument:
            raise QuerySpeechError('Exact server query document required')
        document.__post_init__()
        if bool(result.get('success', True)) != (document.stop_reason == 'completed'):
            raise QuerySpeechError('Query result and document completion disagree')
        parts = tuple(_query_speech_part(entry.answer.summary) for entry in document.entries)
        if not 1 <= len(parts) <= 32:
            raise QuerySpeechError('Native query count exceeds existing plan bound')
        summary = document.summary_reply()
        if not summary:
            raise QuerySpeechError('Complete ordered query summary required')
        if result.get('success', True):
            compact = document.compact_reply(24)
            prefix, tail = ('' if compact else '查询摘要：'), None
            if speech != (compact or prefix + summary):
                raise QuerySpeechError('Native display does not match its captured queries')
        else:
            prefix = '已查询：'
            expected = prefix + summary
            if not speech.startswith(expected) or not speech[len(expected):].strip():
                raise QuerySpeechError('Stopped query display omitted its ordered summary or status')
            tail = speech[len(expected):]
        value = _query_speech_value(display, parts, prefix, tail)
        native_query_speech_parts(value)
        return value
    except (AnswerError, TypeError, ValueError, AttributeError):
        return _query_speech_value(display, (), '', None, rejected=True)


def native_query_speech_parts(value):
    """Read only the exact sealed snapshot; plain/lookalike strings get no scope."""
    if type(value) is not _QuerySpeech:
        return None
    try:
        if value._seal is not _QUERY_SPEECH_SEAL or type(value._rejected) is not bool or value._rejected:
            raise QuerySpeechError('Native query-speech snapshot was rejected')
        parts, prefix, tail = value._parts, value._prefix, value._tail
        if type(parts) is not tuple or not 1 <= len(parts) <= 32:
            raise QuerySpeechError('Immutable bounded query summaries required')
        for part in parts:
            _query_speech_part(part)
        if type(prefix) is not str or prefix not in ('', '查询摘要：', '已查询：'):
            raise QuerySpeechError('Unknown query-speech presentation')
        if prefix == '已查询：':
            _text(tail, REPLY_UTF16, 'query-speech status')
        elif tail is not None:
            raise QuerySpeechError('Successful query speech has no status tail')
        display = prefix + '，'.join(part.rstrip('。！？!?') for part in parts) + '。' + (tail or '')
        if display != str(value) or utf16_units(display) > REPLY_UTF16:
            raise QuerySpeechError('Native query-speech content changed or exceeded budget')
        return prefix, parts, tail
    except (AttributeError, TypeError, ValueError) as error:
        if isinstance(error, QuerySpeechError):
            raise
        raise QuerySpeechError('Invalid sealed query-speech value') from None
