"""Contract tests for the dataset governance layer: object_hash, DatasetMeta, DatasetSnapshot.

The snapshot contract is the load-bearing part: create-only rows whose manifest pins member
*identities*, canonically ordered and sealed by ``manifest_hash`` — so equal membership always
seals to equal bytes, a benchmark claim is checkable, and a snapshot outlives member purge as
unsatisfiable but still verifiable. Erasure winning by construction is asserted here directly:
hard-deleting a member leaves the stored manifest byte-identical and its seal still verifying.
"""

import hashlib
import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from model_bakery import baker

from conftest import patch_json, post_json
from epicurrents.models import AccessRight
from library.models import Dataset, DatasetItem, DatasetMeta, DatasetSnapshot

DATASETS_URL = "/api/v1/library/datasets/"


def _grant(dataset, giver, target, *, can_write=False):
    return AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(dataset, for_concrete_model=False),
        object_id=str(dataset.pk),
        access_giver=giver,
        access_target=target,
        can_read=True,
        can_write=can_write,
    )


def _add_recording(dataset, author, *, content_hash, status="READY", deleted_at=None):
    recording = baker.make(
        "recordings.Recording",
        author=author,
        status=status,
        content_hash=content_hash,
        deleted_at=deleted_at,
    )
    DatasetItem.objects.create(
        dataset=dataset,
        content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
        object_id=str(recording.pk),
    )
    return recording


@pytest.fixture
def dataset(db, user):
    dataset = Dataset.objects.create(author=user, name="benchmark-set")
    _grant(dataset, user, user, can_write=True)
    return dataset


@pytest.mark.django_db
class TestDatasetObjectHash:
    def test_generated_at_save_and_unique(self, user):
        first = Dataset.objects.create(author=user, name="a")
        second = Dataset.objects.create(author=user, name="b")
        assert len(first.object_hash) == 32
        assert first.object_hash != second.object_hash

    def test_stable_across_saves(self, dataset):
        original = dataset.object_hash
        dataset.name = "renamed"
        dataset.save()
        dataset.refresh_from_db()
        assert dataset.object_hash == original

    def test_serialized_in_dataset_responses(self, auth_client, dataset):
        c, _user = auth_client
        resp = c.get(f"{DATASETS_URL}{dataset.pk}/")
        assert resp.status_code == 200, resp.content
        assert resp.json()["object_hash"] == dataset.object_hash

    def test_null_for_collections(self, auth_client):
        from library.models import Collection

        c, user = auth_client
        collection = Collection.objects.create(author=user, name="col")
        resp = c.get(f"/api/v1/library/collections/{collection.pk}/")
        assert resp.status_code == 200, resp.content
        assert resp.json()["object_hash"] is None


@pytest.mark.django_db
class TestDatasetMeta:
    def test_patch_sets_the_licence_pair(self, auth_client, dataset):
        c, _user = auth_client
        resp = patch_json(
            c,
            f"{DATASETS_URL}{dataset.pk}/",
            {"license_spdx": "CC-BY-4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/"},
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["license_spdx"] == "CC-BY-4.0"
        meta = DatasetMeta.objects.get(dataset=dataset)
        assert meta.license_url.endswith("/by/4.0/")

    def test_licence_round_trips_in_get(self, auth_client, dataset):
        c, _user = auth_client
        DatasetMeta.objects.create(dataset=dataset, license_spdx="CC0-1.0")
        resp = c.get(f"{DATASETS_URL}{dataset.pk}/")
        assert resp.json()["license_spdx"] == "CC0-1.0"

    def test_undeclared_licence_is_null(self, auth_client, dataset):
        c, _user = auth_client
        resp = c.get(f"{DATASETS_URL}{dataset.pk}/")
        assert resp.json()["license_spdx"] is None
        assert not DatasetMeta.objects.exists()

    def test_collections_ignore_the_licence_fields(self, auth_client):
        # Same contract as viewer_config: the shared patch schema accepts the
        # fields, the collection endpoint ignores them, and no meta row appears.
        from library.models import Collection

        c, user = auth_client
        collection = Collection.objects.create(author=user, name="col")
        resp = patch_json(
            c,
            f"/api/v1/library/collections/{collection.pk}/",
            {"license_spdx": "CC-BY-4.0"},
        )
        assert resp.status_code == 200, resp.content
        assert not DatasetMeta.objects.exists()

    def test_empty_string_clears_a_declared_value(self, auth_client, dataset):
        c, _user = auth_client
        DatasetMeta.objects.create(dataset=dataset, license_spdx="CC-BY-4.0")
        resp = patch_json(c, f"{DATASETS_URL}{dataset.pk}/", {"license_spdx": ""})
        assert resp.status_code == 200, resp.content
        assert DatasetMeta.objects.get(dataset=dataset).license_spdx == ""


@pytest.mark.django_db
class TestSnapshotCreate:
    def test_author_seals_the_current_membership(self, auth_client, dataset, user):
        c, _user = auth_client
        _add_recording(dataset, user, content_hash="B" * 64)
        _add_recording(dataset, user, content_hash="A" * 64)
        resp = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {"label": "v1"})
        assert resp.status_code == 201, resp.content
        data = resp.json()
        assert data["label"] == "v1"
        assert data["member_count"] == 2
        # Canonical order: sorted by (content_type, identity), regardless of insertion order.
        identities = [entry["identity"] for entry in data["manifest"]]
        assert identities == ["A" * 64, "B" * 64]

    def test_manifest_hash_seals_the_canonical_serialisation(self, auth_client, dataset, user):
        c, _user = auth_client
        _add_recording(dataset, user, content_hash="C" * 64)
        resp = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {})
        snapshot = DatasetSnapshot.objects.get(object_hash=resp.json()["object_hash"])
        canonical = json.dumps(snapshot.manifest, sort_keys=True, separators=(",", ":"))
        assert snapshot.manifest_hash == hashlib.sha256(canonical.encode()).hexdigest()

    def test_equal_membership_seals_to_equal_hashes(self, auth_client, dataset, user):
        c, _user = auth_client
        _add_recording(dataset, user, content_hash="D" * 64)
        first = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {"label": "one"}).json()
        second = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {"label": "two"}).json()
        assert first["manifest_hash"] == second["manifest_hash"]
        assert first["object_hash"] != second["object_hash"]

    def test_membership_change_changes_the_hash(self, auth_client, dataset, user):
        c, _user = auth_client
        _add_recording(dataset, user, content_hash="D" * 64)
        first = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {}).json()
        _add_recording(dataset, user, content_hash="E" * 64)
        second = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {}).json()
        assert first["manifest_hash"] != second["manifest_hash"]

    def test_failed_and_trashed_members_are_excluded(self, auth_client, dataset, user):
        from django.utils import timezone

        c, _user = auth_client
        _add_recording(dataset, user, content_hash="F" * 64)
        _add_recording(dataset, user, content_hash="0" * 64, status="failed")
        _add_recording(dataset, user, content_hash="1" * 64, deleted_at=timezone.now())
        resp = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {})
        assert [entry["identity"] for entry in resp.json()["manifest"]] == ["F" * 64]

    def test_trashed_media_members_are_excluded_too(self, auth_client, dataset, user):
        # The soft-delete exclusion is generic, not Recording-specific.
        from django.utils import timezone

        c, _user = auth_client
        media = baker.make(
            "media.MediaFile",
            author=user,
            content_hash="9" * 32,
            deleted_at=timezone.now(),
        )
        DatasetItem.objects.create(
            dataset=dataset,
            content_type=ContentType.objects.get_for_model(media, for_concrete_model=False),
            object_id=str(media.pk),
        )
        resp = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {})
        assert resp.json()["manifest"] == []

    def test_read_only_grantee_cannot_snapshot(self, dataset, user, make_user):
        grantee = make_user()
        _grant(dataset, user, grantee)
        c = Client()
        c.force_login(grantee)
        assert post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {}).status_code == 403

    def test_unauthenticated_returns_401(self, client, dataset):
        assert post_json(client, f"{DATASETS_URL}{dataset.pk}/snapshots/", {}).status_code == 401


@pytest.mark.django_db
class TestSnapshotRead:
    def _snapshot(self, dataset, user, label=""):
        from library.api.v1.ninja import _canonical_manifest, _manifest_hash

        manifest = _canonical_manifest(dataset)
        return DatasetSnapshot.objects.create(
            dataset=dataset,
            author=user,
            label=label,
            manifest=manifest,
            manifest_hash=_manifest_hash(manifest),
        )

    def test_list_is_newest_first_without_manifests(self, auth_client, dataset, user):
        c, _user = auth_client
        first = self._snapshot(dataset, user, "one")
        second = self._snapshot(dataset, user, "two")
        resp = c.get(f"{DATASETS_URL}{dataset.pk}/snapshots/")
        assert resp.status_code == 200, resp.content
        hashes = [row["object_hash"] for row in resp.json()]
        assert hashes == [second.object_hash, first.object_hash]
        assert all(row["manifest"] is None for row in resp.json())

    def test_grantee_reads_by_hash_with_manifest(self, dataset, user, make_user):
        grantee = make_user()
        _grant(dataset, user, grantee)
        snapshot = self._snapshot(dataset, user)
        c = Client()
        c.force_login(grantee)
        resp = c.get(f"{DATASETS_URL}snapshots/{snapshot.object_hash.lower()}/")
        assert resp.status_code == 200, resp.content
        assert resp.json()["manifest"] == snapshot.manifest

    def test_non_grantee_gets_404_not_403(self, dataset, user, make_user):
        snapshot = self._snapshot(dataset, user)
        other = make_user()
        c = Client()
        c.force_login(other)
        assert c.get(f"{DATASETS_URL}snapshots/{snapshot.object_hash}/").status_code == 404

    def test_trashed_dataset_hides_its_snapshots(self, auth_client, dataset, user):
        from django.utils import timezone

        c, _user = auth_client
        snapshot = self._snapshot(dataset, user)
        dataset.deleted_at = timezone.now()
        dataset.save(update_fields=["deleted_at"])
        assert c.get(f"{DATASETS_URL}snapshots/{snapshot.object_hash}/").status_code == 404


@pytest.mark.django_db
class TestSnapshotImmutability:
    def test_no_update_or_delete_route_exists(self, auth_client, dataset, user):
        c, _user = auth_client
        snapshot = DatasetSnapshot.objects.create(dataset=dataset, author=user, manifest=[], manifest_hash="0" * 64)
        url = f"{DATASETS_URL}snapshots/{snapshot.object_hash}/"
        assert patch_json(c, url, {"label": "x"}).status_code == 405
        assert c.delete(url).status_code == 405

    def test_erasure_wins_and_verification_survives(self, auth_client, dataset, user):
        # Hard-delete a member (the erasure/purge path) and assert the stored
        # manifest stays byte-identical with its seal still verifying.
        c, _user = auth_client
        kept = _add_recording(dataset, user, content_hash="A" * 64)
        purged = _add_recording(dataset, user, content_hash="B" * 64)
        resp = post_json(c, f"{DATASETS_URL}{dataset.pk}/snapshots/", {})
        snapshot = DatasetSnapshot.objects.get(object_hash=resp.json()["object_hash"])
        manifest_before = list(snapshot.manifest)

        purged.delete()

        snapshot.refresh_from_db()
        assert snapshot.manifest == manifest_before
        canonical = json.dumps(snapshot.manifest, sort_keys=True, separators=(",", ":"))
        assert snapshot.manifest_hash == hashlib.sha256(canonical.encode()).hexdigest()
        assert {"content_type": "recordings.recording", "identity": "B" * 64} in snapshot.manifest
        assert kept.content_hash == "A" * 64
