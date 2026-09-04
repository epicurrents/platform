"""Contract tests for the dicom API's session-CSRF conformance.

The dicom write endpoints authenticate via the session cookie, so they must
route through the shared ``enforce_session_csrf`` chokepoint by way of
``_require_auth`` — the same contract the core apps follow. These pin that
``_require_auth`` rejects an unsafe tokenless session write, passes safe
methods through, and still rejects anonymous callers.
"""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings
from ninja.errors import HttpError

from plugins.dicom.urls import _require_auth

_HASH = "A" * 64


@pytest.fixture
def rf():
    return RequestFactory()


@override_settings(SESSION_CSRF_ENFORCED=True)
@pytest.mark.parametrize("method", ["post", "delete"])
def test_require_auth_rejects_unsafe_write_without_csrf_token(rf, user, method):
    # RequestFactory does not set the test-client CSRF exemption, so the check
    # runs for real; an authenticated session write without a token is 403.
    request = getattr(rf, method)(f"/plugin/dicom/api/v1/dicom/studies/{_HASH}/")
    request.user = user
    with pytest.raises(HttpError) as exc:
        _require_auth(request)
    assert exc.value.status_code == 403


@override_settings(SESSION_CSRF_ENFORCED=True)
def test_require_auth_allows_safe_method(rf, user):
    request = rf.get("/plugin/dicom/api/v1/dicom/studies/")
    request.user = user
    assert _require_auth(request) is user


@override_settings(SESSION_CSRF_ENFORCED=True)
def test_require_auth_rejects_anonymous(rf):
    request = rf.post("/plugin/dicom/api/v1/dicom/upload/")
    request.user = AnonymousUser()
    with pytest.raises(HttpError) as exc:
        _require_auth(request)
    assert exc.value.status_code == 401
