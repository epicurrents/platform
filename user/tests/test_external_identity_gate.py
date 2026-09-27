"""Accounts that sign in through an identity provider are never given a local password.

An OIDC account's access is controlled entirely by the provider gates in
user/oidc.py — the tenant (``tid``) claim and the email-domain allowlist. A
local password on such an account answers to neither, so every surface that can
put one there has to refuse: the reset link, the change-password form, the
invitation, and the operator's set-password action.

The whole of this is inert while ``OIDC_ENABLED`` is off, which is what makes
it worth pinning. Nothing here can be noticed by using the platform as it is
configured today; it becomes live on the day an operator turns the flag on, by
which time nobody is looking at this code.

The discriminator is the identity row and the absence of a password together,
not either alone. An invited account has no usable password and must still be
able to set one; an account linked to a provider by verified email keeps the
password it already had, and taking away its recovery path closes nothing.
"""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model

from conftest import post_json
from user.identity import is_externally_authenticated, provider_label
from user.models import ExternalIdentity

RESET_URL = "/api/v1/user/reset-password"
CHANGE_URL = "/api/v1/user/me/change-password"
ACCOUNTS = "/api/v1/user/admin/accounts"


def _link(user, *, provider="entra", subject="sub-1"):
    """Give *user* an external identity, as a first OIDC login would."""
    return ExternalIdentity.objects.create(
        user=user,
        provider=provider,
        issuer="https://login.microsoftonline.com/tenant/v2.0",
        subject=subject,
        email=user.email,
        email_verified=True,
    )


def _provisioned(make_user, **kwargs):
    """An account that exists only because somebody signed in through the provider."""
    user = make_user(**kwargs)
    user.set_unusable_password()
    user.save(update_fields=["password"])
    _link(user, subject=f"sub-{user.pk}")
    return user


@pytest.mark.django_db
class TestTheDiscriminator:
    def test_a_provisioned_account_is_external(self, make_user):
        assert is_externally_authenticated(_provisioned(make_user, username="ext_1", email="ext1@example.com"))

    def test_an_invited_account_is_not(self, make_user):
        """The trap the whole module is built around: an invitation also leaves
        an unusable password, so a test on that alone refuses exactly the
        accounts the invitation exists to serve."""
        invited = make_user(username="invited_1", email="inv1@example.com")
        invited.set_unusable_password()
        invited.save(update_fields=["password"])
        assert not is_externally_authenticated(invited)

    def test_a_linked_local_account_is_not(self, make_user):
        """Linked by verified email to an account that already had a password.
        Its password login exists either way, so refusing it a reset takes away
        a recovery path without closing anything."""
        local = make_user(username="linked_1", password="pw", email="linked1@example.com")
        _link(local, subject="sub-linked")
        assert not is_externally_authenticated(local)

    def test_an_ordinary_account_is_not(self, user):
        assert not is_externally_authenticated(user)

    def test_the_provider_is_named_by_its_label(self, make_user):
        account = _provisioned(make_user, username="ext_label", email="extlabel@example.com")
        assert provider_label(account) == "Microsoft"

    def test_an_unknown_provider_falls_back_to_its_key(self, make_user, settings):
        settings.OIDC_PROVIDERS = {}
        account = _provisioned(make_user, username="ext_unknown", email="extunknown@example.com")
        assert provider_label(account) == "entra"


@pytest.mark.django_db
class TestPasswordReset:
    def test_no_link_is_sent_to_a_provisioned_account(self, client, make_user):
        account = _provisioned(make_user, username="ext_reset", email="ext_reset@example.com")
        with mock.patch("user.tasks.send_password_reset_email.delay") as delay:
            response = post_json(client, RESET_URL, {"email": account.email})
        assert response.status_code == 200
        delay.assert_not_called()

    def test_the_answer_is_indistinguishable_from_any_other(self, client, make_user):
        """Same status and same body as a successful request, or the endpoint
        becomes a way to ask which accounts are external."""
        account = _provisioned(make_user, username="ext_same", email="ext_same@example.com")
        with mock.patch("user.tasks.send_password_reset_email.delay"):
            refused = post_json(client, RESET_URL, {"email": account.email})
            ordinary = post_json(client, RESET_URL, {"email": "nobody-at-all@example.com"})
        assert refused.status_code == ordinary.status_code == 200
        assert refused.json() == ordinary.json()

    def test_the_refusal_is_recorded_as_a_security_event(self, client, make_user, caplog):
        account = _provisioned(make_user, username="ext_event", email="ext_event@example.com")
        with caplog.at_level("WARNING", logger="epicurrents.security"):
            with mock.patch("user.tasks.send_password_reset_email.delay"):
                post_json(client, RESET_URL, {"email": account.email})
        events = [
            r for r in caplog.records if getattr(r, "security_event_type", "") == "auth.password_reset_refused_external"
        ]
        assert len(events) == 1
        assert events[0].target_id == account.pk
        assert not hasattr(events[0], "actor_id")

    def test_the_address_is_not_in_the_refusal_event(self, client, make_user, caplog):
        account = _provisioned(make_user, username="ext_hygiene", email="ext_hygiene@example.com")
        with caplog.at_level("WARNING"):
            with mock.patch("user.tasks.send_password_reset_email.delay"):
                post_json(client, RESET_URL, {"email": account.email})
        assert account.email not in caplog.text

    def test_a_local_account_sharing_the_address_still_gets_its_link(self, client, make_user):
        """Two accounts can hold one address, and one of them being external is
        not a reason the other cannot recover."""
        shared = "shared_ext@example.com"
        external = _provisioned(make_user, username="ext_shared", email=shared)
        local = make_user(username="local_shared", password="pw", email=shared)
        with mock.patch("user.tasks.send_password_reset_email.delay") as delay:
            post_json(client, RESET_URL, {"email": shared})
        queued = [call.args[0] for call in delay.call_args_list]
        assert queued == [local.pk]
        assert external.pk not in queued


@pytest.mark.django_db
class TestChangePassword:
    def test_a_provisioned_account_is_refused_by_name(self, client, make_user):
        account = _provisioned(make_user, username="ext_change", email="ext_change@example.com")
        client.force_login(account)
        response = post_json(client, CHANGE_URL, {"current_password": "x", "new_password": "Str0ng-Passphrase-42"})
        assert response.status_code == 409
        assert "Microsoft" in response.json()["detail"]

    def test_the_answer_is_not_a_wrong_password(self, client, make_user):
        """``check_password`` against an unusable password is false, so without
        the gate the person is told their current password is incorrect — a
        password they have never had."""
        account = _provisioned(make_user, username="ext_change2", email="ext_change2@example.com")
        client.force_login(account)
        response = post_json(client, CHANGE_URL, {"current_password": "x", "new_password": "Str0ng-Passphrase-42"})
        assert "incorrect" not in response.json()["detail"].lower()

    def test_a_linked_local_account_can_still_change_its_password(self, client, make_user):
        account = make_user(username="linked_change", password="Old-Passphrase-42", email="lc@example.com")
        _link(account, subject="sub-linked-change")
        client.force_login(account)
        response = post_json(
            client, CHANGE_URL, {"current_password": "Old-Passphrase-42", "new_password": "Str0ng-Passphrase-42"}
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestConfirmingALink:
    """The gate is repeated at the confirm endpoint because a link outlives the
    state it was minted for. An invitation goes to a local account; before the
    three days are up the person signs in through the provider for the first
    time and is linked by verified email; the link is still valid, and nothing
    else on that path asks."""

    def _link_for(self, account):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        return {
            "uid": urlsafe_base64_encode(force_bytes(account.pk)),
            "token": default_token_generator.make_token(account),
        }

    def test_a_link_minted_before_the_link_up_is_refused(self, client, make_user):
        invited = make_user(username="invited_then_linked", email="itl@example.com")
        invited.set_unusable_password()
        invited.save(update_fields=["password"])
        credentials = self._link_for(invited)

        _link(invited, subject="sub-after-invite")

        response = post_json(
            client, "/api/v1/user/reset-password/confirm", {**credentials, "new_password": "Str0ng-Passphrase-42"}
        )
        assert response.status_code == 409
        assert "Microsoft" in response.json()["detail"]
        invited.refresh_from_db()
        assert not invited.has_usable_password()

    def test_the_refusal_is_recorded_as_a_security_event(self, client, make_user, caplog):
        provisioned = _provisioned(make_user, username="confirm_event", email="ce@example.com")
        credentials = self._link_for(provisioned)
        with caplog.at_level("WARNING", logger="epicurrents.security"):
            post_json(
                client,
                "/api/v1/user/reset-password/confirm",
                {**credentials, "new_password": "Str0ng-Passphrase-42"},
            )
        types = [getattr(record, "security_event_type", "") for record in caplog.records]
        assert "auth.password_reset_refused_external" in types

    def test_an_invited_local_account_can_still_use_its_link(self, client, make_user):
        invited = make_user(username="invited_confirms", email="ic@example.com")
        invited.set_unusable_password()
        invited.save(update_fields=["password"])
        response = post_json(
            client,
            "/api/v1/user/reset-password/confirm",
            {**self._link_for(invited), "new_password": "Str0ng-Passphrase-42"},
        )
        assert response.status_code == 200
        invited.refresh_from_db()
        assert invited.check_password("Str0ng-Passphrase-42")

    def test_a_linked_local_account_can_still_use_its_link(self, client, make_user):
        account = make_user(username="linked_confirms", password="Old-Passphrase-42", email="lcf@example.com")
        _link(account, subject="sub-linked-confirm")
        response = post_json(
            client,
            "/api/v1/user/reset-password/confirm",
            {**self._link_for(account), "new_password": "Str0ng-Passphrase-42"},
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestTheProfilePayload:
    def test_me_names_the_provider(self, client, make_user):
        account = _provisioned(make_user, username="ext_me", email="ext_me@example.com")
        client.force_login(account)
        assert client.get("/api/v1/user/me").json()["user"]["external_provider"] == "Microsoft"

    def test_an_ordinary_account_carries_no_provider(self, auth_client):
        client, _ = auth_client
        assert client.get("/api/v1/user/me").json()["user"]["external_provider"] is None


@pytest.mark.django_db
class TestInvitations:
    def test_a_provisioned_account_cannot_be_invited(self, superuser_client, make_user):
        su_client = superuser_client[0]
        account = _provisioned(make_user, username="ext_invite", email="ext_invite@example.com")
        with mock.patch("user.tasks.send_welcome_email.delay") as delay:
            response = post_json(su_client, f"{ACCOUNTS}/{account.pk}/invite", {})
        assert response.status_code == 409
        assert "Microsoft" in response.json()["detail"]
        delay.assert_not_called()

    def test_the_task_refuses_one_linked_after_the_request(self, make_user):
        """The gap between queueing and sending is where a first provider login
        lands, and the worker is the only place left to notice."""
        from user.tasks import send_welcome_email

        account = _provisioned(make_user, username="ext_task_invite", email="eti@example.com")
        with mock.patch("django.core.mail.send_mail") as send_mail:
            send_welcome_email(account.pk)
        send_mail.assert_not_called()

    def test_the_roster_says_which_accounts_are_external(self, superuser_client, make_user):
        su_client = superuser_client[0]
        account = _provisioned(make_user, username="ext_roster", email="ext_roster@example.com")
        body = su_client.get(f"{ACCOUNTS}/{account.pk}").json()
        assert body["external_provider"] == "Microsoft"
        assert body["is_invite_pending"] is False


@pytest.mark.django_db
class TestOperatorSetPassword:
    """The fourth surface. An operator setting a password on a provider account
    mints the same local credential the reset link would, so it is refused the
    same way — and before step-up, so the refusal costs no confirmation."""

    def test_an_external_account_is_refused(self, superuser_client, make_user):
        su_client = superuser_client[0]
        account = _provisioned(make_user, username="ext_setpw", email="ext_setpw@example.com")
        response = post_json(
            su_client,
            f"{ACCOUNTS}/{account.pk}/password",
            {"new_password": "Str0ng-Passphrase-42", "password": "adminpass123"},
        )
        assert response.status_code == 409
        assert "Microsoft" in response.json()["detail"]
        account.refresh_from_db()
        assert not account.has_usable_password()

    def test_an_invited_local_account_can_still_be_given_one(self, superuser_client, make_user):
        su_client = superuser_client[0]
        invited = make_user(username="invited_setpw", email="isp@example.com")
        invited.set_unusable_password()
        invited.save(update_fields=["password"])
        response = post_json(
            su_client,
            f"{ACCOUNTS}/{invited.pk}/password",
            {"new_password": "Str0ng-Passphrase-42", "password": "adminpass123"},
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestQueryCost:
    def test_the_roster_does_not_cost_a_query_per_account(
        self, superuser_client, make_user, django_assert_max_num_queries
    ):
        """Both new fields read the identity rows, so an unprefetched listing
        adds one query per row — invisible on a test fixture of three accounts
        and linear on a real roster."""
        su_client = superuser_client[0]
        for index in range(6):
            _provisioned(make_user, username=f"ext_cost_{index}", email=f"cost{index}@example.com")
        with django_assert_max_num_queries(12):
            assert su_client.get(ACCOUNTS).status_code == 200
        assert get_user_model().objects.count() >= 7
