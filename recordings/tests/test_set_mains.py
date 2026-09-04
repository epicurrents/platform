"""Tests for the batch mains-frequency setter (recordings API ``/set-mains``)."""

import json

import pytest

from recordings.models import Recording

URL = "/recordings/api/v1/set-mains"


def _make_recording(user, stored_name):
    return Recording.objects.create(
        author=user,
        original_name="t.edf",
        stored_name=stored_name,
        file_extension=".edf",
        file_size=1024,
        file_path="/tmp/t.edf",
        file_hash="a" * 64,
        content_hash="b" * 64,
        status=Recording.Status.READY,
    )


@pytest.mark.django_db
class TestBulkSetMains:
    def test_sets_then_clears_override(self, client, django_user_model):
        user = django_user_model.objects.create_user(username="owner", password="x")
        h = "ABCDEF1234567890ABCDEF1234567890"
        rec = _make_recording(user, f"{h}.edf")
        client.force_login(user)

        resp = client.post(
            URL,
            data=json.dumps({"hashes": [h], "power_line_frequency": 50.0}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json() == {"updated": 1, "skipped": 0}
        rec.refresh_from_db()
        assert rec.power_line_frequency == 50.0

        # null clears the override (inherit deployment default)
        resp = client.post(
            URL,
            data=json.dumps({"hashes": [h], "power_line_frequency": None}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1
        rec.refresh_from_db()
        assert rec.power_line_frequency is None

    def test_skips_unwritable_and_invalid_hash(self, client, django_user_model):
        owner = django_user_model.objects.create_user(username="owner2", password="x")
        stranger = django_user_model.objects.create_user(username="stranger", password="x")
        h = "99998888777766665555444433332222"
        rec = _make_recording(owner, f"{h}.edf")
        client.force_login(stranger)  # cannot write owner's recording

        resp = client.post(
            URL,
            data=json.dumps({"hashes": [h, "bad"], "power_line_frequency": 60.0}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json() == {"updated": 0, "skipped": 2}
        rec.refresh_from_db()
        assert rec.power_line_frequency is None

    def test_rejects_out_of_range_frequency(self, client, django_user_model):
        user = django_user_model.objects.create_user(username="owner3", password="x")
        client.force_login(user)
        resp = client.post(
            URL,
            data=json.dumps({"hashes": [], "power_line_frequency": -5}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_requires_authentication(self, client):
        resp = client.post(
            URL,
            data=json.dumps({"hashes": [], "power_line_frequency": 50.0}),
            content_type="application/json",
        )
        assert resp.status_code == 401
