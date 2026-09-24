"""``Recording.public_source``: the published dataset a recording was taken from, and where it is reported.

The field takes a DOI or an http(s) URL, is set on upload, by PATCH or by ``import_recordings``,
is served to every reader, and is repeated per recording by ``deidentification_report`` and per
grant by ``grant_assessments``.
"""

from __future__ import annotations

import io
import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from model_bakery import baker

from activity.models import Activity
from conftest import patch_json
from epicurrents.models import AccessRight
from library.models import Dataset, DatasetItem
from recordings.models import Recording
from recordings.public_source import PUBLIC_SOURCE_MAX_LENGTH, normalise_public_source

pytestmark = pytest.mark.django_db

DOI = "10.13026/c2f305"
URL = "https://physionet.org/content/chbmit/1.0.0/"


def _recording(author, stored_name="A1B2C3D4E5F60718293A4B5C6D7E8F90", **kwargs):
    defaults = {
        "author": author,
        "stored_name": f"{stored_name}.edf",
        "file_extension": ".edf",
        "file_size": 1,
        "status": Recording.Status.READY,
    }
    defaults.update(kwargs)
    recording = baker.make(Recording, **defaults)
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=author,
        access_target=author,
        can_read=True,
        can_write=True,
        can_share=True,
    )
    return recording


def _grant(obj, giver, target):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct, object_id=str(obj.pk), access_giver=giver, access_target=target, can_read=True
    )


def _hash(recording):
    return recording.stored_name.split(".", 1)[0]


# ---------------------------------------------------------------------------
# The value
# ---------------------------------------------------------------------------


class TestNormalisation:
    @pytest.mark.parametrize(
        "given",
        [DOI, DOI.upper(), f"doi:{DOI}", f"https://doi.org/{DOI}", f"http://dx.doi.org/{DOI.upper()}", f"  {DOI}  "],
    )
    def test_every_doi_spelling_is_stored_bare_and_lower_cased(self, given):
        assert normalise_public_source(given) == DOI

    def test_a_url_is_stored_as_given(self):
        assert normalise_public_source(f" {URL} ") == URL

    @pytest.mark.parametrize("given", [None, "", "   "])
    def test_an_empty_value_clears(self, given):
        assert normalise_public_source(given) == ""

    @pytest.mark.parametrize(
        "given",
        [
            "CHB-MIT Scalp EEG Database, PhysioNet 2010",
            "Hospital X cohort 2019",
            "doi:not-a-doi",
            "ftp://example.org/data",
            "https://",
            "10.1/short",
        ],
    )
    def test_a_citation_or_anything_else_is_refused(self, given):
        with pytest.raises(ValueError, match="DOI"):
            normalise_public_source(given)

    def test_an_overlong_value_is_refused(self):
        with pytest.raises(ValueError, match="at most"):
            normalise_public_source("https://example.org/" + "x" * PUBLIC_SOURCE_MAX_LENGTH)


# ---------------------------------------------------------------------------
# Write surfaces
# ---------------------------------------------------------------------------


class TestPatch:
    def test_the_author_sets_and_clears_it(self, auth_client):
        client, user = auth_client
        recording = _recording(user)
        url = f"/recordings/api/v1/{_hash(recording)}"

        response = patch_json(client, url, {"public_source": f"doi:{DOI.upper()}"})
        assert response.status_code == 200, response.content
        assert response.json()["public_source"] == DOI
        recording.refresh_from_db()
        assert recording.public_source == DOI

        response = patch_json(client, url, {"public_source": ""})
        assert response.status_code == 200
        recording.refresh_from_db()
        assert recording.public_source == ""

    def test_a_patch_without_the_key_leaves_it_alone(self, auth_client):
        client, user = auth_client
        recording = _recording(user, public_source=DOI)
        response = patch_json(client, f"/recordings/api/v1/{_hash(recording)}", {"modality": "emg"})
        assert response.status_code == 200
        recording.refresh_from_db()
        assert recording.public_source == DOI

    def test_a_citation_is_400_and_the_row_is_untouched(self, auth_client):
        client, user = auth_client
        recording = _recording(user, public_source=DOI)
        response = patch_json(
            client, f"/recordings/api/v1/{_hash(recording)}", {"public_source": "CHB-MIT, PhysioNet 2010"}
        )
        assert response.status_code == 400
        assert "DOI" in response.json()["detail"]
        recording.refresh_from_db()
        assert recording.public_source == DOI

    def test_the_write_is_audited_by_field_name_only(self, auth_client):
        client, user = auth_client
        recording = _recording(user)
        patch_json(client, f"/recordings/api/v1/{_hash(recording)}", {"public_source": URL})
        activity = Activity.objects.filter(verb="recordings.update").latest("created_at")
        assert activity.metadata["fields_updated"] == ["public_source"]
        assert URL not in json.dumps(activity.metadata)


class TestUpload:
    def _upload(self, client, tmp_path, public_source: str):
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir(exist_ok=True)
        uploads.mkdir(exist_ok=True)
        with (
            override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"fake edf data")
            f.name = "sample.edf"
            response = client.post(
                f"/recordings/api/v1/upload?public_source={public_source}", {"file": f}, format="multipart"
            )
        return response, staging

    def test_the_upload_records_it(self, auth_client, tmp_path):
        client, user = auth_client
        response, _staging = self._upload(client, tmp_path, f"doi:{DOI}")
        assert response.status_code == 202, response.content
        assert Recording.objects.get(stored_name=response.json()["stored_name"]).public_source == DOI

    def test_a_bad_value_is_refused_before_anything_is_written(self, auth_client, tmp_path):
        client, user = auth_client
        response, staging = self._upload(client, tmp_path, "Hospital X cohort 2019")
        assert response.status_code == 400
        assert "DOI" in response.json()["detail"]
        assert not Recording.objects.filter(author=user).exists()
        assert list(staging.iterdir()) == []


class TestImport:
    def _source(self, tmp_path):
        from recordings.testing import make_edf_bytes

        source = tmp_path / "incoming"
        source.mkdir()
        (source / "study-042.edf").write_bytes(make_edf_bytes())
        (tmp_path / "uploads").mkdir(exist_ok=True)
        return source

    def test_the_option_stamps_every_imported_recording(self, user, tmp_path):
        source = self._source(tmp_path)
        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            call_command(
                "import_recordings", str(source), "--username", user.get_username(), "--public-source", f"doi:{DOI}"
            )
        assert list(Recording.objects.filter(author=user).values_list("public_source", flat=True)) == [DOI]

    def test_without_the_option_the_field_is_empty(self, user, tmp_path):
        source = self._source(tmp_path)
        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            call_command("import_recordings", str(source), "--username", user.get_username())
        assert list(Recording.objects.filter(author=user).values_list("public_source", flat=True)) == [""]

    def test_a_citation_is_refused_and_nothing_is_imported(self, user, tmp_path):
        source = self._source(tmp_path)
        with (
            override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")),
            pytest.raises(CommandError, match="DOI"),
        ):
            call_command(
                "import_recordings", str(source), "--username", user.get_username(), "--public-source", "PhysioNet"
            )
        assert not Recording.objects.filter(author=user).exists()


# ---------------------------------------------------------------------------
# Who reads it
# ---------------------------------------------------------------------------


class TestReaders:
    def test_a_grantee_reads_it_on_the_detail_and_the_listing(self, user, make_user):
        recording = _recording(user, public_source=DOI)
        reader = make_user()
        _grant(recording, user, reader)
        client = Client()
        client.force_login(reader)
        assert client.get(f"/recordings/api/v1/{_hash(recording)}").json()["public_source"] == DOI
        [item] = client.get("/recordings/api/v1/").json()
        assert item["public_source"] == DOI

    def test_an_unset_field_serves_an_empty_string(self, auth_client):
        client, user = auth_client
        recording = _recording(user)
        assert client.get(f"/recordings/api/v1/{_hash(recording)}").json()["public_source"] == ""


# ---------------------------------------------------------------------------
# The reports
# ---------------------------------------------------------------------------


def _run(command, *args):
    out = StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


class TestDeidentificationReport:
    def test_the_row_repeats_the_source_and_the_summary_counts_them(self, user):
        sourced = _recording(user, public_source=DOI)
        local = _recording(user, stored_name="B" * 32)
        report = json.loads(_run("deidentification_report", "--format", "json"))
        by_hash = {row["hash"]: row for row in report["recordings"]}
        assert by_hash[_hash(sourced)]["public_source"] == DOI
        assert by_hash[_hash(local)]["public_source"] is None
        assert report["public_source_count"] == 1

        text = _run("deidentification_report")
        assert "PUBLIC SOURCE" in text
        sourced_line = next(line for line in text.splitlines() if line.startswith(_hash(sourced)))
        assert sourced_line.endswith(DOI)
        local_line = next(line for line in text.splitlines() if line.startswith(_hash(local)))
        assert local_line.endswith("-")
        assert "1 from a published source." in text


class TestGrantAssessments:
    def test_a_grant_reports_how_many_covered_recordings_are_public(self, user, make_user):
        reader = make_user()
        sourced = _recording(user, public_source=DOI)
        local = _recording(user, stored_name="B" * 32)
        dataset = Dataset.objects.create(author=user, name="pool")
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        for recording in (sourced, local):
            DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(recording.pk))
        on_sourced = _grant(sourced, user, reader)
        on_dataset = _grant(dataset, user, reader)

        report = json.loads(_run("grant_assessments", "--format", "json"))
        rows = {row["grant_id"]: row for row in report["grants"]}
        assert (rows[on_sourced.pk]["public_source_count"], rows[on_sourced.pk]["covered_count"]) == (1, 1)
        assert (rows[on_dataset.pk]["public_source_count"], rows[on_dataset.pk]["covered_count"]) == (1, 2)
        assert report["public_source_only"] == 1

        text = _run("grant_assessments")
        assert "PUBLIC" in text
        assert "1/1" in text and "1/2" in text
        assert "1 covering only recordings from a published source." in text

    def test_a_grant_covering_nothing_is_not_counted_as_public(self, user, make_user):
        reader = make_user()
        dataset = Dataset.objects.create(author=user, name="empty")
        _grant(dataset, user, reader)
        report = json.loads(_run("grant_assessments", "--format", "json"))
        [row] = report["grants"]
        assert (row["public_source_count"], row["covered_count"]) == (0, 0)
        assert report["public_source_only"] == 0
