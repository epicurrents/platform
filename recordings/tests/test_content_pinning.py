"""Content pinning: ``expect_stored_hash`` on the byte-serving endpoints answers 412 when the content moved."""

from __future__ import annotations

import pytest

from recordings.tests.test_metadata import _make_recording

pytestmark = pytest.mark.django_db

HASH = "C0FFEE00000000000000000000000000"


@pytest.fixture
def pinned(user, tmp_path):
    return _make_recording(user, tmp_path, n_channels=2, stored_name=f"{HASH}.edf")[0]


class TestContentPinning:
    def test_download_serves_when_the_pin_matches(self, auth_client, pinned):
        client, _ = auth_client
        resp = client.get(f"/recordings/api/v1/{HASH}/file", {"expect_stored_hash": pinned.stored_hash.upper()})
        assert resp.status_code == 200

    def test_download_answers_412_when_the_content_moved(self, auth_client, pinned):
        client, _ = auth_client
        resp = client.get(f"/recordings/api/v1/{HASH}/file", {"expect_stored_hash": "0" * 64})
        assert resp.status_code == 412
        assert "stored_hash" in resp.json()["detail"]

    def test_download_answers_412_when_no_stored_hash_is_known(self, auth_client, pinned):
        client, _ = auth_client
        pinned.stored_hash = ""
        pinned.save(update_fields=["stored_hash"])
        resp = client.get(f"/recordings/api/v1/{HASH}/file", {"expect_stored_hash": "0" * 64})
        assert resp.status_code == 412

    def test_a_malformed_pin_is_a_400(self, auth_client, pinned):
        client, _ = auth_client
        resp = client.get(f"/recordings/api/v1/{HASH}/file", {"expect_stored_hash": "not-a-digest"})
        assert resp.status_code == 400

    def test_slice_honours_the_pin(self, auth_client, pinned):
        client, _ = auth_client
        ok = client.get(
            f"/recordings/api/v1/{HASH}/file/slice", {"t_start": 0, "expect_stored_hash": pinned.stored_hash}
        )
        assert ok.status_code == 200
        moved = client.get(f"/recordings/api/v1/{HASH}/file/slice", {"t_start": 0, "expect_stored_hash": "f" * 64})
        assert moved.status_code == 412

    def test_the_pin_is_checked_after_access(self, client, make_user, pinned):
        client.force_login(make_user())
        resp = client.get(f"/recordings/api/v1/{HASH}/file", {"expect_stored_hash": "0" * 64})
        assert resp.status_code == 403
