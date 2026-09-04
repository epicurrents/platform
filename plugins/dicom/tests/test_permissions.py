"""Tests for the dicom attachment read-permission extension.

A study attached to a recording inherits the recording's read access — from a
direct grant or any other source ``can_read_object`` resolves. Trashed
studies and FAILED-recording parents are excluded.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from epicurrents.permissions import can_read_object
from recordings.models import Recording


@pytest.fixture
def recording(make_user):
    author = make_user()
    rec = baker.make(
        Recording,
        author=author,
        status=Recording.Status.READY,
        content_hash="R" * 64,
    )
    # Author self-grant, mirroring the upload contract — can_read_object has
    # no implicit author fast-path, so the attachment extension's delegation
    # to the parent needs a real AccessRight row for the author too.
    _grant_read(rec, author, author)
    return rec


def _attach(study, parent):
    study.attachment_content_type = ContentType.objects.get_for_model(parent, for_concrete_model=False)
    study.attachment_object_id = str(parent.pk)
    study.save(update_fields=["attachment_content_type", "attachment_object_id"])


def _grant_read(obj, giver, target):
    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(obj, for_concrete_model=False),
        object_id=str(obj.pk),
        access_giver=giver,
        access_target=target,
        can_read=True,
    )


@pytest.mark.django_db
class TestStudyInheritsAttachmentAccess:
    def test_grantee_of_parent_reads_attached_study(self, make_user, make_study, recording):
        study = make_study(recording.author)
        _attach(study, recording)
        grantee = make_user()
        _grant_read(recording, recording.author, grantee)
        assert can_read_object(grantee, study) is True

    def test_no_parent_access_denies(self, make_user, make_study, recording):
        study = make_study(recording.author)
        _attach(study, recording)
        assert can_read_object(make_user(), study) is False

    def test_unattached_study_not_granted(self, make_user, make_study, recording):
        study = make_study(recording.author)
        grantee = make_user()
        _grant_read(recording, recording.author, grantee)
        assert can_read_object(grantee, study) is False

    def test_trashed_study_not_readable_via_parent(self, make_user, make_study, recording):
        from django.utils import timezone

        study = make_study(recording.author, deleted_at=timezone.now())
        _attach(study, recording)
        grantee = make_user()
        _grant_read(recording, recording.author, grantee)
        assert can_read_object(grantee, study) is False

    def test_failed_recording_parent_hides_study_from_grantee(self, make_user, make_study, recording):
        study = make_study(recording.author)
        _attach(study, recording)
        grantee = make_user()
        _grant_read(recording, recording.author, grantee)
        recording.status = Recording.Status.FAILED
        recording.save(update_fields=["status"])
        assert can_read_object(grantee, study) is False
        # The recording's author still reads through the attachment.
        assert can_read_object(recording.author, study) is True
