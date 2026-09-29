"""The release record's governance: deletion and erasure with releases in place, the run's bounds and record.

Covers ``DatasetItem.release`` RESTRICT (a released dataset, its trash purge and its author's erasure all go
through), the per-row resilience of ``purge_deleted_library``, the assessment reference leaving the live row
with its author's account, ``run_release`` refusing future and backdated runs, the per-member pass version,
the sign-off rows and their subject export, the anonymity report's present and withdrawn counts, the web
release operation's arguments, and the dataset PATCH and DELETE paths reading under the row lock.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.test import override_settings
from django.utils import timezone

from conftest import delete_json, patch_json
from library.models import Dataset, DatasetItem, DatasetRelease, DatasetReleaseSignOff
from library.release import run_release
from library.reports import anonymity_report
from library.tests.test_release import _gated_dataset, _grant, _recording, _release, frozen_today  # noqa: F401
from recordings.models import Recording, RecordingMeta

pytestmark = pytest.mark.django_db


def _meta(recording, version):
    ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    return RecordingMeta.objects.update_or_create(
        content_type=ct,
        object_id=str(recording.pk),
        defaults={
            "format": "EDF",
            "duration": 10.0,
            "data_record_count": 10,
            "data_record_duration": 1.0,
            "signal_count": 1,
            "deidentification_version": version,
        },
    )[0]


class TestReleasedDatasetsCanGo:
    def test_a_released_dataset_is_deleted_with_its_releases(self, make_user):
        author = make_user()
        dataset, items = _gated_dataset(author, _recording(author))
        release = _release(dataset, *items)
        dataset.delete()
        assert not DatasetRelease.objects.filter(pk=release.pk).exists()
        assert not DatasetItem.objects.filter(pk=items[0].pk).exists()

    def test_a_release_its_members_point_at_is_still_not_deleted_alone(self, make_user):
        author = make_user()
        dataset, items = _gated_dataset(author, _recording(author))
        release = _release(dataset, *items)
        with pytest.raises(models.RestrictedError):
            release.delete()

    def test_the_trash_purge_takes_a_released_dataset_and_survives_a_row_that_fails(self, make_user, monkeypatch):
        from library.tasks import purge_deleted_library

        author = make_user()
        released, items = _gated_dataset(author, _recording(author))
        _release(released, *items)
        stuck = Dataset.objects.create(author=author, name="stuck")
        after = Dataset.objects.create(author=author, name="after")
        long_ago = timezone.now() - timedelta(days=90)
        Dataset.objects.filter(pk__in=[released.pk, stuck.pk, after.pk]).update(deleted_at=long_ago)
        original_delete = Dataset.delete

        def failing_delete(self, *args, **kwargs):
            if self.pk == stuck.pk:
                raise RuntimeError("cannot")
            return original_delete(self, *args, **kwargs)

        monkeypatch.setattr(Dataset, "delete", failing_delete)
        with override_settings(LIBRARY_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_library()
        assert result["Dataset"] == 2 and result["failed"] == 1
        assert list(Dataset.objects.values_list("pk", flat=True)) == [stuck.pk]

    def test_the_author_of_a_released_dataset_can_be_erased(self, make_user):
        author = make_user(username="releaser")
        dataset, items = _gated_dataset(author, _recording(author))
        release = _release(dataset, *items)
        call_command("erase_user", "releaser", "--yes", stdout=io.StringIO())
        assert not DatasetRelease.objects.filter(pk=release.pk).exists()
        assert not Dataset.objects.filter(pk=dataset.pk).exists()


class TestErasedRunnersReference:
    def test_erasing_the_runner_clears_the_reference_on_the_live_row(self, make_user):
        from activity.models import ObjectChangeLog

        curator, runner = make_user(), make_user(username="runner")
        dataset, items = _gated_dataset(curator, _recording(curator))
        release = _release(dataset, *items)
        DatasetRelease.objects.filter(pk=release.pk).update(author=runner, assessment_reference="Memo by J. Doe")
        call_command("erase_user", "runner", "--yes", stdout=io.StringIO())
        release.refresh_from_db()
        assert release.author is None and release.assessment_reference == ""
        ct = ContentType.objects.get_for_model(DatasetRelease)
        for change in ObjectChangeLog.objects.filter(content_type=ct, object_id=str(release.pk)):
            assert "J. Doe" not in json.dumps([change.before_state, change.changes])


@pytest.mark.usefixtures("frozen_today")
class TestRunBounds:
    def test_a_run_dated_after_today_is_refused(self, make_user):
        author = make_user()
        dataset, _ = _gated_dataset(author, _recording(author, created_at=datetime(2026, 7, 1, tzinfo=UTC)))
        with pytest.raises(ValueError, match="after today"):
            run_release(dataset, as_of=date(2026, 11, 16))
        assert not DatasetRelease.objects.exists()
        out = io.StringIO()
        with pytest.raises(CommandError, match="after today"):
            call_command("release_dataset", dataset.object_hash, "--as-of", "2027-03-01", stdout=out)

    def test_a_run_dated_before_the_latest_release_is_refused(self, make_user):
        author = make_user()
        dataset, _ = _gated_dataset(author, _recording(author, created_at=datetime(2026, 7, 1, tzinfo=UTC)))
        run_release(dataset, as_of=date(2026, 11, 1))
        with pytest.raises(ValueError, match="latest release"):
            run_release(dataset, as_of=date(2026, 10, 1))
        run_release(dataset, as_of=date(2026, 11, 1))
        assert DatasetRelease.objects.filter(dataset=dataset).count() == 2

    def test_the_run_records_each_members_version_and_the_report_compares_per_member(self, make_user):
        author = make_user()
        one = _recording(author, index=0, created_at=datetime(2026, 7, 1, tzinfo=UTC))
        two = _recording(author, index=1, created_at=datetime(2026, 7, 2, tzinfo=UTC))
        _meta(one, 1)
        _meta(two, 2)
        dataset, items = _gated_dataset(author, one, two)
        release, _eligible, _released = run_release(dataset, as_of=date(2026, 10, 1))
        assert release.deidentification_versions == [1, 2]
        items[0].refresh_from_db()
        assert items[0].released_deidentification_version == 1
        # Re-written at a version the run also released: invisible to a set comparison.
        _meta(one, 2)
        report = anonymity_report(dataset, release)
        changed = {m["member"].split(":")[1]: m["changed_since_release"] for m in report["members"]}
        assert changed == {one.stored_name.split(".")[0]: True, two.stored_name.split(".")[0]: False}

    def test_the_sign_off_is_rows_for_existing_accounts_and_exported(self, make_user):
        from user.export import export_user_data

        author, officer = make_user(), make_user()
        dataset, _ = _gated_dataset(author, _recording(author, created_at=datetime(2026, 7, 1, tzinfo=UTC)))
        release, _eligible, _released = run_release(
            dataset, as_of=date(2026, 10, 1), sign_off_user_ids=[officer.pk, 999_999]
        )
        assert list(release.sign_offs.values_list("user_id", flat=True)) == [officer.pk]
        assert anonymity_report(dataset, release)["sign_off_count"] == 1
        assert str(release.pk) in json.dumps(export_user_data(officer), default=str)
        officer.delete()
        assert DatasetReleaseSignOff.objects.get(release=release).user is None

    def test_actor_id_attributes_the_run(self, make_user):
        author = make_user()
        dataset, _ = _gated_dataset(author, _recording(author, created_at=datetime(2026, 7, 1, tzinfo=UTC)))
        call_command(
            "release_dataset",
            dataset.object_hash,
            "--as-of",
            "2026-10-01",
            "--actor-id",
            str(author.pk),
            stdout=io.StringIO(),
        )
        assert DatasetRelease.objects.get(dataset=dataset).author == author


class TestAnonymityReportPresence:
    def test_a_trashed_or_failed_member_counts_as_withdrawn(self, make_user):
        author = make_user()
        live, trashed, failed = (_recording(author, index=i) for i in range(3))
        dataset, items = _gated_dataset(author, live, trashed, failed)
        release = _release(dataset, *items)
        Recording.objects.filter(pk=trashed.pk).update(deleted_at=timezone.now())
        Recording.objects.filter(pk=failed.pk).update(status=Recording.Status.FAILED)
        report = anonymity_report(dataset, release)
        assert (report["present_count"], report["withdrawn_count"], report["pool_count"]) == (1, 2, 1)


class TestReleaseOperation:
    def test_the_web_operation_takes_no_reference_and_names_the_requester(self):
        from maintenance.operations import command_argv, get_operation

        operation = get_operation("library.release_dataset")
        assert "assessment_reference" not in operation.args_schema.model_fields
        args = operation.args_schema(dataset="A" * 32)
        argv = command_argv(operation, args, requested_by_id=7)
        assert argv[-2:] == ["--actor-id", "7"]
        assert command_argv(operation, args, requested_by_id=None) == operation.command_args(args)


class TestDatasetWritesUnderTheLock:
    def test_a_rename_from_a_stale_read_does_not_put_back_pool_state(self, client, make_user, monkeypatch):
        from library.api.v1 import ninja

        author = make_user()
        dataset = Dataset.objects.create(author=author, name="before")
        stale = Dataset.objects.get(pk=dataset.pk)
        Dataset.objects.filter(pk=dataset.pk).update(submission_profile="test.pool", release_gated=True)
        monkeypatch.setattr(ninja, "_get_active_dataset", lambda _identifier: stale)
        client.force_login(author)
        resp = patch_json(client, f"/api/v1/library/datasets/{dataset.object_hash}/", {"name": "after"})
        assert resp.status_code == 200
        dataset.refresh_from_db()
        assert (dataset.name, dataset.submission_profile, dataset.release_gated) == ("after", "test.pool", True)

    def test_delete_checks_the_pool_rule_on_the_locked_row(self, client, make_user, monkeypatch):
        from library.api.v1 import ninja
        from recordings.models import SubmissionLedger

        author = make_user()
        dataset = Dataset.objects.create(author=author, name="ds")
        stale = Dataset.objects.get(pk=dataset.pk)
        Dataset.objects.filter(pk=dataset.pk).update(submission_profile="test.pool", release_gated=True)
        SubmissionLedger.objects.create(dataset=dataset, contributor=make_user())
        from library.tests.test_release import _add

        _add(dataset, _recording(author))
        monkeypatch.setattr(ninja, "_get_active_dataset", lambda _identifier: stale)
        client.force_login(author)
        assert delete_json(client, f"/api/v1/library/datasets/{dataset.object_hash}/").status_code == 409
        dataset.refresh_from_db()
        assert dataset.deleted_at is None


class TestWriteGranteeCannotGate:
    def test_grant_holder_patch(self, client, make_user):
        author, writer = make_user(), make_user()
        dataset = Dataset.objects.create(author=author, name="ds")
        _grant(dataset, author, target=writer, can_write=True)
        client.force_login(writer)
        resp = patch_json(client, f"/api/v1/library/datasets/{dataset.object_hash}/", {"release_gated": True})
        assert resp.status_code == 403
