"""Contract tests for media.permissions.can_read_via_attachment.

Backstops the LOAD-BEARING attachment read-permission extension: media attached
to a parent inherits that parent's read access, from every source (direct grant,
dataset / collection inheritance, share token). A regression here silently hides
attached media from every grantee.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from epicurrents.permissions import can_read_object
from media.models import MediaFile
from recordings.models import Recording


def _make_recording(author):
    return Recording.objects.create(
        author=author,
        stored_name=("A" * 32) + ".edf",
        original_name="case.edf",
        file_size=1024,
        status=Recording.Status.READY,
    )


def _make_media(author, attach_to=None):
    media = MediaFile.objects.create(
        author=author,
        media_type=MediaFile.MediaType.VIDEO,
        original_name="clip.mp4",
        stored_name=("M" * 32) + ".mp4",
        file_extension=".mp4",
        file_size=4,
        file_path="/tmp/clip.mp4",
        file_hash="x" * 64,
        content_hash="c" * 32,
    )
    if attach_to is not None:
        ct = ContentType.objects.get_for_model(attach_to, for_concrete_model=False)
        media.attachment_content_type = ct
        media.attachment_object_id = str(attach_to.pk)
        media.save(update_fields=["attachment_content_type", "attachment_object_id"])
    return media


def _grant_read(target, obj, giver):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(obj.pk),
        access_giver=giver,
        access_target=target,
        can_read=True,
    )


@pytest.mark.django_db
class TestMediaInheritsAttachmentAccess:
    def test_grantee_of_recording_can_read_attached_media(self, user, make_user):
        reader = make_user(username="media_reader")
        rec = _make_recording(user)
        media = _make_media(user, attach_to=rec)
        _grant_read(reader, rec, giver=user)
        assert can_read_object(user=reader, obj=media) is True

    def test_no_recording_access_means_no_media_access(self, user, make_user):
        reader = make_user(username="media_no_access")
        rec = _make_recording(user)
        media = _make_media(user, attach_to=rec)
        # No grant anywhere — the extension must not invent access.
        assert can_read_object(user=reader, obj=media) is False

    def test_unattached_media_not_granted_by_extension(self, user, make_user):
        reader = make_user(username="media_unattached")
        rec = _make_recording(user)
        _grant_read(reader, rec, giver=user)
        media = _make_media(user, attach_to=None)
        # A grant on some unrelated recording must not leak to detached media.
        assert can_read_object(user=reader, obj=media) is False

    def test_inherits_via_dataset_membership(self, user, make_user):
        from library.models import Dataset, DatasetItem

        reader = make_user(username="media_via_dataset")
        rec = _make_recording(user)
        media = _make_media(user, attach_to=rec)
        dataset = baker.make(Dataset, author=user)
        rec_ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        DatasetItem.objects.create(dataset=dataset, content_type=rec_ct, object_id=str(rec.pk))
        _grant_read(reader, dataset, giver=user)
        # The reader has access to the recording only through the dataset; the
        # media must inherit that same transitive access.
        assert can_read_object(user=reader, obj=rec) is True
        assert can_read_object(user=reader, obj=media) is True

    def test_grantee_cannot_read_media_on_failed_recording(self, user, make_user):
        reader = make_user(username="media_failed")
        rec = _make_recording(user)
        rec.status = Recording.Status.FAILED
        rec.save(update_fields=["status"])
        media = _make_media(user, attach_to=rec)
        _grant_read(reader, rec, giver=user)
        # A FAILED recording is 404-hidden from grantees even with a stale
        # AccessRight; its attached media must be hidden the same way.
        assert can_read_object(user=reader, obj=media) is False

    def test_author_still_reads_media_on_own_failed_recording(self, user):
        rec = _make_recording(user)
        rec.status = Recording.Status.FAILED
        rec.save(update_fields=["status"])
        media = _make_media(user, attach_to=rec)
        _grant_read(user, rec, giver=user)  # author self-grant, as upload creates
        assert can_read_object(user=user, obj=media) is True
