"""Bounded confidence from tokens that cover one top-level JSON string value."""
import json
import math


_MAX_BYTES = 65536
_MAX_TOKENS = 16384
_MAX_DEPTH = 64
_WHITESPACE = ' \t\r\n'


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Ambiguous duplicate JSON key')
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Nonfinite JSON number')
    return number


def _invalid_constant(value):
    raise ValueError('Nonstandard JSON constant')


def _skip_space(content, offset):
    while offset < len(content) and content[offset] in _WHITESPACE:
        offset += 1
    return offset


def _bounded_nesting(content):
    depth, quoted, escaped = 0, False, False
    for character in content:
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in '{[':
            depth += 1
            if depth > _MAX_DEPTH:
                return False
        elif character in '}]':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def _value_span(content, field):
    if not _bounded_nesting(content):
        return None
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object,
        parse_float=_finite_float, parse_constant=_invalid_constant)
    root = decoder.decode(content)
    if type(root) is not dict or type(root.get(field)) is not str or not root[field]:
        return None
    offset = _skip_space(content, 0) + 1  # The complete decoded root is an object.
    while True:
        offset = _skip_space(content, offset)
        if content[offset] == '}':
            return None
        key, offset = decoder.raw_decode(content, offset)
        offset = _skip_space(content, offset) + 1  # Validated colon.
        start = _skip_space(content, offset)
        _, offset = decoder.raw_decode(content, start)
        if key == field:
            return start + 1, offset - 1  # Exclude quotes, retain raw escapes.
        offset = _skip_space(content, offset)
        if content[offset] == '}':
            return None
        offset += 1  # Validated comma.


def _token_bytes(token):
    raw = token.get('bytes')
    if raw is not None:
        if type(raw) is not list or len(raw) > _MAX_BYTES:
            raise ValueError('Invalid token bytes')
        if any(type(value) is not int or not 0 <= value <= 255 for value in raw):
            raise ValueError('Invalid token byte')
        return bytes(raw)
    text = token.get('token')
    if type(text) is not str or len(text) > _MAX_BYTES:
        raise ValueError('Invalid token text')
    raw = text.encode('utf-8')
    if len(raw) > _MAX_BYTES:
        raise ValueError('Oversized token')
    return raw


def action_confidence(content, logprobs, field='a'):
    """Return the existing token-probability product, or None without evidence.

    Token bytes must match the output prefix and cover the entire raw field
    value. Raw API bytes take precedence over printable token text, which can
    contain replacement characters when a UTF-8 character spans tokens. Data
    after the complete value is unnecessary, including trailing EOS tokens.
    """
    if type(content) is not str or not content or len(content) > _MAX_BYTES:
        return None
    if type(field) is not str or not field or len(field) > 128:
        return None
    if type(logprobs) not in (list, tuple) or not 0 < len(logprobs) <= _MAX_TOKENS:
        return None
    try:
        wire = content.encode('utf-8')
        if len(wire) > _MAX_BYTES:
            return None
        span = _value_span(content, field)
        if span is None:
            return None
        start, end = (len(content[:offset].encode('utf-8')) for offset in span)
        position, total = 0, 0.0
        for token in logprobs:
            if type(token) is not dict:
                return None
            raw = _token_bytes(token)
            stop = position + len(raw)
            covered_end = min(stop, end)
            if raw[:covered_end - position] != wire[position:covered_end]:
                return None
            if stop > start and position < end:
                probability = token.get('logprob')
                if type(probability) not in (int, float) or not math.isfinite(probability) or probability > 0:
                    return None
                total += probability
            position = stop
            if position >= end:
                return math.exp(total)
        return None
    except (ValueError, TypeError, ArithmeticError, UnicodeError, RecursionError):
        return None
