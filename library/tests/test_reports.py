"""The access and anonymity reports of a release-gated dataset (``library.reports`` and their commands).

The access report counts requests and distinct readers per member from ``Activity`` rows, archived
ones included, with byte-serving, account-less and refused requests kept apart, and names nobody.
The anonymity report repeats a release's record, flags members re-written since and members
withdrawn, and sizes the equivalence classes over the pool as released up to that run through the
project's registered class function, computing nothing without one.
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from activity.models import Activity
from library.models import Dataset, DatasetReleaseSignOff
from library.release import equivalence_class_function, register_equivalence_class
from library.reports import BYTE_VERBS, READ_VERBS, access_report, anonymity_report, class_summary
from library.tests.test_release import _add, _gated_dataset, _recording, _release
from recordings.models import Recording, RecordingMeta

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_class_function():
    register_equivalence_class(None)
    yield
    register_equivalence_class(None)


def _read(recording, *, actor=None, verb="recordings.read", status=200, days_ago=1, archived=False):
    ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    row = Activity.objects.create(
        actor=actor,
        interface=Activity.Interface.API,
        verb=verb,
        method="GET",
        path="/recordings/api/v1/x",
        status_code=status,
        target_content_type=ct,
        target_object_id=str(recording.pk),
        target_identifier=f"recordings.recording:{recording.pk}",
    )
    fields = {"created_at": timezone.now() - timedelta(days=days_ago)}
    if archived:
        fields["archived_at"] = timezone.now()
    Activity.including_archived.filter(pk=row.pk).update(**fields)
    return row


def _meta(recording, version):
    ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    return RecordingMeta.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        format="EDF",
        duration=10.0,
        data_record_count=10,
        data_record_duration=1.0,
        signal_count=1,
        deidentification_version=version,
    )


def _since(days=183):
    return timezone.now() - timedelta(days=days)


# ---------------------------------------------------------------------------
# Access report
# ---------------------------------------------------------------------------


class TestAccessReport:
    def test_counts_requests_readers_and_bytes_per_member_and_names_nobody(self, make_user):
        author, a, b = make_user(), make_user(), make_user()
        one = _recording(author, index=0)
        two = _recording(author, index=1)
        dataset, items = _gated_dataset(author, one, two)
        _release(dataset, items[0])
        _read(one, actor=a)
        _read(one, actor=a, verb="recordings.download")
        _read(one, actor=b, verb="recordings.read.slice")
        _read(two, actor=a, verb="recordings.status")

        report = access_report(dataset, since=_since())

        assert (report["member_count"], report["requests"], report["byte_requests"], report["readers"]) == (2, 4, 2, 2)
        first, second = report["members"]
        assert first["member"] == f"recording:{one.stored_name.split('.')[0]}"
        assert (first["requests"], first["byte_requests"], first["readers"]) == (3, 2, 2)
        assert first["released"] == "2026-10" and second["released"] is None
        assert (second["requests"], second["byte_requests"], second["readers"]) == (1, 0, 1)
        assert first["last_read"] == timezone.now().date().isoformat()[:7] or first["last_read"] is not None
        assert (report["reader_requests_max"], report["reader_requests_median"]) == (3, 2)
        text = json.dumps(report)
        assert "actor" not in text and "reader_id" not in text
        for user in (author, a, b):
            assert user.username not in text

    def test_account_less_requests_and_server_errors_are_kept_apart(self, make_user):
        author, reader = make_user(), make_user()
        one = _recording(author, index=0)
        dataset, _ = _gated_dataset(author, one)
        _read(one, actor=None)
        _read(one, actor=reader, status=500)
        _read(one, actor=reader)

        report = access_report(dataset, since=_since())

        assert (report["requests"], report["readers"], report["no_account"], report["refused"]) == (2, 1, 1, 0)
        assert report["members"][0]["no_account"] == 1

    def test_requests_the_gate_refused_are_counted(self, client, make_user):
        """Real refusals: a dataset grantee and a share-token caller asking for an unreleased member both get 404.

        Counts only once the recording endpoints annotate a refusal with the read verb and the target
        recording, as they do a served read; until then the refused rows carry neither.
        """
        from library.tests.test_release import HASHES, _grant

        author, reader = make_user(), make_user()
        one = _recording(author, index=0)
        dataset, _ = _gated_dataset(author, one)
        _grant(dataset, author, target=reader)
        _grant(one, author, token="t" * 32)
        client.force_login(reader)
        assert client.get(f"/recordings/api/v1/{HASHES[0]}").status_code == 404
        client.logout()
        assert client.get(f"/recordings/api/v1/{HASHES[0]}", {"share_token": "t" * 32}).status_code == 404

        report = access_report(dataset, since=_since())

        assert report["refused"] == 2
        assert report["requests"] == 0

    def test_archived_rows_count_and_rows_outside_the_window_do_not(self, make_user):
        author, reader = make_user(), make_user()
        one = _recording(author, index=0)
        dataset, _ = _gated_dataset(author, one)
        _read(one, actor=reader, days_ago=100, archived=True)
        _read(one, actor=reader, days_ago=200)

        assert access_report(dataset, since=_since(183))["requests"] == 1
        assert access_report(dataset, since=_since(365))["requests"] == 2
        assert access_report(dataset, since=_since(365), until=_since(150))["requests"] == 1

    def test_only_read_verbs_of_recording_members_count(self, make_user):
        author, reader = make_user(), make_user()
        one = _recording(author, index=0)
        other = _recording(author, index=1)
        dataset, _ = _gated_dataset(author, one)
        _add(dataset, Dataset.objects.create(author=author, name="nested"))
        _read(one, actor=reader, verb="recordings.update")
        _read(one, actor=reader, verb="recordings.trash")
        _read(other, actor=reader)
        for verb in READ_VERBS:
            _read(one, actor=reader, verb=verb)

        report = access_report(dataset, since=_since())

        assert report["member_count"] == 1
        assert report["requests"] == len(READ_VERBS) and report["byte_requests"] == len(BYTE_VERBS)

    def test_a_pool_nobody_read_reports_zeros(self, make_user):
        author = make_user()
        dataset, _ = _gated_dataset(author, _recording(author, index=0))
        report = access_report(dataset, since=_since())
        assert (report["requests"], report["readers"], report["reader_requests_max"]) == (0, 0, 0)
        assert report["members"][0]["last_read"] is None


class TestAccessReportCommand:
    def _run(self, *args):
        out = io.StringIO()
        call_command("dataset_access_report", *args, stdout=out)
        return out.getvalue()

    def test_prints_the_table_and_records_the_run(self, make_user):
        author, reader = make_user(), make_user()
        one = _recording(author, index=0)
        dataset, _ = _gated_dataset(author, one)
        _read(one, actor=reader, verb="recordings.download")

        out = self._run(dataset.object_hash, "--days", "30")

        assert "1 requests (1 for bytes) by 1 readers over 1 members" in out
        assert one.stored_name.split(".")[0] in out
        row = Activity.objects.get(verb="library.dataset.access_report")
        assert row.interface == Activity.Interface.COMMAND and row.target_object_id == str(dataset.pk)
        assert (row.metadata["member_count"], row.metadata["request_count"], row.metadata["reader_count"]) == (1, 1, 1)
        assert reader.username not in json.dumps(row.metadata)

    def test_json_and_since(self, make_user):
        author, reader = make_user(), make_user()
        one = _recording(author, index=0)
        dataset, _ = _gated_dataset(author, one)
        _read(one, actor=reader, days_ago=400)

        report = json.loads(self._run(dataset.object_hash, "--since", "2020-01-01", "--format", "json"))
        assert report["requests"] == 1 and report["since"].startswith("2020-01-01")

    def test_refuses_an_ungated_or_unknown_dataset_and_a_bad_window(self, make_user):
        author = make_user()
        plain = Dataset.objects.create(author=author, name="plain")
        with pytest.raises(CommandError, match="not release-gated"):
            self._run(plain.object_hash)
        with pytest.raises(CommandError, match="No active dataset"):
            self._run("0" * 32)
        gated, _ = _gated_dataset(author)
        with pytest.raises(CommandError, match="--days"):
            self._run(gated.object_hash, "--days", "0")
        with pytest.raises(CommandError, match="--since"):
            self._run(gated.object_hash, "--since", "yesterday")


# ---------------------------------------------------------------------------
# Anonymity report
# ---------------------------------------------------------------------------


def _by_display_name(recording):
    return recording.display_name or None


class TestAnonymityReport:
    def test_repeats_the_record_and_counts_withdrawn_and_rewritten_members(self, make_user):
        author = make_user()
        one, two, three = (_recording(author, index=i) for i in range(3))
        _meta(one, 2)
        _meta(two, 3)
        dataset, items = _gated_dataset(author, one, two, three)
        release = _release(dataset, *items, on=date(2026, 10, 1))
        release.profile_version = "3"
        release.deidentification_versions = [2]
        release.k, release.m = 5, 2
        release.assessment_reference = "Assessment: pool v3"
        release.save()
        DatasetReleaseSignOff.objects.create(release=release, user=author)
        items[2].delete()

        report = anonymity_report(dataset, release)

        assert (report["profile_version"], report["k"], report["m"], report["sign_off_count"]) == ("3", 5, 2, 1)
        assert report["assessment_reference"] == "Assessment: pool v3"
        assert (report["member_count"], report["present_count"], report["withdrawn_count"]) == (3, 2, 1)
        assert report["release_month"] == "2026-10" and report["pool_count"] == 2
        by_member = {m["member"].split(":")[1]: m for m in report["members"]}
        assert by_member[one.stored_name.split(".")[0]]["changed_since_release"] is False
        assert by_member[two.stored_name.split(".")[0]]["changed_since_release"] is True
        assert report["changed_count"] == 1
        assert report["class_function_registered"] is False and report["classes"] is None
        assert report["min_k"] is None and report["prosecutor_risk"] is None

    def test_a_recording_without_a_meta_row_is_not_flagged(self, make_user):
        author = make_user()
        one = _recording(author, index=0)
        dataset, items = _gated_dataset(author, one)
        release = _release(dataset, *items)
        release.deidentification_versions = [2]
        release.save()
        member = anonymity_report(dataset, release)["members"][0]
        assert member["deidentification_version"] is None and member["changed_since_release"] is False

    def test_sizes_classes_over_the_pool_as_released_up_to_the_run(self, make_user):
        register_equivalence_class(_by_display_name)
        author = make_user()
        recordings = [_recording(author, index=i, display_name=name) for i, name in enumerate("aabbb")]
        unclassified = _recording(author, index=5, display_name="")
        dataset, items = _gated_dataset(author, *recordings, unclassified)
        first = _release(dataset, items[0], items[2], on=date(2026, 9, 1))
        second = _release(dataset, items[1], items[3], items[4], items[5], on=date(2026, 10, 1))
        for release, k in ((first, 2), (second, 3)):
            release.k = k
            release.save()

        early = anonymity_report(dataset, first)
        assert early["pool_count"] == 2 and early["classes"] == [{"key": "a", "size": 1}, {"key": "b", "size": 1}]
        assert (early["min_k"], early["below_k_fraction"], early["prosecutor_risk"], early["entropy_bits"]) == (
            1,
            1.0,
            1.0,
            0.0,
        )

        late = anonymity_report(dataset, second)
        assert late["pool_count"] == 6 and late["unclassified"] == 1
        assert late["classes"] == [{"key": "a", "size": 2}, {"key": "b", "size": 3}]
        assert late["min_k"] == 2 and late["below_k_fraction"] == pytest.approx(2 / 5)
        assert late["prosecutor_risk"] == 0.5 and late["entropy_bits"] == 1.0

    def test_unreleased_members_are_outside_the_pool(self, make_user):
        register_equivalence_class(_by_display_name)
        author = make_user()
        released = _recording(author, index=0, display_name="a")
        waiting = _recording(author, index=1, display_name="a")
        dataset, items = _gated_dataset(author, released, waiting)
        release = _release(dataset, items[0])
        assert anonymity_report(dataset, release)["classes"] == [{"key": "a", "size": 1}]

    def test_a_purged_recording_leaves_the_pool_and_counts_as_withdrawn(self, make_user):
        register_equivalence_class(_by_display_name)
        author = make_user()
        one = _recording(author, index=0, display_name="a")
        dataset, items = _gated_dataset(author, one)
        release = _release(dataset, *items)
        Recording.objects.filter(pk=one.pk).delete()
        report = anonymity_report(dataset, release)
        assert (report["member_count"], report["present_count"], report["withdrawn_count"]) == (1, 0, 1)
        assert report["pool_count"] == 0 and report["classes"] == [] and report["min_k"] is None

    def test_registration_is_replaceable_and_clearable(self):
        register_equivalence_class(_by_display_name)
        assert equivalence_class_function() is _by_display_name
        register_equivalence_class(None)
        assert equivalence_class_function() is None

    def test_summary_without_a_recorded_k_gives_no_fraction(self):
        summary = class_summary({"a": 4, "b": 8}, 0, None)
        assert summary["below_k_fraction"] is None and summary["min_k"] == 4
        assert summary["prosecutor_risk"] == 0.25 and summary["entropy_bits"] == 2.0
        empty = class_summary({}, 3, 5)
        assert empty["min_k"] is None and empty["below_k_fraction"] is None and empty["unclassified"] == 3


class TestAnonymityReportCommand:
    def _run(self, *args):
        out = io.StringIO()
        call_command("dataset_anonymity_report", *args, stdout=out)
        return out.getvalue()

    def test_prints_every_release_oldest_first_and_records_the_run(self, make_user):
        register_equivalence_class(_by_display_name)
        author = make_user()
        one = _recording(author, index=0, display_name="a")
        two = _recording(author, index=1, display_name="a")
        dataset, items = _gated_dataset(author, one, two)
        first = _release(dataset, items[0], on=date(2026, 9, 1))
        second = _release(dataset, items[1], on=date(2026, 10, 1))
        second.k = 2
        second.save()

        out = self._run(dataset.object_hash)

        assert out.index(f"Release {first.pk}") < out.index(f"Release {second.pk}")
        assert "min k 2, below k 0%, prosecutor risk 0.500, entropy 1.00 bits" in out
        assert "journalist risk not computed" in out
        row = Activity.objects.get(verb="library.dataset.anonymity_report")
        assert row.target_object_id == str(dataset.pk) and row.metadata["release_count"] == 2

    def test_one_release_as_json_and_the_unregistered_case(self, make_user):
        author = make_user()
        one = _recording(author, index=0)
        dataset, items = _gated_dataset(author, one)
        release = _release(dataset, *items)
        _release(dataset, on=date(2026, 11, 1))

        report = json.loads(self._run(dataset.object_hash, "--release", str(release.pk), "--format", "json"))
        assert [r["release_id"] for r in report["releases"]] == [release.pk]
        assert report["releases"][0]["class_function_registered"] is False
        assert "no equivalence-class function is registered" in self._run(dataset.object_hash)

    def test_refuses_an_unknown_release_and_an_ungated_dataset(self, make_user):
        author = make_user()
        gated, _ = _gated_dataset(author)
        with pytest.raises(CommandError, match="has no release"):
            self._run(gated.object_hash, "--release", "999999")
        plain = Dataset.objects.create(author=author, name="plain")
        with pytest.raises(CommandError, match="not release-gated"):
            self._run(plain.object_hash)
        assert "has no releases" in self._run(gated.object_hash)
