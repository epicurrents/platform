"""Creating an account by invitation, and what crosses the broker to do it.

The point of the flow is that no operator ever holds the new account's
password: the account is created with an unusable one and the person chooses
their own through a link. Two properties carry that, and both fail silently.

The link must not be in the task payload. Redis persists its queue to an
append-only file, so a rendered URL sits on disk with a live token beside the
recipient's address until a rewrite that a quiet deployment may not do for
months. The contract is the same one
:mod:`user.tests.test_reset_email_payload` pins for the reset task, restated
here because a new task is where it gets forgotten.

And the account must be reachable. An invitation to an account with no address
goes nowhere, leaving an account nobody can sign in to and no error anywhere —
so the address is required at creation time rather than discovered missing in a
worker.
"""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model

from activity.models import Activity
from conftest import post_json

ACCOUNTS = "/api/v1/user/admin/accounts"


@pytest.fixture
def su_client(superuser_client):
    return superuser_client[0]


@pytest.mark.django_db
class TestCreationByInvitation:
    def test_an_account_without_a_password_is_created_unusable_and_invited(
        self, su_client, django_capture_on_commit_callbacks
    ):
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            with django_capture_on_commit_callbacks(execute=True):
                response = post_json(su_client, ACCOUNTS, {"username": "invitee", "email": "invitee@example.com"})
        assert response.status_code == 201
        account = get_user_model().objects.get(username="invitee")
        assert not account.has_usable_password()
        delay.assert_called_once_with(account.pk)

    def test_the_response_says_the_invitation_is_outstanding(self, su_client):
        with mock.patch("user.tasks.send_welcome_email.delay"):
            body = post_json(su_client, ACCOUNTS, {"username": "pending", "email": "pending@example.com"}).json()
        assert body["is_invite_pending"] is True
        assert body["external_provider"] is None

    def test_an_invitation_needs_somewhere_to_go(self, su_client):
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            response = post_json(su_client, ACCOUNTS, {"username": "nowhere"})
        assert response.status_code == 400
        assert not get_user_model().objects.filter(username="nowhere").exists()
        delay.assert_not_called()

    def test_a_supplied_password_still_works_and_sends_nothing(self, su_client):
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            response = post_json(
                su_client,
                ACCOUNTS,
                {"username": "self_served", "password": "Str0ng-Passphrase-42", "email": "s@example.com"},
            )
        assert response.status_code == 201
        assert response.json()["is_invite_pending"] is False
        assert get_user_model().objects.get(username="self_served").check_password("Str0ng-Passphrase-42")
        delay.assert_not_called()

    def test_the_audit_row_says_which_kind_of_creation_it_was(self, su_client):
        """An operator asking later how an account came to exist is asking
        exactly this: whether anyone else ever knew its password."""
        with mock.patch("user.tasks.send_welcome_email.delay"):
            post_json(su_client, ACCOUNTS, {"username": "audited_invite", "email": "ai@example.com"})
        activity = Activity.objects.filter(verb="user.account.create").latest("id")
        assert activity.metadata["invited"] is True
        assert activity.metadata["invitation_sent"] is True

    def test_an_account_created_deactivated_is_not_invited_yet(self, su_client, django_capture_on_commit_callbacks):
        """The task would refuse it anyway, but silently — leaving an operator
        who prepared the account ahead of time believing the mail went out."""
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            with django_capture_on_commit_callbacks(execute=True):
                response = post_json(
                    su_client, ACCOUNTS, {"username": "not_yet", "email": "ny@example.com", "is_active": False}
                )
        assert response.status_code == 201
        assert response.json()["is_invite_pending"] is True
        delay.assert_not_called()
        activity = Activity.objects.filter(verb="user.account.create").latest("id")
        assert activity.metadata["invited"] is True
        assert activity.metadata["invitation_sent"] is False

    def test_a_refused_creation_queues_nothing(self, su_client, user, django_capture_on_commit_callbacks):
        """The dispatch is on commit, so a refusal after the save would otherwise
        still mail a link for an account that does not exist."""
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            with django_capture_on_commit_callbacks(execute=True):
                response = post_json(su_client, ACCOUNTS, {"username": user.username, "email": "d@example.com"})
        assert response.status_code == 409
        delay.assert_not_called()


@pytest.mark.django_db
class TestBrokerPayload:
    def test_only_the_primary_key_is_queued(self, su_client, django_capture_on_commit_callbacks):
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            with django_capture_on_commit_callbacks(execute=True):
                post_json(su_client, ACCOUNTS, {"username": "payload_invite", "email": "payload_invite@example.com"})
        account = get_user_model().objects.get(username="payload_invite")
        delay.assert_called_once_with(account.pk)

    def test_no_link_or_address_reaches_the_payload(self, su_client, django_capture_on_commit_callbacks):
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            with django_capture_on_commit_callbacks(execute=True):
                post_json(su_client, ACCOUNTS, {"username": "leak_invite", "email": "leak_invite@example.com"})
        serialized = repr(delay.call_args)
        assert "leak_invite@example.com" not in serialized
        assert "reset-password?uid=" not in serialized


@pytest.mark.django_db
class TestTheTaskItself:
    def _send(self, user_id):
        from user.tasks import send_welcome_email

        with mock.patch("django.core.mail.send_mail") as send_mail:
            send_welcome_email(user_id)
        return send_mail

    def _invited(self, make_user, **kwargs):
        account = make_user(**kwargs)
        account.set_unusable_password()
        account.save(update_fields=["password"])
        return account

    def test_it_renders_a_set_password_link_from_a_primary_key(self, make_user, settings):
        settings.FRONTEND_URL = "https://eeg.example.com"
        account = self._invited(make_user, username="task_invite", email="task_invite@example.com")
        kwargs = self._send(account.pk).call_args.kwargs
        assert kwargs["recipient_list"] == ["task_invite@example.com"]
        assert "https://eeg.example.com/reset-password?uid=" in kwargs["message"]
        assert "token=" in kwargs["message"]
        assert "welcome=1" in kwargs["message"]

    def test_a_trailing_slash_in_frontend_url_does_not_double(self, make_user, settings):
        settings.FRONTEND_URL = "https://eeg.example.com/"
        account = self._invited(make_user, username="slash_invite", email="slash_invite@example.com")
        assert "https://eeg.example.com/reset-password?uid=" in self._send(account.pk).call_args.kwargs["message"]

    def test_an_account_that_has_since_set_a_password_is_left_alone(self, make_user):
        """The person can follow a link and set a password in the seconds before
        a retry lands; a second live link is then one more way into an account
        whose owner has already finished."""
        account = make_user(username="already_set", password="pw", email="already_set@example.com")
        self._send(account.pk).assert_not_called()

    def test_a_deactivated_account_is_not_invited(self, make_user):
        account = self._invited(make_user, username="deactivated_invite", email="di@example.com")
        account.is_active = False
        account.save(update_fields=["is_active"])
        self._send(account.pk).assert_not_called()

    def test_a_missing_account_is_not_an_error(self):
        self._send(999_999_999).assert_not_called()

    def test_an_account_with_no_address_is_not_invited(self, make_user):
        account = self._invited(make_user, username="addressless", email="")
        self._send(account.pk).assert_not_called()


@pytest.mark.django_db
class TestResendingTheInvitation:
    def _invited(self, make_user, **kwargs):
        account = make_user(**kwargs)
        account.set_unusable_password()
        account.save(update_fields=["password"])
        return account

    def test_a_pending_invitation_can_be_sent_again(self, su_client, make_user):
        account = self._invited(make_user, username="resend_me", email="resend_me@example.com")
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            response = post_json(su_client, f"{ACCOUNTS}/{account.pk}/invite", {})
        assert response.status_code == 200
        delay.assert_called_once_with(account.pk)
        assert Activity.objects.filter(verb="user.account.invite.resend").exists()

    def test_an_account_with_a_password_is_refused(self, su_client, make_user):
        """Otherwise this is a way to mail a password link to any account on the
        roster, which is the account holder's own request to make."""
        account = make_user(username="has_password", password="pw", email="hp@example.com")
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            assert post_json(su_client, f"{ACCOUNTS}/{account.pk}/invite", {}).status_code == 409
        delay.assert_not_called()

    def test_an_account_with_no_address_is_refused(self, su_client, make_user):
        account = self._invited(make_user, username="no_address", email="")
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            assert post_json(su_client, f"{ACCOUNTS}/{account.pk}/invite", {}).status_code == 409
        delay.assert_not_called()

    def test_a_deactivated_account_is_refused(self, su_client, make_user):
        account = self._invited(make_user, username="deactivated_resend", email="dr@example.com")
        account.is_active = False
        account.save(update_fields=["is_active"])
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            assert post_json(su_client, f"{ACCOUNTS}/{account.pk}/invite", {}).status_code == 409
        delay.assert_not_called()

    def test_staff_without_superuser_cannot_send_one(self, client, make_user):
        staff = make_user(username="staff_resend", password="pw")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        account = self._invited(make_user, username="target_resend", email="tr@example.com")
        client.force_login(staff)
        assert post_json(client, f"{ACCOUNTS}/{account.pk}/invite", {}).status_code == 403

    def test_a_missing_account_is_a_404(self, su_client):
        assert post_json(su_client, f"{ACCOUNTS}/999999999/invite", {}).status_code == 404
