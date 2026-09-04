"""Tests for media Celery tasks — purge_deleted_media.

``TestPurgeDeletedMediaContract`` pins the load-bearing erasure invariants:
active rows are never touched, the retention cutoff is honoured in both
directions, a file-unlink failure preserves the DB row for retry, and every
purged row produces a DELETE audit entry under the ``media.purge`` activity.
"""

from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from media.models import MediaFile
from media.tasks import purge_deleted_media


def _make_media(user, tmp_path, *, name="clip.mp4", deleted_days_ago=None, write=True):
    path = tmp_path / name
    if write:
        path.write_bytes(b"data")
    deleted_at = timezone.now() - timezone.timedelta(days=deleted_days_ago) if deleted_days_ago is not None else None
    return MediaFile.objects.create(
        author=user,
        media_type=MediaFile.MediaType.VIDEO,
        original_name=name,
        stored_name=f"{name.upper().replace('.', '')}{'A' * 8}.mp4",
        file_extension=".mp4",
        file_size=4,
        file_path=str(path),
        file_hash="x" * 64,
        content_hash="y" * 32,
        deleted_at=deleted_at,
    )


@pytest.mark.django_db
class TestPurgeDeletedMediaContract:
    def test_hard_deletes_past_retention(self, user, tmp_path):
        media = _make_media(user, tmp_path, deleted_days_ago=31)
        path = media.file_path
        with override_settings(MEDIA_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_media()
        assert result["purged"] == 1
        assert not MediaFile.objects.filter(pk=media.pk).exists()
        from pathlib import Path

        assert not Path(path).exists()

    def test_keeps_recently_deleted(self, user, tmp_path):
        media = _make_media(user, tmp_path, deleted_days_ago=1)
        with override_settings(MEDIA_TRASH_RETENTION_DAYS=30):
            purge_deleted_media()
        assert MediaFile.objects.filter(pk=media.pk).exists()

    def test_active_row_never_purged_regardless_of_age(self, user, tmp_path):
        """A row with no ``deleted_at`` is live and must survive any age."""
        media = _make_media(user, tmp_path, deleted_days_ago=None)
        MediaFile.objects.filter(pk=media.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=3650),
        )
        with override_settings(MEDIA_TRASH_RETENTION_DAYS=30):
            purge_deleted_media()
        assert MediaFile.objects.filter(pk=media.pk).exists()

    def test_cutoff_boundary_just_inside_window_kept(self, user, tmp_path):
        media = _make_media(user, tmp_path, deleted_days_ago=29)
        with override_settings(MEDIA_TRASH_RETENTION_DAYS=30):
            purge_deleted_media()
        assert MediaFile.objects.filter(pk=media.pk).exists()

    def test_unlink_failure_preserves_row(self, user, tmp_path):
        """If the file can't be removed, the row stays so the next run retries."""
        media = _make_media(user, tmp_path, deleted_days_ago=31)
        with override_settings(MEDIA_TRASH_RETENTION_DAYS=30):
            with patch("media.tasks.Path.unlink", side_effect=OSError("locked")):
                result = purge_deleted_media()
        assert result["errors"] == 1
        assert result["purged"] == 0
        assert MediaFile.objects.filter(pk=media.pk).exists()

    def test_missing_file_still_purges_row(self, user, tmp_path):
        """A row whose file already vanished is still erased (no file to unlink)."""
        media = _make_media(user, tmp_path, deleted_days_ago=31, write=False)
        with override_settings(MEDIA_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_media()
        assert result["purged"] == 1
        assert not MediaFile.objects.filter(pk=media.pk).exists()

    def test_produces_delete_audit_row(self, user, tmp_path):
        from activity.models import Activity, ObjectChangeLog

        media = _make_media(user, tmp_path, deleted_days_ago=31)
        media_pk = media.pk
        with override_settings(MEDIA_TRASH_RETENTION_DAYS=30):
            purge_deleted_media()

        delete_rows = list(
            ObjectChangeLog.objects.filter(
                object_id=str(media_pk),
                action=ObjectChangeLog.ACTION_DELETE,
            )
        )
        assert len(delete_rows) == 1, (
            "purge_deleted_media must produce a DELETE audit row for each purged "
            "media file via the pre_delete signal inside the audited scope"
        )
        parent = Activity.objects.filter(verb="media.purge").latest("created_at")
        assert delete_rows[0].activity_id == parent.pk
