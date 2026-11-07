"""Context-local execution revocation checks; no transport, model or actor.

Validators are trusted local zero-argument callables. They authorize only when
they return the literal singleton True. A scope never grants actor permission:
the registry still performs the actor's normal authorization and readback.
"""
from contextlib import contextmanager
from contextvars import ContextVar


_validators = ContextVar("canaan_execution_validators", default=())


@contextmanager
def execution_guard(validator):
    """Add one revocation condition for this synchronous/async context.

    Nested scopes are ANDed. ContextVar propagation follows Python's normal
    task/context rules; an unrelated thread does not inherit this scope.
    This is boundary revalidation, not an atomic lock around an OS side effect.
    """
    if not callable(validator):
        raise TypeError("An execution validator must be callable")
    token = _validators.set(_validators.get() + (validator,))
    try:
        yield
    finally:
        _validators.reset(token)


def execution_allowed():
    """Fail closed on revocation, a non-True validator result or an exception."""
    try:
        from canaan_user_context import execution_cancel_event
        cancellation = execution_cancel_event()
        if cancellation is not None and cancellation.is_set() is not False:
            return False
        for validator in _validators.get():
            if validator() is not True:
                return False
        return True
    except BaseException:
        # In particular, a cancelled validator must never authorize execution.
        return False
