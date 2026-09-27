"""Step-up confirmation: which credentials an account confirms with, and the lockout on failure."""

import logging

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from user import two_factor as tf
from user.models import TwoFactorCredential
from user.stepup import LOCKOUT_SECONDS, MAX_FAILURES, confirm_step_up, step_up_method

PASSWORD = "testpass123"


def _enrol(user, backup_codes=()):
    presented, stored = tf.generate_backup_codes()
    credential = TwoFactorCredential.objects.create(
        user=user,
        secret=tf.generate_secret(),
        confirmed_at=timezone.now(),
        backup_codes=stored[: len(backup_codes)],
    )
    credential.presented_backup_codes = presented[: len(backup_codes)]
    return credential


def _code(credential, offset=0):
    import time

    return tf.code_at(credential.secret, int(time.time()) + offset * tf.STEP_SECONDS)


@pytest.fixture
def request_(rf):
    return rf.post("/x")


@pytest.fixture
def events(caplog):
    caplog.set_level(logging.WARNING, logger="epicurrents.security")
    return caplog


def _stepup_events(caplog):
    return [r for r in caplog.records if getattr(r, "security_event_type", "") == "auth.stepup_failed"]


@pytest.mark.django_db
class TestMethod:
    def test_password_only(self, make_user):
        assert step_up_method(make_user(password=PASSWORD)) == "password"

    def test_password_and_totp(self, make_user):
        user = make_user(password=PASSWORD)
        _enrol(user)
        assert step_up_method(user) == "password+totp"

    def test_totp_only_for_an_external_account(self, make_user):
        user = make_user()
        user.set_unusable_password()
        user.save()
        _enrol(user)
        assert step_up_method(user) == "totp"

    def test_nothing_for_an_external_account_without_a_factor(self, make_user):
        user = make_user()
        user.set_unusable_password()
        user.save()
        assert step_up_method(user) is None

    def test_an_unconfirmed_enrolment_does_not_count(self, make_user):
        user = make_user(password=PASSWORD)
        TwoFactorCredential.objects.create(user=user, secret=tf.generate_secret())
        assert step_up_method(user) == "password"


@pytest.mark.django_db
class TestConfirm:
    def test_password_confirms(self, make_user, request_):
        user = make_user(password=PASSWORD)
        assert confirm_step_up(request_, user, password=PASSWORD) == "password"

    def test_a_wrong_or_missing_password_is_400_and_logged(self, make_user, request_, events):
        user = make_user(password=PASSWORD)
        with pytest.raises(HttpError) as excinfo:
            confirm_step_up(request_, user, password="nope")
        assert excinfo.value.status_code == 400
        with pytest.raises(HttpError):
            confirm_step_up(request_, user)
        logged = _stepup_events(events)
        assert [r.reason for r in logged] == ["password", "password"]
        assert all(r.actor_id == user.pk for r in logged)

    def test_with_a_factor_the_password_alone_is_not_enough(self, make_user, request_, events):
        user = make_user(password=PASSWORD)
        credential = _enrol(user)
        with pytest.raises(HttpError):
            confirm_step_up(request_, user, password=PASSWORD)
        assert _stepup_events(events)[-1].reason == "second_factor"
        assert confirm_step_up(request_, user, password=PASSWORD, totp_code=_code(credential)) == "password+totp"

    def test_a_totp_code_is_spent(self, make_user, request_):
        user = make_user(password=PASSWORD)
        credential = _enrol(user)
        code = _code(credential)
        confirm_step_up(request_, user, password=PASSWORD, totp_code=code)
        with pytest.raises(HttpError):
            confirm_step_up(request_, user, password=PASSWORD, totp_code=code)

    def test_a_backup_code_works_once(self, make_user, request_):
        user = make_user(password=PASSWORD)
        credential = _enrol(user, backup_codes=[True])
        code = credential.presented_backup_codes[0]
        confirm_step_up(request_, user, password=PASSWORD, totp_code=code)
        with pytest.raises(HttpError):
            confirm_step_up(request_, user, password=PASSWORD, totp_code=code)

    def test_the_second_factor_can_be_waived(self, make_user, request_):
        user = make_user(password=PASSWORD)
        _enrol(user)
        assert confirm_step_up(request_, user, password=PASSWORD, second_factor=False) == "password+totp"

    def test_waiving_the_factor_does_not_waive_an_external_account_s_only_credential(self, make_user, request_):
        user = make_user()
        user.set_unusable_password()
        user.save()
        credential = _enrol(user)
        with pytest.raises(HttpError) as excinfo:
            confirm_step_up(request_, user, second_factor=False)
        assert excinfo.value.status_code == 400
        assert confirm_step_up(request_, user, totp_code=_code(credential), second_factor=False) == "totp"

    def test_an_external_account_confirms_with_its_factor_alone(self, make_user, request_):
        user = make_user()
        user.set_unusable_password()
        user.save()
        credential = _enrol(user)
        assert confirm_step_up(request_, user, totp_code=_code(credential)) == "totp"
        with pytest.raises(HttpError):
            confirm_step_up(request_, user, password="anything")

    def test_an_external_account_without_a_factor_is_409(self, make_user, request_):
        user = make_user()
        user.set_unusable_password()
        user.save()
        with pytest.raises(HttpError) as excinfo:
            confirm_step_up(request_, user, password="anything", totp_code="123456")
        assert excinfo.value.status_code == 409

    def test_failures_lock_the_account_out_and_success_clears_the_count(self, make_user, request_, events):
        user = make_user(password=PASSWORD)
        for _ in range(MAX_FAILURES):
            with pytest.raises(HttpError) as excinfo:
                confirm_step_up(request_, user, password="nope")
            assert excinfo.value.status_code == 400
        with pytest.raises(HttpError) as excinfo:
            confirm_step_up(request_, user, password=PASSWORD)
        assert excinfo.value.status_code == 429
        assert _stepup_events(events)[-1].reason == "locked_out"
        assert LOCKOUT_SECONDS >= 60

    def test_a_success_before_the_limit_resets_the_count(self, make_user, request_):
        user = make_user(password=PASSWORD)
        for _ in range(MAX_FAILURES - 1):
            with pytest.raises(HttpError):
                confirm_step_up(request_, user, password="nope")
        confirm_step_up(request_, user, password=PASSWORD)
        for _ in range(MAX_FAILURES - 1):
            with pytest.raises(HttpError):
                confirm_step_up(request_, user, password="nope")
        assert confirm_step_up(request_, user, password=PASSWORD) == "password"


@pytest.mark.django_db
class TestSharedFailureBudget:
    """Login and step-up share their failures, so alternating between them buys no extra guesses."""

    def _login(self, client, username, password):
        from conftest import post_json

        return post_json(client, "/api/v1/user/login", {"username": username, "password": password})

    def test_a_stranger_cannot_lock_step_up_below_the_login_threshold(self, make_user, request_, client):
        """The login form is anonymous; step-up's threshold is lower. Charging step-up from login failures
        would let anyone block a superuser's rollback confirmation with a handful of wrong passwords."""
        from user.lockout import LOGIN_MAX_ATTEMPTS

        user = make_user(password=PASSWORD)
        for _ in range(LOGIN_MAX_ATTEMPTS - 1):
            assert self._login(client, user.username, "nope").status_code == 401
        assert confirm_step_up(request_, user, password=PASSWORD) == "password"

    def test_a_locked_login_locks_step_up_too(self, make_user, request_, client):
        from user.lockout import LOGIN_MAX_ATTEMPTS

        user = make_user(password=PASSWORD)
        for _ in range(LOGIN_MAX_ATTEMPTS):
            self._login(client, user.username, "nope")
        with pytest.raises(HttpError) as excinfo:
            confirm_step_up(request_, user, password=PASSWORD)
        assert excinfo.value.status_code == 429

    def test_step_up_failures_count_toward_login(self, make_user, request_, client):
        from user.lockout import LOGIN_MAX_ATTEMPTS

        user = make_user(password=PASSWORD)
        for _ in range(MAX_FAILURES):
            with pytest.raises(HttpError):
                confirm_step_up(request_, user, password="nope")
        for _ in range(LOGIN_MAX_ATTEMPTS - MAX_FAILURES):
            self._login(client, user.username, "nope")
        assert self._login(client, user.username, PASSWORD).status_code == 429

    def test_a_username_typed_in_another_case_still_charges_the_account(self, make_user, request_, client):
        from user.lockout import LOGIN_MAX_ATTEMPTS

        user = make_user(username="MixedCase", password=PASSWORD)
        for _ in range(LOGIN_MAX_ATTEMPTS):
            self._login(client, "mixedcase", "nope")
        with pytest.raises(HttpError) as excinfo:
            confirm_step_up(request_, user, password=PASSWORD)
        assert excinfo.value.status_code == 429

    def test_a_correct_login_does_not_reset_the_step_up_budget(self, make_user, request_, client):
        """A step-up count may hold guessed codes, which a correct password says nothing about."""
        user = make_user(password=PASSWORD)
        for _ in range(MAX_FAILURES - 1):
            with pytest.raises(HttpError):
                confirm_step_up(request_, user, password="nope")
        assert self._login(client, user.username, PASSWORD).status_code == 200
        with pytest.raises(HttpError):
            confirm_step_up(request_, user, password="nope")
        with pytest.raises(HttpError) as excinfo:
            confirm_step_up(request_, user, password=PASSWORD)
        assert excinfo.value.status_code == 429

    def test_a_waived_factor_does_not_reset_the_count_of_guessed_codes(self, make_user, request_):
        user = make_user(password=PASSWORD)
        _enrol(user)
        for _ in range(MAX_FAILURES - 1):
            with pytest.raises(HttpError):
                confirm_step_up(request_, user, password=PASSWORD, totp_code="000000")
        confirm_step_up(request_, user, password=PASSWORD, second_factor=False)
        with pytest.raises(HttpError):
            confirm_step_up(request_, user, password=PASSWORD, totp_code="000000")
        with pytest.raises(HttpError) as excinfo:
            confirm_step_up(request_, user, password=PASSWORD, totp_code="000000")
        assert excinfo.value.status_code == 429

    def test_the_password_rechecks_draw_on_the_same_budget(self, make_user, client):
        from conftest import post_json

        user = make_user(password=PASSWORD)
        client.force_login(user)
        for _ in range(MAX_FAILURES):
            response = post_json(
                client, "/api/v1/user/me/change-password", {"current_password": "nope", "new_password": "x"}
            )
            assert response.status_code == 400
        response = post_json(client, "/api/v1/user/me/2fa", {"password": PASSWORD})
        assert response.status_code == 429

    def test_the_counter_is_incremented_atomically(self, make_user):
        """``add`` then ``incr``: a read-modify-write lets concurrent failures overwrite each other."""
        from unittest import mock

        from user import lockout

        user = make_user(password=PASSWORD)
        with mock.patch.object(lockout.cache, "set", wraps=lockout.cache.set) as cache_set:
            lockout.record_stepup_failure(user, second_factor=False)
            lockout.record_stepup_failure(user, second_factor=False)
        attempt_key = lockout.stepup_keys(user.pk)[0]
        assert not any(call.args and call.args[0] == attempt_key for call in cache_set.call_args_list)
        assert lockout.cache.get(attempt_key) == 2
