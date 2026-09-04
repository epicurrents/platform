"""Bulk import must not write personal data into the permanent audit trail.

Found by the 2026-08-26 GDPR re-audit, which is why these are anchored to the
mechanism rather than to the command. Two separate permanent stores are in play
and each fails differently.

``Activity.metadata`` is reachable by ``erase_subject`` only where the row's
``target_content_type`` is the user model. The import row targets the
``ImportJob``, so anything personal in its metadata is beyond every erasure path
the platform has — not scrubbed, not tombstoned, not deleted with the account.
An identifier is safe there because it stops resolving once the account is
gone; a username never stops being one.

``ObjectChangeLog`` is reachable, but only for fields a registration names. The
import rows carry the source filename and the operator's directory, which are
``original_name`` and ``processing_error`` wearing different field names — and
those two are masked on ``Recording`` precisely because uploaded filenames carry
subject identifiers.
"""

import pytest
from django.contrib.contenttypes.models import ContentType

from activity.audit import MASK_PREFIX
from activity.erasure import erase_subject
from activity.models import Activity, ObjectChangeLog
from activity.system_activity import with_system_activity
from recordings.models import ImportJob, ImportJobFile

# Both stand in for a real export directory named after its subject, which is
# the shape the masking exists for.
SUBJECT_DIR = "/data/imports/SmithJohn_1984-02-11"
SUBJECT_FILE = "SmithJohn_1984-02-11_eeg.edf"


def _run_import_scope(job, owner):
    """Reproduce the audited scope `import_recordings` opens, and one file write.

    Deliberately not a call into the command: the command needs a real tree of
    EDF files on disk, and what is under test is the shape of what it records,
    which is this block. `TestTheCommandStillUsesThisShape` is what keeps the
    two from drifting apart.
    """
    with with_system_activity(
        "recordings.import",
        interface=Activity.Interface.COMMAND,
        target=job,
        metadata={
            "structure": job.structure,
            "owner_id": owner.pk,
            "reprocess": False,
        },
    ):
        job_file = ImportJobFile.objects.create(job=job, relative_path=SUBJECT_FILE)
        job_file.status = ImportJobFile.Status.DONE
        job_file.save()
        return job_file


@pytest.fixture
def imported(make_user):
    owner = make_user(username="alice_smith", email="alice@example.org")
    job = ImportJob.objects.create(
        owner=owner,
        source_path=SUBJECT_DIR,
        pipeline_label="import",
        structure=ImportJob.Structure.FLAT,
    )
    _run_import_scope(job, owner)
    return owner, job


def _audit_blob() -> str:
    """Every permanent payload the import touched, as one searchable string."""
    parts = [str(list(Activity.including_archived.all().values_list("verb", "metadata")))]
    for model in (ImportJob, ImportJobFile):
        content_type = ContentType.objects.get_for_model(model)
        rows = ObjectChangeLog.objects.filter(content_type=content_type)
        parts.append(str(list(rows.values_list("action", "before_state", "changes"))))
    return "".join(parts)


@pytest.mark.django_db
class TestNoPersonalDataInTheImportAuditTrail:
    def test_the_owners_username_is_never_recorded(self, imported):
        owner, _ = imported
        assert owner.username not in _audit_blob()

    def test_the_owner_is_identified_by_primary_key_instead(self, imported):
        """The metadata still has to identify who ran the import, or removing
        the username would be a loss of audit value rather than a fix."""
        owner, _ = imported
        row = Activity.including_archived.get(verb="recordings.import")
        assert row.metadata["owner_id"] == owner.pk

    def test_the_source_directory_is_not_in_activity_metadata(self, imported):
        row = Activity.including_archived.get(verb="recordings.import")
        assert SUBJECT_DIR not in str(row.metadata)

    def test_the_source_directory_is_still_on_the_live_row(self, imported):
        """It is not deleted, only kept out of the permanent trail — the
        operator needs to know what was imported, and the live row can be
        deleted where an audit row cannot."""
        _, job = imported
        assert ImportJob.objects.get(pk=job.pk).source_path == SUBJECT_DIR

    def test_the_source_filename_is_masked_in_the_change_log(self, imported):
        content_type = ContentType.objects.get_for_model(ImportJobFile)
        rows = ObjectChangeLog.objects.filter(content_type=content_type)
        assert rows.exists(), "no change-log rows written — this would pass vacuously"
        blob = str(list(rows.values_list("before_state", "changes")))
        assert SUBJECT_FILE not in blob
        assert MASK_PREFIX in blob

    def test_nothing_personal_survives_erasing_the_account(self, imported):
        """The whole point: after the account is gone and the trail scrubbed,
        neither the person nor the subject their file was named after is
        recoverable from what remains."""
        owner, _ = imported
        owner_pk = owner.pk
        owner.delete()
        erase_subject(owner_pk)

        blob = _audit_blob()
        assert "alice_smith" not in blob
        assert "SmithJohn" not in blob


@pytest.mark.django_db
class TestTheCommandStillUsesThisShape:
    """The tests above run a hand-built scope, so on their own they would keep
    passing if the command drifted. This reads the command instead."""

    def _source(self) -> str:
        from pathlib import Path

        from django.conf import settings

        path = Path(settings.BASE_DIR) / "recordings" / "management" / "commands" / "import_recordings.py"
        return path.read_text()

    def test_the_command_records_the_owner_by_id(self):
        assert '"owner_id": owner.pk' in self._source()

    def test_the_command_does_not_record_a_username(self):
        source = self._source()
        assert "owner_username" not in source
        assert "get_username()" not in source

    def test_the_command_does_not_put_the_source_path_in_metadata(self):
        """The literal that was there. Narrow on purpose — a broad search for
        `source_path` would match the many legitimate uses in the same file."""
        assert '"source_path": str(source_path)' not in self._source()
