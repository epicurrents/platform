"""Contract tests for the session-CSRF chokepoint (epicurrents.auth).

These pin the behaviour the load-bearing docstring promises: safe methods and
the kill switch are no-ops, an enforced unsafe request without a token is
rejected, and the Django test client's CSRF exemption keeps the wider suite
unaffected.
"""

import pytest
from django.test import RequestFactory, override_settings
from ninja.errors import HttpError

from epicurrents.auth import enforce_session_csrf


@pytest.fixture
def rf():
    return RequestFactory()


@override_settings(SESSION_CSRF_ENFORCED=True)
def test_safe_method_is_noop(rf):
    # A GET never carries CSRF semantics, so it passes regardless of token.
    request = rf.get("/recordings/api/v1/")
    assert enforce_session_csrf(request) is None


@override_settings(SESSION_CSRF_ENFORCED=True)
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_unsafe_method_without_token_is_rejected(rf, method):
    # RequestFactory does not set the test-client CSRF exemption flag, so the
    # check runs for real; with no token attached it must raise 403.
    request = getattr(rf, method)("/recordings/api/v1/")
    with pytest.raises(HttpError) as exc:
        enforce_session_csrf(request)
    assert exc.value.status_code == 403


@override_settings(SESSION_CSRF_ENFORCED=False)
def test_kill_switch_disables_enforcement(rf):
    # With the master switch off, even an unsafe tokenless request passes.
    request = rf.post("/recordings/api/v1/")
    assert enforce_session_csrf(request) is None


@override_settings(SESSION_CSRF_ENFORCED=True)
def test_test_client_is_exempt(client, django_user_model):
    # The Django test client defaults to enforce_csrf_checks=False, which sets
    # request._dont_enforce_csrf_checks; check_csrf honours it, so a logged-in
    # client write succeeds without a token even when enforcement is on. This
    # is what keeps the rest of the suite green.
    user = django_user_model.objects.create_user(username="csrf_probe", password="x")
    client.force_login(user)
    request = RequestFactory().post("/")
    request._dont_enforce_csrf_checks = True
    assert enforce_session_csrf(request) is None
