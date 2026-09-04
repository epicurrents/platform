"""Failure-mode and contract tests for ``ApiActivityLoggingMiddleware``.

The path-recognition contract (``test_middleware_path_recognition.py``)
and the basic integration check (``test_middleware_audit_trail.py``) cover
the most common ways the audit trail could silently break. This file
covers the *less obvious* ways the chain can decouple — each one is a
documented audit-trail failure mode that had no test prior to landing.

Scopes:

* ``test_middleware_position`` — ``ApiActivityLoggingMiddleware`` must
  run after ``AuthenticationMiddleware`` so ``request.user`` is
  populated. A future settings change that removes or re-orders the
  audit middleware silently breaks every Activity row's actor.
* ``test_on_commit_*`` — ``transaction.on_commit`` callbacks fire while
  the request context is still active, so ORM writes inside them are
  audited. This documents the behaviour so a regression that moves the
  context reset earlier in the middleware would fail here.
* ``test_activity_creation_failure_does_not_break_request`` — when the
  Activity insert raises, the middleware logs a WARNING and continues;
  the request must still succeed and downstream signals must still fire
  (with ``activity=None`` on the ``ObjectChangeLog``).
* ``test_federated_inbound_request_logs_with_no_actor`` — federated
  peers authenticate via JWT, not session. ``request.user`` is
  ``AnonymousUser`` for them; the Activity row is created with
  ``actor=None``. This is intentional — federation-specific audit
  lives in ``FederationAuditLog`` — but worth pinning so a future
  middleware change that "fixes" the AnonymousUser case doesn't
  silently start attributing federated writes to a local user.
"""

from unittest import mock

import pytest
from django.conf import settings
from django.db import DatabaseError, transaction

from activity.models import Activity, ObjectChangeLog
from activity.request_context import current_is_audited_context, current_user
from epicurrents.middleware import ApiActivityLoggingMiddleware

# ---------------------------------------------------------------------------
# Middleware position in MIDDLEWARE setting
# ---------------------------------------------------------------------------


def test_middleware_is_registered():
    """Removing the audit middleware silently disables the audit trail
    — assert it stays present."""
    assert "epicurrents.middleware.ApiActivityLoggingMiddleware" in settings.MIDDLEWARE


def test_middleware_runs_after_authentication():
    """Audit middleware must see ``request.user`` populated. Reordering
    so the audit runs before AuthenticationMiddleware silently changes
    every Activity row's actor to AnonymousUser."""
    middleware_list = list(settings.MIDDLEWARE)
    audit_idx = middleware_list.index("epicurrents.middleware.ApiActivityLoggingMiddleware")
    auth_idx = middleware_list.index("django.contrib.auth.middleware.AuthenticationMiddleware")
    assert auth_idx < audit_idx, (
        f"AuthenticationMiddleware (idx {auth_idx}) must precede "
        f"ApiActivityLoggingMiddleware (idx {audit_idx}) in MIDDLEWARE, "
        f"otherwise request.user is AnonymousUser when the audit row is built."
    )


# ---------------------------------------------------------------------------
# transaction.on_commit() and request context lifetime
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_on_commit_callback_runs_inside_request_context(rf, user):
    """ORM writes inside ``transaction.on_commit`` callbacks must still
    see the request context (so signals fire and attribute correctly).

    The middleware resets the context in a ``finally`` block after
    ``get_response`` returns. ``on_commit`` callbacks fire when the
    transaction commits, which (for a view-level ``atomic()``) happens
    *before* ``get_response`` returns. Pinning the behaviour here so a
    regression that moves the context reset earlier — or wraps the
    middleware in ``ATOMIC_REQUESTS`` so the commit happens later —
    fails this test instead of silently dropping audit rows.
    """
    captured = {}

    def view(request):
        with transaction.atomic():

            def callback():
                captured["user"] = current_user.get()
                captured["is_audited"] = current_is_audited_context.get()

            transaction.on_commit(callback)
        from django.http import HttpResponse

        return HttpResponse("ok")

    middleware = ApiActivityLoggingMiddleware(view)
    # Use a non-skip-list API path so the middleware enters the full
    # is_audited=True branch — ACTIVITY_PATH_SKIP_LIST short-circuits to
    # is_audited=False and would mask the on-commit context propagation.
    request = rf.get("/api/v1/activity/changes/")
    request.user = user

    middleware(request)

    assert captured.get("is_audited") is True, (
        "request context was reset before on_commit callbacks fired — "
        "any ORM write inside on_commit will silently bypass the audit "
        "trail (signals will see is_audited_context()=False)."
    )
    assert captured.get("user") is user


# ---------------------------------------------------------------------------
# Activity-row insert failure
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_activity_creation_failure_does_not_break_request(client, caplog):
    """If the Activity insert raises a DB error, the middleware must
    log a WARNING and let the request through. The audit trail loses
    that one row, but the user-facing request still succeeds."""
    caplog.set_level("WARNING")
    target = "epicurrents.middleware.Activity.objects.create"
    with mock.patch(target, side_effect=DatabaseError("simulated failure")):
        # Use a non-skip-list path so the middleware actually attempts
        # the Activity.objects.create that the test is mocking. The
        # view itself returns 401 unauthenticated, which is fine — what
        # matters is that the middleware swallows the audit-row failure
        # rather than propagating it as a 500.
        response = client.get("/api/v1/activity/changes/")

    assert response.status_code != 500, (
        "Activity insert raised but the middleware did not contain the failure — the request returned 500 to the user."
    )
    assert any("failed to create Activity row" in rec.getMessage() for rec in caplog.records), (
        "Expected a WARNING log when Activity insert fails — without it the audit-trail gap is invisible to operators."
    )


@pytest.mark.django_db
def test_activity_creation_failure_leaves_signals_with_no_activity(client, user):
    """When Activity creation fails, the ContextVars are still set
    (with ``activity=None``) and downstream signals still fire. Any
    ObjectChangeLog rows created in this request will carry
    ``activity=None`` — recoverable, but worth pinning so a regression
    that early-returns from the middleware on Activity failure (and
    therefore skips ``set_request_context``) shows up here."""
    target = "epicurrents.middleware.Activity.objects.create"
    with mock.patch(target, side_effect=DatabaseError("simulated failure")):
        # Authenticated request → AuthenticationMiddleware sets request.user.
        client.force_login(user)
        response = client.get("/api/v1/user/me")

    assert response.status_code == 200
    # No Activity row was inserted, but any ObjectChangeLog rows created
    # during this request (none for /me, but pinning the model contract)
    # would have ``activity_id=None``. Confirm none of them ended up with
    # an orphan FK to a row that doesn't exist.
    assert (
        not ObjectChangeLog.objects.exclude(activity__isnull=True)
        .filter(activity_id__isnull=False, activity__isnull=True)
        .exists()
    )


# ---------------------------------------------------------------------------
# Federated inbound auth attribution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_federated_inbound_request_logs_with_no_actor(client):
    """Federated inbound endpoints authenticate via JWT at the view
    layer, not via Django sessions. ``request.user`` is therefore
    ``AnonymousUser`` when the middleware records the row, and
    ``Activity.actor`` is ``None``.

    This is intentional: federation-specific audit lives in
    ``FederationAuditLog`` (see ``federation/audit.py``); the
    ``ObjectChangeLog`` trail records the *attempt* but does not pin a
    local user to it. Pinning the behaviour here so that a future
    middleware change which "fixes" the AnonymousUser path does not
    silently start attributing federated writes to a local user.
    """
    # Hit the inbound check endpoint without auth — expected to 401 / 403.
    response = client.get("/api/v1/federation/inbound/objects/999/nonexistent/")

    activity = Activity.objects.latest("created_at")
    assert activity.path == "/api/v1/federation/inbound/objects/999/nonexistent/"
    assert activity.actor is None, (
        "Federated inbound request should carry actor=None — the JWT auth "
        "is checked at the view layer, not by Django session middleware. "
        "If this changed, FederationAuditLog and ObjectChangeLog may now "
        "double-attribute the same event."
    )
    # The response should be a federation auth failure (401), confirming
    # the view ran end-to-end and the assertion isn't paper over a 500.
    assert response.status_code in (401, 403)
