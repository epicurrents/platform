"""Free-text warnings on the recording upload and rename responses, and the push-body handle."""

import hashlib
import io
import json
from unittest.mock import patch

import pytest
from django.test import override_settings
from model_bakery import baker

from recordings.models import Recording
from recordings.tasks import process_recording
from recordings.tests.test_edf_processor import _make_edfplus_file

UPLOAD_URL = "/recordings/api/v1/upload"
HASH = "0123456789ABCDEF0123456789ABCDEF"


def _kinds(payload) -> list[tuple[str, str]]:
    return [(row["field"], row["kind"]) for row in payload["warnings"]]


def _upload(client, tmp_path, display_name: str | None):
    staging = tmp_path / "staging"
    uploads = tmp_path / "uploads"
    staging.mkdir(exist_ok=True)
    uploads.mkdir(exist_ok=True)
    query = f"?display_name={display_name}" if display_name is not None else ""
    with (
        override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
        patch("recordings.tasks.process_recording.delay"),
    ):
        f = io.BytesIO(b"fake edf data")
        f.name = "sample.edf"
        return client.post(f"{UPLOAD_URL}{query}", {"file": f}, format="multipart")


@pytest.mark.django_db
class TestUpload:
    def test_identifier_shaped_label_warns_and_is_accepted(self, auth_client, tmp_path):
        c, _ = auth_client
        resp = _upload(c, tmp_path, "Doe, Jane")
        assert resp.status_code == 202
        assert _kinds(resp.json()) == [("display_name", "person_name")]
        assert Recording.objects.get(stored_name=resp.json()["stored_name"]).display_name == "Doe, Jane"

    def test_clean_label_has_no_warnings(self, auth_client, tmp_path):
        c, _ = auth_client
        resp = _upload(c, tmp_path, "Baseline")
        assert resp.status_code == 202
        assert resp.json()["warnings"] == []

    def test_no_label_has_no_warnings(self, auth_client, tmp_path):
        """The hash-prefix fallback is never flagged, and the original filename is not inspected."""
        c, _ = auth_client
        resp = _upload(c, tmp_path, None)
        assert resp.status_code == 202
        assert resp.json()["warnings"] == []


@pytest.mark.django_db
class TestPatch:
    def _recording(self, user):
        return baker.make(Recording, author=user, stored_name=f"{HASH}.edf", file_size=1, status=Recording.Status.READY)

    def test_rename_warns_and_renames(self, auth_client):
        c, user = auth_client
        rec = self._recording(user)
        resp = c.patch(
            f"/recordings/api/v1/{HASH}", json.dumps({"display_name": "MRN 123456789"}), content_type="application/json"
        )
        assert resp.status_code == 200
        assert _kinds(resp.json()) == [("display_name", "digit_run")]
        rec.refresh_from_db()
        assert rec.display_name == "MRN 123456789"

    def test_clearing_the_label_has_no_warnings(self, auth_client):
        c, user = auth_client
        self._recording(user)
        resp = c.patch(f"/recordings/api/v1/{HASH}", json.dumps({"display_name": ""}), content_type="application/json")
        assert resp.status_code == 200
        assert resp.json()["warnings"] == []

    def test_modality_only_patch_does_not_reinspect_the_label(self, auth_client):
        c, user = auth_client
        rec = self._recording(user)
        rec.display_name = "Jane Doe"
        rec.save(update_fields=["display_name"])
        resp = c.patch(f"/recordings/api/v1/{HASH}", json.dumps({"modality": "eeg"}), content_type="application/json")
        assert resp.status_code == 200
        assert resp.json()["warnings"] == []


@pytest.mark.django_db
class TestPushBody:
    def test_push_body_names_the_recording_by_its_handle_never_its_label(self, user, tmp_path):
        content = _make_edfplus_file()
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        staged = staging / f"{HASH}.edf"
        staged.write_bytes(content)
        recording = Recording.objects.create(
            author=user,
            original_name="doe-jane-export.edf",
            display_name="Doe, Jane",
            stored_name=f"{HASH}.edf",
            file_extension=".edf",
            file_size=len(content),
            file_path=str(staged),
            file_hash=hashlib.sha256(content).hexdigest(),
            content_hash="",
            status=Recording.Status.PENDING,
        )
        with (
            override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
            patch("notifications.tasks.send_push_to_user.delay") as push,
        ):
            process_recording(recording.pk)
        recording.refresh_from_db()
        assert recording.status == Recording.Status.READY
        push.assert_called_once()
        body = push.call_args.kwargs["body"]
        assert HASH[:8] in body
        assert "Doe" not in body
        assert "doe-jane" not in body
