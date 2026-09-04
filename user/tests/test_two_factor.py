"""Tests for the TOTP second factor — mechanism, enrolment, and the login gate.

Structured to match where the failure modes are rather than the file layout.
``TestRfc6238Vectors`` pins the algorithm against published values, so a
substituted or misconfigured implementation fails here rather than by silently
disagreeing with every authenticator app in the field. ``TestReplayGuard``
covers the property that a code is spent rather than merely checked, which is
what a validity window costs if it is not enforced. Everything after that is the
flow: an enrolment that cannot lock its own account out, and a login that does
not open a session before the second factor lands.
"""

import base64
import json
import time

import pytest
from django.contrib.contenttypes.models import ContentType

from activity.audit import MASK_PREFIX
from activity.models import ObjectChangeLog
from conftest import delete_json, post_json
from user import two_factor as tf
from user.models import TwoFactorCredential

LOGIN_URL = "/api/v1/user/login"
LOGIN_2FA_URL = "/api/v1/user/login/2fa"
ME_URL = "/api/v1/user/me"
TFA_URL = "/api/v1/user/me/2fa"
TFA_CONFIRM_URL = "/api/v1/user/me/2fa/confirm"
TFA_DISABLE_URL = "/api/v1/user/me/2fa/disable"
TFA_BACKUP_URL = "/api/v1/user/me/2fa/backup-codes"

PASSWORD = "s3cret-passphrase"


def _enrol(client, password=PASSWORD):
    """Run a full enrolment and return ``(secret, backup_codes)``."""
    start = post_json(client, TFA_URL, {"password": password})
    assert start.status_code == 200, start.content
    secret = start.json()["secret"]
    confirm = post_json(client, TFA_CONFIRM_URL, {"code": tf.code_at(secret, int(time.time()))})
    assert confirm.status_code == 200, confirm.content
    return secret, confirm.json()["backup_codes"]


def _next_code(secret):
    """A code from the step after the current one, which the server still accepts.

    Tests that log in moments after enrolling need this, because confirmation
    spends the step it was given and the replay guard will not hand it back. In
    use the gap is minutes; in a test it is microseconds, so the step has to be
    advanced deliberately rather than waited out. Still inside the drift window,
    so this is a code the server accepts — not a way around the guard.
    """
    return tf.code_at(secret, int(time.time()) + tf.STEP_SECONDS)


class TestRfc6238Vectors:
    """The published SHA-1 test vectors from RFC 6238 Appendix B.

    The seed is the ASCII string ``12345678901234567890``; the RFC tabulates
    eight-digit codes, so these are checked at eight digits and the platform's
    six-digit default is checked separately as a suffix of the same value.
    """

    SEED = base64.b32encode(b"12345678901234567890").decode()

    @pytest.mark.parametrize(
        "timestamp,expected",
        [
            (59, "94287082"),
            (1111111109, "07081804"),
            (1111111111, "14050471"),
            (1234567890, "89005924"),
            (2000000000, "69279037"),
            (20000000000, "65353130"),
        ],
    )
    def test_published_vector(self, timestamp, expected):
        import pyotp

        assert pyotp.TOTP(self.SEED, digits=8, interval=30).at(timestamp) == expected

    def test_platform_six_digit_codes_are_the_same_value_truncated(self):
        """Six digits is the low-order six of the eight-digit code, so the
        vectors above cover the platform's configuration too."""
        assert tf.CODE_DIGITS == 6
        assert tf.code_at(self.SEED, 59) == "94287082"[-6:]


class TestCodeVerification:
    def test_accepts_the_current_step(self):
        secret = tf.generate_secret()
        now = 1700000000
        assert tf.verify_totp_code(secret, tf.code_at(secret, now), now=now) == now // 30

    @pytest.mark.parametrize("offset", [-30, 30])
    def test_accepts_one_step_of_drift(self, offset):
        secret = tf.generate_secret()
        now = 1700000000
        code = tf.code_at(secret, now + offset)
        assert tf.verify_totp_code(secret, code, now=now) == (now + offset) // 30

    @pytest.mark.parametrize("offset", [-60, 60, -3600, 3600])
    def test_rejects_beyond_the_drift_window(self, offset):
        secret = tf.generate_secret()
        now = 1700000000
        assert tf.verify_totp_code(secret, tf.code_at(secret, now + offset), now=now) is None

    def test_rejects_another_secrets_code(self):
        now = 1700000000
        assert tf.verify_totp_code(tf.generate_secret(), tf.code_at(tf.generate_secret(), now), now=now) is None

    @pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34 5", None, "000000x"])
    def test_rejects_malformed_input(self, code):
        assert tf.verify_totp_code(tf.generate_secret(), code, now=1700000000) is None

    def test_tolerates_a_space_separated_code(self):
        """Authenticator apps display codes as ``123 456`` and users paste that."""
        secret = tf.generate_secret()
        now = 1700000000
        code = tf.code_at(secret, now)
        assert tf.verify_totp_code(secret, f"{code[:3]} {code[3:]}", now=now) is not None


class TestBackupCodeGeneration:
    def test_issues_the_configured_number_of_codes(self):
        presented, stored = tf.generate_backup_codes()
        assert len(presented) == tf.BACKUP_CODE_COUNT
        assert len(stored) == tf.BACKUP_CODE_COUNT

    def test_codes_are_unique(self):
        presented, _ = tf.generate_backup_codes()
        assert len(set(presented)) == tf.BACKUP_CODE_COUNT

    def test_stored_form_is_a_hash_not_the_code(self):
        presented, stored = tf.generate_backup_codes()
        for code in presented:
            assert code not in stored
            assert code.replace("-", "") not in stored
        assert all(len(digest) == 64 for digest in stored)

    def test_at_most_one_of_each_confusable_pair_can_appear(self):
        """A recovery code is read off paper, so a misread must be resolvable:
        ``O`` and ``I`` are in the alphabet and ``0`` and ``1`` are not, which
        is what lets the normaliser fold the digits onto the letters."""
        alphabet = set(tf._BACKUP_ALPHABET)
        assert "O" in alphabet and "0" not in alphabet
        assert "I" in alphabet and "1" not in alphabet
        assert not set("".join(tf.generate_backup_codes()[0])) & {"0", "1"}

    @pytest.mark.parametrize(
        "mutate",
        [
            str.lower,
            lambda c: c.replace("-", ""),
            lambda c: f" {c} ",
            lambda c: c.replace("O", "0").replace("I", "1"),
        ],
    )
    def test_hash_is_taken_over_a_normalised_form(self, mutate):
        presented, stored = tf.generate_backup_codes()
        assert tf.hash_backup_code(mutate(presented[0])) == stored[0]

    def test_folding_digits_does_not_collapse_distinct_codes(self):
        """The fold maps only characters the generator never emits, so no two
        real codes can normalise to the same value."""
        presented, stored = tf.generate_backup_codes()
        assert len({tf.hash_backup_code(code) for code in presented}) == len(presented)
        assert len(set(stored)) == len(stored)


class TestProvisioningUri:
    def test_carries_the_secret_and_issuer(self):
        uri = tf.build_provisioning_uri("ABCDEFGH", account_name="alice", issuer="eeg.example.com")
        assert uri.startswith("otpauth://totp/")
        assert "secret=ABCDEFGH" in uri
        assert "issuer=eeg.example.com" in uri
        assert "eeg.example.com%3Aalice" in uri

    def test_states_the_parameters_rather_than_relying_on_defaults(self):
        """An app that assumes different defaults would generate codes this
        server never accepts, and the failure looks like a wrong secret."""
        uri = tf.build_provisioning_uri("ABCDEFGH", account_name="alice", issuer="host")
        assert "digits=6" in uri
        assert "period=30" in uri
        assert "algorithm=SHA1" in uri

    def test_escapes_a_username_containing_a_separator(self):
        uri = tf.build_provisioning_uri("ABC", account_name="a:b c", issuer="host")
        assert "host%3Aa%3Ab%20c" in uri


@pytest.mark.django_db
class TestReplayGuard:
    def _credential(self, make_user):
        user = make_user()
        return TwoFactorCredential.objects.create(user=user, secret=tf.generate_secret())

    def test_a_code_works_once(self, make_user):
        credential = self._credential(make_user)
        now = int(time.time())
        code = tf.code_at(credential.secret, now)
        assert tf.consume_totp(credential, code, now=now) is True
        assert tf.consume_totp(credential, code, now=now) is False

    def test_an_earlier_step_is_dead_once_a_later_one_is_spent(self, make_user):
        """The drift window would otherwise leave the previous step's code live
        for another 30 seconds after the current one has been used."""
        credential = self._credential(make_user)
        now = int(time.time())
        assert tf.consume_totp(credential, tf.code_at(credential.secret, now), now=now) is True
        assert tf.consume_totp(credential, tf.code_at(credential.secret, now - 30), now=now) is False

    def test_a_later_step_still_works_after_an_earlier_one(self, make_user):
        credential = self._credential(make_user)
        now = int(time.time())
        assert tf.consume_totp(credential, tf.code_at(credential.secret, now - 30), now=now) is True
        assert tf.consume_totp(credential, tf.code_at(credential.secret, now), now=now) is True

    def test_the_spent_step_is_persisted_not_just_held_in_memory(self, make_user):
        credential = self._credential(make_user)
        now = int(time.time())
        code = tf.code_at(credential.secret, now)
        assert tf.consume_totp(credential, code, now=now) is True
        # A second request loads its own instance; the guard has to live in the
        # row, not on the object the first request happened to be holding.
        reloaded = TwoFactorCredential.objects.get(pk=credential.pk)
        assert tf.consume_totp(reloaded, code, now=now) is False

    def test_a_wrong_code_does_not_advance_the_counter(self, make_user):
        credential = self._credential(make_user)
        now = int(time.time())
        assert tf.consume_totp(credential, "000000", now=now) is False
        assert TwoFactorCredential.objects.get(pk=credential.pk).last_counter == 0


@pytest.mark.django_db
class TestBackupCodeConsumption:
    def _credential(self, make_user):
        user = make_user()
        presented, stored = tf.generate_backup_codes()
        credential = TwoFactorCredential.objects.create(user=user, secret=tf.generate_secret(), backup_codes=stored)
        return credential, presented

    def test_a_backup_code_works_once(self, make_user):
        credential, presented = self._credential(make_user)
        assert tf.consume_backup_code(credential, presented[0]) is True
        assert tf.consume_backup_code(credential, presented[0]) is False

    def test_spending_one_leaves_the_others(self, make_user):
        credential, presented = self._credential(make_user)
        tf.consume_backup_code(credential, presented[0])
        assert len(TwoFactorCredential.objects.get(pk=credential.pk).backup_codes) == len(presented) - 1
        assert tf.consume_backup_code(credential, presented[1]) is True

    def test_accepts_the_ungrouped_and_lowercased_form(self, make_user):
        credential, presented = self._credential(make_user)
        assert tf.consume_backup_code(credential, presented[0].replace("-", "").lower()) is True

    def test_rejects_an_unissued_code(self, make_user):
        credential, _ = self._credential(make_user)
        assert tf.consume_backup_code(credential, "AAAA-BBBB-CCCC") is False

    def test_a_totp_code_is_not_accepted_as_a_backup_code(self, make_user):
        credential, _ = self._credential(make_user)
        assert tf.consume_backup_code(credential, tf.code_at(credential.secret, int(time.time()))) is False


@pytest.mark.django_db
class TestEnrolment:
    @pytest.fixture
    def enrolling(self, make_user, client):
        user = make_user(password=PASSWORD)
        client.force_login(user)
        return client, user

    def test_status_is_disabled_before_enrolment(self, enrolling):
        client, _ = enrolling
        body = client.get(TFA_URL).json()
        assert body == {"enabled": False, "confirmed_at": None, "backup_codes_remaining": 0}

    def test_start_requires_the_password(self, enrolling):
        client, _ = enrolling
        resp = post_json(client, TFA_URL, {"password": "wrong"})
        assert resp.status_code == 400
        assert not TwoFactorCredential.objects.exists()

    def test_start_returns_a_secret_and_a_matching_uri(self, enrolling):
        client, user = enrolling
        body = post_json(client, TFA_URL, {"password": PASSWORD}).json()
        assert body["secret"] in body["provisioning_uri"]
        assert user.username in body["provisioning_uri"]

    def test_start_leaves_the_credential_unconfirmed(self, enrolling):
        client, user = enrolling
        post_json(client, TFA_URL, {"password": PASSWORD})
        assert TwoFactorCredential.objects.get(user=user).confirmed_at is None

    def test_an_unconfirmed_enrolment_does_not_gate_login(self, enrolling, client):
        """The failure this two-step flow exists to prevent: a user who scans
        nothing, or scans it wrong, must still be able to sign in."""
        _, user = enrolling
        post_json(client, TFA_URL, {"password": PASSWORD})
        client.logout()
        body = post_json(client, LOGIN_URL, {"username": user.username, "password": PASSWORD}).json()
        assert body["two_factor_required"] is False
        assert body["authenticated"] is True

    def test_restarting_replaces_the_pending_secret(self, enrolling):
        client, user = enrolling
        first = post_json(client, TFA_URL, {"password": PASSWORD}).json()["secret"]
        second = post_json(client, TFA_URL, {"password": PASSWORD}).json()["secret"]
        assert first != second
        assert TwoFactorCredential.objects.filter(user=user).count() == 1
        assert TwoFactorCredential.objects.get(user=user).secret == second

    def test_confirm_rejects_a_wrong_code(self, enrolling):
        client, user = enrolling
        post_json(client, TFA_URL, {"password": PASSWORD})
        resp = post_json(client, TFA_CONFIRM_URL, {"code": "000000"})
        assert resp.status_code == 400
        assert TwoFactorCredential.objects.get(user=user).confirmed_at is None

    def test_confirm_activates_and_issues_backup_codes(self, enrolling):
        client, user = enrolling
        _, codes = _enrol(client)
        assert len(codes) == tf.BACKUP_CODE_COUNT
        credential = TwoFactorCredential.objects.get(user=user)
        assert credential.confirmed_at is not None
        assert len(credential.backup_codes) == tf.BACKUP_CODE_COUNT

    def test_the_confirming_code_cannot_be_replayed_at_the_login_prompt(self, enrolling, client):
        """Confirmation spends its own time step, closing the window in which
        the code that activated the factor would also satisfy it."""
        _, user = enrolling
        start = post_json(client, TFA_URL, {"password": PASSWORD}).json()
        code = tf.code_at(start["secret"], int(time.time()))
        assert post_json(client, TFA_CONFIRM_URL, {"code": code}).status_code == 200
        client.logout()
        post_json(client, LOGIN_URL, {"username": user.username, "password": PASSWORD})
        assert post_json(client, LOGIN_2FA_URL, {"code": code}).status_code == 401

    def test_confirm_without_an_enrolment_is_404(self, enrolling):
        client, _ = enrolling
        assert post_json(client, TFA_CONFIRM_URL, {"code": "000000"}).status_code == 404

    def test_confirm_twice_is_refused(self, enrolling):
        client, _ = enrolling
        secret, _ = _enrol(client)
        resp = post_json(client, TFA_CONFIRM_URL, {"code": tf.code_at(secret, int(time.time()))})
        assert resp.status_code == 409

    def test_re_enrolling_over_a_live_factor_is_refused(self, enrolling):
        client, user = enrolling
        secret, _ = _enrol(client)
        resp = post_json(client, TFA_URL, {"password": PASSWORD})
        assert resp.status_code == 409
        assert TwoFactorCredential.objects.get(user=user).secret == secret

    def test_status_reports_an_active_factor(self, enrolling):
        client, _ = enrolling
        _enrol(client)
        body = client.get(TFA_URL).json()
        assert body["enabled"] is True
        assert body["confirmed_at"] is not None
        assert body["backup_codes_remaining"] == tf.BACKUP_CODE_COUNT

    def test_an_oidc_only_account_cannot_enrol(self, make_user, client):
        """No usable password means nothing to re-confirm against, and the
        provider owns the account's second factor anyway."""
        user = make_user(password=None)
        client.force_login(user)
        resp = post_json(client, TFA_URL, {"password": ""})
        assert resp.status_code == 409

    def test_enrolment_requires_authentication(self, client):
        assert post_json(client, TFA_URL, {"password": PASSWORD}).status_code == 401
        assert client.get(TFA_URL).status_code == 401


@pytest.mark.django_db
class TestBackupCodeManagement:
    @pytest.fixture
    def enrolled(self, make_user, client):
        user = make_user(password=PASSWORD)
        client.force_login(user)
        secret, codes = _enrol(client)
        return client, user, secret, codes

    def test_regenerating_requires_the_password(self, enrolled):
        client, user, _, codes = enrolled
        assert post_json(client, TFA_BACKUP_URL, {"password": "wrong"}).status_code == 400
        assert TwoFactorCredential.objects.get(user=user).backup_codes == [tf.hash_backup_code(code) for code in codes]

    def test_regenerating_invalidates_the_old_codes(self, enrolled):
        client, user, _, old_codes = enrolled
        fresh = post_json(client, TFA_BACKUP_URL, {"password": PASSWORD}).json()["backup_codes"]
        assert set(fresh) & set(old_codes) == set()
        credential = TwoFactorCredential.objects.get(user=user)
        assert tf.consume_backup_code(credential, old_codes[0]) is False
        assert tf.consume_backup_code(credential, fresh[0]) is True

    def test_regenerating_without_a_factor_is_refused(self, make_user, client):
        client.force_login(make_user(password=PASSWORD))
        assert post_json(client, TFA_BACKUP_URL, {"password": PASSWORD}).status_code == 409


@pytest.mark.django_db
class TestDisable:
    @pytest.fixture
    def enrolled(self, make_user, client):
        user = make_user(password=PASSWORD)
        client.force_login(user)
        secret, codes = _enrol(client)
        return client, user, secret, codes

    def test_requires_the_password(self, enrolled):
        client, user, _, _ = enrolled
        assert post_json(client, TFA_DISABLE_URL, {"password": "wrong"}).status_code == 400
        assert TwoFactorCredential.objects.filter(user=user).exists()

    def test_removes_the_credential(self, enrolled):
        client, user, _, _ = enrolled
        assert post_json(client, TFA_DISABLE_URL, {"password": PASSWORD}).status_code == 200
        assert not TwoFactorCredential.objects.filter(user=user).exists()

    def test_login_is_single_step_again(self, enrolled):
        client, user, _, _ = enrolled
        post_json(client, TFA_DISABLE_URL, {"password": PASSWORD})
        client.logout()
        body = post_json(client, LOGIN_URL, {"username": user.username, "password": PASSWORD}).json()
        assert body["authenticated"] is True

    def test_removes_an_abandoned_enrolment_too(self, make_user, client):
        user = make_user(password=PASSWORD)
        client.force_login(user)
        post_json(client, TFA_URL, {"password": PASSWORD})
        assert post_json(client, TFA_DISABLE_URL, {"password": PASSWORD}).status_code == 200
        assert not TwoFactorCredential.objects.filter(user=user).exists()

    def test_without_a_factor_is_refused(self, make_user, client):
        client.force_login(make_user(password=PASSWORD))
        assert post_json(client, TFA_DISABLE_URL, {"password": PASSWORD}).status_code == 409


@pytest.mark.django_db
class TestLoginGate:
    @pytest.fixture
    def enrolled(self, make_user, client):
        user = make_user(password=PASSWORD)
        client.force_login(user)
        secret, codes = _enrol(client)
        client.logout()
        return client, user, secret, codes

    def _start(self, client, user):
        return post_json(client, LOGIN_URL, {"username": user.username, "password": PASSWORD})

    def test_password_alone_does_not_open_a_session(self, enrolled):
        client, user, _, _ = enrolled
        body = self._start(client, user).json()
        assert body == {
            "authenticated": False,
            "two_factor_required": True,
            # False: this account is enrolled, so it is being challenged
            # rather than asked to enrol.
            "two_factor_enrolment_required": False,
            "backup_codes": None,
            "user": None,
        }
        assert client.get(ME_URL).json()["authenticated"] is False

    def test_a_wrong_password_still_401s_rather_than_prompting(self, enrolled):
        """The prompt must not become an oracle for whether a username exists."""
        client, user, _, _ = enrolled
        resp = post_json(client, LOGIN_URL, {"username": user.username, "password": "wrong"})
        assert resp.status_code == 401

    def test_the_code_completes_the_login(self, enrolled):
        client, user, secret, _ = enrolled
        self._start(client, user)
        resp = post_json(client, LOGIN_2FA_URL, {"code": _next_code(secret)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["user"]["username"] == user.username
        assert body["user"]["is_2fa_enabled"] is True
        assert client.get(ME_URL).json()["authenticated"] is True

    def test_a_wrong_code_leaves_the_session_closed(self, enrolled):
        client, user, _, _ = enrolled
        self._start(client, user)
        assert post_json(client, LOGIN_2FA_URL, {"code": "000000"}).status_code == 401
        assert client.get(ME_URL).json()["authenticated"] is False

    def test_the_code_step_needs_a_login_in_progress(self, enrolled):
        client, _, secret, _ = enrolled
        resp = post_json(client, LOGIN_2FA_URL, {"code": _next_code(secret)})
        assert resp.status_code == 401

    def test_the_pending_marker_expires(self, enrolled, monkeypatch):
        client, user, secret, _ = enrolled
        self._start(client, user)
        from user.api.v1 import ninja as user_api

        monkeypatch.setattr(user_api, "_PENDING_2FA_TTL", -1)
        resp = post_json(client, LOGIN_2FA_URL, {"code": _next_code(secret)})
        assert resp.status_code == 401

    def test_the_marker_does_not_survive_into_the_session(self, enrolled):
        client, user, secret, _ = enrolled
        self._start(client, user)
        post_json(client, LOGIN_2FA_URL, {"code": _next_code(secret)})
        assert "pending_two_factor" not in client.session

    def test_deactivating_the_account_mid_flow_stops_the_login(self, enrolled):
        client, user, secret, _ = enrolled
        self._start(client, user)
        user.is_active = False
        user.save()
        resp = post_json(client, LOGIN_2FA_URL, {"code": _next_code(secret)})
        assert resp.status_code == 401
        assert client.get(ME_URL).json()["authenticated"] is False

    def test_removing_the_factor_mid_flow_stops_the_login(self, enrolled):
        """A half-passed login must not complete against a credential that no
        longer exists — otherwise an operator reset would hand the session to
        whoever was already holding the password."""
        client, user, secret, _ = enrolled
        self._start(client, user)
        TwoFactorCredential.objects.filter(user=user).delete()
        resp = post_json(client, LOGIN_2FA_URL, {"code": _next_code(secret)})
        assert resp.status_code == 401

    def test_a_code_cannot_be_replayed_on_a_second_login(self, enrolled):
        client, user, secret, _ = enrolled
        code = _next_code(secret)
        self._start(client, user)
        assert post_json(client, LOGIN_2FA_URL, {"code": code}).status_code == 200
        client.logout()
        self._start(client, user)
        assert post_json(client, LOGIN_2FA_URL, {"code": code}).status_code == 401

    def test_a_backup_code_completes_the_login_and_is_spent(self, enrolled):
        client, user, _, codes = enrolled
        self._start(client, user)
        assert post_json(client, LOGIN_2FA_URL, {"code": codes[0]}).status_code == 200
        assert client.get(TFA_URL).json()["backup_codes_remaining"] == tf.BACKUP_CODE_COUNT - 1
        client.logout()
        self._start(client, user)
        assert post_json(client, LOGIN_2FA_URL, {"code": codes[0]}).status_code == 401

    def test_repeated_wrong_codes_lock_the_step_out(self, enrolled):
        client, user, secret, _ = enrolled
        self._start(client, user)
        from user.api.v1.ninja import _LOGIN_MAX_ATTEMPTS

        for _ in range(_LOGIN_MAX_ATTEMPTS):
            assert post_json(client, LOGIN_2FA_URL, {"code": "000000"}).status_code == 401
        # The correct code is refused too — the lockout is on the step, not on
        # the guess, or it would be trivially reset by submitting a good one.
        resp = post_json(client, LOGIN_2FA_URL, {"code": _next_code(secret)})
        assert resp.status_code == 429

    def test_an_oversized_code_is_rejected_by_the_schema(self, enrolled):
        """The endpoint answers before authentication, so the payload bound is
        the schema's rather than anything downstream."""
        client, user, _, _ = enrolled
        self._start(client, user)
        resp = post_json(client, LOGIN_2FA_URL, {"code": "1" * 5000})
        assert resp.status_code == 422

    def test_a_backend_no_longer_configured_stops_the_login(self, enrolled, settings):
        """The pending marker records which backend verified the password, and
        `login()` is handed that one. A marker naming a backend a redeployment
        has since removed must fail the login rather than reach `login()` with
        it — the guard is what keeps that from becoming a 500 on the login path.
        """
        client, user, secret, _ = enrolled
        self._start(client, user)
        assert client.session["pending_two_factor"]["backend"] in settings.AUTHENTICATION_BACKENDS
        settings.AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.RemoteUserBackend"]
        resp = post_json(client, LOGIN_2FA_URL, {"code": _next_code(secret)})
        assert resp.status_code == 401

    def test_a_session_opened_before_enrolment_is_unaffected(self, make_user, client):
        """Enrolment does not retroactively challenge sessions that already
        exist, including the one the user enrolled from."""
        user = make_user(password=PASSWORD)
        client.force_login(user)
        _enrol(client)
        assert client.get(ME_URL).json()["authenticated"] is True


@pytest.mark.django_db
class TestOperatorReset:
    @pytest.fixture
    def enrolled_user(self, make_user, client):
        user = make_user(password=PASSWORD)
        client.force_login(user)
        _enrol(client)
        client.logout()
        return user

    def _url(self, user):
        return f"/api/v1/user/admin/accounts/{user.pk}/2fa"

    def test_a_superuser_can_reset(self, superuser_client, enrolled_user):
        su_client, _ = superuser_client
        assert su_client.delete(self._url(enrolled_user)).status_code == 200
        assert not TwoFactorCredential.objects.filter(user=enrolled_user).exists()

    def test_the_account_can_then_sign_in_with_the_password_alone(self, superuser_client, enrolled_user, client):
        su_client, _ = superuser_client
        su_client.delete(self._url(enrolled_user))
        body = post_json(client, LOGIN_URL, {"username": enrolled_user.username, "password": PASSWORD}).json()
        assert body["authenticated"] is True

    def test_staff_without_superuser_cannot_reset(self, make_user, enrolled_user, client):
        staff = make_user(password=PASSWORD, is_staff=True)
        client.force_login(staff)
        assert client.delete(self._url(enrolled_user)).status_code == 403
        assert TwoFactorCredential.objects.filter(user=enrolled_user).exists()

    def test_an_ordinary_user_cannot_reset_another_account(self, make_user, enrolled_user, client):
        client.force_login(make_user(password=PASSWORD))
        assert client.delete(self._url(enrolled_user)).status_code == 403

    def test_resetting_an_account_without_a_factor_is_refused(self, superuser_client, make_user):
        su_client, _ = superuser_client
        assert su_client.delete(self._url(make_user())).status_code == 409

    def test_resetting_an_unknown_account_is_404(self, superuser_client):
        su_client, _ = superuser_client
        assert su_client.delete("/api/v1/user/admin/accounts/999999/2fa").status_code == 404

    def test_the_reset_is_audited(self, superuser_client, enrolled_user):
        from activity.models import Activity

        su_client, _ = superuser_client
        su_client.delete(self._url(enrolled_user))
        assert Activity.objects.filter(verb="user.account.2fa.reset").exists()

    def test_the_roster_reports_who_has_a_factor(self, superuser_client, enrolled_user):
        su_client, _ = superuser_client
        rows = su_client.get(f"/api/v1/user/admin/accounts?q={enrolled_user.username}").json()
        assert [row["is_2fa_enabled"] for row in rows] == [True]


@pytest.mark.django_db
class TestTheSecretStaysOutOfTheAuditTrail:
    def test_the_change_log_never_carries_the_secret(self, make_user, client):
        """Enrolment writes from inside an audited request, so without the
        registered mask the shared secret would be copied verbatim into a change
        log that is deliberately permanent and deliberately not deletable."""
        user = make_user(password=PASSWORD)
        client.force_login(user)
        secret, codes = _enrol(client)

        content_type = ContentType.objects.get_for_model(TwoFactorCredential)
        rows = ObjectChangeLog.objects.filter(content_type=content_type)
        assert rows.exists(), "no audit rows written — this test would pass vacuously"
        blob = json.dumps([[row.before_state, row.changes] for row in rows])
        assert secret not in blob
        for code in codes:
            assert code.replace("-", "") not in blob
        assert MASK_PREFIX in blob

    def test_no_activity_metadata_carries_the_secret_either(self, make_user, client):
        """The change log is not the only permanent stream. ``Activity.metadata``
        is written by the same requests and is not covered by the field mask,
        which reaches ``ObjectChangeLog`` payloads only."""
        from activity.models import Activity

        user = make_user(password=PASSWORD)
        client.force_login(user)
        secret, codes = _enrol(client)

        rows = Activity.objects.filter(verb__startswith="user.2fa.")
        assert rows.exists(), "no 2FA activity rows written — this test would pass vacuously"
        blob = json.dumps([row.metadata for row in Activity.objects.all()])
        assert secret not in blob
        for code in codes:
            assert code.replace("-", "") not in blob

    def test_the_secret_is_absent_from_every_response_but_enrolment(self, make_user, client):
        user = make_user(password=PASSWORD)
        client.force_login(user)
        secret, _ = _enrol(client)
        for url in (ME_URL, TFA_URL):
            assert secret not in client.get(url).content.decode()

    def test_erasure_scrubs_the_credential_rows(self, make_user, client):
        """Registered for subject erasure as well as masked, and this runs the
        erasure rather than asserting the registration exists — a registration
        naming a field the serializer never writes scrubs nothing and reports a
        clean run, which is the failure mode the whole registry has.

        The mask means the payloads hold a sentinel rather than the secret
        before erasure runs, so what is checked is that the rows are reached and
        tombstoned at all: an unreached row keeps whatever it holds, and the
        mask is a second line of defence rather than a reason to skip this one.
        """
        from activity.erasure import erase_subject

        user = make_user(password=PASSWORD)
        client.force_login(user)
        _enrol(client)

        content_type = ContentType.objects.get_for_model(TwoFactorCredential)
        rows = ObjectChangeLog.objects.filter(content_type=content_type)
        assert rows.count() > 0
        assert not rows.exclude(erased_at=None).exists()

        summary = erase_subject(user.pk)

        assert summary.get("user.twofactorcredential", 0) == rows.count()
        assert not rows.filter(erased_at=None).exists()
        blob = json.dumps([[row.before_state, row.changes] for row in rows])
        assert MASK_PREFIX not in blob, "masked sentinels survived the scrub"


@pytest.mark.django_db
class TestSurfaceRejectsUnsafeCallers:
    @pytest.mark.parametrize(
        "url,payload",
        [
            (TFA_URL, {"password": PASSWORD}),
            (TFA_CONFIRM_URL, {"code": "000000"}),
            (TFA_BACKUP_URL, {"password": PASSWORD}),
            (TFA_DISABLE_URL, {"password": PASSWORD}),
        ],
    )
    def test_every_write_requires_authentication(self, client, url, payload):
        assert post_json(client, url, payload).status_code == 401

    def test_the_reset_endpoint_requires_authentication(self, client, make_user):
        assert delete_json(client, f"/api/v1/user/admin/accounts/{make_user().pk}/2fa").status_code == 401
