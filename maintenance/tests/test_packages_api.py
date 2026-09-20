"""The package endpoints: upload with every refusal, listing, removal, pruning and reconciliation.

Each test builds a real package — a small tarball, a manifest in the shape
``release_sign.py`` writes and an Ed25519 signature over it — and posts the
three parts the way the Maintenance tab does, so what is exercised is the
same verification the deployment performs.
"""

import base64
import hashlib
import io
import json
import logging
import os
import sys
import tarfile
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from activity.models import Activity
from epicurrents.version import __version__
from maintenance import packaging, spool
from maintenance.models import MaintenanceJob, MaintenancePackage
from maintenance.tests.conftest import PASSWORD, place_package_files

BASE = "/api/v1/maintenance"
NEWER = "0.1.2"
SECURITY = "epicurrents.security"


@dataclass
class Signer:
    """A release key pair; ``sign`` returns the base64 signature the packager writes."""

    private: object

    def sign(self, data: bytes) -> bytes:
        return base64.b64encode(self.private.sign(data))


@pytest.fixture
def release_key(tmp_path):
    """A generated release key installed as ``REMOTE_UPDATE_RELEASE_KEY_PATH``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    public = tmp_path / "RELEASE_KEY.pub"
    public.write_bytes(
        private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    with override_settings(REMOTE_UPDATE_RELEASE_KEY_PATH=str(public)):
        yield Signer(private)


@pytest.fixture
def plain_deployment(monkeypatch):
    """No project, no plugins, no pins: what a package built with the same identity matches."""
    monkeypatch.delenv("EPICURRENTS_PROJECT", raising=False)
    monkeypatch.delenv("EPICURRENTS_PLUGINS", raising=False)
    monkeypatch.setattr(packaging, "_pinned_apps", list)


@pytest.fixture
def ready(host_enabled, release_key, plain_deployment, superuser_client):
    """Everything an upload needs: both flags, a key, a matching identity and a signed-in superuser."""
    client, user = superuser_client
    return SimpleNamespace(spool=host_enabled, key=release_key, client=client, user=user)


def tarball(version: str = NEWER, filler: bytes = b"") -> bytes:
    """A tiny package: one top-level directory holding a version module."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        body = f'__version__ = "{version}"\n'.encode() + filler
        info = tarfile.TarInfo(name="epicurrents-platform/epicurrents/version.py")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def manifest_for(data: bytes, *, version: str = NEWER, project: str = "", plugins=(), **overrides) -> bytes:
    """The manifest ``release_sign.py`` would write for ``data``, with fields overridden for a refusal."""
    fields = {
        "agent_version": 1,
        "built_at": "2026-09-20T12:00:00Z",
        "key_id": "abcdef0123456789",
        "manifest_version": packaging.MANIFEST_VERSION,
        "min_updater_version": 2,
        "package": "epicurrents-platform.tar.gz",
        "platform_compatible": ">=0.1,<0.2",
        "plugins": list(plugins),
        "project": project,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "version": version,
    }
    fields.update(overrides)
    return (
        "{\n" + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(fields.items())) + "\n}\n"
    ).encode()


def post_package(client, data: bytes, manifest: bytes, signature: bytes):
    return client.post(
        f"{BASE}/packages",
        {
            "package": SimpleUploadedFile("epicurrents-platform.tar.gz", data),
            "manifest": SimpleUploadedFile("epicurrents-platform.tar.gz.manifest.json", manifest),
            "signature": SimpleUploadedFile("epicurrents-platform.tar.gz.manifest.sig", signature),
        },
    )


def upload(ready, data: bytes | None = None, manifest: bytes | None = None, signature: bytes | None = None, **kw):
    """Post a package built from ``data`` (a fresh tarball by default); parts given explicitly are used as is."""
    data = data if data is not None else tarball()
    manifest = manifest if manifest is not None else manifest_for(data, **kw)
    signature = signature if signature is not None else ready.key.sign(manifest)
    return post_package(ready.client, data, manifest, signature)


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def security_events(caplog, event_type: str):
    return [r for r in caplog.records if getattr(r, "security_event_type", "") == event_type]


def incoming_dirs():
    root = spool.packages_dir()
    return [p.name for p in root.iterdir() if p.name.startswith(packaging.INCOMING_PREFIX)] if root.is_dir() else []


def _post_json(client, path, data=None):
    return client.post(path, json.dumps(data or {}), content_type="application/json")


@pytest.mark.django_db
class TestUpload:
    def test_a_signed_newer_package_is_stored_beside_its_manifest_and_signature(self, ready, caplog):
        caplog.set_level(logging.WARNING, logger=SECURITY)
        data = tarball()
        response = upload(ready, data)
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["sha256"] == sha_of(data) and body["version"] == NEWER and body["state"] == "available"
        assert body["applicable"] is True and body["uploaded_by"] == ready.user.username
        assert body["agent_version"] == 1 and body["size"] == len(data)
        assert "path" not in json.dumps(body)

        directory = packaging.package_dir(sha_of(data))
        assert sorted(p.name for p in directory.iterdir()) == sorted(
            [packaging.TARBALL_NAME, packaging.MANIFEST_NAME, packaging.SIGNATURE_NAME, packaging.UPLOAD_NAME]
        )
        assert (directory / packaging.TARBALL_NAME).read_bytes() == data
        assert (directory / packaging.MANIFEST_NAME).read_bytes() == manifest_for(data)
        record = json.loads((directory / packaging.UPLOAD_NAME).read_text())
        assert record["uploaded_by_id"] == ready.user.pk and record["protocol"] == spool.PROTOCOL
        assert incoming_dirs() == []

        row = MaintenancePackage.objects.get(sha256=sha_of(data))
        assert row.uploaded_by == ready.user and row.manifest["min_updater_version"] == 2
        assert row.built_at is not None and row.plugins == []

        activity = Activity.objects.filter(verb="maintenance.package.create").get()
        assert activity.actor == ready.user and activity.target_object_id == str(row.pk)
        assert activity.metadata == {}, "the target row carries the hash and the version"
        (event,) = security_events(caplog, "maintenance.package_uploaded")
        assert event.sha256 == row.sha256 and event.version == NEWER and event.pruned == 0

    @pytest.mark.parametrize(
        ("build", "status", "reason"),
        [
            pytest.param(
                lambda r, d: {"manifest": manifest_for(d) + b"\n", "signature": r.key.sign(manifest_for(d))},
                400,
                "signature",
                id="altered-manifest",
            ),
            pytest.param(lambda r, d: {"signature": b"bm90IGEgc2lnbmF0dXJl"}, 400, "signature", id="wrong-signature"),
            pytest.param(lambda r, d: {"signature": b"@@not base64@@"}, 400, "signature", id="garbage-signature"),
            pytest.param(lambda r, d: {"data": d + b"x"}, 400, "hash", id="altered-tarball"),
            pytest.param(lambda r, d: {"size": len(d) + 1}, 400, "hash", id="size-mismatch"),
            pytest.param(lambda r, d: {"manifest_version": 2}, 400, "manifest", id="newer-manifest-format"),
            pytest.param(lambda r, d: {"sha256": "nope"}, 400, "manifest", id="unusable-sha"),
            pytest.param(lambda r, d: {"version": "0.1.2-rc1"}, 400, "manifest", id="prerelease-version"),
            pytest.param(lambda r, d: {"version": __version__}, 400, "version_not_newer", id="same-version"),
            pytest.param(lambda r, d: {"version": "0.1.0"}, 400, "version_not_newer", id="older-version"),
            pytest.param(lambda r, d: {"project": "somecourse"}, 400, "incompatible", id="other-project"),
            pytest.param(lambda r, d: {"plugins": ["dicom"]}, 400, "incompatible", id="other-plugins"),
        ],
    )
    def test_a_bad_package_is_refused_by_name_and_leaves_nothing_behind(self, ready, caplog, build, status, reason):
        caplog.set_level(logging.WARNING, logger=SECURITY)
        data = tarball()
        parts = build(ready, data)
        posted = parts.pop("data", data)
        manifest = parts.pop("manifest", None)
        signature = parts.pop("signature", None)
        if manifest is None:
            manifest = manifest_for(data, **parts)
        if signature is None:
            signature = ready.key.sign(manifest)
        response = post_package(ready.client, posted, manifest, signature)
        assert response.status_code == status, response.content
        assert response.json()["reason"] == reason
        assert response.json()["detail"]
        assert not MaintenancePackage.objects.exists()
        assert not list(spool.packages_dir().iterdir()) if spool.packages_dir().is_dir() else True
        assert not Activity.objects.filter(verb="maintenance.package.create").exists()
        (event,) = security_events(caplog, "maintenance.package_rejected")
        assert event.reason == reason and event.actor_id == ready.user.pk

    def test_a_json_manifest_that_is_not_an_object_is_refused(self, ready):
        manifest = b"[1, 2]\n"
        response = upload(ready, manifest=manifest)
        assert response.status_code == 400 and response.json()["reason"] == "manifest"

    def test_a_package_over_the_cap_is_413_before_it_is_copied(self, ready):
        data = tarball()
        with override_settings(REMOTE_UPDATE_MAX_PACKAGE_SIZE=len(data) - 1):
            response = upload(ready, data)
        assert response.status_code == 413 and response.json()["reason"] == "too_large"
        assert incoming_dirs() == [] and not MaintenancePackage.objects.exists()

    def test_a_package_that_streams_past_the_cap_is_413(self, ready):
        """The manifest may lie about the size; the copy is capped on its own."""
        data = tarball()
        manifest = manifest_for(data, size=10)
        with override_settings(REMOTE_UPDATE_MAX_PACKAGE_SIZE=20):
            response = upload(ready, data, manifest=manifest)
        assert response.status_code == 413 and response.json()["reason"] == "too_large"
        assert incoming_dirs() == []

    def test_no_release_key_is_409_and_names_the_setting(self, ready, caplog):
        caplog.set_level(logging.WARNING, logger=SECURITY)
        with override_settings(REMOTE_UPDATE_RELEASE_KEY_PATH=str(ready.spool / "missing.pub")):
            response = upload(ready)
        assert response.status_code == 409 and response.json()["reason"] == "key_missing"
        assert "REMOTE_UPDATE_RELEASE_KEY_PATH" in response.json()["detail"]
        assert security_events(caplog, "maintenance.package_rejected")[0].reason == "key_missing"

    def test_a_key_of_the_wrong_kind_counts_as_missing(self, ready, tmp_path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

        rsa = generate_private_key(public_exponent=65537, key_size=2048)
        path = tmp_path / "rsa.pub"
        path.write_bytes(
            rsa.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        with override_settings(REMOTE_UPDATE_RELEASE_KEY_PATH=str(path)):
            response = upload(ready)
        assert response.status_code == 409 and response.json()["reason"] == "key_missing"

    def test_the_same_package_twice_is_409_and_the_first_copy_is_untouched(self, ready):
        data = tarball()
        assert upload(ready, data).status_code == 201
        stamp = os.stat(packaging.package_dir(sha_of(data)) / packaging.TARBALL_NAME).st_mtime_ns
        response = upload(ready, data)
        assert response.status_code == 409 and response.json()["reason"] == "duplicate"
        assert os.stat(packaging.package_dir(sha_of(data)) / packaging.TARBALL_NAME).st_mtime_ns == stamp
        assert MaintenancePackage.objects.count() == 1 and incoming_dirs() == []

    def test_a_pin_the_package_version_does_not_satisfy_is_refused(self, ready, monkeypatch):
        pinned = SimpleNamespace(name="projects.somecourse", label="somecourse", requires_platform=">=0.2,<0.3")
        monkeypatch.setattr(packaging, "_pinned_apps", lambda: [pinned])
        response = upload(ready)
        assert response.status_code == 400 and response.json()["reason"] == "incompatible"
        assert "somecourse" in response.json()["detail"] and ">=0.2,<0.3" in response.json()["detail"]

    def test_a_pin_the_package_satisfies_passes(self, ready, monkeypatch):
        pinned = SimpleNamespace(name="plugins.something", label="something", requires_platform=">=0.1,<0.2")
        monkeypatch.setattr(packaging, "_pinned_apps", lambda: [pinned])
        assert upload(ready).status_code == 201

    def test_the_identity_check_reads_the_deployment_environment(self, ready, monkeypatch):
        monkeypatch.setenv("EPICURRENTS_PROJECT", "somecourse")
        monkeypatch.setenv("EPICURRENTS_PLUGINS", "dicom, other")
        assert upload(ready).status_code == 400
        # Plugin order and spacing do not matter; the set does.
        assert upload(ready, project="somecourse", plugins=["other", "dicom"]).status_code == 201

    def test_a_manifest_or_signature_part_that_is_far_too_large_is_refused_unread(self, ready):
        response = upload(ready, signature=b"A" * (packaging.SIGNATURE_LIMIT + 1))
        assert response.status_code == 400 and response.json()["reason"] == "manifest"

    def test_a_disk_without_room_for_two_copies_is_409_before_the_copy(self, ready, monkeypatch, caplog):
        caplog.set_level(logging.WARNING, logger=SECURITY)
        data = tarball()
        usage = SimpleNamespace(total=10**12, used=0, free=len(data) * 2 - 1)
        monkeypatch.setattr(packaging.shutil, "disk_usage", lambda path: usage)
        response = upload(ready, data)
        assert response.status_code == 409 and response.json()["reason"] == "disk"
        assert incoming_dirs() == [] and not MaintenancePackage.objects.exists()
        assert security_events(caplog, "maintenance.package_rejected")[0].reason == "disk"

    def test_an_unwritable_packages_directory_is_409(self, ready, monkeypatch):
        def refuse(*args, **kwargs):
            raise PermissionError("read-only")

        monkeypatch.setattr(packaging.Path, "mkdir", refuse)
        response = upload(ready)
        assert response.status_code == 409 and response.json()["reason"] == "spool"


@pytest.mark.django_db
class TestGates:
    def test_the_feature_off_is_404(self, spool_dir, superuser_client):
        client, _ = superuser_client
        with override_settings(REMOTE_MAINTENANCE_ENABLED=False):
            assert client.get(f"{BASE}/packages").status_code == 404
            assert post_package(client, b"x", b"{}", b"x").status_code == 404
            assert client.delete(f"{BASE}/packages/{'a' * 64}").status_code == 404

    def test_staff_list_but_cannot_upload_or_remove(self, enabled, staff_client):
        client, _ = staff_client
        assert client.get(f"{BASE}/packages").status_code == 200
        assert post_package(client, b"x", b"{}", b"x").status_code == 403
        assert client.delete(f"{BASE}/packages/{'a' * 64}").status_code == 403

    def test_the_host_tier_off_refuses_an_upload(self, enabled, release_key, plain_deployment, superuser_client):
        client, _ = superuser_client
        data = tarball()
        manifest = manifest_for(data)
        assert post_package(client, data, manifest, release_key.sign(manifest)).status_code == 403
        assert not MaintenancePackage.objects.exists()

    def test_a_missing_part_is_422(self, ready):
        response = ready.client.post(f"{BASE}/packages", {"package": SimpleUploadedFile("p.tar.gz", b"x")})
        assert response.status_code == 422


@pytest.mark.django_db
class TestListAndRemove:
    def test_packages_list_newest_first_with_pruned_ones_kept(self, ready):
        first, second = tarball(filler=b"1"), tarball(filler=b"2")
        assert upload(ready, first).status_code == 201
        assert upload(ready, second, version="0.1.3").status_code == 201
        rows = ready.client.get(f"{BASE}/packages").json()
        assert [row["version"] for row in rows] == ["0.1.3", NEWER]
        assert Activity.objects.filter(verb="maintenance.package.list").exists()

    def test_remove_deletes_the_files_and_keeps_the_row_as_pruned(self, ready, caplog):
        caplog.set_level(logging.WARNING, logger=SECURITY)
        data = tarball()
        upload(ready, data)
        response = ready.client.delete(f"{BASE}/packages/{sha_of(data)}")
        assert response.status_code == 200, response.content
        assert response.json()["state"] == "pruned" and response.json()["applicable"] is False
        assert not packaging.package_dir(sha_of(data)).exists()
        row = MaintenancePackage.objects.get(sha256=sha_of(data))
        assert row.state == "pruned"
        activity = Activity.objects.filter(verb="maintenance.package.delete").get()
        assert activity.target_object_id == str(row.pk) and activity.metadata == {}
        (event,) = security_events(caplog, "maintenance.package_removed")
        assert event.sha256 == row.sha256
        assert ready.client.delete(f"{BASE}/packages/{sha_of(data)}").status_code == 409

    def test_a_package_a_job_in_flight_names_cannot_be_removed(self, ready):
        data = tarball()
        upload(ready, data)
        row = MaintenancePackage.objects.get()
        MaintenanceJob.objects.create(
            operation="platform.update", executor="host", requested_by=ready.user, package=row, state="running"
        )
        assert ready.client.delete(f"{BASE}/packages/{sha_of(data)}").status_code == 409
        assert packaging.package_dir(sha_of(data)).is_dir()

    def test_files_that_will_not_go_leave_the_row_available_and_answer_409(self, ready, monkeypatch):
        data = tarball()
        upload(ready, data)

        def refuse(sha256):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(packaging, "remove_files", refuse)
        response = ready.client.delete(f"{BASE}/packages/{sha_of(data)}")
        assert response.status_code == 409 and "could not be removed" in response.json()["detail"]
        assert MaintenancePackage.objects.get().state == "available"

    def test_an_unknown_or_malformed_hash_is_404(self, ready):
        assert ready.client.delete(f"{BASE}/packages/{'a' * 64}").status_code == 404
        assert ready.client.delete(f"{BASE}/packages/not-a-hash").status_code == 404

    def test_a_removed_package_can_be_uploaded_again_and_revives_its_row(self, ready):
        data = tarball()
        upload(ready, data)
        ready.client.delete(f"{BASE}/packages/{sha_of(data)}")
        response = upload(ready, data)
        assert response.status_code == 201 and response.json()["state"] == "available"
        assert MaintenancePackage.objects.count() == 1
        assert packaging.package_dir(sha_of(data)).is_dir()


@pytest.mark.django_db
class TestPruning:
    def test_only_the_newest_packages_keep_their_files(self, ready, caplog):
        caplog.set_level(logging.WARNING, logger=SECURITY)
        blobs = [tarball(filler=str(i).encode()) for i in range(3)]
        with override_settings(REMOTE_UPDATE_KEEP_PACKAGES=2):
            for i, data in enumerate(blobs):
                assert upload(ready, data, version=f"0.1.{2 + i}").status_code == 201
        states = {row.version: row.state for row in MaintenancePackage.objects.all()}
        assert states == {"0.1.2": "pruned", "0.1.3": "available", "0.1.4": "available"}
        assert not packaging.package_dir(sha_of(blobs[0])).exists()
        assert packaging.package_dir(sha_of(blobs[1])).is_dir() and packaging.package_dir(sha_of(blobs[2])).is_dir()
        assert security_events(caplog, "maintenance.package_uploaded")[-1].pruned == 1

    def test_a_package_a_job_in_flight_names_survives_pruning(self, ready):
        old = tarball(filler=b"old")
        upload(ready, old)
        row = MaintenancePackage.objects.get()
        MaintenanceJob.objects.create(
            operation="platform.update", executor="host", requested_by=ready.user, package=row, state="running"
        )
        with override_settings(REMOTE_UPDATE_KEEP_PACKAGES=1):
            assert upload(ready, tarball(filler=b"new"), version="0.1.3").status_code == 201
        row.refresh_from_db()
        assert row.state == "available" and packaging.package_dir(row.sha256).is_dir()

    def test_a_prune_that_cannot_remove_files_does_not_fail_the_upload(self, ready, monkeypatch):
        upload(ready, tarball(filler=b"old"))

        def refuse(sha256):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(packaging, "remove_files", refuse)
        with override_settings(REMOTE_UPDATE_KEEP_PACKAGES=1):
            assert upload(ready, tarball(filler=b"new"), version="0.1.3").status_code == 201
        assert MaintenancePackage.objects.filter(state="available").count() == 2

    def test_keep_zero_still_keeps_the_package_just_uploaded(self, ready):
        data = tarball()
        with override_settings(REMOTE_UPDATE_KEEP_PACKAGES=0):
            assert upload(ready, data).status_code == 201
        assert packaging.package_dir(sha_of(data)).is_dir()


@pytest.mark.django_db
class TestReconciliation:
    def test_rows_erased_by_a_restore_come_back_from_the_directories(self, ready):
        data = tarball()
        upload(ready, data)
        MaintenancePackage.objects.all().delete()
        # The list runs the sync, but under its five-second lock the upload just took; force it as beat would.
        assert spool.sync(force=True)["packages"] == {"created": 1, "pruned": 0, "swept": 0}
        rows = ready.client.get(f"{BASE}/packages").json()
        assert [row["sha256"] for row in rows] == [sha_of(data)]
        assert rows[0]["uploaded_by"] == ready.user.username and rows[0]["version"] == NEWER
        row = MaintenancePackage.objects.get()
        assert row.manifest["sha256"] == sha_of(data) and row.state == "available"
        assert Activity.objects.filter(verb="maintenance.package.sync").exists()

    def test_a_directory_removed_by_hand_marks_the_row_pruned(self, ready):
        data = tarball()
        upload(ready, data)
        packaging.remove_files(sha_of(data))
        assert spool.sync(force=True)["packages"] == {"created": 0, "pruned": 1, "swept": 0}
        assert MaintenancePackage.objects.get().state == "pruned"

    def test_incoming_directories_and_strays_are_not_packages(self, ready):
        root = spool.packages_dir()
        (root / f"{packaging.INCOMING_PREFIX}abc").mkdir(parents=True)
        (root / f"{packaging.INCOMING_PREFIX}abc" / packaging.MANIFEST_NAME).write_bytes(manifest_for(b"x"))
        (root / ("b" * 64)).mkdir()
        (root / ("b" * 64) / packaging.MANIFEST_NAME).write_bytes(manifest_for(b"x"))
        (root / "notes.txt").write_text("hello")
        assert ready.client.get(f"{BASE}/packages").json() == []
        assert not MaintenancePackage.objects.exists()

    def test_a_stale_incoming_directory_is_swept_and_a_fresh_one_kept(self, ready):
        root = spool.packages_dir()
        stale = root / f"{packaging.INCOMING_PREFIX}stale"
        fresh = root / f"{packaging.INCOMING_PREFIX}fresh"
        stale.mkdir(parents=True)
        fresh.mkdir()
        (stale / packaging.TARBALL_NAME).write_bytes(b"half")
        old = os.stat(stale).st_mtime - packaging.INCOMING_MAX_AGE_SECONDS - 60
        os.utime(stale, (old, old))
        assert spool.sync(force=True)["packages"]["swept"] == 1
        assert not stale.exists() and fresh.is_dir()

    def test_a_directory_whose_manifest_names_another_hash_is_ignored(self, ready):
        data = tarball()
        wrong = spool.packages_dir() / ("c" * 64)
        wrong.mkdir(parents=True)
        (wrong / packaging.MANIFEST_NAME).write_bytes(manifest_for(data))
        (wrong / packaging.TARBALL_NAME).write_bytes(data)
        assert ready.client.get(f"{BASE}/packages").json() == []

    def test_a_confirmed_update_marks_its_package_applied(self, ready, write_status, no_push):
        data = tarball()
        upload(ready, data)
        row = MaintenancePackage.objects.get()
        job = MaintenanceJob.objects.create(
            operation="platform.update", executor="host", requested_by=ready.user, package=row, state="running"
        )
        write_status(job.job_id, "succeeded", finished_at="2026-09-20T10:06:00Z")
        spool.sync(force=True)
        row.refresh_from_db()
        assert row.state == "applied"
        listed = ready.client.get(f"{BASE}/packages").json()[0]
        assert listed["state"] == "applied" and listed["applicable"] is False


@pytest.mark.django_db
class TestRequestingAnUpdate:
    def _request(self, ready, sha256, **args):
        return _post_json(
            ready.client,
            f"{BASE}/jobs",
            {"operation": "platform.update", "args": {"package_sha256": sha256, **args}, "password": PASSWORD},
        )

    def test_the_deployment_default_window_is_written_into_the_request(self, ready, django_capture_on_commit_callbacks):
        data = tarball()
        upload(ready, data)
        with (
            override_settings(REMOTE_UPDATE_VERIFY_WINDOW_MINUTES=45),
            django_capture_on_commit_callbacks(execute=True),
        ):
            response = self._request(ready, sha_of(data))
        assert response.status_code == 202, response.content
        job = MaintenanceJob.objects.get(job_id=response.json()["job_id"])
        assert job.args == {"package_sha256": sha_of(data), "verify_window_minutes": 45}
        assert job.target_version == NEWER and job.package.sha256 == sha_of(data)
        request = spool.read_request(job.job_id)
        assert request["args"]["verify_window_minutes"] == 45

    def test_a_window_the_request_names_is_kept(self, ready, django_capture_on_commit_callbacks):
        data = tarball()
        upload(ready, data)
        with django_capture_on_commit_callbacks(execute=True):
            response = self._request(ready, sha_of(data), verify_window_minutes=10)
        assert response.status_code == 202
        assert MaintenanceJob.objects.get().args["verify_window_minutes"] == 10

    def test_a_package_that_is_no_longer_newer_is_refused(self, ready):
        place_package_files("d" * 64, "0.1.0")
        row = MaintenancePackage.objects.create(sha256="d" * 64, version="0.1.0")
        response = self._request(ready, row.sha256)
        assert response.status_code == 400 and "not newer" in response.json()["detail"]
        assert not MaintenanceJob.objects.exists()

    def test_a_pruned_package_cannot_be_named(self, ready):
        data = tarball()
        upload(ready, data)
        ready.client.delete(f"{BASE}/packages/{sha_of(data)}")
        assert self._request(ready, sha_of(data)).status_code == 400


@pytest.mark.django_db
class TestAgentSummary:
    def test_capabilities_and_the_updater_script_version_pass_through(self, enabled, staff_client):
        client, _ = staff_client
        spool.write_json_atomic(
            spool.spool_path() / "agent.json",
            {
                "protocol": 1,
                "version": "1",
                "enabled": True,
                "runtime": "docker",
                "last_run": spool.now_iso(),
                "capabilities": ["platform.update"],
                "updater_script": "2",
            },
        )
        agent = client.get(f"{BASE}/status").json()["agent"]
        assert agent["capabilities"] == ["platform.update"] and agent["updater_script"] == 2

    def test_a_heartbeat_without_them_reads_as_none_and_empty(self, enabled, staff_client):
        client, _ = staff_client
        spool.write_json_atomic(
            spool.spool_path() / "agent.json",
            {"protocol": 1, "version": "1", "enabled": False, "runtime": None, "last_run": spool.now_iso()},
        )
        agent = client.get(f"{BASE}/status").json()["agent"]
        assert agent["capabilities"] == [] and agent["updater_script"] is None

    def test_no_heartbeat_carries_the_same_shape(self, enabled, staff_client):
        client, _ = staff_client
        agent = client.get(f"{BASE}/status").json()["agent"]
        assert agent["installed"] is False and agent["capabilities"] == [] and agent["updater_script"] is None


class TestProxyBodyGuard:
    """The boot guard compares the proxy's body cap with the package cap once remote updates are on."""

    def _run(self, monkeypatch, proxy: str):
        monkeypatch.setenv("PROXY_MAX_BODY_SIZE", proxy)
        monkeypatch.setattr(sys, "argv", ["manage.py", "runserver"])
        django_apps.get_app_config("epicurrents")._guard_proxy_body_limit()

    def test_a_proxy_cap_below_the_package_cap_stops_the_boot_with_the_setting_named(self, monkeypatch):
        with (
            override_settings(
                REMOTE_UPDATE_ENABLED=True,
                REMOTE_UPDATE_MAX_PACKAGE_SIZE=100 * 1024 * 1024,
                RECORDINGS_MAX_UPLOAD_SIZE=1024,
            ),
            pytest.raises(ImproperlyConfigured, match="REMOTE_UPDATE_MAX_PACKAGE_SIZE"),
        ):
            self._run(monkeypatch, "50MiB")

    def test_the_package_cap_does_not_count_while_remote_updates_are_off(self, monkeypatch):
        with override_settings(
            REMOTE_UPDATE_ENABLED=False,
            REMOTE_UPDATE_MAX_PACKAGE_SIZE=100 * 1024 * 1024,
            RECORDINGS_MAX_UPLOAD_SIZE=1024,
        ):
            self._run(monkeypatch, "50MiB")

    def test_the_recordings_cap_still_wins_when_it_is_the_larger(self, monkeypatch):
        with (
            override_settings(
                REMOTE_UPDATE_ENABLED=True,
                REMOTE_UPDATE_MAX_PACKAGE_SIZE=1024,
                RECORDINGS_MAX_UPLOAD_SIZE=100 * 1024 * 1024,
            ),
            pytest.raises(ImproperlyConfigured, match="RECORDINGS_MAX_UPLOAD_SIZE"),
        ):
            self._run(monkeypatch, "50MiB")
