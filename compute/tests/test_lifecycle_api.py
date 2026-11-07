"""The recording's jobs and versions over the API: who sees what, and the version-fetch contract's shapes."""

import json
from pathlib import Path

import numpy as np
import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from compute import derive
from compute.models import ArtifactCacheEntry, RecordingJob
from compute.tests.test_lifecycle import _ingest, _RecordingJob, _XorStage, isolated  # noqa: F401
from epicurrents.models import AccessRight
from recordings.models import Recording
from recordings.pipeline import SOURCE_VERSION_ID, registry
from recordings.processors.edf_window import read_window


def _handle(recording):
    return recording.stored_name.split(".", 1)[0]


def _grant(recording, reader, *, apply_middleware=True):
    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
        object_id=str(recording.pk),
        access_giver=recording.author,
        access_target=reader,
        can_read=True,
        apply_middleware=apply_middleware,
    )


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _frame(body: bytes):
    length = int.from_bytes(body[:4], "little")
    header = json.loads(body[4 : 4 + length])
    payload = np.frombuffer(body[4 + length :], dtype="<f4")
    channels, offset = {}, 0
    for channel in header["channels"]:
        channels[channel["name"]] = payload[offset : offset + channel["count"]]
        offset += channel["count"]
    return header, channels


@pytest.fixture
def built(user, tmp_path, django_capture_on_commit_callbacks):
    registry.register_reconstruction_stage(_XorStage())
    recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
    return recording


@pytest.mark.django_db
class TestJobs:
    def test_the_author_sees_the_stages_and_the_counts(self, user, built):
        resp = _client(user).get(f"/recordings/api/v1/{_handle(built)}/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert (body["status"], body["done"], body["total"]) == ("ready", 1, 1)
        (job,) = body["jobs"]
        assert (job["name"], job["phase"], job["state"]) == ("xq_flip", "reconstruct", "done")

    def test_a_grantee_reads_the_jobs_without_their_errors(
        self, user, make_user, tmp_path, django_capture_on_commit_callbacks
    ):
        registry.register_reconstruction_stage(_XorStage(fail=True))
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        reader = make_user()
        _grant(recording, reader)
        author_job = _client(user).get(f"/recordings/api/v1/{_handle(recording)}/jobs").json()["jobs"][0]
        reader_job = _client(reader).get(f"/recordings/api/v1/{_handle(recording)}/jobs").json()["jobs"][0]
        assert "refused this input" in author_job["error"]
        assert reader_job["error"] is None
        assert reader_job["state"] == "failed"

    def test_a_stranger_is_refused(self, make_user, built):
        assert _client(make_user()).get(f"/recordings/api/v1/{_handle(built)}/jobs").status_code == 403


@pytest.mark.django_db
class TestJobChanges:
    def test_disabling_a_stage_reruns_and_the_default_becomes_the_source(
        self, user, built, django_capture_on_commit_callbacks
    ):
        client = _client(user)
        url = f"/recordings/api/v1/{_handle(built)}/jobs"
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.patch(
                url, {"stages": [{"name": "xq_flip", "enabled": False}]}, content_type="application/json"
            )
        assert resp.status_code == 200
        built.refresh_from_db()
        assert built.status == Recording.Status.READY
        assert RecordingJob.objects.get(recording=built).state == RecordingJob.State.SKIPPED
        listing = client.get(f"/recordings/api/v1/{_handle(built)}/versions").json()
        assert listing["default_version"] == SOURCE_VERSION_ID

    def test_an_order_that_breaks_a_dependency_is_refused(self, user, tmp_path, django_capture_on_commit_callbacks):
        registry.register_reconstruction_stage(_XorStage("xq_first", order_hint=10))
        registry.register_reconstruction_stage(_XorStage("xq_second", order_hint=20, requires=("xq_first",)))
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        resp = _client(user).patch(
            f"/recordings/api/v1/{_handle(recording)}/jobs",
            {"order": ["xq_second", "xq_first"]},
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert "must run after xq_first" in resp.json()["detail"]

    def test_an_unknown_stage_is_404(self, user, built):
        resp = _client(user).patch(
            f"/recordings/api/v1/{_handle(built)}/jobs",
            {"stages": [{"name": "nope", "enabled": False}]},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_a_reader_cannot_change_the_jobs(self, user, make_user, built):
        reader = make_user()
        _grant(built, reader, apply_middleware=False)
        resp = _client(reader).patch(
            f"/recordings/api/v1/{_handle(built)}/jobs",
            {"stages": [{"name": "xq_flip", "enabled": False}]},
            content_type="application/json",
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestVersions:
    def test_the_listing_names_the_source_and_the_built_default_with_provenance(self, user, built):
        listing = _client(user).get(f"/recordings/api/v1/{_handle(built)}/versions").json()
        entry = ArtifactCacheEntry.objects.get(recording=built)
        assert listing["source_hash"] == f"sha256:{built.stored_hash}"
        assert listing["default_version"] == entry.version_id
        source, version = listing["versions"]
        assert source["version_id"] == SOURCE_VERSION_ID
        assert all(channel["modified_by"] == [] for channel in source["channels"])
        assert version["version_id"] == entry.version_id
        assert [s["stage"] for s in version["manifest"]] == ["xq_flip"]
        assert all(channel["modified_by"] == ["xq_flip"] for channel in version["channels"])
        assert {c["name"] for c in version["channels"]} == {c["name"] for c in source["channels"]}

    def test_a_version_still_being_built_is_listed_as_building(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks, run_dispatch=False)
        listing = _client(user).get(f"/recordings/api/v1/{_handle(recording)}/versions").json()
        building = listing["versions"][-1]
        assert building["state"] == "building"
        assert listing["default_version"] == building["version_id"]
        resp = _client(user).get(
            f"/recordings/api/v1/{_handle(recording)}/versions/{building['version_id']}/signal",
            {"channels": listing["versions"][0]["channels"][0]["name"]},
        )
        assert resp.status_code == 202
        assert resp.json()["state"] == "building"


@pytest.mark.django_db
class TestSignal:
    def test_a_window_decodes_each_channel_of_the_version(self, user, built):
        entry = ArtifactCacheEntry.objects.get(recording=built)
        names = [
            c["name"]
            for c in _client(user)
            .get(f"/recordings/api/v1/{_handle(built)}/versions")
            .json()["versions"][0]["channels"]
        ]
        resp = _client(user).get(
            f"/recordings/api/v1/{_handle(built)}/versions/{entry.version_id}/signal",
            {"channels": ",".join(names), "start_sec": 1, "duration_sec": 1},
        )
        assert resp.status_code == 200
        header, channels = _frame(resp.content)
        assert header["version_id"] == entry.version_id
        assert [c["name"] for c in header["channels"]] == names
        expected = read_window(Path(entry.storage_path), names, 1, 1)
        for window in expected:
            assert np.array_equal(channels[window.name], window.samples)
            assert header["channels"][names.index(window.name)]["start"] == window.start

    def test_the_source_and_the_version_differ_where_the_stage_changed_them(self, user, built):
        entry = ArtifactCacheEntry.objects.get(recording=built)
        client = _client(user)
        name = client.get(f"/recordings/api/v1/{_handle(built)}/versions").json()["versions"][0]["channels"][0]["name"]
        base = f"/recordings/api/v1/{_handle(built)}/versions"
        _h, source = _frame(client.get(f"{base}/source/signal", {"channels": name}).content)
        _h, version = _frame(client.get(f"{base}/{entry.version_id}/signal", {"channels": name}).content)
        assert not np.array_equal(source[name], version[name])

    def test_a_window_off_the_record_grid_is_refused(self, user, built):
        resp = _client(user).get(
            f"/recordings/api/v1/{_handle(built)}/versions/source/signal",
            {"channels": "x", "start_sec": 0.5},
        )
        assert resp.status_code == 400
        assert resp.json()["record_duration_sec"] == 1.0

    def test_a_channel_the_version_lacks_is_refused(self, user, built):
        resp = _client(user).get(
            f"/recordings/api/v1/{_handle(built)}/versions/source/signal", {"channels": "NOT_A_CHANNEL"}
        )
        assert resp.status_code == 400

    def test_a_de_identifying_grant_reads_the_same_samples(self, user, make_user, built):
        reader = make_user()
        _grant(built, reader)
        url = f"/recordings/api/v1/{_handle(built)}/versions/source/signal"
        name = (
            _client(user)
            .get(f"/recordings/api/v1/{_handle(built)}/versions")
            .json()["versions"][0]["channels"][0]["name"]
        )
        _h, author = _frame(_client(user).get(url, {"channels": name}).content)
        _h, grantee = _frame(_client(reader).get(url, {"channels": name}).content)
        assert np.array_equal(author[name], grantee[name])
        assert "no-store" in _client(reader).get(url, {"channels": name})["Cache-Control"]

    def test_a_linked_recording_is_not_listed_to_a_de_identifying_grant(self, user, make_user, built):
        reader = make_user()
        _grant(built, reader)
        Recording.objects.filter(pk=built.pk).update(storage=Recording.Storage.LINKED)
        resp = _client(reader).get(f"/recordings/api/v1/{_handle(built)}/versions")
        assert resp.status_code == 403


@pytest.mark.django_db
class TestVersionDownload:
    def test_the_default_version_downloads_as_a_whole_edf(self, user, built):
        entry = ArtifactCacheEntry.objects.get(recording=built)
        resp = _client(user).get(f"/recordings/api/v1/{_handle(built)}/file", {"version": "default"})
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == Path(entry.storage_path).read_bytes()

    def test_the_content_pin_cannot_be_combined_with_a_version(self, user, built):
        resp = _client(user).get(
            f"/recordings/api/v1/{_handle(built)}/file",
            {"version": "default", "expect_stored_hash": built.stored_hash},
        )
        assert resp.status_code == 400

    def test_a_version_that_is_not_materialised_is_409(self, user, built):
        resp = _client(user).get(f"/recordings/api/v1/{_handle(built)}/file", {"version": "d" * 64})
        assert resp.status_code == 409

    def test_no_version_still_serves_the_stored_file(self, user, built):
        resp = _client(user).get(f"/recordings/api/v1/{_handle(built)}/file")
        assert b"".join(resp.streaming_content) == Path(built.file_path).read_bytes()


@pytest.mark.django_db
def test_a_recording_with_no_pipeline_lists_no_versions(user, tmp_path, django_capture_on_commit_callbacks):
    derive.register_derive_job(_RecordingJob())
    recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
    listing = _client(user).get(f"/recordings/api/v1/{_handle(recording)}/versions").json()
    assert [v["version_id"] for v in listing["versions"]] == [SOURCE_VERSION_ID]
    assert listing["default_version"] == SOURCE_VERSION_ID


@pytest.mark.django_db
class TestDigestWithheld:
    """A version id hashes a manifest that names the stored digest, so a reader denied the digest is denied the ids."""

    def _text_preserved(self, recording):
        from recordings.models import RecordingMeta

        RecordingMeta.objects.filter(
            content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
            object_id=str(recording.pk),
        ).update(annotation_text_preserved=True)

    def test_a_text_preserving_recording_lists_no_versions_to_a_de_identifying_grant(self, user, make_user, built):
        reader = make_user()
        _grant(built, reader)
        self._text_preserved(built)
        client = _client(reader)
        assert client.get(f"/recordings/api/v1/{_handle(built)}/versions").status_code == 403
        entry = ArtifactCacheEntry.objects.get(recording=built)
        resp = client.get(f"/recordings/api/v1/{_handle(built)}/versions/{entry.version_id}/signal", {"channels": "x"})
        assert resp.status_code == 403
        jobs = client.get(f"/recordings/api/v1/{_handle(built)}/jobs").json()["jobs"]
        assert all(job["version_id"] == "" for job in jobs)
        # The default version still opens, as a file that names no id.
        assert client.get(f"/recordings/api/v1/{_handle(built)}/file", {"version": "default"}).status_code == 200

    def test_a_raw_grant_on_a_linked_recording_lists_no_versions(self, user, make_user, built):
        reader = make_user()
        _grant(built, reader, apply_middleware=False)
        Recording.objects.filter(pk=built.pk).update(storage=Recording.Storage.LINKED)
        assert _client(reader).get(f"/recordings/api/v1/{_handle(built)}/versions").status_code == 403

    def test_the_author_still_lists_versions(self, user, built):
        self._text_preserved(built)
        assert _client(user).get(f"/recordings/api/v1/{_handle(built)}/versions").status_code == 200


@pytest.mark.django_db
def test_a_reader_gets_no_job_times(user, make_user, built):
    """A stage starts within minutes of the upload, the time ``created_at`` is truncated to the month to hide."""
    reader = make_user()
    _grant(built, reader, apply_middleware=False)
    author_job = _client(user).get(f"/recordings/api/v1/{_handle(built)}/jobs").json()["jobs"][0]
    reader_job = _client(reader).get(f"/recordings/api/v1/{_handle(built)}/jobs").json()["jobs"][0]
    assert author_job["started_at"] is not None
    assert (reader_job["started_at"], reader_job["finished_at"]) == (None, None)
    assert reader_job["state"] == "done"
