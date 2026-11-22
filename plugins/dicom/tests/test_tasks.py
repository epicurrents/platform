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

    def test_staging_sweep_removes_stale_files_no_row_names(self, user, make_study, dicom_dirs):
        import os

        upload_dir, staging_dir = dicom_dirs
        old = (timezone.now() - timedelta(hours=25)).timestamp()
        stale = [staging_dir / "aaaa.dcm", staging_dir / "aaaa.dcm.deid", staging_dir / "bbbb.dcm.deid"]
        fresh = [staging_dir / "cccc.dcm", staging_dir / "cccc.dcm.deid"]
        # Not a name the upload endpoint writes: left alone whatever its age.
        foreign = [staging_dir / "notes.txt", staging_dir / "dddd.dcm.part"]
        for path in stale + fresh + foreign:
            path.write_bytes(b"x")
        for path in stale + foreign:
            os.utime(path, (old, old))
        (staging_dir / "eeee.dcm").mkdir()
        os.utime(staging_dir / "eeee.dcm", (old, old))
        stored = upload_dir / "aaaa.dcm"
        stored.write_bytes(b"x")
        os.utime(stored, (old, old))

        result = purge_deleted_dicom_studies()

        assert result["swept"] == 3
        assert not any(path.exists() for path in stale)
        assert all(path.exists() for path in fresh + foreign)
        assert (staging_dir / "eeee.dcm").is_dir()
        # Only the staging directory is swept.
        assert stored.exists()

    def test_staging_sweep_leaves_the_files_of_a_pending_instance(self, user, make_study, dicom_dirs):
        import os

        _, staging_dir = dicom_dirs
        series = make_study(user).series.first()
        DicomInstance.objects.create(
            series=series,
            sop_instance_uid="1.2.3.4",
            stored_name="pending.dcm",
            file_size=1,
            status=DicomInstance.Status.PENDING,
        )
        old = (timezone.now() - timedelta(hours=30)).timestamp()
        for name in ("pending.dcm", "pending.dcm.deid"):
            (staging_dir / name).write_bytes(b"x")
            os.utime(staging_dir / name, (old, old))

        # The row is younger than the reaper's threshold, so the request may still own its files.
        result = purge_deleted_dicom_studies()

        assert (result["reaped"], result["swept"]) == (0, 0)
        assert (staging_dir / "pending.dcm").exists() and (staging_dir / "pending.dcm.deid").exists()

    def test_staging_sweep_takes_the_rewritten_copy_of_a_reaped_orphan(self, user, make_study, dicom_dirs):
        import os

        _, staging_dir = dicom_dirs
        series = make_study(user).series.first()
        orphan = DicomInstance.objects.create(
            series=series,
            sop_instance_uid="1.2.3.5",
            stored_name="orphan.dcm",
            file_size=1,
            status=DicomInstance.Status.PENDING,
        )
        _backdate_created(orphan, days=2)
        old = (timezone.now() - timedelta(days=2)).timestamp()
        for name in ("orphan.dcm", "orphan.dcm.deid"):
            (staging_dir / name).write_bytes(b"x")
            os.utime(staging_dir / name, (old, old))

        result = purge_deleted_dicom_studies()

        assert (result["reaped"], result["swept"]) == (1, 1)
        assert not any(staging_dir.iterdir())

    def test_staging_sweep_survives_a_missing_directory(self, settings, tmp_path):
        settings.DICOM_STAGING_PATH = str(tmp_path / "absent")
        assert purge_deleted_dicom_studies()["swept"] == 0

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
