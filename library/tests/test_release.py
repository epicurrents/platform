"""Release gating: hidden until released, refused to share-token callers, dated and ordered by release.

Covers the two read-visibility gates in ``library.release``, the batch forms the listings use, the
release month replacing ``created_at`` on the recording surfaces, the dataset item listing for
managers and readers, the snapshot manifest, the ``release_gated`` write rule, the monthly cadence
and the ``release_dataset`` command with and without a project selector.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from model_bakery import baker

from activity.models import Activity, ObjectChangeLog
from epicurrents.models import AccessRight
from epicurrents.permissions import can_read_object, get_federated_read_access_result
from library.models import Dataset, DatasetItem, DatasetRelease
from library.release import (
    ReleaseDecision,
    eligibility_cutoff,
    eligible_items,
    member_hidden_from_reader,
    register_equivalence_class,
    register_release_selector,
    release_month_for,
    select_by_class_size,
    unreleased_member_ids,
)
from recordings.models import Recording
from recordings.tests.test_federation import _as_peer, _make_peer

pytestmark = pytest.mark.django_db

HASHES = [f"{i:032X}" for i in range(1, 8)]


def _recording(author, *, index=0, created_at=None, **kwargs):
    defaults = {
        "author": author,
        "stored_name": f"{HASHES[index]}.edf",
        "file_extension": ".edf",
        "file_size": 1,
        "status": Recording.Status.READY,
        "display_name": "",
    }
    defaults.update(kwargs)
    recording = baker.make(Recording, **defaults)
    if created_at is not None:
        Recording.objects.filter(pk=recording.pk).update(created_at=created_at)
        recording.refresh_from_db()
    return recording


def _grant(obj, giver, *, target=None, group=None, token=None, **fields):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(obj.pk),
        access_giver=giver,
        access_target=target,
        access_target_group=group,
        public_share_token=token,
        can_read=True,
        **fields,
    )


def _gated_dataset(author, *members):
    dataset = Dataset.objects.create(author=author, name="pool", release_gated=True)
    items = [_add(dataset, member) for member in members]
    return dataset, items


def _add(dataset, obj):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(obj.pk))


def _release(dataset, *items, on=date(2026, 10, 1)):
    release = DatasetRelease.objects.create(dataset=dataset, released_on=on, member_count=len(items))
    for item in items:
        item.release = release
        item.save(update_fields=["release"])
    return release


@pytest.fixture(autouse=True)
def _no_selector():
    register_release_selector(None)
    yield
    register_release_selector(None)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestMemberGate:
    def test_unreleased_member_is_hidden_from_a_direct_grantee(self, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author)
        _gated_dataset(author, recording)
        _grant(recording, author, target=reader)
        assert can_read_object(user=reader, obj=recording) is False

    def test_unreleased_member_is_hidden_from_a_dataset_grantee(self, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author)
        dataset, _ = _gated_dataset(author, recording)
        _grant(dataset, author, target=reader)
        assert can_read_object(user=reader, obj=recording) is False

    def test_released_member_resolves_for_the_dataset_grantee(self, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author)
        dataset, items = _gated_dataset(author, recording)
        _grant(dataset, author, target=reader)
        _release(dataset, *items)
        assert can_read_object(user=reader, obj=recording) is True

    def test_ungated_dataset_is_unaffected(self, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author)
        dataset = Dataset.objects.create(author=author, name="open")
        _add(dataset, recording)
        _grant(dataset, author, target=reader)
        assert can_read_object(user=reader, obj=recording) is True

    def test_the_recording_author_keeps_seeing_it(self, make_user):
        curator, uploader = make_user(), make_user()
        recording = _recording(uploader)
        _gated_dataset(curator, recording)
        assert member_hidden_from_reader(uploader, recording) is False

    def test_the_dataset_author_and_a_write_grantee_manage_unreleased_members(self, make_user):
        author, manager, reader = make_user(), make_user(), make_user()
        recording = _recording(author)
        dataset, _ = _gated_dataset(make_user(), recording)
        _grant(dataset, dataset.author, target=manager, can_write=True)
        _grant(dataset, dataset.author, target=reader)
        assert member_hidden_from_reader(dataset.author, recording) is False
        assert member_hidden_from_reader(manager, recording) is False
        assert member_hidden_from_reader(reader, recording) is True

    def test_superuser_never_reaches_the_gate(self, make_user, make_superuser):
        author = make_user()
        recording = _recording(author)
        _gated_dataset(author, recording)
        assert can_read_object(user=make_superuser(), obj=recording) is True

    def test_a_share_token_never_resolves_a_member_even_when_released(self, make_user):
        author = make_user()
        recording = _recording(author)
        dataset, items = _gated_dataset(author, recording)
        _release(dataset, *items)
        _grant(recording, author, token="tok-on-recording")
        _grant(dataset, author, token="tok-on-dataset")
        assert can_read_object(user=None, obj=recording, share_token="tok-on-recording") is False
        assert can_read_object(user=None, obj=recording, share_token="tok-on-dataset") is False
        assert can_read_object(user=None, obj=dataset, share_token="tok-on-dataset") is False

    def test_a_grantee_carrying_a_token_is_refused_too(self, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author)
        dataset, items = _gated_dataset(author, recording)
        _release(dataset, *items)
        _grant(dataset, author, target=reader)
        assert can_read_object(user=reader, obj=recording) is True
        assert can_read_object(user=reader, obj=recording, share_token="anything") is False

    def test_unreleased_in_one_gated_dataset_hides_it_everywhere(self, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author)
        _gated_dataset(author, recording)
        open_dataset = Dataset.objects.create(author=author, name="open")
        _add(open_dataset, recording)
        _grant(open_dataset, author, target=reader)
        assert can_read_object(user=reader, obj=recording) is False

    def test_a_peer_reads_released_members_only(self, make_user):
        author = make_user()
        peer = _make_peer(author)
        first, second = _recording(author, index=0), _recording(author, index=1)
        dataset, items = _gated_dataset(author, first, second)
        _release(dataset, items[0])
        ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct, object_id=str(dataset.pk), access_giver=author, federated_peer=peer, can_read=True
        )
        assert get_federated_read_access_result(peer, "u1", first).granted is True
        assert get_federated_read_access_result(peer, "u1", second).granted is False

    def test_batch_helpers_agree_with_the_gate(self, make_user):
        author = make_user()
        first, second = _recording(author, index=0), _recording(author, index=1)
        dataset, items = _gated_dataset(author, first, second)
        _release(dataset, items[0], on=date(2026, 9, 15))
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        assert unreleased_member_ids(ct) == {str(second.pk)}
        assert release_month_for(first) == datetime(2026, 9, 1, tzinfo=UTC)
        assert release_month_for(second) is None

    def test_a_trashed_dataset_gates_nothing(self, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author)
        dataset, _ = _gated_dataset(author, recording)
        dataset.deleted_at = datetime.now(UTC)
        dataset.save()
        _grant(recording, author, target=reader)
        assert can_read_object(user=reader, obj=recording) is True


# ---------------------------------------------------------------------------
# Recording surfaces
# ---------------------------------------------------------------------------


def _detail(client, recording, **params):
    return client.get(f"/recordings/api/v1/{recording.stored_name.split('.')[0]}", params)


class TestRecordingSurfaces:
    def test_unreleased_member_answers_404_to_a_grantee(self, client, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author)
        dataset, _ = _gated_dataset(author, recording)
        _grant(dataset, author, target=reader)
        client.force_login(reader)
        assert _detail(client, recording).status_code == 404
        assert client.get(f"/recordings/api/v1/{HASHES[0]}/file").status_code == 404

    def test_share_token_download_of_a_released_member_answers_404(self, client, make_user):
        author = make_user()
        recording = _recording(author)
        dataset, items = _gated_dataset(author, recording)
        _release(dataset, *items)
        _grant(recording, author, token="tok")
        resp = client.get(f"/recordings/api/v1/{HASHES[0]}/file", {"share_token": "tok"})
        assert resp.status_code == 404

    def test_share_token_detail_of_a_released_member_answers_404(self, client, make_user):
        author = make_user()
        recording = _recording(author)
        dataset, items = _gated_dataset(author, recording)
        _release(dataset, *items)
        _grant(recording, author, token="tok")
        assert _detail(client, recording, share_token="tok").status_code == 404

    def test_staff_export_drops_unreleased_members_it_does_not_manage(self, client, make_user, settings):
        from annotations.models import Event

        settings.ANNOTATION_EXPORT_ALL_ANNOTATORS_REQUIRES_SUPERUSER = False
        author, staff = make_user(), make_user(is_staff=True)
        released, pending = (
            _recording(author, index=0, content_hash="a" * 64),
            _recording(author, index=1, content_hash="b" * 64),
        )
        dataset, items = _gated_dataset(author, released, pending)
        _release(dataset, items[0])
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        for index, recording in enumerate((released, pending)):
            Event.objects.create(
                author=author,
                target_content_type=ct,
                target_object_id=str(recording.pk),
                object_hash=f"{index:032X}",
                name="spike",
                timestamp=1.0,
                duration=0.5,
            )
        client.force_login(staff)
        body = json.loads(client.get("/annotations/api/v1/export?types=events").content)
        assert [row["target_ref"] for row in body["events"]] == ["a" * 64]

    def test_released_member_serves_the_release_month_to_a_reader(self, client, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author, created_at=datetime(2026, 7, 14, 9, 30, tzinfo=UTC))
        dataset, items = _gated_dataset(author, recording)
        _release(dataset, *items, on=date(2026, 9, 3))
        _grant(dataset, author, target=reader)
        client.force_login(reader)
        body = _detail(client, recording).json()
        assert body["created_at"].startswith("2026-09-01T00:00:00")
        listed = client.get("/recordings/api/v1/").json()
        assert [row["created_at"][:10] for row in listed] == ["2026-09-01"]

    def test_the_author_keeps_the_exact_upload_time(self, client, make_user):
        author = make_user()
        recording = _recording(author, created_at=datetime(2026, 7, 14, 9, 30, tzinfo=UTC))
        dataset, items = _gated_dataset(author, recording)
        _release(dataset, *items, on=date(2026, 9, 3))
        client.force_login(author)
        assert _detail(client, recording).json()["created_at"].startswith("2026-07-14T09:30")

    def test_listing_hides_unreleased_and_orders_released_members_by_name(self, client, make_user):
        author, reader = make_user(), make_user()
        zeta = _recording(author, index=0, display_name="Zeta", created_at=datetime(2026, 7, 1, tzinfo=UTC))
        alpha = _recording(author, index=1, display_name="alpha", created_at=datetime(2026, 7, 2, tzinfo=UTC))
        pending = _recording(author, index=2, display_name="Pending", created_at=datetime(2026, 7, 3, tzinfo=UTC))
        own = _recording(reader, index=3, display_name="Mine", created_at=datetime(2026, 8, 20, tzinfo=UTC))
        dataset, items = _gated_dataset(author, zeta, alpha, pending)
        _release(dataset, items[0], items[1], on=date(2026, 9, 1))
        _grant(dataset, author, target=reader)
        client.force_login(reader)
        names = [row["display_name"] for row in client.get("/recordings/api/v1/").json()]
        # The release month (September) sorts the members above the reader's own
        # August upload; among members, name order rather than arrival order.
        assert names == ["alpha", "Zeta", "Mine"]
        assert own.display_name == "Mine"

    def test_federated_listing_excludes_unreleased_members(self, client, make_user):
        author = make_user()
        peer = _make_peer(author)
        first, second = _recording(author, index=0), _recording(author, index=1)
        dataset, items = _gated_dataset(author, first, second)
        _release(dataset, items[0])
        ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct, object_id=str(dataset.pk), access_giver=author, federated_peer=peer, can_read=True
        )
        with _as_peer(peer):
            hashes = [row["hash"] for row in client.get("/recordings/api/v1/").json()]
        assert hashes == [HASHES[0]]


class TestStoredDigest:
    """A gated member's stored_hash, and the pin that tests it, reach its managers and superusers only."""

    DIGEST = "c" * 64

    def _released_member(self, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author, stored_hash=self.DIGEST)
        dataset, items = _gated_dataset(author, recording)
        _release(dataset, *items)
        return author, reader, recording, dataset

    def test_a_reader_receives_no_digest_on_detail_or_listing(self, client, make_user):
        author, reader, recording, dataset = self._released_member(make_user)
        _grant(dataset, author, target=reader)
        client.force_login(reader)
        assert _detail(client, recording).json()["stored_hash"] == ""
        assert [row["stored_hash"] for row in client.get("/recordings/api/v1/").json()] == [""]

    def test_a_reader_cannot_pin_the_content(self, client, make_user):
        author, reader, recording, dataset = self._released_member(make_user)
        _grant(dataset, author, target=reader)
        client.force_login(reader)
        for pin in (self.DIGEST, "d" * 64):
            resp = client.get(f"/recordings/api/v1/{HASHES[0]}/file", {"expect_stored_hash": pin})
            # The same answer for the right and a wrong digest: a pin is not an oracle.
            assert resp.status_code == 400

    def test_a_manager_receives_the_digest_and_may_pin(self, client, make_user):
        author, manager, recording, dataset = self._released_member(make_user)
        _grant(dataset, author, target=manager, can_write=True)
        client.force_login(manager)
        assert _detail(client, recording).json()["stored_hash"] == self.DIGEST
        resp = client.get(f"/recordings/api/v1/{HASHES[0]}/file", {"expect_stored_hash": "d" * 64})
        assert resp.status_code == 412

    def test_the_author_and_a_superuser_receive_the_digest(self, client, make_user, superuser):
        author, _reader, recording, _dataset = self._released_member(make_user)
        for caller in (author, superuser):
            client.force_login(caller)
            assert _detail(client, recording).json()["stored_hash"] == self.DIGEST

    def test_a_peer_receives_no_digest(self, client, make_user):
        author, _reader, _recording_obj, dataset = self._released_member(make_user)
        peer = _make_peer(author)
        ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct, object_id=str(dataset.pk), access_giver=author, federated_peer=peer, can_read=True
        )
        with _as_peer(peer):
            rows = client.get("/recordings/api/v1/").json()
        assert [row["stored_hash"] for row in rows] == [""]

    def test_a_recording_outside_any_gated_dataset_keeps_its_digest(self, client, make_user):
        author, reader = make_user(), make_user()
        recording = _recording(author, stored_hash=self.DIGEST)
        _grant(recording, author, target=reader)
        client.force_login(reader)
        assert _detail(client, recording).json()["stored_hash"] == self.DIGEST


# ---------------------------------------------------------------------------
# Dataset surfaces
# ---------------------------------------------------------------------------


class TestDatasetSurfaces:
    def _items(self, client, dataset, **params):
        return client.get(f"/api/v1/library/datasets/{dataset.object_hash}/items/", params)

    def test_reader_lists_released_members_by_name_dated_by_release(self, client, make_user):
        author, reader = make_user(), make_user()
        zeta = _recording(author, index=0, display_name="Zeta")
        alpha = _recording(author, index=1, display_name="alpha")
        pending = _recording(author, index=2, display_name="Pending")
        dataset, items = _gated_dataset(author, zeta, alpha, pending)
        _release(dataset, items[0], items[1], on=date(2026, 9, 3))
        _grant(dataset, author, target=reader)
        client.force_login(reader)
        rows = self._items(client, dataset).json()
        assert [row["object_name"] for row in rows] == ["alpha", "Zeta"]
        assert all(row["added_at"].startswith("2026-09-01T00:00:00") for row in rows)
        assert all(row["release_month"].startswith("2026-09-01") for row in rows)

    def test_media_and_nested_datasets_sort_by_name_beside_recordings(self, client, make_user):
        from media.models import MediaFile

        author = make_user()
        recording = _recording(author, index=0, display_name="middle")
        clip = baker.make(MediaFile, author=author, display_name="Alpha clip", stored_name="C" * 32, file_size=1)
        unnamed = baker.make(MediaFile, author=author, display_name=None, stored_name="D" * 32, file_size=1)
        nested = Dataset.objects.create(author=author, name="zulu")
        dataset, items = _gated_dataset(author, recording, clip, unnamed, nested)
        _release(dataset, *items, on=date(2026, 9, 3))
        client.force_login(author)
        rows = self._items(client, dataset).json()
        assert [row["object_id"] for row in rows] == [str(clip.pk), str(unnamed.pk), str(recording.pk), str(nested.pk)]

    def test_manager_sees_unreleased_members_with_a_null_release_month(self, client, make_user):
        author = make_user()
        zeta = _recording(author, index=0, display_name="Zeta")
        pending = _recording(author, index=1, display_name="Pending")
        dataset, items = _gated_dataset(author, zeta, pending)
        _release(dataset, items[0], on=date(2026, 9, 3))
        client.force_login(author)
        rows = self._items(client, dataset).json()
        assert [(row["object_name"], row["release_month"] is None) for row in rows] == [
            ("Pending", True),
            ("Zeta", False),
        ]

    def test_share_token_cannot_open_a_gated_dataset(self, client, make_user):
        author = make_user()
        dataset, _ = _gated_dataset(author, _recording(author))
        _grant(dataset, author, token="tok")
        base = f"/api/v1/library/datasets/{dataset.object_hash}/"
        assert client.get(base, {"share_token": "tok"}).status_code == 403
        assert client.get(base + "items/", {"share_token": "tok"}).status_code == 403

    def test_snapshot_manifest_omits_unreleased_members(self, client, make_user):
        author = make_user()
        first = _recording(author, index=0, content_hash="a" * 64)
        second = _recording(author, index=1, content_hash="b" * 64)
        dataset, items = _gated_dataset(author, first, second)
        _release(dataset, items[0])
        client.force_login(author)
        resp = client.post(
            f"/api/v1/library/datasets/{dataset.object_hash}/snapshots/",
            json.dumps({"label": "r1"}),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.content
        snapshot = client.get(
            f"/api/v1/library/datasets/snapshots/{resp.json()['object_hash']}/", {"include_manifest": "true"}
        )
        identities = [entry["identity"] for entry in snapshot.json()["manifest"]]
        assert identities == ["a" * 64]

    def test_only_the_author_or_a_superuser_toggles_the_gate(self, client, make_user, make_superuser):
        author, writer = make_user(), make_user()
        dataset = Dataset.objects.create(author=author, name="ds")
        _grant(dataset, author, target=writer, can_write=True)
        url = f"/api/v1/library/datasets/{dataset.object_hash}/"
        client.force_login(writer)
        resp = client.patch(url, json.dumps({"release_gated": True}), content_type="application/json")
        assert resp.status_code == 403
        assert client.patch(url, json.dumps({"name": "renamed"}), content_type="application/json").status_code == 200
        client.force_login(author)
        resp = client.patch(url, json.dumps({"release_gated": True}), content_type="application/json")
        assert resp.status_code == 200 and resp.json()["release_gated"] is True
        client.force_login(make_superuser())
        resp = client.patch(url, json.dumps({"release_gated": False}), content_type="application/json")
        assert resp.status_code == 200 and resp.json()["release_gated"] is False


# ---------------------------------------------------------------------------
# Cadence and the release command
# ---------------------------------------------------------------------------


class TestCadence:
    @pytest.mark.parametrize(
        ("as_of", "cutoff"),
        [
            (date(2026, 10, 1), datetime(2026, 9, 1, tzinfo=UTC)),
            (date(2026, 10, 31), datetime(2026, 9, 1, tzinfo=UTC)),
            (date(2026, 1, 15), datetime(2025, 12, 1, tzinfo=UTC)),
        ],
    )
    def test_uploaded_in_month_m_is_eligible_from_m_plus_two(self, as_of, cutoff):
        assert eligibility_cutoff(as_of) == cutoff

    def test_eligible_items_apply_the_cutoff_and_skip_unreadable_members(self, make_user):
        author = make_user()
        august = _recording(author, index=0, created_at=datetime(2026, 8, 31, 23, 59, tzinfo=UTC))
        september = _recording(author, index=1, created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC))
        failed = _recording(author, index=2, created_at=datetime(2026, 7, 1, tzinfo=UTC), status="failed")
        trashed = _recording(author, index=3, created_at=datetime(2026, 7, 1, tzinfo=UTC), deleted_at=datetime.now(UTC))
        dataset, items = _gated_dataset(author, august, september, failed, trashed)
        eligible = eligible_items(dataset, as_of=date(2026, 10, 1))
        assert [item.pk for item in eligible] == [items[0].pk]


class TestClassSize:
    """``select_by_class_size``: publish an eligible member once its class holds k recordings."""

    @pytest.fixture(autouse=True)
    def _classes(self):
        # A recording's class is its display name; an empty name is unclassified.
        register_equivalence_class(lambda recording: recording.display_name or None)
        yield
        register_equivalence_class(None)

    def test_a_class_below_k_waits_and_one_at_k_is_published(self, make_user):
        author = make_user()
        small = _recording(author, index=0, display_name="small")
        big = [_recording(author, index=n, display_name="big") for n in (1, 2, 3)]
        dataset, items = _gated_dataset(author, small, *big)
        selected = select_by_class_size(dataset, items, k=3)
        assert [item.pk for item in selected] == [item.pk for item in items[1:]]

    def test_released_members_count_towards_the_class(self, make_user):
        author = make_user()
        released = [_recording(author, index=n, display_name="c") for n in (0, 1)]
        newcomer = _recording(author, index=2, display_name="c")
        dataset, items = _gated_dataset(author, *released, newcomer)
        _release(dataset, *items[:2])
        assert [item.pk for item in select_by_class_size(dataset, [items[2]], k=3)] == [items[2].pk]

    def test_a_trashed_released_member_no_longer_counts(self, make_user):
        author = make_user()
        released = [_recording(author, index=n, display_name="c") for n in (0, 1)]
        newcomer = _recording(author, index=2, display_name="c")
        dataset, items = _gated_dataset(author, *released, newcomer)
        _release(dataset, *items[:2])
        Recording.objects.filter(pk=released[0].pk).update(deleted_at=datetime.now(UTC))
        assert select_by_class_size(dataset, [items[2]], k=3) == []

    def test_unclassified_members_are_never_selected(self, make_user):
        author = make_user()
        members = [_recording(author, index=n, display_name="") for n in (0, 1)]
        dataset, items = _gated_dataset(author, *members)
        assert select_by_class_size(dataset, items, k=1) == []

    def test_nothing_is_selected_without_a_class_function(self, make_user):
        register_equivalence_class(None)
        author = make_user()
        dataset, items = _gated_dataset(author, _recording(author, index=0, display_name="c"))
        assert select_by_class_size(dataset, items, k=1) == []


class TestReleaseCommand:
    def _run(self, *args):
        out = io.StringIO()
        call_command("release_dataset", *args, stdout=out)
        return out.getvalue()

    def test_releases_everything_eligible_without_a_selector_and_records_the_run(self, make_user):
        author = make_user()
        old = _recording(author, index=0, created_at=datetime(2026, 7, 5, tzinfo=UTC))
        new = _recording(author, index=1, created_at=datetime(2026, 9, 5, tzinfo=UTC))
        dataset, items = _gated_dataset(author, old, new)
        out = self._run(
            dataset.object_hash,
            "--as-of",
            "2026-10-01",
            "--profile-version",
            "3",
            "--k",
            "5",
            "--m",
            "2",
            "--sign-off",
            str(author.pk),
            "--assessment-reference",
            "Assessment: pool v3",
            "--actor",
            author.username,
        )
        assert "Released 1 of 1 eligible" in out
        release = DatasetRelease.objects.get(dataset=dataset)
        assert release.release_month == datetime(2026, 10, 1, tzinfo=UTC)
        assert (release.profile_version, release.k, release.m, release.member_count) == ("3", 5, 2, 1)
        assert release.sign_off_user_ids == [author.pk] and release.author == author
        assert release.assessment_reference == "Assessment: pool v3"
        items[0].refresh_from_db()
        items[1].refresh_from_db()
        assert items[0].release == release and items[1].release is None
        activity = Activity.objects.get(verb="library.dataset.release")
        assert activity.interface == Activity.Interface.COMMAND
        assert activity.metadata == {"as_of": "2026-10-01"}
        assert activity.target_object_id == str(dataset.pk)
        assert ObjectChangeLog.objects.filter(activity=activity).count() >= 2

    def test_a_selector_decides_the_subset_and_fills_the_record(self, make_user):
        author = make_user()
        first = _recording(author, index=0, created_at=datetime(2026, 7, 5, tzinfo=UTC))
        second = _recording(author, index=1, created_at=datetime(2026, 7, 6, tzinfo=UTC))
        dataset, items = _gated_dataset(author, first, second)
        seen = {}

        def selector(ds, eligible, *, as_of):
            seen["eligible"] = [item.pk for item in eligible]
            seen["as_of"] = as_of
            return ReleaseDecision(items=[eligible[1]], profile_version="edu-7", k=5, m=2)

        register_release_selector(selector)
        out = self._run(dataset.object_hash, "--as-of", "2026-10-01")
        assert seen == {"eligible": [items[0].pk, items[1].pk], "as_of": date(2026, 10, 1)}
        assert "Released 1 of 2 eligible" in out and "withheld" in out
        release = DatasetRelease.objects.get(dataset=dataset)
        assert (release.profile_version, release.k, release.m) == ("edu-7", 5, 2)
        items[1].refresh_from_db()
        assert items[1].release == release

    def test_a_selector_cannot_release_what_is_not_eligible(self, make_user):
        author = make_user()
        new = _recording(author, index=0, created_at=datetime(2026, 9, 5, tzinfo=UTC))
        dataset, items = _gated_dataset(author, new)
        register_release_selector(lambda ds, eligible, *, as_of: ReleaseDecision(items=list(items)))
        out = self._run(dataset.object_hash, "--as-of", "2026-10-01")
        assert "Released 0 of 0 eligible" in out
        items[0].refresh_from_db()
        assert items[0].release is None

    def test_dry_run_writes_nothing(self, make_user):
        author = make_user()
        old = _recording(author, index=0, created_at=datetime(2026, 7, 5, tzinfo=UTC))
        dataset, items = _gated_dataset(author, old)
        out = self._run(dataset.object_hash, "--as-of", "2026-10-01", "--dry-run", "--format", "json")
        report = json.loads(out)
        assert report["dry_run"] is True and report["released_count"] == 1
        assert DatasetRelease.objects.count() == 0
        items[0].refresh_from_db()
        assert items[0].release is None

    def test_refuses_an_ungated_dataset(self, make_user):
        dataset = Dataset.objects.create(author=make_user(), name="ds")
        with pytest.raises(CommandError, match="not release-gated"):
            self._run(dataset.object_hash)


class TestOriginalsCheck:
    def test_release_gated_deployment_refuses_the_originals_volume(self, settings):
        from library.checks import check_release_gated_deployment_keeps_no_originals as check

        settings.LIBRARY_RELEASE_GATED_DEPLOYMENT = True
        settings.RECORDINGS_ORIGINALS_PATH = "/mnt/originals"
        assert [error.id for error in check(None)] == ["library.E001"]
        settings.RECORDINGS_ORIGINALS_PATH = None
        assert check(None) == []
        settings.LIBRARY_RELEASE_GATED_DEPLOYMENT = False
        settings.RECORDINGS_ORIGINALS_PATH = "/mnt/originals"
        assert check(None) == []

    def test_release_gated_deployment_must_discard_embedded_text(self, settings):
        from library.checks import check_release_gated_deployment_discards_embedded_text as check

        settings.LIBRARY_RELEASE_GATED_DEPLOYMENT = True
        settings.RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS = False
        assert [error.id for error in check(None)] == ["library.E002"]
        settings.RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS = True
        assert check(None) == []
        settings.LIBRARY_RELEASE_GATED_DEPLOYMENT = False
        settings.RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS = False
        assert check(None) == []

    def test_the_release_reference_is_registered_for_erasure_as_it_is_for_export(self):
        from activity.erasure import registered_subject_pii
        from user.export import RELATION_HANDLING

        spec = registered_subject_pii()["library.datasetrelease"]
        assert spec.owner_field == "author_id" and "assessment_reference" in spec.pii_fields
        assert "assessment_reference" in RELATION_HANDLING["library.datasetrelease:author"].fields

    def test_the_gate_is_registered(self):
        from epicurrents.permissions import _READ_VISIBILITY_GATES

        assert member_hidden_from_reader in _READ_VISIBILITY_GATES["recordings.recording"]
        assert any(g.__name__ == "dataset_hidden_from_reader" for g in _READ_VISIBILITY_GATES["library.dataset"])
