"""Temporary literal-body provenance for a server-validated message slot."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class _MessageSource:
    text: str
    contact: str
    body: str
    valid: object


_SOURCE = ContextVar("canaan_message_source", default=None)


@contextmanager
def _message_source_scope(text, contact, body, valid):
    if (not isinstance(text, str) or not isinstance(contact, str) or not contact.strip()
            or not isinstance(body, str) or not body.strip() or not callable(valid) or valid() is not True):
        raise ValueError("A currently validated server message source is required")
    token = _SOURCE.set(_MessageSource(text, contact, body, valid))
    try:
        yield
        if valid() is not True:
            raise RuntimeError("The server message source changed during validation")
    finally:
        _SOURCE.reset(token)


def _message_source_matches(message, text):
    source = _SOURCE.get()
    return bool(source is not None and message == source.body and text == source.text and source.valid() is True)
