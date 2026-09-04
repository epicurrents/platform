"""Tests for the erase_user management command (GDPR Art. 17 account erasure).

Contract test for the load-bearing erasure pathway: the command must remove
the account and its cascade, unlink owned files, flush sessions, and leave
no residual subject identifiers anywhere in the audit trail.
"""

import json
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError

from activity.models import Activity, ObjectChangeLog
from activity.system_activity import with_system_activity

USERNAME = "erasecmd"
EMAIL = "erase.cmd@example.com"


def _make_subject(make_user, tmp_path, client):
    """Create a user with owned files, subscriptions, identity, and a session."""
    from model_bakery import baker

    user = make_user(username=USERNAME, email=EMAIL, first_name="Erase")

    recording_file = tmp_path / "recording.edf"
    recording_file.write_bytes(b"\x00" * 16)
    with with_system_activity("tests.subject.setup", interface=Activity.Interface.COMMAND):
        baker.make(
            "recordings.Recording",
            author=user,
            original_name="patient-initials.edf",
            file_path=str(recording_file),
        )
        media_file = tmp_path / "clip.mp4"
        media_file.write_bytes(b"\x00" * 16)
        baker.make(
            "media.MediaFile",
            author=user,
            media_type="video",
            original_name="clip.mp4",
            file_path=str(media_file),
            file_size=16,
        )
        baker.make("notifications.PushSubscription", user=user)
        baker.make("user.ExternalIdentity", user=user, email=EMAIL)

    response = client.post(
        "/api/v1/user/login",
        json.dumps({"username": USERNAME, "password": "testpass123"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return user, recording_file, media_file


def _subject_sessions(user_pk):
    return [s for s in Session.objects.all() if s.get_decoded().get("_auth_user_id") == str(user_pk)]


@pytest.mark.django_db
class TestEraseUserCommand:
    def test_full_erasure(self, make_user, tmp_path, client):
        user, recording_file, media_file = _make_subject(make_user, tmp_path, client)
        user_pk = user.pk
        assert _subject_sessions(user_pk)

        out = StringIO()
        call_command("erase_user", USERNAME, "--yes", stdout=out)

        User = get_user_model()
        assert not User.objects.filter(pk=user_pk).exists()
        assert not recording_file.exists()
        assert not media_file.exists()
        assert not _subject_sessions(user_pk)

        # No residual subject identifiers anywhere in the audit trail.
        for row in ObjectChangeLog.objects.all():
            payload = json.dumps([row.before_state, row.changes])
            assert USERNAME not in payload
            assert EMAIL not in payload
        for activity in Activity.including_archived.all():
            assert USERNAME not in json.dumps(activity.metadata)

        assert ObjectChangeLog.objects.filter(action=ObjectChangeLog.ACTION_ERASE, object_id=str(user_pk)).exists()
        assert Activity.objects.filter(verb="user.account.erase").exists()

    def test_dry_run_changes_nothing(self, make_user, tmp_path, client):
        user, recording_file, _ = _make_subject(make_user, tmp_path, client)
        out = StringIO()
        call_command("erase_user", USERNAME, stdout=out)
        assert "Dry run" in out.getvalue()
        assert get_user_model().objects.filter(pk=user.pk).exists()
        assert recording_file.exists()

    def test_unlink_failure_aborts_before_db_changes(self, make_user, tmp_path, client):
        from model_bakery import baker

        user = make_user(username=USERNAME, email=EMAIL)
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        (blocked / "content").write_bytes(b"\x00")
        with with_system_activity("tests.subject.setup", interface=Activity.Interface.COMMAND):
            baker.make("recordings.Recording", author=user, file_path=str(blocked))
        with pytest.raises(CommandError, match="Could not delete file"):
            call_command("erase_user", USERNAME, "--yes")
        assert get_user_model().objects.filter(pk=user.pk).exists()

    def test_scrub_only_for_already_deleted_user(self, make_user):
        user = make_user(username=USERNAME, email=EMAIL)
        user_pk = user.pk
        with with_system_activity("tests.subject.delete", interface=Activity.Interface.COMMAND):
            user.delete()
        assert ObjectChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(get_user_model()),
            object_id=str(user_pk),
            before_state__username=USERNAME,
        ).exists()

        out = StringIO()
        call_command("erase_user", "--user-id", str(user_pk), "--yes", stdout=out)

        for row in ObjectChangeLog.objects.all():
            payload = json.dumps([row.before_state, row.changes])
            assert USERNAME not in payload
            assert EMAIL not in payload

    def test_scrub_only_refuses_existing_user(self, make_user):
        user = make_user(username=USERNAME)
        with pytest.raises(CommandError, match="still exists"):
            call_command("erase_user", "--user-id", str(user.pk), "--yes")

    def test_missing_user_suggests_scrub_path(self, db):
        with pytest.raises(CommandError, match="--user-id"):
            call_command("erase_user", "ghost", "--yes")

    def test_mismatched_user_id_rejected(self, make_user):
        user = make_user(username=USERNAME)
        with pytest.raises(CommandError, match="does not match"):
            call_command("erase_user", USERNAME, "--user-id", str(user.pk + 1), "--yes")
