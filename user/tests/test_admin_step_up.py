"""Admin writes that could hand someone a way in need a fresh credential, not just a session.

A superuser session cookie was enough to create a new superuser, or to strip the
caller's own second factor and then set a new password on it — so one stolen
cookie became a permanent account without the password or the second factor ever
being proven. These tests pin which writes now ask for step-up, which do not,
the refusals on the caller's own account, the security events each emits, and
that no invitation is reported as sent when no mail backend would send it.
"""

import logging
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from conftest import delete_json, patch_json, post_json
from user import two_factor as tf
from user.models import TwoFactorCredential

ACCOUNTS = "/api/v1/user/admin/accounts"
GROUPS = "/api/v1/user/admin/groups"
SU_PASSWORD = "adminpass123"
STEP = {"password": SU_PASSWORD}
CONSOLE = "django.core.mail.backends.console.EmailBackend"


def put_json(client, url, data):
    """PUT JSON data and return the response."""
    import json

    return client.put(url, json.dumps(data), content_type="application/json")


@pytest.fixture
def su(superuser_client):
    """``(client, superuser)``."""
    return superuser_client


@pytest.fixture
def events(caplog):
    caplog.set_level(logging.WARNING, logger="epicurrents.security")
    return caplog


def _events(caplog, event_type):
    return [r for r in caplog.records if getattr(r, "security_event_type", "") == event_type]


def _enrol(user):
    return TwoFactorCredential.objects.create(
        user=user, secret=tf.generate_secret(), confirmed_at="2026-01-01T00:00:00Z", backup_codes=[]
    )


@pytest.mark.django_db
class TestAccountCreation:
    def test_a_new_superuser_needs_step_up(self, su):
        client, _ = su
        body = {"username": "new_root", "email": "nr@example.com", "is_superuser": True}
        with mock.patch("user.tasks.send_welcome_email.delay"):
            assert post_json(client, ACCOUNTS, body).status_code == 400
            assert not get_user_model().objects.filter(username="new_root").exists()
            assert post_json(client, ACCOUNTS, {**body, "current_password": SU_PASSWORD}).status_code == 201

    def test_a_new_staff_account_needs_step_up(self, su):
        client, _ = su
        body = {"username": "new_staff", "email": "ns@example.com", "is_staff": True}
        with mock.patch("user.tasks.send_welcome_email.delay"):
            assert post_json(client, ACCOUNTS, body).status_code == 400

    def test_a_supplied_password_needs_step_up(self, su):
        """The caller then knows a credential for the account, which outlives their session."""
        client, _ = su
        body = {"username": "known_pw", "password": "Str0ng-Passphrase-42"}
        assert post_json(client, ACCOUNTS, body).status_code == 400
        assert post_json(client, ACCOUNTS, {**body, "current_password": SU_PASSWORD}).status_code == 201

    def test_the_wrong_confirmation_is_refused(self, su):
        client, _ = su
        body = {"username": "known_pw2", "password": "Str0ng-Passphrase-42", "current_password": "wrong"}
        assert post_json(client, ACCOUNTS, body).status_code == 400
        assert not get_user_model().objects.filter(username="known_pw2").exists()

    def test_an_invited_ordinary_account_needs_none(self, su):
        client, _ = su
        with mock.patch("user.tasks.send_welcome_email.delay"):
            response = post_json(client, ACCOUNTS, {"username": "invitee", "email": "inv@example.com"})
        assert response.status_code == 201

    def test_the_creation_is_a_security_event(self, su, events):
        client, actor = su
        with mock.patch("user.tasks.send_welcome_email.delay"):
            post_json(
                client,
                ACCOUNTS,
                {
                    "username": "evented",
                    "email": "ev@example.com",
                    "is_superuser": True,
                    "current_password": SU_PASSWORD,
                },
            )
        (event,) = _events(events, "admin.account_created")
        assert event.actor_id == actor.pk
        assert event.target_id == get_user_model().objects.get(username="evented").pk
        assert event.is_superuser is True


@pytest.mark.django_db
class TestAccountUpdate:
    def test_promotion_needs_step_up(self, su, user):
        client, _ = su
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", {"is_superuser": True}).status_code == 400
        user.refresh_from_db()
        assert user.is_superuser is False
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", {"is_superuser": True, **STEP}).status_code == 200

    def test_an_email_change_needs_step_up(self, su, user):
        client, _ = su
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", {"email": "moved@example.com"}).status_code == 400
        user.refresh_from_db()
        assert user.email != "moved@example.com"

    def test_an_operator_set_address_is_linkable_again(self, su, user):
        client, _ = su
        user.email_self_asserted = True
        user.save(update_fields=["email_self_asserted"])
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", {"email": "ops@example.com", **STEP}).status_code == 200
        user.refresh_from_db()
        assert user.email_self_asserted is False

    def test_reactivation_needs_step_up(self, su, user):
        client, _ = su
        user.is_active = False
        user.save(update_fields=["is_active"])
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", {"is_active": True}).status_code == 400
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", {"is_active": True, **STEP}).status_code == 200

    def test_a_name_edit_or_a_no_op_needs_none(self, su, user):
        client, _ = su
        unchanged = {"first_name": "Renamed", "is_staff": user.is_staff, "email": user.email}
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", unchanged).status_code == 200

    def test_deactivation_needs_none(self, su, user):
        """Closing a way in is what an operator does in a hurry, and it grants nothing."""
        client, _ = su
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", {"is_active": False}).status_code == 200

    def test_privilege_and_email_changes_are_security_events(self, su, user, events):
        client, actor = su
        patch_json(client, f"{ACCOUNTS}/{user.pk}", {"is_staff": True, "email": "x@example.com", **STEP})
        (privilege,) = _events(events, "admin.account_privilege_changed")
        assert privilege.fields == ["is_staff"]
        assert privilege.target_id == user.pk
        (email,) = _events(events, "admin.account_email_changed")
        assert email.actor_id == actor.pk
        assert "x@example.com" not in events.text


@pytest.mark.django_db
class TestSetPassword:
    def test_it_needs_step_up(self, su, user):
        client, _ = su
        body = {"new_password": "An0ther-Str0ng-Pass"}
        assert post_json(client, f"{ACCOUNTS}/{user.pk}/password", body).status_code == 400
        user.refresh_from_db()
        assert not user.check_password("An0ther-Str0ng-Pass")

    def test_it_is_refused_on_the_callers_own_account(self, su):
        client, actor = su
        body = {"new_password": "An0ther-Str0ng-Pass", **STEP}
        assert post_json(client, f"{ACCOUNTS}/{actor.pk}/password", body).status_code == 409
        actor.refresh_from_db()
        assert actor.check_password(SU_PASSWORD)

    def test_it_is_a_security_event(self, su, user, events):
        client, actor = su
        post_json(client, f"{ACCOUNTS}/{user.pk}/password", {"new_password": "An0ther-Str0ng-Pass", **STEP})
        (event,) = _events(events, "admin.password_set")
        assert (event.actor_id, event.target_id) == (actor.pk, user.pk)

    def test_the_callers_second_factor_is_asked_for(self, su, user):
        client, actor = su
        credential = _enrol(actor)
        body = {"new_password": "An0ther-Str0ng-Pass", **STEP}
        assert post_json(client, f"{ACCOUNTS}/{user.pk}/password", body).status_code == 400
        code = tf.code_at(credential.secret, __import__("time").time())
        assert post_json(client, f"{ACCOUNTS}/{user.pk}/password", {**body, "totp_code": code}).status_code == 200


@pytest.mark.django_db
class TestTwoFactorReset:
    def test_it_needs_step_up(self, su, user):
        client, _ = su
        _enrol(user)
        assert delete_json(client, f"{ACCOUNTS}/{user.pk}/2fa").status_code == 400
        assert delete_json(client, f"{ACCOUNTS}/{user.pk}/2fa", {"password": "wrong"}).status_code == 400
        assert TwoFactorCredential.objects.filter(user=user).exists()
        assert delete_json(client, f"{ACCOUNTS}/{user.pk}/2fa", STEP).status_code == 200

    def test_it_is_refused_on_the_callers_own_account(self, su):
        """The first half of the takeover: strip one's own factor, then set a password."""
        client, actor = su
        _enrol(actor)
        assert delete_json(client, f"{ACCOUNTS}/{actor.pk}/2fa", STEP).status_code == 409
        assert TwoFactorCredential.objects.filter(user=actor).exists()


@pytest.mark.django_db
class TestGroupWrites:
    def test_adding_a_member_needs_step_up(self, su, user):
        client, _ = su
        group = Group.objects.create(name="Cardiology")
        assert put_json(client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group.pk]}).status_code == 400
        assert user.groups.count() == 0
        assert put_json(client, f"{GROUPS}/{group.pk}/members", {"user_ids": [user.pk]}).status_code == 400
        assert group.user_set.count() == 0

    def test_removing_a_member_needs_none(self, su, user):
        client, _ = su
        group = Group.objects.create(name="Cardiology")
        user.groups.add(group)
        assert put_json(client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": []}).status_code == 200
        user.groups.add(group)
        assert put_json(client, f"{GROUPS}/{group.pk}/members", {"user_ids": []}).status_code == 200

    def test_an_addition_is_a_security_event(self, su, user, events):
        client, _ = su
        group = Group.objects.create(name="Cardiology")
        put_json(client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group.pk], **STEP})
        (event,) = _events(events, "admin.group_membership_granted")
        assert event.target_id == user.pk
        assert event.group_ids == [group.pk]

    def test_a_rename_needs_none(self, su):
        client, _ = su
        group = Group.objects.create(name="Cardiology")
        assert patch_json(client, f"{GROUPS}/{group.pk}", {"name": "Neurology"}).status_code == 200


@pytest.mark.django_db
class TestInvitationDelivery:
    """With the console backend outside development, an invitation would print
    its live set-password link and the address to the worker's stdout."""

    def test_creation_reports_that_nothing_was_sent(self, su, settings):
        client, _ = su
        settings.EMAIL_BACKEND = CONSOLE
        settings.DEBUG = False
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            response = post_json(client, ACCOUNTS, {"username": "unmailed", "email": "um@example.com"})
        assert response.status_code == 201
        assert response.json()["invitation_sent"] is False
        delay.assert_not_called()

    def test_creation_reports_a_sent_invitation(self, su, django_capture_on_commit_callbacks):
        client, _ = su
        with mock.patch("user.tasks.send_welcome_email.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                response = post_json(client, ACCOUNTS, {"username": "mailed", "email": "m@example.com"})
        assert response.json()["invitation_sent"] is True

    def _invited(self, make_user):
        account = make_user(username="pending_invite", email="pi@example.com")
        account.set_unusable_password()
        account.save(update_fields=["password"])
        return account

    def test_resend_reports_that_nothing_was_sent(self, su, make_user, settings, events):
        client, _ = su
        settings.EMAIL_BACKEND = CONSOLE
        settings.DEBUG = False
        account = self._invited(make_user)
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            response = post_json(client, f"{ACCOUNTS}/{account.pk}/invite", {})
        assert response.json() == {"status": "not_sent", "invitation_sent": False}
        delay.assert_not_called()
        (event,) = _events(events, "admin.invitation_sent")
        assert event.sent is False

    def test_resend_is_a_security_event(self, su, make_user, events):
        client, actor = su
        account = self._invited(make_user)
        with mock.patch("user.tasks.send_welcome_email.delay"):
            response = post_json(client, f"{ACCOUNTS}/{account.pk}/invite", {})
        assert response.json() == {"status": "sent", "invitation_sent": True}
        (event,) = _events(events, "admin.invitation_sent")
        assert (event.actor_id, event.target_id, event.sent) == (actor.pk, account.pk, True)


@pytest.mark.django_db
class TestTheTasksRefuseWithoutABackend:
    def _invited(self, make_user):
        account = make_user(username="task_refuse", email="tr@example.com")
        account.set_unusable_password()
        account.save(update_fields=["password"])
        return account

    @pytest.mark.parametrize("task_name", ["send_welcome_email", "send_password_reset_email"])
    def test_nothing_is_sent_and_the_log_names_neither_link_nor_address(self, make_user, settings, caplog, task_name):
        from user import tasks

        settings.EMAIL_BACKEND = CONSOLE
        settings.DEBUG = False
        account = self._invited(make_user)
        with caplog.at_level(logging.WARNING, logger="user.tasks"):
            with mock.patch("django.core.mail.send_mail") as send_mail:
                getattr(tasks, task_name)(account.pk)
        send_mail.assert_not_called()
        assert "not configured" in caplog.text
        assert account.email not in caplog.text
        assert "uid=" not in caplog.text

    def test_development_still_prints_to_the_console(self, make_user, settings):
        from user.tasks import send_welcome_email

        settings.EMAIL_BACKEND = CONSOLE
        settings.DEBUG = True
        account = self._invited(make_user)
        with mock.patch("django.core.mail.send_mail") as send_mail:
            send_welcome_email(account.pk)
        send_mail.assert_called_once()
