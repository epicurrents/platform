"""Tests for the bulk annotation export — access tiers, target hiding, and both output formats."""

import csv
import io
import json

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from activity.models import Activity
from annotations.models import Event, Label
from epicurrents.models import AccessRight

EXPORT_URL = "/annotations/api/v1/export"


def _recording_ct(recording):
    return ContentType.objects.get_for_model(recording, for_concrete_model=False)


def _grant_read(user, recording, giver):
    return AccessRight.objects.create(
        content_type=_recording_ct(recording),
        object_id=str(recording.pk),
        access_giver=giver,
        access_target=user,
        can_read=True,
    )


def _make_recording(author, **kwargs):
    from recordings.models import Recording

    defaults = {
        "author": author,
        "status": Recording.Status.READY,
        "deleted_at": None,
        "original_name": "SECRET-PATIENT-NAME.edf",
        "display_name": "",
        "stored_name": baker.random_gen.gen_string(40),
        "content_hash": baker.random_gen.gen_string(64),
    }
    defaults.update(kwargs)
    return baker.make("recordings.Recording", **defaults)


def _make_event(author, recording, **kwargs):
    defaults = {
        "author": author,
        "target_content_type": _recording_ct(recording),
        "target_object_id": str(recording.pk),
        "object_hash": baker.random_gen.gen_string(32).upper()[:32],
        "name": "spike",
        "timestamp": 12.5,
        "duration": 0.8,
        "event_class": "epileptiform",
        "value": {"amplitude": 42},
    }
    defaults.update(kwargs)
    return baker.make(Event, **defaults)


def _make_label(author, recording, **kwargs):
    defaults = {
        "author": author,
        "target_content_type": _recording_ct(recording),
        "target_object_id": str(recording.pk),
        "object_hash": baker.random_gen.gen_string(32).upper()[:32],
        "name": "artifact",
        "value": {"confidence": 0.9},
    }
    defaults.update(kwargs)
    return baker.make(Label, **defaults)


def _staff(make_user):
    user = make_user()
    user.is_staff = True
    user.first_name = "Staff"
    user.last_name = "Member"
    user.save()
    return user


def _json_body(response):
    return json.loads(response.content.decode())


@pytest.mark.django_db
class TestExportAccessTiers:
    def test_anonymous_is_rejected(self, client):
        assert client.get(EXPORT_URL).status_code == 401

    def test_staff_sees_every_author(self, client, make_user):
        staff = _staff(make_user)
        rater_a, rater_b = make_user(), make_user()
        recording = _make_recording(rater_a)
        _make_event(rater_a, recording)
        _make_event(rater_b, recording)

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events"))

        assert len(body["events"]) == 2
        assert body["metadata"]["restricted_to_own_annotations"] is False
        author_ids = {row["author_id"] for row in body["events"]}
        assert author_ids == {rater_a.pk, rater_b.pk}

    def test_superuser_sees_every_author(self, superuser_client, make_user):
        su_client, _ = superuser_client
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)

        body = _json_body(su_client.get(f"{EXPORT_URL}?types=events"))
        assert len(body["events"]) == 1

    def test_plain_user_gets_only_own_rows(self, client, make_user, superuser):
        mine, theirs = make_user(), make_user()
        recording = _make_recording(mine)
        _grant_read(mine, recording, superuser)
        _make_event(mine, recording, name="mine")
        _make_event(theirs, recording, name="theirs")

        client.force_login(mine)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events"))

        assert [row["name"] for row in body["events"]] == ["mine"]
        assert body["metadata"]["restricted_to_own_annotations"] is True

    def test_plain_user_naming_another_annotator_is_denied(self, client, make_user):
        mine, theirs = make_user(), make_user()
        client.force_login(mine)
        response = client.get(f"{EXPORT_URL}?annotator_id={theirs.pk}")
        assert response.status_code == 403

    def test_plain_user_may_name_themselves(self, client, make_user, superuser):
        mine = make_user()
        recording = _make_recording(mine)
        _grant_read(mine, recording, superuser)
        _make_event(mine, recording)

        client.force_login(mine)
        response = client.get(f"{EXPORT_URL}?types=events&annotator_id={mine.pk}")
        assert response.status_code == 200
        assert len(_json_body(response)["events"]) == 1

    def test_staff_can_filter_to_one_annotator(self, client, make_user):
        staff = _staff(make_user)
        rater_a, rater_b = make_user(), make_user()
        recording = _make_recording(rater_a)
        _make_event(rater_a, recording)
        _make_event(rater_b, recording)

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events&annotator_id={rater_b.pk}"))

        assert len(body["events"]) == 1
        assert body["events"][0]["author_id"] == rater_b.pk

    def test_staff_can_filter_to_several_annotators(self, client, make_user):
        staff = _staff(make_user)
        rater_a, rater_b, rater_c = make_user(), make_user(), make_user()
        recording = _make_recording(rater_a)
        for rater in (rater_a, rater_b, rater_c):
            _make_event(rater, recording)

        client.force_login(staff)
        url = f"{EXPORT_URL}?types=events&annotator_id={rater_a.pk}&annotator_id={rater_c.pk}"
        body = _json_body(client.get(url))

        assert {row["author_id"] for row in body["events"]} == {rater_a.pk, rater_c.pk}


@pytest.mark.django_db
class TestAnnotatorMetadata:
    def test_roster_lists_each_annotator_id_with_per_type_counts(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_event(rater, recording)
        _make_label(rater, recording)

        client.force_login(staff)
        meta = _json_body(client.get(EXPORT_URL))["metadata"]

        assert meta["annotators"] == [{"id": rater.pk, "events": 2, "labels": 1}]
        assert meta["counts"] == {"events": 2, "labels": 1}

    def test_header_identifies_the_exporter_by_id_only(self, client, make_user):
        staff = _staff(make_user)
        client.force_login(staff)
        meta = _json_body(client.get(EXPORT_URL))["metadata"]
        assert meta["exported_by"] == {"id": staff.pk}

    def test_roster_excludes_annotators_whose_rows_were_filtered_out(self, client, make_user):
        staff = _staff(make_user)
        rater_a, rater_b = make_user(), make_user()
        recording = _make_recording(rater_a)
        _make_event(rater_a, recording)
        _make_event(rater_b, recording)

        client.force_login(staff)
        meta = _json_body(client.get(f"{EXPORT_URL}?types=events&annotator_id={rater_a.pk}"))["metadata"]

        assert [entry["id"] for entry in meta["annotators"]] == [rater_a.pk]

    def test_no_username_or_name_appears_anywhere_in_either_format(self, client, make_user):
        """The GDPR contract of format_version 2: annotator and exporter identity leaves the
        platform as an opaque id only; the mapping stays behind the roster endpoint."""
        staff = _staff(make_user)
        rater = make_user()
        rater.first_name, rater.last_name = "Jane", "Doe"
        rater.save()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_label(rater, recording)

        client.force_login(staff)
        for url in (EXPORT_URL, f"{EXPORT_URL}?format=csv&types=events"):
            text = client.get(url).content.decode()
            for token in (rater.get_username(), "Jane", "Doe", staff.get_username(), "Staff", "Member"):
                assert token not in text


@pytest.mark.django_db
class TestFormats:
    def test_json_carries_both_types(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_label(rater, recording)

        client.force_login(staff)
        response = client.get(EXPORT_URL)

        assert response["Content-Type"].startswith("application/json")
        body = _json_body(response)
        assert len(body["events"]) == 1
        assert len(body["labels"]) == 1

    def test_csv_with_both_types_is_rejected(self, client, make_user):
        client.force_login(_staff(make_user))
        response = client.get(f"{EXPORT_URL}?format=csv&types=events,labels")
        assert response.status_code == 422

    def test_csv_defaults_are_rejected_because_both_types_are_implied(self, client, make_user):
        client.force_login(_staff(make_user))
        assert client.get(f"{EXPORT_URL}?format=csv").status_code == 422

    def test_csv_carries_a_comment_header_then_the_rows(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording, name="spike")

        client.force_login(staff)
        response = client.get(f"{EXPORT_URL}?format=csv&types=events")

        assert response["Content-Type"].startswith("text/csv")
        text = response.content.decode()
        comments = [line for line in text.splitlines() if line.startswith("#")]
        assert any(f"exported_by: user id {staff.pk}" in line for line in comments)
        assert any(f"id {rater.pk} - 1 events" in line for line in comments)

        rows = list(csv.DictReader(io.StringIO("\n".join(l for l in text.splitlines() if not l.startswith("#")))))
        assert len(rows) == 1
        assert rows[0]["name"] == "spike"
        assert rows[0]["author_id"] == str(rater.pk)
        assert json.loads(rows[0]["value"]) == {"amplitude": 42}

    def test_csv_columns_differ_between_events_and_labels(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_label(rater, recording)
        client.force_login(staff)

        def _header(kind):
            text = client.get(f"{EXPORT_URL}?format=csv&types={kind}").content.decode()
            body = [line for line in text.splitlines() if not line.startswith("#")]
            return body[0].split(",")

        event_columns, label_columns = _header("events"), _header("labels")
        assert "timestamp" in event_columns
        assert "timestamp" not in label_columns

    def test_downloads_are_attachments_with_a_typed_filename(self, client, make_user):
        client.force_login(_staff(make_user))
        csv_disp = client.get(f"{EXPORT_URL}?format=csv&types=labels")["Content-Disposition"]
        json_disp = client.get(EXPORT_URL)["Content-Disposition"]

        assert csv_disp.startswith("attachment")
        assert "annotations-labels-" in csv_disp and csv_disp.rstrip('"').endswith(".csv")
        assert "annotations-events-labels-" in json_disp and json_disp.rstrip('"').endswith(".json")

    def test_unknown_type_and_format_are_rejected(self, client, make_user):
        client.force_login(_staff(make_user))
        assert client.get(f"{EXPORT_URL}?types=interruptions").status_code == 422
        assert client.get(f"{EXPORT_URL}?format=xlsx").status_code == 422


@pytest.mark.django_db
class TestTargetHiding:
    """FAILED-hidden and soft-deleted recordings must not surface through the export."""

    def test_failed_recording_is_hidden_from_staff(self, client, make_user):
        from recordings.models import Recording

        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater, status=Recording.Status.FAILED)
        _make_event(rater, recording)

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events"))
        assert body["events"] == []

    def test_failed_recording_is_visible_to_a_superuser(self, superuser_client, make_user):
        from recordings.models import Recording

        su_client, _ = superuser_client
        rater = make_user()
        recording = _make_recording(rater, status=Recording.Status.FAILED)
        _make_event(rater, recording)

        body = _json_body(su_client.get(f"{EXPORT_URL}?types=events"))
        assert len(body["events"]) == 1

    def test_soft_deleted_recording_is_hidden(self, client, make_user):
        from django.utils import timezone

        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater, deleted_at=timezone.now())
        _make_event(rater, recording)

        client.force_login(staff)
        assert _json_body(client.get(f"{EXPORT_URL}?types=events"))["events"] == []

    def test_original_name_never_appears(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater, original_name="SECRET-PATIENT-NAME.edf")
        _make_event(rater, recording)

        client.force_login(staff)
        text = client.get(f"{EXPORT_URL}?types=events").content.decode()
        assert "SECRET-PATIENT-NAME" not in text

    def test_rows_omit_created_and_modified_timestamps(self, client, make_user):
        """AGENTS.md de-identification: annotation-type responses omit id / created_at /
        modified_at. Row order still carries the sequence."""
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_label(rater, recording)

        client.force_login(staff)
        body = _json_body(client.get(EXPORT_URL))

        for type_name in ("events", "labels"):
            row = body[type_name][0]
            assert "created_at" not in row
            assert "modified_at" not in row
            assert "id" not in row

    def test_csv_columns_omit_timestamps_and_annotator_identity(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)

        client.force_login(staff)
        text = client.get(f"{EXPORT_URL}?format=csv&types=events").content.decode()
        columns = next(line for line in text.splitlines() if not line.startswith("#")).split(",")

        assert "created_at" not in columns
        assert "modified_at" not in columns
        assert "author_name" not in columns
        assert "author_username" not in columns
        assert "author_id" in columns

    def test_non_recording_target_uses_its_opaque_hash_not_a_pk(self, client, make_user):
        """A project-plugin target (here an Annotation standing in for one) publishes its own
        object_hash; the export must prefer it over the sequential primary key."""
        from annotations.models import Annotation

        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        parent = baker.make(
            Annotation,
            author=rater,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="D" * 32,
            content={"note": "x"},
        )
        _make_event(
            rater,
            recording,
            target_content_type=ContentType.objects.get_for_model(Annotation),
            target_object_id=str(parent.pk),
        )

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events"))
        row = next(r for r in body["events"] if r["target_type"] == "annotations.annotation")

        assert row["target_ref"] == "D" * 32
        assert row["target_ref"] != str(parent.pk)

    def test_target_is_identified_by_content_hash_and_display_name(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater, content_hash="c" * 64, display_name="Case 7")
        _make_event(rater, recording)

        client.force_login(staff)
        row = _json_body(client.get(f"{EXPORT_URL}?types=events"))["events"][0]

        assert row["target_type"] == "recordings.recording"
        assert row["target_ref"] == "c" * 64
        assert row["target_label"] == "Case 7"

    def test_plain_user_loses_the_target_when_read_access_is_revoked(self, client, make_user, superuser):
        mine = make_user()
        recording = _make_recording(mine)
        grant = _grant_read(mine, recording, superuser)
        _make_event(mine, recording)
        client.force_login(mine)
        assert len(_json_body(client.get(f"{EXPORT_URL}?types=events"))["events"]) == 1

        grant.delete()
        assert _json_body(client.get(f"{EXPORT_URL}?types=events"))["events"] == []


@pytest.mark.django_db
class TestFilters:
    def test_recording_filter_selects_by_content_hash(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        wanted = _make_recording(rater, content_hash="a" * 64)
        other = _make_recording(rater, content_hash="b" * 64)
        _make_event(rater, wanted, name="wanted")
        _make_event(rater, other, name="other")

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events&recording={'a' * 64}"))
        assert [row["name"] for row in body["events"]] == ["wanted"]

    def test_recording_filter_accepts_several_hashes(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        first = _make_recording(rater, content_hash="a" * 64)
        second = _make_recording(rater, content_hash="b" * 64)
        third = _make_recording(rater, content_hash="c" * 64)
        for recording in (first, second, third):
            _make_event(rater, recording)

        client.force_login(staff)
        url = f"{EXPORT_URL}?types=events&recording={'a' * 64}&recording={'b' * 64}"
        assert len(_json_body(client.get(url))["events"]) == 2

    def test_dataset_filter_selects_dataset_members(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        inside = _make_recording(rater)
        outside = _make_recording(rater)
        _make_event(rater, inside, name="inside")
        _make_event(rater, outside, name="outside")

        dataset = baker.make("library.Dataset", author=rater)
        baker.make(
            "library.DatasetItem",
            dataset=dataset,
            content_type=_recording_ct(inside),
            object_id=str(inside.pk),
        )

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events&dataset_id={dataset.pk}"))
        assert [row["name"] for row in body["events"]] == ["inside"]

    def test_unmatched_filter_returns_an_empty_export_not_everything(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater, content_hash="a" * 64)
        _make_event(rater, recording)

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events&recording={'f' * 64}"))
        assert body["events"] == []

    def test_date_bounds_are_inclusive_of_the_named_day(self, client, make_user):
        from datetime import datetime
        from datetime import timezone as dt_timezone

        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        early = _make_event(rater, recording, name="early")
        late = _make_event(rater, recording, name="late")
        # auto_now_add fields have to be backdated with .update() after creation.
        Event.objects.filter(pk=early.pk).update(created_at=datetime(2026, 8, 1, 9, 0, tzinfo=dt_timezone.utc))
        Event.objects.filter(pk=late.pk).update(created_at=datetime(2026, 8, 11, 23, 30, tzinfo=dt_timezone.utc))

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events&since=2026-08-11"))
        assert [row["name"] for row in body["events"]] == ["late"]

        body = _json_body(client.get(f"{EXPORT_URL}?types=events&until=2026-08-11"))
        assert {row["name"] for row in body["events"]} == {"early", "late"}

    def test_unparseable_date_is_rejected(self, client, make_user):
        client.force_login(_staff(make_user))
        assert client.get(f"{EXPORT_URL}?since=not-a-date").status_code == 422

    def test_version_id_filter(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording, name="base")
        _make_event(rater, recording, name="derived", version_id="v2")

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events&version_id=v2"))
        assert [row["name"] for row in body["events"]] == ["derived"]


@pytest.mark.django_db
class TestAuditTrail:
    def test_export_is_audited_with_counts_and_filters(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)

        client.force_login(staff)
        client.get(f"{EXPORT_URL}?types=events&format=csv")

        activity = Activity.objects.filter(verb="annotations.export").latest("id")
        assert activity.metadata["format"] == "csv"
        assert activity.metadata["types"] == ["events"]
        assert activity.metadata["returned_counts"] == {"events": 1}
        assert activity.metadata["annotator_count"] == 1

    def test_audit_row_names_the_annotators_by_id(self, client, make_user):
        staff = _staff(make_user)
        rater_a, rater_b = make_user(), make_user()
        recording = _make_recording(rater_a)
        _make_event(rater_a, recording)
        _make_label(rater_b, recording)

        client.force_login(staff)
        client.get(EXPORT_URL)

        activity = Activity.objects.filter(verb="annotations.export").latest("id")
        assert sorted(activity.metadata["annotator_ids"]) == sorted([rater_a.pk, rater_b.pk])

    def test_audit_row_never_stores_a_raw_username(self, client, make_user):
        """The audit trail is permanent and this row targets no user, so erase_subject cannot
        reach it — a username written here would outlive the account it names."""
        staff = _staff(make_user)
        rater = make_user(username="identifiable_person")
        recording = _make_recording(rater)
        _make_event(rater, recording)

        client.force_login(staff)
        client.get(f"{EXPORT_URL}?types=events&annotator_id={rater.pk}")

        activity = Activity.objects.filter(verb="annotations.export").latest("id")
        assert "identifiable_person" not in json.dumps(activity.metadata)
        assert activity.metadata["filters"]["annotator_ids"] == [rater.pk]


@pytest.mark.django_db
class TestExportExtensions:
    """The project hook that complements rows with target-derived columns."""

    @pytest.fixture
    def recording_extension(self, monkeypatch):
        from annotations import export as annotation_export

        calls = []

        def resolver(*, caller, objects):
            calls.append({"caller": caller, "objects": list(objects)})
            return {str(obj.pk): {"site_code": f"site-{obj.pk}"} for obj in objects}

        monkeypatch.setattr(
            annotation_export,
            "_EXPORT_EXTENSIONS",
            {"recordings.recording": [(("site_code",), resolver)]},
        )
        return calls

    def test_rows_gain_extension_columns(self, client, make_user, recording_extension):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)

        client.force_login(staff)
        row = _json_body(client.get(f"{EXPORT_URL}?types=events"))["events"][0]
        assert row["site_code"] == f"site-{recording.pk}"

    def test_resolver_is_called_once_with_caller_and_distinct_targets(self, client, make_user, recording_extension):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_event(rater, recording)

        client.force_login(staff)
        client.get(f"{EXPORT_URL}?types=events")

        assert len(recording_extension) == 1
        assert recording_extension[0]["caller"].pk == staff.pk
        assert [obj.pk for obj in recording_extension[0]["objects"]] == [recording.pk]

    def test_partial_resolver_yields_none_not_ragged_rows(self, client, make_user, monkeypatch):
        from annotations import export as annotation_export

        monkeypatch.setattr(
            annotation_export,
            "_EXPORT_EXTENSIONS",
            {"recordings.recording": [(("site_code",), lambda *, caller, objects: {})]},
        )
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)

        client.force_login(staff)
        row = _json_body(client.get(f"{EXPORT_URL}?types=events"))["events"][0]
        assert row["site_code"] is None

    def test_csv_header_carries_extension_columns(self, client, make_user, recording_extension):
        staff = _staff(make_user)
        rater = make_user()
        _make_event(rater, _make_recording(rater))

        client.force_login(staff)
        text = client.get(f"{EXPORT_URL}?format=csv&types=events").content.decode()
        header = next(line for line in text.splitlines() if not line.startswith("#")).split(",")
        assert header[-1] == "site_code"

    def test_metadata_lists_extension_columns(self, client, make_user, recording_extension):
        client.force_login(_staff(make_user))
        meta = _json_body(client.get(f"{EXPORT_URL}?types=events"))["metadata"]
        assert meta["extension_columns"] == ["site_code"]

    def test_registering_a_base_column_name_is_rejected(self, monkeypatch):
        from annotations import export as annotation_export

        monkeypatch.setattr(annotation_export, "_EXPORT_EXTENSIONS", {})
        with pytest.raises(ValueError, match="author_id"):
            annotation_export.register_export_extension(
                "recordings.recording",
                columns=("author_id",),
                resolver=lambda *, caller, objects: {},
            )


ANNOTATORS_URL = f"{EXPORT_URL}/annotators"


@pytest.mark.django_db
class TestAnnotatorRoster:
    """The staff-only roster endpoint that maps exported author ids back to identities."""

    def test_anonymous_is_rejected(self, client):
        assert client.get(ANNOTATORS_URL).status_code == 401

    def test_plain_user_is_denied(self, client, make_user):
        client.force_login(make_user())
        assert client.get(ANNOTATORS_URL).status_code == 403

    def test_staff_gets_ids_identities_and_per_type_counts(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        rater.first_name, rater.last_name = "Jane", "Doe"
        rater.save()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_event(rater, recording)
        _make_label(rater, recording)

        client.force_login(staff)
        body = _json_body(client.get(ANNOTATORS_URL))

        assert body["annotators"] == [
            {"id": rater.pk, "username": rater.get_username(), "name": "Jane Doe", "events": 2, "labels": 1}
        ]

    def test_name_falls_back_to_username(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)

        client.force_login(staff)
        body = _json_body(client.get(ANNOTATORS_URL))
        assert body["annotators"][0]["name"] == rater.get_username()

    def test_roster_is_sorted_by_username(self, client, make_user):
        staff = _staff(make_user)
        rater_b = make_user(username="beta_rater")
        rater_a = make_user(username="alpha_rater")
        recording = _make_recording(rater_a)
        _make_event(rater_b, recording)
        _make_label(rater_a, recording)

        client.force_login(staff)
        body = _json_body(client.get(ANNOTATORS_URL))
        assert [entry["username"] for entry in body["annotators"]] == ["alpha_rater", "beta_rater"]

    def test_users_without_annotations_are_absent(self, client, make_user):
        staff = _staff(make_user)
        make_user()

        client.force_login(staff)
        assert _json_body(client.get(ANNOTATORS_URL))["annotators"] == []

    def test_audit_row_carries_a_count_but_no_identity(self, client, make_user):
        staff = _staff(make_user)
        rater = make_user(username="identifiable_person")
        recording = _make_recording(rater)
        _make_event(rater, recording)

        client.force_login(staff)
        client.get(ANNOTATORS_URL)

        activity = Activity.objects.filter(verb="annotations.annotator.list").latest("id")
        assert activity.metadata == {"annotator_count": 1}
        assert "identifiable_person" not in json.dumps(activity.metadata)


def _row_source(type_name, **kwargs):
    from annotations import export as annotation_export

    defaults = {"type_name": type_name, "label": type_name.title(), "columns": ("author_id",)}
    defaults.update(kwargs)
    defaults.setdefault("get_queryset", lambda: None)
    defaults.setdefault("serialise", lambda obj: {})
    return annotation_export.ExportRowSource(**defaults)


def _make_peer(added_by, **kwargs):
    defaults = {"added_by": added_by, "url": f"https://{baker.random_gen.gen_string(12).lower()}.example"}
    defaults.update(kwargs)
    return baker.make("federation.FederatedPeer", **defaults)


def _make_interruption(author, recording, **kwargs):
    from annotations.models import Interruption

    defaults = {
        "author": author,
        "target_content_type": _recording_ct(recording),
        "target_object_id": str(recording.pk),
        "object_hash": baker.random_gen.gen_string(32).upper()[:32],
        "timestamp": 3.5,
        "duration": 1.0,
    }
    defaults.update(kwargs)
    return baker.make(Interruption, **defaults)


@pytest.mark.django_db
class TestExportRowSources:
    """The project hook that contributes rows of its own as an additional export type.

    Backed by ``Interruption`` — a real annotation model the export does not carry as a core type,
    so it stands in for a project table without needing one installed. ``FederatedPeer`` stands in
    where the test needs a nullable author.
    """

    TYPE = "interruptions"

    @pytest.fixture
    def row_source(self, monkeypatch):
        from annotations import export as annotation_export
        from annotations.models import Interruption

        def serialise(obj):
            return {
                # Deliberately wrong: the export must overwrite it from the row's own FK.
                "author_id": -1,
                "object_hash": obj.object_hash,
                "timestamp": obj.timestamp,
            }

        source = _row_source(
            self.TYPE,
            columns=("author_id", "object_hash", "timestamp"),
            get_queryset=lambda: Interruption.objects.all(),
            serialise=serialise,
        )
        monkeypatch.setattr(annotation_export, "_ROW_SOURCES", {self.TYPE: source})
        return source

    def test_registered_type_is_exportable(self, row_source):
        from annotations import export as annotation_export

        assert annotation_export.exportable_types() == ("events", "labels", self.TYPE)

    def test_rows_are_exported_under_their_own_key(self, client, make_user, row_source):
        staff = _staff(make_user)
        rater = make_user()
        interruption = _make_interruption(rater, _make_recording(rater))

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}"))

        assert [row["object_hash"] for row in body[self.TYPE]] == [interruption.object_hash]
        assert body["metadata"]["counts"] == {self.TYPE: 1}

    def test_omitting_types_exports_registered_sources_too(self, client, make_user, row_source):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_interruption(rater, recording)

        client.force_login(staff)
        body = _json_body(client.get(EXPORT_URL))

        assert body["metadata"]["counts"] == {"events": 1, "labels": 0, self.TYPE: 1}

    def test_author_id_comes_from_the_row_not_the_serialiser(self, client, make_user, row_source):
        staff = _staff(make_user)
        rater = make_user()
        _make_interruption(rater, _make_recording(rater))

        client.force_login(staff)
        row = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}"))[self.TYPE][0]

        assert row["author_id"] == rater.pk

    def test_rows_are_projected_onto_the_declared_columns(self, client, make_user, monkeypatch):
        """An undeclared key never leaves the platform, and a missing declared one is null."""
        from annotations import export as annotation_export
        from annotations.models import Interruption

        source = _row_source(
            self.TYPE,
            columns=("author_id", "object_hash", "timestamp"),
            get_queryset=lambda: Interruption.objects.all(),
            serialise=lambda obj: {"object_hash": obj.object_hash, "created_at": obj.created_at},
        )
        monkeypatch.setattr(annotation_export, "_ROW_SOURCES", {self.TYPE: source})
        staff = _staff(make_user)
        rater = make_user()
        _make_interruption(rater, _make_recording(rater))

        client.force_login(staff)
        row = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}"))[self.TYPE][0]

        assert list(row) == ["author_id", "object_hash", "timestamp"]
        assert row["timestamp"] is None

    def test_non_staff_caller_sees_only_own_rows(self, client, make_user, row_source):
        mine = make_user()
        theirs = make_user()
        recording = _make_recording(mine)
        own = _make_interruption(mine, recording)
        _make_interruption(theirs, recording)

        client.force_login(mine)
        body = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}"))

        assert [row["object_hash"] for row in body[self.TYPE]] == [own.object_hash]

    def test_annotator_filter_narrows_rows(self, client, make_user, row_source):
        staff = _staff(make_user)
        wanted = make_user()
        other = make_user()
        recording = _make_recording(wanted)
        kept = _make_interruption(wanted, recording)
        _make_interruption(other, recording)

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}&annotator_id={wanted.pk}"))

        assert [row["object_hash"] for row in body[self.TYPE]] == [kept.object_hash]

    def test_date_window_applies_to_the_sources_created_field(self, client, make_user, row_source):
        from annotations.models import Interruption

        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        old = _make_interruption(rater, recording)
        recent = _make_interruption(rater, recording)
        Interruption.objects.filter(pk=old.pk).update(created_at="2020-01-01T00:00:00Z")

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}&since=2021-01-01"))

        assert [row["object_hash"] for row in body[self.TYPE]] == [recent.object_hash]

    def test_recording_filter_omits_the_source(self, client, make_user, row_source):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_interruption(rater, recording)

        client.force_login(staff)
        url = f"{EXPORT_URL}?types=events,{self.TYPE}&recording={recording.content_hash}"
        body = _json_body(client.get(url))

        # The core type matches, so the omission is the source's, not an empty target scope.
        assert len(body["events"]) == 1
        assert body[self.TYPE] == []

    def test_dataset_filter_omits_the_source(self, client, make_user, row_source):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_event(rater, recording)
        _make_interruption(rater, recording)
        dataset = baker.make("library.Dataset", author=rater)
        baker.make(
            "library.DatasetItem",
            dataset=dataset,
            content_type=_recording_ct(recording),
            object_id=str(recording.pk),
        )

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=events,{self.TYPE}&dataset_id={dataset.pk}"))

        assert len(body["events"]) == 1
        assert body[self.TYPE] == []

    def test_version_filter_omits_a_source_without_a_version_field(self, client, make_user, row_source):
        staff = _staff(make_user)
        rater = make_user()
        _make_interruption(rater, _make_recording(rater), version_id="v1")

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}&version_id=v1"))

        assert body[self.TYPE] == []

    def test_a_version_aware_source_has_the_version_filter_applied(self, client, make_user, monkeypatch):
        from annotations import export as annotation_export
        from annotations.models import Interruption

        source = _row_source(
            self.TYPE,
            columns=("author_id", "object_hash"),
            get_queryset=lambda: Interruption.objects.all(),
            serialise=lambda obj: {"object_hash": obj.object_hash},
            version_field="version_id",
        )
        monkeypatch.setattr(annotation_export, "_ROW_SOURCES", {self.TYPE: source})
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        kept = _make_interruption(rater, recording, version_id="v1")
        _make_interruption(rater, recording, version_id="v2")

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}&version_id=v1"))

        assert [row["object_hash"] for row in body[self.TYPE]] == [kept.object_hash]

    def test_rows_without_an_author_are_left_out(self, client, make_user, monkeypatch):
        from annotations import export as annotation_export
        from federation.models import FederatedPeer

        source = _row_source(
            "peers",
            columns=("author_id", "url"),
            get_queryset=lambda: FederatedPeer.objects.all(),
            serialise=lambda obj: {"url": obj.url},
            author_field="added_by",
        )
        monkeypatch.setattr(annotation_export, "_ROW_SOURCES", {"peers": source})
        staff = _staff(make_user)
        rater = make_user()
        attributed = _make_peer(rater)
        _make_peer(None)

        client.force_login(staff)
        body = _json_body(client.get(f"{EXPORT_URL}?types=peers"))
        roster = _json_body(client.get(ANNOTATORS_URL))["annotators"]

        assert body["peers"] == [{"author_id": rater.pk, "url": attributed.url}]
        assert [entry["id"] for entry in body["metadata"]["annotators"]] == [rater.pk]
        assert [entry["id"] for entry in roster] == [rater.pk]

    def test_csv_uses_the_sources_columns_without_extension_columns(self, client, make_user, row_source, monkeypatch):
        from annotations import export as annotation_export

        monkeypatch.setattr(
            annotation_export,
            "_EXPORT_EXTENSIONS",
            {"recordings.recording": [(("site_code",), lambda *, caller, objects: {})]},
        )
        staff = _staff(make_user)
        rater = make_user()
        _make_interruption(rater, _make_recording(rater))

        client.force_login(staff)
        body = client.get(f"{EXPORT_URL}?types={self.TYPE}&format=csv").content.decode()
        reader = csv.DictReader(io.StringIO("\n".join(line for line in body.splitlines() if not line.startswith("#"))))

        assert reader.fieldnames == ["author_id", "object_hash", "timestamp"]
        assert [row["author_id"] for row in reader] == [str(rater.pk)]

    def test_export_roster_counts_the_registered_type(self, client, make_user, row_source):
        staff = _staff(make_user)
        rater = make_user()
        _make_interruption(rater, _make_recording(rater))

        client.force_login(staff)
        annotators = _json_body(client.get(f"{EXPORT_URL}?types={self.TYPE}"))["metadata"]["annotators"]

        assert annotators == [{"id": rater.pk, "events": 0, "labels": 0, self.TYPE: 1}]

    def test_annotator_roster_endpoint_counts_the_registered_type(self, client, make_user, row_source):
        staff = _staff(make_user)
        rater = make_user()
        _make_interruption(rater, _make_recording(rater))

        client.force_login(staff)
        roster = _json_body(client.get(ANNOTATORS_URL))["annotators"]

        assert [(entry["id"], entry["events"], entry[self.TYPE]) for entry in roster] == [(rater.pk, 0, 1)]

    def test_roster_count_is_not_split_by_an_ordered_queryset(self, client, make_user, monkeypatch):
        from annotations import export as annotation_export
        from federation.models import FederatedPeer

        source = _row_source(
            "peers",
            get_queryset=lambda: FederatedPeer.objects.order_by("url"),
            author_field="added_by",
        )
        monkeypatch.setattr(annotation_export, "_ROW_SOURCES", {"peers": source})
        staff = _staff(make_user)
        rater = make_user()
        _make_peer(rater, url="https://a.example")
        _make_peer(rater, url="https://b.example")

        client.force_login(staff)
        roster = _json_body(client.get(ANNOTATORS_URL))["annotators"]

        assert [(entry["id"], entry["peers"]) for entry in roster] == [(rater.pk, 2)]

    def test_unknown_type_is_still_rejected(self, client, make_user, row_source):
        client.force_login(_staff(make_user))
        response = client.get(f"{EXPORT_URL}?types=nonesuch")

        assert response.status_code == 422
        assert self.TYPE in response.content.decode()


@pytest.mark.django_db
class TestExportTypesEndpoint:
    """The list the export form builds its options from."""

    URL = f"{EXPORT_URL}/types"

    def test_core_types_are_listed_to_a_non_staff_caller(self, client, make_user):
        client.force_login(make_user())
        body = _json_body(client.get(self.URL))

        assert body["types"] == [
            {"name": "events", "label": "Events"},
            {"name": "labels", "label": "Labels"},
        ]

    def test_registered_source_is_offered_after_the_core_types(self, client, make_user, monkeypatch):
        from annotations import export as annotation_export

        monkeypatch.setattr(annotation_export, "_ROW_SOURCES", {"widgets": _row_source("widgets", label="Widgets")})
        client.force_login(make_user())
        body = _json_body(client.get(self.URL))

        assert [entry["name"] for entry in body["types"]] == ["events", "labels", "widgets"]
        assert body["types"][-1] == {"name": "widgets", "label": "Widgets"}

    def test_requires_authentication(self, client):
        assert client.get(self.URL).status_code == 401

    def test_listing_is_audited_with_a_count_only(self, client, make_user):
        client.force_login(make_user())
        client.get(self.URL)

        activity = Activity.objects.filter(verb="annotations.export.types").latest("id")
        assert activity.metadata == {"type_count": 2}


class TestRowSourceRegistration:
    """Registration-time refusals, which are the only place a misregistration is cheap to catch."""

    @pytest.fixture(autouse=True)
    def empty_registries(self, monkeypatch):
        from annotations import export as annotation_export

        monkeypatch.setattr(annotation_export, "_ROW_SOURCES", {})
        monkeypatch.setattr(annotation_export, "_EXPORT_EXTENSIONS", {})
        return annotation_export

    def _register(self, name, **kwargs):
        from annotations import export as annotation_export

        defaults = {"label": "Widgets", "columns": ("author_id",), "get_queryset": lambda: None}
        defaults.update(kwargs)
        defaults.setdefault("serialise", lambda obj: {})
        annotation_export.register_export_row_source(name, **defaults)

    def test_valid_registration_becomes_exportable(self, empty_registries):
        self._register("widgets", version_field="version_id")

        assert empty_registries.exportable_types() == ("events", "labels", "widgets")
        assert empty_registries._ROW_SOURCES["widgets"].version_field == "version_id"

    def test_duplicate_type_name_is_refused(self):
        self._register("widgets")
        with pytest.raises(ValueError, match="already registered"):
            self._register("widgets")

    @pytest.mark.parametrize("name", ["events", "labels"])
    def test_core_type_name_is_refused(self, name):
        with pytest.raises(ValueError, match="already registered"):
            self._register(name)

    def test_metadata_type_name_is_refused(self):
        # A source under this name would replace the JSON export's header with its rows.
        with pytest.raises(ValueError, match="reserved"):
            self._register("metadata")

    def test_source_without_author_id_column_is_refused(self):
        with pytest.raises(ValueError, match="author_id"):
            self._register("widgets", columns=("name",))

    @pytest.mark.parametrize("name", ["epoch labels", "epoch,labels", "EpochLabels", "2labels", "", "widgets\n"])
    def test_unselectable_type_names_are_refused(self, name):
        with pytest.raises(ValueError, match="must match"):
            self._register(name)

    def test_source_column_taken_by_an_extension_is_refused(self, empty_registries):
        empty_registries.register_export_extension(
            "recordings.recording", columns=("site_code",), resolver=lambda *, caller, objects: {}
        )
        with pytest.raises(ValueError, match="site_code"):
            self._register("widgets", columns=("author_id", "site_code"))

    def test_extension_column_taken_by_a_source_is_refused(self, empty_registries):
        self._register("widgets", columns=("author_id", "site_code"))
        with pytest.raises(ValueError, match="site_code"):
            empty_registries.register_export_extension(
                "recordings.recording", columns=("site_code",), resolver=lambda *, caller, objects: {}
            )


@pytest.mark.django_db
class TestWithheldColumns:
    """``id``, ``created_at`` and ``modified_at`` leave only when a registration opts in by name."""

    TYPE = "interruptions"

    @pytest.fixture(autouse=True)
    def registries(self, monkeypatch):
        from annotations import export as annotation_export

        monkeypatch.setattr(annotation_export, "_ROW_SOURCES", {})
        monkeypatch.setattr(annotation_export, "_EXPORT_EXTENSIONS", {})
        return annotation_export

    def _register_source(self, registries, **kwargs):
        from annotations.models import Interruption

        registries.register_export_row_source(
            self.TYPE,
            label="Interruptions",
            columns=("author_id", "id", "object_hash", "created_at", "modified_at"),
            get_queryset=lambda: Interruption.objects.all(),
            serialise=lambda obj: {
                "id": obj.pk,
                "object_hash": obj.object_hash,
                "created_at": obj.created_at,
                "modified_at": obj.modified_at,
            },
            **kwargs,
        )

    def _export_one(self, client, make_user, url_suffix):
        staff = _staff(make_user)
        rater = make_user()
        recording = _make_recording(rater)
        _make_interruption(rater, recording)
        _make_event(rater, recording)
        client.force_login(staff)
        return client.get(f"{EXPORT_URL}?{url_suffix}")

    def test_a_row_source_withholds_them_by_default(self, client, make_user, registries):
        self._register_source(registries)

        row = _json_body(self._export_one(client, make_user, f"types={self.TYPE}"))[self.TYPE][0]
        text = self._export_one(client, make_user, f"types={self.TYPE}&format=csv").content.decode()
        header = next(line for line in text.splitlines() if not line.startswith("#")).split(",")

        assert list(row) == ["author_id", "object_hash"]
        assert header == ["author_id", "object_hash"]

    def test_a_row_source_exports_only_the_withheld_column_it_names(self, client, make_user, registries):
        self._register_source(registries, include_withheld=("created_at",))

        row = _json_body(self._export_one(client, make_user, f"types={self.TYPE}"))[self.TYPE][0]

        assert list(row) == ["author_id", "object_hash", "created_at"]
        assert row["created_at"]

    def test_an_extension_withholds_them_by_default(self, client, make_user, registries):
        registries.register_export_extension(
            "recordings.recording",
            columns=("site_code", "created_at"),
            resolver=lambda *, caller, objects: {
                str(obj.pk): {"site_code": "s", "created_at": "2026-01-01"} for obj in objects
            },
        )

        body = _json_body(self._export_one(client, make_user, "types=events"))

        assert "created_at" not in body["events"][0]
        assert body["events"][0]["site_code"] == "s"
        assert body["metadata"]["extension_columns"] == ["site_code"]

    def test_an_extension_exports_the_withheld_column_it_names(self, client, make_user, registries):
        registries.register_export_extension(
            "recordings.recording",
            columns=("site_code", "created_at"),
            resolver=lambda *, caller, objects: {
                str(obj.pk): {"site_code": "s", "created_at": "2026-01-01"} for obj in objects
            },
            include_withheld=("created_at",),
        )

        row = _json_body(self._export_one(client, make_user, "types=events"))["events"][0]

        assert row["created_at"] == "2026-01-01"

    def test_naming_a_column_that_is_not_withheld_is_refused(self, registries):
        with pytest.raises(ValueError, match="not withheld"):
            self._register_source(registries, include_withheld=("object_hash",))

    def test_naming_an_undeclared_withheld_column_is_refused(self, registries):
        with pytest.raises(ValueError, match="undeclared"):
            registries.register_export_extension(
                "recordings.recording",
                columns=("site_code",),
                resolver=lambda *, caller, objects: {},
                include_withheld=("created_at",),
            )
