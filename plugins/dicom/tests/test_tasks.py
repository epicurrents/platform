"""Contract tests for the dicom purge task.

``TestPurgeDeletedDicomStudiesContract`` pins the load-bearing invariants of
``purge_deleted_dicom_studies``: active studies survive any age, the cutoff
boundary cuts in the right direction, an unlink failure preserves the rows
for the next run, a missing file still purges, every purged row lands in the
audit trail under the ``dicom.purge`` activity, and the orphan branch reaps
only stale PENDING instances.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from plugins.dicom.models import DicomInstance, DicomStudy
from plugins.dicom.tasks import purge_deleted_dicom_studies


def _backdate_created(instance, days):
    type(instance).objects.filter(pk=instance.pk).update(created_at=timezone.now() - timedelta(days=days))


@pytest.mark.django_db
class TestPurgeDeletedDicomStudiesContract:
    def test_hard_deletes_past_retention(self, user, make_study, dicom_dirs):
        upload_dir, _ = dicom_dirs
        study = make_study(user, deleted_at=timezone.now() - timedelta(days=31), instance_count=2)
        stored = list(DicomInstance.objects.filter(series__study=study).values_list("stored_name", flat=True))
        result = purge_deleted_dicom_studies()
        assert result["purged"] == 1
        assert not DicomStudy.objects.filter(pk=study.pk).exists()
        assert not DicomInstance.objects.exists()
        for name in stored:
            assert not (upload_dir / name).exists()

    def test_keeps_recently_deleted(self, user, make_study):
        study = make_study(user, deleted_at=timezone.now() - timedelta(days=5))
        purge_deleted_dicom_studies()
        assert DicomStudy.objects.filter(pk=study.pk).exists()

    def test_active_study_never_purged_regardless_of_age(self, user, make_study):
        study = make_study(user)
        DicomStudy.objects.filter(pk=study.pk).update(created_at=timezone.now() - timedelta(days=3650))
        purge_deleted_dicom_studies()
        assert DicomStudy.objects.filter(pk=study.pk).exists()

    def test_cutoff_boundary_just_inside_window_kept(self, user, make_study, settings):
        settings.DICOM_TRASH_RETENTION_DAYS = 30
        study = make_study(user, deleted_at=timezone.now() - timedelta(days=29, hours=23))
        purge_deleted_dicom_studies()
        assert DicomStudy.objects.filter(pk=study.pk).exists()

    def test_unlink_failure_preserves_rows(self, user, make_study):
        study = make_study(user, deleted_at=timezone.now() - timedelta(days=31))
        with patch("plugins.dicom.signals.Path.unlink", side_effect=OSError("locked")):
            result = purge_deleted_dicom_studies()
        assert result["errors"] == 1
        assert DicomStudy.objects.filter(pk=study.pk).exists()
        assert DicomInstance.objects.filter(series__study=study).exists()

    def test_missing_file_still_purges_row(self, user, make_study):
        study = make_study(user, deleted_at=timezone.now() - timedelta(days=31), with_files=False)
        result = purge_deleted_dicom_studies()
        assert result["purged"] == 1
        assert not DicomStudy.objects.filter(pk=study.pk).exists()

    def test_produces_delete_audit_rows(self, user, make_study):
        from activity.models import Activity, ObjectChangeLog

        study = make_study(user, deleted_at=timezone.now() - timedelta(days=31))
        study_pk = study.pk
        purge_deleted_dicom_studies()
        activity = Activity.objects.filter(verb="dicom.purge").first()
        assert activity is not None
        assert ObjectChangeLog.objects.filter(
            object_id=str(study_pk),
            action=ObjectChangeLog.ACTION_DELETE,
        ).exists()

    def test_orphan_reaper_removes_stale_pending(self, user, make_study, dicom_dirs):
        upload_dir, staging_dir = dicom_dirs
        study = make_study(user)
        series = study.series.first()
        orphan = DicomInstance.objects.create(
            series=series,
            sop_instance_uid=f"{study.study_instance_uid}.orphan",
            stored_name="orphan.dcm",
            file_size=1,
            status=DicomInstance.Status.PENDING,
        )
        (staging_dir / "orphan.dcm").write_bytes(b"x")
        _backdate_created(orphan, days=2)

        result = purge_deleted_dicom_studies()
        assert result["reaped"] == 1
        assert not DicomInstance.objects.filter(pk=orphan.pk).exists()
        assert not (staging_dir / "orphan.dcm").exists()
        # The READY sibling and the study survive; aggregates refreshed.
        study.refresh_from_db()
        assert study.num_instances == 1

    def test_orphan_reaper_keeps_fresh_pending_and_old_ready(self, user, make_study):
        study = make_study(user)
        series = study.series.first()
        fresh = DicomInstance.objects.create(
            series=series,
            sop_instance_uid=f"{study.study_instance_uid}.fresh",
            stored_name="fresh.dcm",
            file_size=1,
            status=DicomInstance.Status.PENDING,
        )
        ready = DicomInstance.objects.get(series=series, status=DicomInstance.Status.READY)
        _backdate_created(ready, days=400)

        result = purge_deleted_dicom_studies()
        assert result["reaped"] == 0
        assert DicomInstance.objects.filter(pk=fresh.pk).exists()
        assert DicomInstance.objects.filter(pk=ready.pk).exists()
