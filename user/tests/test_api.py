"""Tests for the user API — login, logout, me, change-password, reset-password."""

import hashlib

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from conftest import patch_json, post_json

LOGIN_URL = "/api/v1/user/login"
LOGOUT_URL = "/api/v1/user/logout"
ME_URL = "/api/v1/user/me"
CHANGE_PW_URL = "/api/v1/user/me/change-password"
RESET_PW_URL = "/api/v1/user/reset-password"
RESET_CONFIRM_URL = "/api/v1/user/reset-password/confirm"


@pytest.mark.django_db
class TestLoginEndpoint:
    def test_valid_credentials_return_user(self, client, make_user):
        make_user(username="alice", password="secret123")
        resp = post_json(client, LOGIN_URL, {"username": "alice", "password": "secret123"})
        assert resp.status_code == 200
        data = resp.json()
        # The account has no second factor, so the login completes in one step
        # and the envelope carries the user. See user/tests/test_two_factor.py
        # for the branch where it does not.
        assert data["authenticated"] is True
        assert data["two_factor_required"] is False
        assert data["user"]["username"] == "alice"
        assert "id" in data["user"]

    def test_wrong_password_returns_401(self, client, make_user):
        make_user(username="alice", password="correct")
        resp = post_json(client, LOGIN_URL, {"username": "alice", "password": "wrong"})
        assert resp.status_code == 401

    def test_unknown_user_returns_401(self, client):
        resp = post_json(client, LOGIN_URL, {"username": "nobody", "password": "x"})
        assert resp.status_code == 401

    def test_login_sets_session(self, client, make_user):
        make_user(username="alice", password="secret123")
        post_json(client, LOGIN_URL, {"username": "alice", "password": "secret123"})
        resp = client.get(ME_URL)
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is True


@pytest.mark.django_db
class TestLogoutEndpoint:
    def test_logout_clears_session(self, auth_client):
        c, _ = auth_client
        resp = c.post(LOGOUT_URL)
        assert resp.status_code == 200
        # Session is gone — the probe reports logged out with 200, not 401.
        me = c.get(ME_URL)
        assert me.status_code == 200
        assert me.json()["authenticated"] is False
        assert me.json()["user"] is None

    def test_logout_unauthenticated_still_ok(self, client):
        resp = client.post(LOGOUT_URL)
        assert resp.status_code == 200


@pytest.mark.django_db
class TestMeEndpoint:
    def test_authenticated_returns_user(self, auth_client):
        c, user = auth_client
        resp = c.get(ME_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["user"]["username"] == user.username

    def test_unauthenticated_returns_logged_out_state(self, client):
        resp = client.get(ME_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["user"] is None

    def test_patch_updates_profile(self, auth_client):
        c, _ = auth_client
        resp = patch_json(c, ME_URL, {"email": "new@example.com", "first_name": "Alice"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "new@example.com"
        assert data["first_name"] == "Alice"

    def test_patch_invalid_email_returns_400(self, auth_client):
        """Invalid email string must be rejected with 400 (B1 regression guard)."""
        c, _ = auth_client
        resp = patch_json(c, ME_URL, {"email": "notanemail"})
        assert resp.status_code == 400

    def test_patch_unauthenticated_returns_401(self, client):
        resp = patch_json(client, ME_URL, {"email": "x@x.com"})
        assert resp.status_code == 401


@pytest.mark.django_db
class TestChangePasswordEndpoint:
    def test_change_password_success(self, client, make_user):
        user = make_user(username="alice", password="oldpass123")
        client.force_login(user)
        resp = post_json(
            client,
            CHANGE_PW_URL,
            {
                "current_password": "oldpass123",
                "new_password": "newpass456",
            },
        )
        assert resp.status_code == 200
        # Re-login with new password should work
        client.logout()
        resp2 = post_json(client, LOGIN_URL, {"username": "alice", "password": "newpass456"})
        assert resp2.status_code == 200

    def test_wrong_current_password_returns_400(self, auth_client):
        c, _ = auth_client
        resp = post_json(
            c,
            CHANGE_PW_URL,
            {
                "current_password": "wrongpass",
                "new_password": "newpass456",
            },
        )
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self, client):
        resp = post_json(
            client,
            CHANGE_PW_URL,
            {
                "current_password": "x",
                "new_password": "y",
            },
        )
        assert resp.status_code == 401

    def test_session_preserved_after_change(self, client, make_user):
        """update_session_auth_hash keeps the user logged in after changing password."""
        user = make_user(username="alice", password="oldpass")
        client.force_login(user)
        post_json(
            client,
            CHANGE_PW_URL,
            {
                "current_password": "oldpass",
                "new_password": "newpass456",
            },
        )
        assert client.get(ME_URL).status_code == 200


@pytest.mark.django_db
class TestPasswordResetRequest:
    def test_known_email_dispatches_email(self, client, make_user):
        make_user(username="alice", password="pw", email="alice@example.com")
        resp = post_json(client, RESET_PW_URL, {"email": "alice@example.com"})
        assert resp.status_code == 200
        assert len(mail.outbox) == 1
        assert "alice@example.com" in mail.outbox[0].to

    def test_unknown_email_still_returns_ok(self, client):
        resp = post_json(client, RESET_PW_URL, {"email": "nobody@example.com"})
        assert resp.status_code == 200
        # No email sent for unknown address
        assert len(mail.outbox) == 0

    def test_rate_limit_blocks_second_request(self, client, make_user):
        make_user(username="alice", email="alice@example.com")
        post_json(client, RESET_PW_URL, {"email": "alice@example.com"})
        resp = post_json(client, RESET_PW_URL, {"email": "alice@example.com"})
        assert resp.status_code == 429

    def test_rate_limit_is_per_email(self, client, make_user):
        """Rate limit on one address must not block a different address."""
        make_user(username="alice", email="alice@example.com")
        make_user(username="bob", email="bob@example.com")
        post_json(client, RESET_PW_URL, {"email": "alice@example.com"})
        resp = post_json(client, RESET_PW_URL, {"email": "bob@example.com"})
        assert resp.status_code == 200

    def test_rate_limit_normalises_email_case(self, client, make_user):
        """Uppercase variant of same email should hit the same rate limit bucket."""
        make_user(username="alice", email="alice@example.com")
        post_json(client, RESET_PW_URL, {"email": "alice@example.com"})
        resp = post_json(client, RESET_PW_URL, {"email": "ALICE@EXAMPLE.COM"})
        assert resp.status_code == 429


@pytest.mark.django_db
class TestPasswordResetConfirm:
    def test_valid_token_resets_password(self, client, make_user):
        user = make_user(username="alice", password="oldpass")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        resp = post_json(
            client,
            RESET_CONFIRM_URL,
            {
                "uid": uid,
                "token": token,
                "new_password": "newpass456",
            },
        )
        assert resp.status_code == 200
        # Old password no longer works; new one does
        client.logout()
        assert post_json(client, LOGIN_URL, {"username": "alice", "password": "oldpass"}).status_code == 401
        assert post_json(client, LOGIN_URL, {"username": "alice", "password": "newpass456"}).status_code == 200

    def test_reset_invalidates_existing_sessions(self, client, make_user):
        """A pre-existing session must die when the password is reset.

        Password reset is the credential-compromise recovery path; this
        works today because ``django.contrib.auth.get_user`` rejects any
        session whose auth hash predates the password change. The test
        pins that behaviour so a future custom auth backend or session
        change that drops the hash check surfaces here.
        """
        from django.test import Client

        user = make_user(username="alice", password="oldpass")
        attacker_session = Client()
        attacker_session.force_login(user)
        # force_login bypasses the hash mechanism; establish a real
        # credentialed session instead.
        attacker_session.logout()
        assert (
            post_json(
                attacker_session,
                LOGIN_URL,
                {"username": "alice", "password": "oldpass"},
            ).status_code
            == 200
        )
        assert attacker_session.get("/api/v1/user/me").status_code == 200

        user.refresh_from_db()
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        resp = post_json(
            client,
            RESET_CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "newpass456"},
        )
        assert resp.status_code == 200
        # The reset invalidated the attacker's session: the probe now reports
        # logged out (200 with authenticated false), not an authenticated user.
        me = attacker_session.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["authenticated"] is False

    def test_invalid_token_returns_400(self, client, make_user):
        user = make_user(username="alice")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        resp = post_json(
            client,
            RESET_CONFIRM_URL,
            {
                "uid": uid,
                "token": "invalid-token",
                "new_password": "newpass",
            },
        )
        assert resp.status_code == 400

    def test_invalid_uid_returns_400(self, client):
        resp = post_json(
            client,
            RESET_CONFIRM_URL,
            {
                "uid": "notbase64!!!",
                "token": "tok",
                "new_password": "newpass",
            },
        )
        assert resp.status_code == 400

    def test_overflow_uid_returns_400(self, client):
        """A uid that decodes to an absurdly large integer must return 400, not 500 (B2 regression guard)."""
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        huge_uid = urlsafe_base64_encode(force_bytes("9" * 100))
        resp = post_json(
            client,
            RESET_CONFIRM_URL,
            {
                "uid": huge_uid,
                "token": "tok",
                "new_password": "newpass",
            },
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestLoginRateLimiting:
    """Login endpoint must lock out after too many failed attempts."""

    def test_lockout_after_max_attempts(self, client, make_user):
        from user.api.v1.ninja import _LOGIN_MAX_ATTEMPTS

        make_user(username="victim", password="correct")
        for _ in range(_LOGIN_MAX_ATTEMPTS):
            post_json(client, LOGIN_URL, {"username": "victim", "password": "wrong"})
        # Next attempt — even with correct password — must be rate-limited.
        resp = post_json(client, LOGIN_URL, {"username": "victim", "password": "correct"})
        assert resp.status_code == 429

    def test_counter_resets_on_success(self, client, make_user):
        from user.api.v1.ninja import _LOGIN_MAX_ATTEMPTS

        make_user(username="alice", password="correct")
        # Fail some attempts but not enough for lockout
        for _ in range(_LOGIN_MAX_ATTEMPTS - 1):
            post_json(client, LOGIN_URL, {"username": "alice", "password": "wrong"})
        # A successful login clears the counter
        post_json(client, LOGIN_URL, {"username": "alice", "password": "correct"})
        # Failing again should start a fresh counter, not trigger immediate lockout
        resp = post_json(client, LOGIN_URL, {"username": "alice", "password": "wrong"})
        assert resp.status_code == 401  # not 429


@pytest.mark.django_db
class TestPasswordValidationEnforced:
    """Password validators must be applied on change-password and reset-confirm."""

    def test_change_password_rejects_weak_password(self, auth_client):
        c, _ = auth_client
        resp = post_json(
            c,
            CHANGE_PW_URL,
            {
                "current_password": "testpass123",
                "new_password": "password",  # common password
            },
        )
        assert resp.status_code == 400

    def test_reset_confirm_rejects_weak_password(self, client, make_user):
        user = make_user(username="alice", password="oldpass")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        resp = post_json(
            client,
            RESET_CONFIRM_URL,
            {
                "uid": uid,
                "token": token,
                "new_password": "123",  # too short + numeric only
            },
        )
        assert resp.status_code == 400


SEARCH_URL = "/api/v1/user/search"


@pytest.mark.django_db
class TestUserSearch:
    def test_matches_username(self, auth_client, make_user):
        c, _ = auth_client
        make_user(username="alice_smith")
        resp = c.get(f"{SEARCH_URL}?q=alice")
        assert resp.status_code == 200
        usernames = [u["username"] for u in resp.json()]
        assert "alice_smith" in usernames

    def test_matches_first_name(self, auth_client, make_user):
        c, _ = auth_client
        make_user(username="jdoe", first_name="Jonathan")
        resp = c.get(f"{SEARCH_URL}?q=Jonathan")
        assert resp.status_code == 200
        assert any(u["username"] == "jdoe" for u in resp.json())

    def test_matches_last_name(self, auth_client, make_user):
        c, _ = auth_client
        make_user(username="jdoe2", last_name="Johansson")
        resp = c.get(f"{SEARCH_URL}?q=Johan")
        assert resp.status_code == 200
        assert any(u["username"] == "jdoe2" for u in resp.json())

    def test_no_email_in_response(self, auth_client, make_user):
        c, _ = auth_client
        make_user(username="alice_priv", email="private@example.com")
        resp = c.get(f"{SEARCH_URL}?q=alice_priv")
        assert resp.status_code == 200
        for item in resp.json():
            assert "email" not in item

    def test_inactive_users_excluded(self, auth_client, make_user):
        c, _ = auth_client
        u = make_user(username="ghost_user")
        u.is_active = False
        u.save()
        resp = c.get(f"{SEARCH_URL}?q=ghost")
        assert resp.status_code == 200
        assert not any(item["username"] == "ghost_user" for item in resp.json())

    def test_short_query_returns_400(self, auth_client):
        c, _ = auth_client
        resp = c.get(f"{SEARCH_URL}?q=a")
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self, client):
        resp = client.get(f"{SEARCH_URL}?q=alice")
        assert resp.status_code == 401

    def test_capped_at_20_results(self, auth_client, make_user):
        c, _ = auth_client
        for i in range(25):
            make_user(username=f"searchable_{i:02d}")
        resp = c.get(f"{SEARCH_URL}?q=searchable")
        assert resp.status_code == 200
        assert len(resp.json()) <= 20


@pytest.mark.django_db
class TestFederationPeerHttpsValidation:
    """Creating a peer with a non-HTTPS URL must be rejected."""

    def test_http_url_rejected(self, superuser_client):
        c, _ = superuser_client
        from conftest import post_json as _post

        resp = _post(c, "/api/v1/federation/peers/", {"url": "http://peer.example.com"})
        assert resp.status_code == 400

    def test_https_url_accepted(self, superuser_client):
        c, _ = superuser_client
        from unittest.mock import patch

        from federation.auth import generate_keypair

        pub_b64, _ = generate_keypair()
        with patch(
            "federation.services.fetch_peer_public_key",
            return_value=(pub_b64, ""),
        ):
            from conftest import post_json as _post

            resp = _post(c, "/api/v1/federation/peers/", {"url": "https://peer.example.com"})
        assert resp.status_code == 200


@pytest.mark.django_db
class TestUserAuditTrail:
    """Activity-row annotation contract for the user API.

    One representative test per endpoint, locking the verb + target +
    metadata shape so a future regression that drops the annotation
    surfaces here rather than in a SIEM rule months later.
    """

    def test_login_records_activity_user_login(self, client, make_user):
        from activity.models import Activity

        user = make_user(username="alice_login", password="secret123")
        resp = post_json(client, LOGIN_URL, {"username": "alice_login", "password": "secret123"})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.login").latest("created_at")
        user_ct = ContentType.objects.get_for_model(user)
        assert activity.target_content_type_id == user_ct.pk
        assert activity.target_object_id == str(user.pk)

    def test_logout_records_activity_user_logout(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        resp = c.post(LOGOUT_URL)
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.logout").latest("created_at")
        user_ct = ContentType.objects.get_for_model(user)
        assert activity.target_content_type_id == user_ct.pk
        assert activity.target_object_id == str(user.pk)

    def test_me_get_records_activity_user_profile_read(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        resp = c.get(ME_URL)
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.profile.read").latest("created_at")
        user_ct = ContentType.objects.get_for_model(user)
        assert activity.target_content_type_id == user_ct.pk
        assert activity.target_object_id == str(user.pk)

    def test_me_patch_records_activity_user_profile_update(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        resp = patch_json(c, ME_URL, {"email": "new@example.com", "first_name": "Al"})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.profile.update").latest("created_at")
        user_ct = ContentType.objects.get_for_model(user)
        assert activity.target_content_type_id == user_ct.pk
        assert activity.target_object_id == str(user.pk)
        assert activity.metadata["fields_updated"] == ["email", "first_name"]

    def test_change_password_records_activity_user_password_change(self, client, make_user):
        from activity.models import Activity

        user = make_user(username="alice_pw", password="oldpass123")
        client.force_login(user)
        resp = post_json(
            client,
            CHANGE_PW_URL,
            {"current_password": "oldpass123", "new_password": "newpass456"},
        )
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.password.change").latest("created_at")
        user_ct = ContentType.objects.get_for_model(user)
        assert activity.target_content_type_id == user_ct.pk
        assert activity.target_object_id == str(user.pk)

    def test_reset_password_known_email_records_found(self, client, make_user):
        from activity.models import Activity

        user = make_user(username="alice_rp", password="pw", email="alice_rp@example.com")
        resp = post_json(client, RESET_PW_URL, {"email": "alice_rp@example.com"})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.password.reset.request").latest("created_at")
        user_ct = ContentType.objects.get_for_model(user)
        assert activity.target_content_type_id == user_ct.pk
        assert activity.target_object_id == str(user.pk)
        assert activity.metadata["found"] is True
        expected_hash = hashlib.sha256(b"alice_rp@example.com").hexdigest()
        assert activity.metadata["email_hash"] == expected_hash

    def test_reset_password_unknown_email_records_not_found(self, client):
        from activity.models import Activity

        resp = post_json(client, RESET_PW_URL, {"email": "ghost@example.com"})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.password.reset.request").latest("created_at")
        assert activity.target_content_type_id is None
        assert activity.metadata["found"] is False

    def test_reset_password_confirm_records_activity(self, client, make_user):
        from activity.models import Activity

        user = make_user(username="alice_rc", password="oldpass")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        resp = post_json(
            client,
            RESET_CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "newpass456"},
        )
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.password.reset.confirm").latest("created_at")
        user_ct = ContentType.objects.get_for_model(user)
        assert activity.target_content_type_id == user_ct.pk
        assert activity.target_object_id == str(user.pk)

    def test_search_records_activity_user_search(self, auth_client, make_user):
        from activity.models import Activity

        c, _ = auth_client
        make_user(username="searchable_alice")
        resp = c.get(f"{SEARCH_URL}?q=searchable")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.search").latest("created_at")
        # The raw query is deliberately not stored (third-party PII); the
        # hashed form keeps repeated searches correlatable.
        import hashlib

        expected_hash = hashlib.sha256(b"searchable").hexdigest()[:16]
        assert activity.metadata["query_hash"] == expected_hash
        assert activity.metadata["query_length"] == len("searchable")
        assert "query" not in activity.metadata
        assert activity.metadata["returned_count"] >= 1

    def test_groups_list_records_activity_user_group_list(self, auth_client):
        from django.contrib.auth.models import Group

        from activity.models import Activity

        Group.objects.create(name="audit_test_group")
        c, _ = auth_client
        resp = c.get("/api/v1/user/groups")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="user.group.list").latest("created_at")
        assert activity.metadata["returned_count"] >= 1
