"""Tests for the global API request-rate throttle (epicurrents.throttle).

Cover the gating, the NAT-safe identity resolution and its priority order, the
per-scope limits and IP backstop, the fail-open contract, and the middleware's
429 translation.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, override_settings

from epicurrents import throttle
from epicurrents.middleware import ApiThrottleMiddleware

RATES = {"default": 2, "upload": 1}
SCOPE_MAP = (("/recordings/api/v1/upload", "upload"),)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def rf():
    return RequestFactory()


def _api_get(rf, path="/api/v1/x", **params):
    request = rf.get(path, params)
    request.user = AnonymousUser()
    request.session = SimpleNamespace(session_key=None)
    return request


@override_settings(API_THROTTLE_ENABLED=False, API_THROTTLE_RATES=RATES)
def test_disabled_is_noop(rf):
    request = _api_get(rf)
    for _ in range(10):
        assert throttle.check_request_throttle(request) is None


@override_settings(API_THROTTLE_ENABLED=True, API_THROTTLE_RATES=RATES, API_THROTTLE_IP_RATE=5)
def test_non_api_path_is_ignored(rf):
    request = _api_get(rf, path="/viewer/index.html")
    for _ in range(10):
        assert throttle.check_request_throttle(request) is None


@override_settings(API_THROTTLE_ENABLED=True, API_THROTTLE_RATES=RATES, API_THROTTLE_IP_RATE=0)
def test_user_within_then_over_limit(rf, django_user_model):
    user = django_user_model.objects.create_user(username="t1", password="x")
    request = _api_get(rf)
    request.user = user
    # default scope limit is 2: first two pass, the third trips.
    assert throttle.check_request_throttle(request) is None
    assert throttle.check_request_throttle(request) is None
    retry = throttle.check_request_throttle(request)
    assert isinstance(retry, int) and 0 < retry <= 60


@override_settings(
    API_THROTTLE_ENABLED=True, API_THROTTLE_RATES=RATES, API_THROTTLE_SCOPE_MAP=SCOPE_MAP, API_THROTTLE_IP_RATE=0
)
def test_upload_scope_has_its_own_tighter_limit(rf, django_user_model):
    user = django_user_model.objects.create_user(username="t2", password="x")
    request = rf.post("/recordings/api/v1/upload")
    request.user = user
    request.session = SimpleNamespace(session_key=None)
    # upload limit is 1: first passes, second trips. A different scope for the
    # same user is counted independently.
    assert throttle.check_request_throttle(request) is None
    assert isinstance(throttle.check_request_throttle(request), int)
    other = _api_get(rf)
    other.user = user
    assert throttle.check_request_throttle(other) is None


@override_settings(API_THROTTLE_ENABLED=True, API_THROTTLE_RATES=RATES, API_THROTTLE_IP_RATE=0)
def test_identity_priority_user_over_token_over_session(rf, django_user_model):
    user = django_user_model.objects.create_user(username="t3", password="x")
    request = rf.get("/api/v1/x", {"share_token": "tok"})
    request.user = user
    request.session = SimpleNamespace(session_key="sess")
    assert throttle._identity(request) == ("user", str(user.pk))

    request.user = AnonymousUser()
    assert throttle._identity(request)[0] == "token"

    request = _api_get(rf)
    request.session = SimpleNamespace(session_key="sess")
    kind, identity = throttle._identity(request)
    assert kind == "session"
    # The session key is a bearer credential — the identity component must be
    # its hash, never the raw key.
    assert identity != "sess"
    assert identity == hashlib.sha256(b"sess").hexdigest()[:32]


@override_settings(API_THROTTLE_ENABLED=True, API_THROTTLE_RATES=RATES, API_THROTTLE_IP_RATE=2)
def test_ip_backstop_uses_its_own_ceiling(rf):
    # Anonymous, no token, no session → keyed on IP against API_THROTTLE_IP_RATE.
    request = _api_get(rf)
    assert throttle._identity(request)[0] == "ip"
    assert throttle.check_request_throttle(request) is None
    assert throttle.check_request_throttle(request) is None
    assert isinstance(throttle.check_request_throttle(request), int)


@override_settings(API_THROTTLE_ENABLED=True, API_THROTTLE_RATES=RATES, API_THROTTLE_IP_RATE=0)
def test_ip_tier_disabled_when_rate_zero(rf):
    request = _api_get(rf)
    for _ in range(10):
        assert throttle.check_request_throttle(request) is None


@override_settings(API_THROTTLE_ENABLED=True, API_THROTTLE_RATES=RATES, API_THROTTLE_IP_RATE=5)
def test_fails_open_on_cache_error(rf):
    request = _api_get(rf)
    with patch.object(throttle.cache, "add", side_effect=RuntimeError("redis down")):
        assert throttle.check_request_throttle(request) is None


@override_settings(API_THROTTLE_ENABLED=True, API_THROTTLE_RATES={"default": 1}, API_THROTTLE_IP_RATE=0)
def test_middleware_returns_429_with_retry_after(rf, django_user_model):
    user = django_user_model.objects.create_user(username="t4", password="x")

    def get_response(request):
        return SimpleNamespace(status_code=200)

    mw = ApiThrottleMiddleware(get_response)

    def build():
        request = _api_get(rf)
        request.user = user
        return request

    assert mw(build()).status_code == 200
    blocked = mw(build())
    assert blocked.status_code == 429
    assert int(blocked["Retry-After"]) > 0
