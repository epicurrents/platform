"""Tests for the explain_access management command — resolution-path reporting, read-only."""

from io import StringIO

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command
from model_bakery import baker

from epicurrents.models import AccessRight


def _run(*args, **kwargs):
    out = StringIO()
    call_command("explain_access", *args, stdout=out, **kwargs)
    return out.getvalue()


@pytest.fixture
def recording(db, user):
    return baker.make("recordings.Recording", author=user, status="ready")


@pytest.mark.django_db
class TestExplainAccess:
    def test_superuser_fast_path(self, recording, make_superuser):
        admin = make_superuser()
        output = _run("recordings.recording", str(recording.pk), admin.get_username())
        assert "GRANTED at step 1" in output

    def test_author_shortcut_reported(self, recording, user):
        output = _run("recordings.recording", str(recording.pk), user.get_username())
        assert "AUTHOR" in output

    def test_direct_row_reports_middleware_flag(self, recording, user, make_user):
        reader = make_user()
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
            apply_middleware=True,
        )
        output = _run("recordings.recording", str(recording.pk), reader.get_username())
        assert "GRANTED at step 4" in output
        assert "apply_middleware=True" in output
        assert f"user:{reader.pk}" in output

    def test_direct_row_ordering_matches_resolver(self, recording, user, make_user):
        # The command mirrors the resolver's tie-break: the direct user row is
        # the one reported, not the earlier-created sanitizing group row.
        from django.contrib.auth.models import Group

        reader = make_user()
        group = Group.objects.create(name="explain-readers")
        reader.groups.add(group)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target_group=group,
            can_read=True,
            apply_middleware=True,
        )
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
            apply_middleware=False,
        )
        output = _run("recordings.recording", str(recording.pk), reader.get_username())
        assert "GRANTED at step 4" in output
        assert "apply_middleware=False" in output
        assert f"user:{reader.pk}" in output

    def test_dataset_extension_reported(self, recording, user, make_user):
        from library.models import Dataset, DatasetItem

        reader = make_user()
        dataset = Dataset.objects.create(author=user, name="exp")
        DatasetItem.objects.create(
            dataset=dataset,
            content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
            object_id=str(recording.pk),
        )
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(dataset, for_concrete_model=False),
            object_id=str(dataset.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
        )
        output = _run("recordings.recording", str(recording.pk), reader.get_username())
        assert "GRANTED at step 5" in output
        assert "can_read_via_dataset" in output

    def test_share_token_matches_direct_row(self, recording, user):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            public_share_token="explain-tok",
            can_read=True,
        )
        output = _run("recordings.recording", str(recording.pk), share_token="explain-tok")
        assert "GRANTED at step 4" in output
        assert "share-token" in output

    def test_visibility_gate_reported(self, recording, user, make_user):
        reader = make_user()
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
        )
        recording.status = "failed"
        recording.save(update_fields=["status"])
        output = _run("recordings.recording", str(recording.pk), reader.get_username())
        assert "DENIED at step 2" in output
        assert "recording_hidden_from_reader" in output
        assert "no grant can surface it" in output

    def test_denied_reports_every_step(self, recording, make_user):
        outsider = make_user()
        output = _run("recordings.recording", str(recording.pk), outsider.get_username())
        assert "DENIED" in output
        assert "granted=False" in output
        assert "step 4: no matching direct AccessRight row" in output

    def test_missing_object_errors(self, db):
        with pytest.raises(CommandError):
            _run("recordings.recording", "999999")

    def test_unknown_model_errors(self, db):
        with pytest.raises(CommandError):
            _run("nosuch.model", "1")
