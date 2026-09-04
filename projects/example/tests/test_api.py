"""Tests for the example project's note endpoints — the URL-slot half of the extension contract.

Beyond exercising the template's own behaviour, two of these are regression tests for defects the
template used to teach: resolution by ``content_hash`` (which is not the public hash, so every
lookup 404'd) and a ``request.user`` read that bypassed the ``enforce_session_csrf`` chokepoint on
a session-authenticated write.
"""

import inspect
import json

import pytest
from django.test import Client

from epicurrents.models import AccessRight
from projects.example.models import RecordingNote


def _put(client, url, data):
    return client.put(url, json.dumps(data), content_type="application/json")


@pytest.mark.django_db
class TestGetNote:
    def test_unauthenticated_returns_401(self, client, note_url):
        assert client.get(note_url).status_code == 401

    def test_no_note_returns_404(self, auth_client, recording, note_url):
        c, _user = auth_client
        assert c.get(note_url).status_code == 404

    def test_author_reads_note(self, auth_client, recording, note_url):
        c, _user = auth_client
        RecordingNote.objects.create(recording=recording, site_id="S-1", notes="baseline study")
        resp = c.get(note_url)
        assert resp.status_code == 200, resp.content
        data = resp.json()
        assert data["site_id"] == "S-1"
        assert data["notes"] == "baseline study"

    def test_non_reader_gets_404_not_403(self, recording, note_url, make_user):
        # Access failure is indistinguishable from absence, matching the core API.
        RecordingNote.objects.create(recording=recording, notes="private")
        other = make_user()
        c = Client()
        c.force_login(other)
        assert c.get(note_url).status_code == 404

    def test_lowercase_hash_resolves(self, auth_client, recording):
        c, _user = auth_client
        RecordingNote.objects.create(recording=recording, notes="x")
        assert c.get(f"/project/api/v1/notes/{'a' * 32}").status_code == 200

    def test_failed_recording_is_hidden_from_grantees(self, recording, note_url, make_user, user):
        # The FAILED-hidden rule: a grantee must not be able to distinguish a
        # failed upload from a recording that does not exist.
        from django.contrib.contenttypes.models import ContentType

        RecordingNote.objects.create(recording=recording, notes="x")
        recording.status = "failed"
        recording.save(update_fields=["status"])
        grantee = make_user()
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
            object_id=str(recording.pk),
            access_giver=user,
            access_target=grantee,
            can_read=True,
        )
        c = Client()
        c.force_login(grantee)
        resp = c.get(note_url)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Recording not found."

    def test_failed_recording_note_stays_readable_to_the_author(self, auth_client, recording, note_url):
        c, _user = auth_client
        RecordingNote.objects.create(recording=recording, notes="x")
        recording.status = "failed"
        recording.save(update_fields=["status"])
        assert c.get(note_url).status_code == 200

    def test_malformed_hash_returns_404(self, auth_client, recording):
        c, _user = auth_client
        assert c.get("/project/api/v1/notes/tooshort").status_code == 404


@pytest.mark.django_db
class TestUpsertNote:
    def test_unauthenticated_returns_401(self, client, note_url):
        assert _put(client, note_url, {"notes": "x"}).status_code == 401

    def test_author_creates_note(self, auth_client, recording, note_url):
        c, _user = auth_client
        resp = _put(c, note_url, {"site_id": "S-9", "notes": "sleep-deprived EEG"})
        assert resp.status_code == 200, resp.content
        note = RecordingNote.objects.get(recording=recording)
        assert note.site_id == "S-9"
        assert note.notes == "sleep-deprived EEG"

    def test_second_put_updates_in_place(self, auth_client, recording, note_url):
        c, _user = auth_client
        assert _put(c, note_url, {"notes": "first"}).status_code == 200
        assert _put(c, note_url, {"notes": "second"}).status_code == 200
        assert RecordingNote.objects.count() == 1
        assert RecordingNote.objects.get().notes == "second"

    def test_non_writer_gets_403(self, recording, note_url, make_user, user):
        from django.contrib.contenttypes.models import ContentType

        other = make_user()
        # A read grant is not enough to write the note.
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
            object_id=str(recording.pk),
            access_giver=user,
            access_target=other,
            can_read=True,
        )
        c = Client()
        c.force_login(other)
        assert _put(c, note_url, {"notes": "x"}).status_code == 403

    def test_note_length_limit_is_enforced(self, auth_client, recording, note_url, settings):
        settings.EXAMPLE_NOTE_MAX_LENGTH = 10
        c, _user = auth_client
        assert _put(c, note_url, {"notes": "x" * 11}).status_code == 400
        assert not RecordingNote.objects.exists()

    def test_public_hash_is_the_stored_name_prefix_not_content_hash(self, auth_client, recording):
        # Regression: the template used to resolve by content_hash, which no
        # response ever serves as the URL hash — so its own flow always 404'd.
        c, _user = auth_client
        recording.content_hash = "B" * 64
        recording.save(update_fields=["content_hash"])
        assert _put(c, f"/project/api/v1/notes/{'B' * 32}", {"notes": "x"}).status_code == 404
        assert _put(c, f"/project/api/v1/notes/{'A' * 32}", {"notes": "x"}).status_code == 200


class TestCsrfChokepointDiscipline:
    def test_require_auth_calls_the_session_csrf_chokepoint(self):
        # Regression: the template's _require_auth used to skip
        # enforce_session_csrf, teaching a session write outside the chokepoint.
        from projects.example.urls import _require_auth

        assert "enforce_session_csrf(request)" in inspect.getsource(_require_auth)

    def test_no_endpoint_reads_request_user_directly(self):
        # The chokepoint only covers callers resolved through _require_auth*.
        import projects.example.urls as example_urls

        source = inspect.getsource(example_urls)
        endpoint_source = source.split("# Endpoints")[1]
        assert "request.user" not in endpoint_source
