"""Tests for the index_dicom bulk-import management command."""

import pytest
from django.core.management import call_command

from plugins.dicom.models import DicomInstance, DicomStudy


@pytest.mark.django_db
class TestIndexDicomCommand:
    def test_imports_directory(self, user, make_dicom_file, tmp_path, dicom_dirs):
        upload_dir, _ = dicom_dirs
        src = tmp_path / "archive"
        make_dicom_file(src, study_uid="1.2.3.1", series_uid="1.2.3.1.1", sop_uid="1.2.3.1.1.1")
        make_dicom_file(src, study_uid="1.2.3.1", series_uid="1.2.3.1.1", sop_uid="1.2.3.1.1.2")

        call_command("index_dicom", str(src), "--user", user.username)

        study = DicomStudy.objects.get(study_instance_uid="1.2.3.1")
        assert study.author == user
        assert study.num_instances == 2
        for inst in DicomInstance.objects.all():
            assert inst.status == DicomInstance.Status.READY
            assert (upload_dir / inst.stored_name).exists()

    def test_dry_run_makes_no_changes(self, user, make_dicom_file, tmp_path):
        src = tmp_path / "archive"
        make_dicom_file(src)
        call_command("index_dicom", str(src), "--user", user.username, "--dry-run")
        assert DicomStudy.objects.count() == 0
        assert DicomInstance.objects.count() == 0

    def test_resume_skips_already_imported(self, user, make_dicom_file, tmp_path):
        src = tmp_path / "archive"
        make_dicom_file(src, name="a.dcm")
        call_command("index_dicom", str(src), "--user", user.username)
        assert DicomInstance.objects.count() == 1

        # Second run with --resume re-imports nothing (same content hash).
        call_command("index_dicom", str(src), "--user", user.username, "--resume")
        assert DicomInstance.objects.count() == 1

    def test_duplicate_sop_without_resume_not_duplicated(self, user, make_dicom_file, tmp_path):
        src = tmp_path / "archive"
        make_dicom_file(src, study_uid="1.2.3.1", series_uid="1.2.3.1.1", sop_uid="1.2.3.1.1.1")
        call_command("index_dicom", str(src), "--user", user.username)
        call_command("index_dicom", str(src), "--user", user.username)
        assert DicomInstance.objects.count() == 1

    def test_non_dicom_files_skipped(self, user, tmp_path):
        src = tmp_path / "archive"
        src.mkdir()
        (src / "junk.dcm").write_bytes(b"not dicom at all")
        call_command("index_dicom", str(src), "--user", user.username)
        assert DicomInstance.objects.count() == 0

    def test_unknown_user_fails(self, db, tmp_path):
        from django.core.management.base import CommandError

        src = tmp_path / "archive"
        src.mkdir()
        with pytest.raises(CommandError):
            call_command("index_dicom", str(src), "--user", "nobody")

    def test_import_runs_in_system_activity_scope(self, user, make_dicom_file, tmp_path):
        from activity.models import Activity

        src = tmp_path / "archive"
        make_dicom_file(src)
        call_command("index_dicom", str(src), "--user", user.username)
        activity = Activity.objects.filter(verb="dicom.import").first()
        assert activity is not None
        assert activity.interface == Activity.Interface.COMMAND
