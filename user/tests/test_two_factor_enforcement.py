"""Tests for requiring a second factor on password login.

Two settings, both defaulting off, resolving as
``FOR_ALL or (FOR_STAFF and (is_staff or is_superuser))``.

The property that makes them safe to turn on is that an account with no factor
is not refused but redirected into enrolment. Every other enrolment endpoint
sits behind ``_require_auth``, so enrolling needs a session while enforcement
means withholding it; without the pre-session enrolment step, switching either
setting on would lock out exactly the accounts it was meant to protect — and
under ``FOR_ALL``, all of them at once. Most of what follows is about that.
"""

import json
import time

import pytest
from django.test import override_settings

from user.models import TwoFactorCredential
from user.two_factor import code_at, generate_secret

LOGIN = "/api/v1/user/login"
SETUP = "/api/v1/user/login/2fa/setup"
VERIFY = "/api/v1/user/login/2fa"
PASSWORD = "pw-for-2fa-tests-9271"


def _login(client, username, password=PASSWORD):
    return client.post(
        LOGIN,
        json.dumps({"username": username, "password": password}),
        content_type="application/json",
    )


def _verify(client, code):
    return client.post(VERIFY, json.dumps({"code": code}), content_type="application/json")


@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_user(username="staffer", password=PASSWORD, is_staff=True)


@pytest.fixture
def plain(django_user_model):
    return django_user_model.objects.create_user(username="plain", password=PASSWORD)


@pytest.mark.django_db
class TestResolution:
    """`FOR_ALL` dominates; `FOR_STAFF` is scoped; both off means off."""

    def test_off_by_default_a_staff_account_signs_in_with_a_password(self, client, staff):
        body = _login(client, "staffer").json()
        assert body["authenticated"] is True
        assert body["two_factor_enrolment_required"] is False

    @override_settings(TWO_FACTOR_REQUIRED_FOR_STAFF=True)
    def test_for_staff_stops_a_staff_account(self, client, staff):
        body = _login(client, "staffer").json()
        assert body["authenticated"] is False
        assert body["two_factor_enrolment_required"] is True

    @override_settings(TWO_FACTOR_REQUIRED_FOR_STAFF=True)
    def test_for_staff_leaves_a_non_staff_account_alone(self, client, plain):
        assert _login(client, "plain").json()["authenticated"] is True

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_for_all_covers_a_non_staff_account(self, client, plain):
        assert _login(client, "plain").json()["two_factor_enrolment_required"] is True

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_for_all_covers_staff_without_for_staff_being_set(self, client, staff):
        # The two settings compose rather than conflict: FOR_ALL includes staff,
        # so the combination is redundant and not contradictory.
        assert _login(client, "staffer").json()["two_factor_enrolment_required"] is True

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_an_account_with_no_usable_password_is_exempt(self, client, django_user_model):
        # Externally authenticated accounts cannot enrol at all: every enrolment
        # endpoint re-confirms the password. Requiring a factor of them would
        # lock them out of a platform they have no route back into.
        from user.two_factor import two_factor_required

        user = django_user_model.objects.create_user(username="sso-user")
        user.set_unusable_password()
        user.save()
        assert two_factor_required(user) is False

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_a_wrong_password_is_still_just_a_wrong_password(self, client, plain):
        # Enforcement must not become an oracle: a failed password looks the
        # same whether or not the account would have been asked to enrol.
        assert _login(client, "plain", "wrong").status_code == 401


@pytest.mark.django_db
class TestEnrolmentWithoutASession:
    """The circularity-breaking path: enrol on the strength of the pending marker."""

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_a_password_alone_opens_no_session(self, client, plain):
        _login(client, "plain")
        assert client.session.get("_auth_user_id") is None

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_setup_needs_the_pending_marker(self, client):
        # Without a preceding password step there is no identity to enrol.
        assert client.post(SETUP).status_code == 401

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_setup_mints_a_secret_after_the_password_step(self, client, plain):
        _login(client, "plain")
        resp = client.post(SETUP)
        assert resp.status_code == 200
        body = resp.json()
        assert body["secret"]
        assert body["provisioning_uri"].startswith("otpauth://totp/")

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_the_whole_flow_ends_in_a_session_and_backup_codes(self, client, plain):
        _login(client, "plain")
        secret = client.post(SETUP).json()["secret"]
        body = _verify(client, code_at(secret, int(time.time()))).json()
        assert body["authenticated"] is True
        assert len(body["backup_codes"]) > 0
        assert client.session.get("_auth_user_id") == str(plain.pk)

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_the_credential_is_unconfirmed_until_a_code_proves_it(self, client, plain):
        # A one-step enrolment would let a mistyped or unscanned secret lock the
        # account out of the login it was meant to unlock.
        _login(client, "plain")
        client.post(SETUP)
        assert TwoFactorCredential.objects.get(user=plain).confirmed_at is None

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_a_wrong_code_does_not_confirm_or_sign_in(self, client, plain):
        _login(client, "plain")
        client.post(SETUP)
        assert _verify(client, "000000").status_code == 401
        assert TwoFactorCredential.objects.get(user=plain).confirmed_at is None
        assert client.session.get("_auth_user_id") is None

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_the_second_login_is_an_ordinary_challenge(self, client, plain):
        _login(client, "plain")
        secret = client.post(SETUP).json()["secret"]
        _verify(client, code_at(secret, int(time.time())))
        client.logout()

        body = _login(client, "plain").json()
        assert body["two_factor_required"] is True
        assert body["two_factor_enrolment_required"] is False


@pytest.mark.django_db
class TestSetupIsNotAWeakerEnrolmentRoute:
    """The endpoint must not become a way around the password re-confirmation."""

    def test_no_marker_exists_at_all_when_enforcement_is_off(self, client, plain):
        # With both settings off, POST /login opens a session outright, so the
        # endpoint is unreachable for want of a pending marker.
        _login(client, "plain")
        assert client.post(SETUP).status_code == 401

    def test_refused_when_enforcement_stops_applying_mid_flow(self, client, plain):
        # The realistic way to hold a marker without enforcement: an operator
        # turns the setting off between the password step and the setup call.
        # The guard has to be the thing that refuses, which the previous test
        # cannot show — there, the 401 for a missing marker would mask its
        # absence entirely.
        with override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True):
            _login(client, "plain")
        resp = client.post(SETUP)
        assert resp.status_code == 403
        assert not TwoFactorCredential.objects.filter(user=plain).exists()

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_refused_when_the_account_already_has_a_factor(self, client, plain):
        secret = generate_secret()
        TwoFactorCredential.objects.create(user=plain, secret=secret, confirmed_at="2026-01-01T00:00:00Z")
        _login(client, "plain")
        assert client.post(SETUP).status_code == 409

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_a_code_lockout_is_not_escapable_by_re_enrolling(self, client, plain):
        # Otherwise the lockout is a delay rather than a limit: mint a fresh
        # secret, and the failed-attempt budget resets with it.
        from django.core.cache import cache

        _login(client, "plain")
        client.post(SETUP)
        cache.set(f"login_2fa_lockout:{plain.pk}", 1, timeout=300)
        try:
            assert client.post(SETUP).status_code == 429
        finally:
            cache.delete(f"login_2fa_lockout:{plain.pk}")

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_the_marker_does_not_survive_into_the_session(self, client, plain):
        _login(client, "plain")
        secret = client.post(SETUP).json()["secret"]
        _verify(client, code_at(secret, int(time.time())))
        assert "pending_two_factor" not in client.session


@pytest.mark.django_db
class TestTheCodeStepDoesNotActivateAStrayEnrolment:
    """An unconfirmed credential must not be activated by a login.

    A user can start enrolment through the ordinary route — which re-confirms
    their password — and abandon it, leaving an unconfirmed credential behind.
    ``active_credential`` does not return it, so the login code step treats such
    an account as enrolling. Without a check that enforcement actually requires
    a factor, a later password login would confirm that half-finished enrolment
    and hand out backup codes, reaching the same credential by a path that never
    re-confirmed the password.
    """

    def test_a_stray_unconfirmed_credential_is_not_confirmed_by_logging_in(self, client, plain):
        secret = generate_secret()
        TwoFactorCredential.objects.create(user=plain, secret=secret, confirmed_at=None)

        # Enforcement off: the password alone is enough, and the stray row must
        # not turn the login into an enrolment.
        body = _login(client, "plain").json()
        assert body["authenticated"] is True
        assert body["backup_codes"] is None
        assert TwoFactorCredential.objects.get(user=plain).confirmed_at is None

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_the_code_step_refuses_a_stray_credential_when_enforcement_lapses(self, client, plain):
        secret = generate_secret()
        TwoFactorCredential.objects.create(user=plain, secret=secret, confirmed_at=None)
        _login(client, "plain")

        # Enforcement withdrawn between the password step and the code step.
        with override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=False):
            resp = _verify(client, code_at(secret, int(time.time())))
        assert resp.status_code == 401
        assert TwoFactorCredential.objects.get(user=plain).confirmed_at is None
        assert client.session.get("_auth_user_id") is None


@pytest.mark.django_db
class TestSetupEnforcesCsrf:
    """The setup endpoint is the one login-flow endpoint with ambient authority.

    POST /login and POST /login/2fa each carry an unguessable value in the body
    — a password, a TOTP code — so a forged cross-site request cannot produce a
    meaningful one. This endpoint takes no body: the account it acts on is
    decided entirely by session-cookie state, which is exactly what the CSRF
    chokepoint exists to protect.

    Without it, a forged call timed against a real enrolment replaces the secret
    underneath the user, turns the code they are about to type into a failure,
    and — because it shares lockout keys with the code step — repeated, drives
    the account into a lockout that blocks ordinary login too, all without
    knowing the password.
    """

    def _csrf_client(self):
        from django.test import Client

        return Client(enforce_csrf_checks=True)

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True, SESSION_CSRF_ENFORCED=True)
    def test_a_request_without_a_token_is_refused(self, plain):
        client = self._csrf_client()
        _login(client, "plain")
        assert client.post(SETUP).status_code == 403
        assert not TwoFactorCredential.objects.filter(user=plain).exists()

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True, SESSION_CSRF_ENFORCED=True)
    def test_a_request_with_a_token_succeeds(self, plain):
        from django.middleware.csrf import get_token

        client = self._csrf_client()
        _login(client, "plain")
        # Mirrors what the SPA does: axios echoes the csrftoken cookie the
        # document was seeded with back in the X-CSRFToken header.
        token = get_token(client.request().wsgi_request)
        client.cookies["csrftoken"] = token
        resp = client.post(SETUP, HTTP_X_CSRFTOKEN=token)
        assert resp.status_code == 200, resp.content


@pytest.mark.django_db
class TestSetupIsIdempotent:
    """A repeat call must not invalidate a secret the user may already hold.

    Minting afresh every time means any repeat — a double submit, a retry, or a
    forged request that got past the CSRF check — silently replaces a scanned
    secret, and the user meets it as a code that will not verify. An unconfirmed
    credential has never authenticated anything, so there is nothing to rotate.
    """

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_a_second_call_returns_the_same_secret(self, client, plain):
        _login(client, "plain")
        first = client.post(SETUP).json()["secret"]
        second = client.post(SETUP).json()["secret"]
        assert first == second

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True)
    def test_a_secret_already_scanned_still_verifies_after_a_repeat(self, client, plain):
        # The property the idempotence exists for, exercised end to end.
        _login(client, "plain")
        secret = client.post(SETUP).json()["secret"]
        client.post(SETUP)
        assert _verify(client, code_at(secret, int(time.time()))).json()["authenticated"] is True


@pytest.mark.django_db
class TestTheCodeStepEnforcesCsrfToo:
    """The sibling endpoint carries the same ambient authority.

    A forged POST cannot guess a code, but it does not need to: a wrong code
    still increments `login_2fa_attempts`, and enough of them reach the lockout
    that blocks the victim's own login. That is the same shared-lockout denial
    the setup endpoint's fix closed, reachable through the endpoint next to it.
    The "unguessable code" argument omitted the mutation that mattered.
    """

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ALL=True, SESSION_CSRF_ENFORCED=True)
    def test_a_code_submitted_without_a_token_is_refused(self, plain):
        from django.core.cache import cache
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        _login(client, "plain")
        # JSON, because the schema is validated before the view runs: a
        # malformed body answers 400 and never reaches the CSRF check.
        resp = client.post(VERIFY, json.dumps({"code": "000000"}), content_type="application/json")
        assert resp.status_code == 403
        # The point of the fix: the forged attempt must not reach the counter
        # the lockout is built on.
        assert cache.get(f"login_2fa_attempts:{plain.pk}") is None
