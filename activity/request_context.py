"""Per-request context storage for the activity-logging subsystem.

⚠️ LOAD-BEARING — middleware ↔ signals bridge.
The ContextVar contract (defaults, set/reset shape) is what lets
``epicurrents/middleware.py`` (and ``activity.system_activity``) hand
audited-scope state to ``activity/signals.py``. Breaking the contract on
either side decouples them — same end result as breaking either side
individually: audit signals fire with no Activity / user attribution, or
don't fire at all. See AGENTS.md → *Load-bearing files* before modifying.
The middleware integration test in
``epicurrents/tests/test_middleware_audit_trail.py`` covers the chain
end-to-end.

Audit signals (``activity/signals.py``) fire deep inside ORM ``save``/``delete``
calls and have no direct access to the originating caller. To bridge that
gap, the entry-point layer (``ApiActivityLoggingMiddleware`` for HTTP and
``with_system_activity`` for Celery tasks / management commands) stashes
the active user, the current ``Activity`` row, and a flag indicating whether
the calling scope is *audited* into the module-level ``ContextVar``\\s
defined below. Signals then read those values via ``get_current_user`` /
``get_current_activity``.

``current_is_audited_context`` is the gate that decides whether destructive
ORM writes are audited. Audited scopes are those wrapped by an entry-point
layer that has created a parent ``Activity`` row — HTTP requests through the
middleware, and explicit non-request scopes opened with
``with_system_activity``. Writes outside an audited scope (raw Celery
tasks that haven't opened a scope, the shell, ad-hoc scripts) are still
skipped — the audit trail is opt-in for non-request callers and the
entry-point helper is the opt-in mechanism.

``current_change_logging_suppressed`` is a separate flag used by rollback
execution to silence the auto-logging signals while it replays prior state
(otherwise restoring an object would itself generate a change-log entry).

``ContextVar`` (rather than thread-local storage) is used so the values flow
correctly through ``async`` views and Celery's request-scoped coroutines.
"""

from contextvars import ContextVar

current_user = ContextVar("current_user", default=None)
current_activity = ContextVar("current_activity", default=None)
current_is_audited_context = ContextVar("current_is_audited_context", default=False)
current_change_logging_suppressed = ContextVar("current_change_logging_suppressed", default=False)


def set_request_context(*, user=None, activity=None, is_audited=False):
    """Store per-scope context values and return reset tokens."""

    user_token = current_user.set(user)
    activity_token = current_activity.set(activity)
    audited_token = current_is_audited_context.set(is_audited)
    return user_token, activity_token, audited_token


def reset_request_context(tokens):
    """Reset context variables using previously captured tokens."""

    user_token, activity_token, audited_token = tokens
    current_user.reset(user_token)
    current_activity.reset(activity_token)
    current_is_audited_context.reset(audited_token)


def get_current_user():
    """Return the active user from context-local storage."""

    return current_user.get()


def get_current_activity():
    """Return the active Activity from context-local storage."""

    return current_activity.get()


def is_audited_context() -> bool:
    """Return True when the active scope opts into audit-trail writes.

    True inside an HTTP API request (set by ``ApiActivityLoggingMiddleware``)
    or inside a ``with_system_activity`` block (Celery tasks, management
    commands that explicitly opt in). False for ad-hoc shell / script
    contexts, where audit writes are deliberately suppressed.
    """

    return bool(current_is_audited_context.get())


def set_change_logging_suppressed(value: bool):
    """Temporarily suppress automatic signal-based change logging."""

    return current_change_logging_suppressed.set(bool(value))


def reset_change_logging_suppressed(token):
    """Reset suppression flag using token from set_change_logging_suppressed."""

    current_change_logging_suppressed.reset(token)


def is_change_logging_suppressed() -> bool:
    """Return True when auto change logging is suppressed in this context."""

    return bool(current_change_logging_suppressed.get())
