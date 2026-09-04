"""Contract tests for how the password-reset request resolves an address.

Three properties, each of which failed before:

- An address that is not an address must not reach the query. The user model does
  not require an email, so blank is the stored default for admin-created and
  OIDC-provisioned accounts, and an empty string matched all of them.
- Two active accounts may share an address, because nothing constrains it to be
  unique. Resolving with ``get`` raised MultipleObjectsReturned, which is a 500 —
  and a 500 where every other outcome is 200 is exactly the enumeration signal
  the endpoint is written to avoid.
- The not-found branch writes an Activity row with no target, and ``erase_subject``
  walks Activity by target, so anything identifying in that row's metadata cannot
  be erased afterwards.
"""

from unittest import mock

import pytest

from conftest import post_json

RESET_PW_URL = "/api/v1/user/reset-password"


def _request(client, email):
    with mock.patch("user.tasks.send_password_reset_email.delay") as delay:
        response = post_json(client, RESET_PW_URL, {"email": email})
    return response, delay


@pytest.mark.django_db
class TestMalformedAddress:
    @pytest.mark.parametrize("email", ["", "   ", "not-an-address", "@example.com", "a@", "a b@example.com"])
    def test_answers_ok_and_queues_nothing(self, client, make_user, email):
        response, delay = _request(client, email)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        delay.assert_not_called()

    def test_blank_does_not_match_accounts_with_no_email(self, client, make_user):
        """The failing case: `email__iexact=""` matched every blank-email account,
        so an unauthenticated caller could mail a reset link to whoever held the
        first one."""
        make_user(username="blank_email_acct", password="pw", email="")
        response, delay = _request(client, "")
        assert response.status_code == 200
        delay.assert_not_called()


@pytest.mark.django_db
class TestDuplicateAddress:
    def test_two_active_accounts_sharing_an_address_do_not_500(self, client, make_user):
        make_user(username="dup_a", password="pw", email="dup@example.com")
        make_user(username="dup_b", password="pw", email="DUP@example.com")
        response, delay = _request(client, "dup@example.com")
        assert response.status_code == 200

    def test_every_matching_account_gets_its_own_link(self, client, make_user):
        """Sending to only one would leave the other account unable to reset,
        with nothing to tell its owner why."""
        first = make_user(username="dup_c", password="pw", email="dup2@example.com")
        second = make_user(username="dup_d", password="pw", email="dup2@example.com")
        _, delay = _request(client, "dup2@example.com")
        assert sorted(call.args[0] for call in delay.call_args_list) == sorted([first.pk, second.pk])

    def test_inactive_accounts_are_excluded(self, client, make_user):
        active = make_user(username="dup_e", password="pw", email="dup3@example.com")
        inactive = make_user(username="dup_f", password="pw", email="dup3@example.com")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        _, delay = _request(client, "dup3@example.com")
        delay.assert_called_once_with(active.pk)


@pytest.mark.django_db
class TestAuditRowIsErasable:
    def test_the_not_found_row_carries_no_address_hash(self, client):
        """That row has no target, and erase_subject reaches Activity rows only
        through target_content_type / target_object_id — so a hash written here
        survives the erasure request of the person who owns the address."""
        from activity.models import Activity

        _request(client, "ghost_lookup@example.com")
        activity = Activity.objects.filter(verb="user.password.reset.request").latest("created_at")
        assert activity.target_content_type_id is None
        assert activity.metadata.get("found") is False
        assert "email_hash" not in activity.metadata

    def test_a_shared_address_records_shape_not_identity(self, client, make_user):
        """One Activity row exists per request, so it cannot target every matching
        account — and erase_subject reaches rows only through target. An
        identifier on this row would therefore be unerasable for every account
        but one, which is the same trap the not-found branch fell into."""
        from activity.models import Activity

        make_user(username="shape_a", password="pw", email="shape@example.com")
        make_user(username="shape_b", password="pw", email="shape@example.com")
        _request(client, "shape@example.com")
        activity = Activity.objects.filter(verb="user.password.reset.request").latest("created_at")
        assert activity.target_content_type_id is None
        assert activity.metadata["account_count"] == 2
        assert "email_hash" not in activity.metadata

    def test_exactly_one_row_is_written_per_request(self, client, make_user):
        """log_activity annotates the request's existing row rather than
        appending, so calling it once per account overwrote the target instead of
        recording each — leaving every account but the last with no row at all."""
        from activity.models import Activity

        make_user(username="onerow_a", password="pw", email="onerow@example.com")
        make_user(username="onerow_b", password="pw", email="onerow@example.com")
        before = Activity.objects.filter(verb="user.password.reset.request").count()
        _request(client, "onerow@example.com")
        assert Activity.objects.filter(verb="user.password.reset.request").count() == before + 1

    def test_the_found_row_keeps_the_hash_because_erasure_can_reach_it(self, client, make_user):
        from activity.models import Activity

        user = make_user(username="found_row", password="pw", email="found_row@example.com")
        _request(client, "found_row@example.com")
        activity = Activity.objects.filter(verb="user.password.reset.request").latest("created_at")
        assert activity.target_object_id == str(user.pk)
        assert "email_hash" in activity.metadata

    def test_erasure_reaches_the_found_row(self, client, make_user):
        """The reason the hash is allowed to stay on this branch."""
        from activity.erasure import ERASED_SENTINEL, erase_subject
        from activity.models import Activity

        user = make_user(username="erase_row", password="pw", email="erase_row@example.com")
        _request(client, "erase_row@example.com")
        erase_subject(user.pk)
        activity = Activity.including_archived.filter(verb="user.password.reset.request").latest("created_at")
        assert activity.metadata["email_hash"] == ERASED_SENTINEL
