"""Contract tests for what the password-reset flow puts on the Celery broker.

Redis persists the queue to an append-only file, so anything in a task payload
lands on disk and stays there until an AOF rewrite — which on a low-volume
deployment can be months, long past a reset token's three-day expiry. The reset
URL and the recipient address must therefore not be arguments; the task takes a
primary key and mints the token at send time.

This is the shape every other ``.delay`` in the codebase already uses. It only
became load-bearing when the broker started persisting.
"""

import hashlib
from unittest import mock

import pytest

from conftest import post_json

RESET_PW_URL = "/api/v1/user/reset-password"


@pytest.mark.django_db
class TestBrokerPayload:
    def test_only_the_primary_key_is_queued(self, client, make_user):
        user = make_user(username="payload_pk", password="pw", email="payload_pk@example.com")
        with mock.patch("user.tasks.send_password_reset_email.delay") as delay:
            assert post_json(client, RESET_PW_URL, {"email": "payload_pk@example.com"}).status_code == 200
        delay.assert_called_once_with(user.pk)

    def test_no_token_or_address_reaches_the_payload(self, client, make_user):
        """Asserts against the serialized arguments rather than the call shape, so
        a future signature that smuggles either one back in still fails."""
        make_user(username="payload_leak", password="pw", email="payload_leak@example.com")
        with mock.patch("user.tasks.send_password_reset_email.delay") as delay:
            post_json(client, RESET_PW_URL, {"email": "payload_leak@example.com"})
        serialized = repr(delay.call_args)
        assert "payload_leak@example.com" not in serialized
        assert "reset-password?uid=" not in serialized


@pytest.mark.django_db
class TestTaskStillSends:
    def test_the_task_renders_and_sends_the_link(self, make_user, settings):
        """A primary key is enough to build the whole message: the worker reads
        the address off the row and mints the token itself."""
        from user.tasks import send_password_reset_email

        settings.FRONTEND_URL = "https://eeg.example.com"
        user = make_user(username="sends_ok", password="pw", email="sends_ok@example.com")

        with mock.patch("django.core.mail.send_mail") as send_mail:
            send_password_reset_email(user.pk)

        kwargs = send_mail.call_args.kwargs
        assert kwargs["recipient_list"] == ["sends_ok@example.com"]
        assert "https://eeg.example.com/reset-password?uid=" in kwargs["message"]
        assert "token=" in kwargs["message"]

    def test_a_missing_user_is_not_an_error(self, make_user):
        """The account can be closed between the request and the send, and the
        endpoint answers identically either way to avoid disclosing which
        addresses exist — so the task must not raise on the gap."""
        from user.tasks import send_password_reset_email

        with mock.patch("django.core.mail.send_mail") as send_mail:
            send_password_reset_email(999_999_999)
        send_mail.assert_not_called()

    def test_a_trailing_slash_in_frontend_url_does_not_double(self, make_user, settings):
        settings.FRONTEND_URL = "https://eeg.example.com/"
        user = make_user(username="slash_rp", password="pw", email="slash_rp@example.com")

        with mock.patch("django.core.mail.send_mail") as send_mail:
            from user.tasks import send_password_reset_email

            send_password_reset_email(user.pk)

        assert "https://eeg.example.com/reset-password?uid=" in send_mail.call_args.kwargs["message"]

    def test_an_inactive_user_is_not_mailed(self, make_user):
        from user.tasks import send_password_reset_email

        user = make_user(username="inactive_rp", password="pw", email="inactive_rp@example.com")
        user.is_active = False
        user.save(update_fields=["is_active"])

        with mock.patch("django.core.mail.send_mail") as send_mail:
            send_password_reset_email(user.pk)
        send_mail.assert_not_called()


@pytest.mark.django_db
class TestDeliveryFailureLogging:
    """The one path that could put a raw address into the log stream.

    AGENTS.md makes hashing identifiers before they reach the logs a
    cross-cutting rule, and every other test here mocks delivery to succeed —
    so without this the hashing could be deleted with the suite still green.
    """

    def _fail_delivery(self, user, caplog, message="smtp refused alice@example.com"):
        """Drive one failed send and return what was logged.

        The task's own retry is replaced with a raise so the call terminates
        instead of scheduling; the substitute is asserted on, so a delivery
        failure that silently stopped retrying would fail these tests too.
        """
        from user.tasks import send_password_reset_email

        with mock.patch("django.core.mail.send_mail", side_effect=OSError(message)):
            with mock.patch.object(send_password_reset_email, "retry", side_effect=RuntimeError("retried")) as retry:
                with caplog.at_level("WARNING"):
                    with pytest.raises(RuntimeError, match="retried"):
                        send_password_reset_email(user.pk)
        assert retry.called, "a failed delivery must be retried, not dropped"
        return caplog.text

    def test_the_address_is_hashed_not_logged(self, make_user, caplog):
        user = make_user(username="fail_rp", password="pw", email="fail_rp@example.com")
        text = self._fail_delivery(user, caplog)
        assert "fail_rp@example.com" not in text
        assert hashlib.sha256(b"fail_rp@example.com").hexdigest()[:16] in text

    def test_the_exception_text_is_reduced_to_its_class(self, make_user, caplog):
        """SMTP rejection messages routinely echo the address back, so the
        exception cannot be logged verbatim either."""
        user = make_user(username="fail_rp2", password="pw", email="fail_rp2@example.com")
        text = self._fail_delivery(user, caplog, message="rejected: fail_rp2@example.com unknown")
        assert "fail_rp2@example.com" not in text
        assert "OSError" in text
