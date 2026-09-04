"""Tests for the annotations API — per-type CRUD with permission enforcement."""

import pytest
from django.contrib.contenttypes.models import ContentType

from annotations.models import Annotation, Code, Event, Interruption, Label
from conftest import patch_json, post_json
from epicurrents.models import AccessRight

HEALTH_URL = "/annotations/api/v1/health"
CONTENT_TYPES_URL = "/annotations/api/v1/content-types"
ANNOTATIONS_URL = "/annotations/api/v1/annotations/"
EVENTS_URL = "/annotations/api/v1/events/"
INTERRUPTIONS_URL = "/annotations/api/v1/interruptions/"
LABELS_URL = "/annotations/api/v1/labels/"
CODES_URL = "/annotations/api/v1/codes/"


def _recording_ct(recording):
    return ContentType.objects.get_for_model(recording, for_concrete_model=False)


def _grant_read(user, recording, giver):
    ct = _recording_ct(recording)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        access_target=user,
        can_read=True,
    )


def _grant_write(user, recording, giver):
    ct = _recording_ct(recording)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        access_target=user,
        can_read=True,
        can_write=True,
    )


def _base_payload(recording, **kwargs):
    ct = _recording_ct(recording)
    defaults = {
        "target_content_type_id": ct.pk,
        "target_object_id": str(recording.pk),
        "object_hash": "A" * 32,
    }
    defaults.update(kwargs)
    return defaults


def _list_url(base_url, recording):
    ct = _recording_ct(recording)
    return f"{base_url}?target_content_type_id={ct.pk}&target_object_id={recording.pk}"


# ── Health ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestHealthEndpoint:
    def test_returns_ok(self, client):
        resp = client.get(HEALTH_URL)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ── Content types ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestContentTypesEndpoint:
    def test_unauthenticated_returns_401(self, client):
        assert client.get(CONTENT_TYPES_URL).status_code == 401

    def test_authenticated_returns_list(self, auth_client):
        c, _ = auth_client
        resp = c.get(CONTENT_TYPES_URL)
        assert resp.status_code == 200
        assert len(resp.json()) > 0

    def test_filter_by_app_label(self, auth_client):
        c, _ = auth_client
        resp = c.get(f"{CONTENT_TYPES_URL}?app_label=recordings")
        assert resp.status_code == 200
        assert all(item["app_label"] == "recordings" for item in resp.json())


# ── Annotation (bundle) ───────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAnnotationCRUD:
    def _make_annotation(self, user, recording):
        return Annotation.objects.create(
            author=user,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="A" * 32,
            content={"note": "test"},
        )

    def test_unauthenticated_create_returns_401(self, client, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        payload = _base_payload(recording, content={"x": 1})
        assert post_json(client, ANNOTATIONS_URL, payload).status_code == 401

    def test_hash_collision_resolves_to_callers_own_row(self, client, make_user):
        """A shared object_hash on another user's target must not capture
        the caller's PATCH resolution.

        object_hash is unique only per (target, model), so two users can
        legitimately hold the same hash on different targets. The lookup
        must prefer the caller's own row — otherwise whoever owns the
        lower-pk row locks the other author out of editing by hash.
        """
        from model_bakery import baker

        attacker = make_user(username="hash_attacker")
        victim = make_user(username="hash_victim")
        # Attacker claims the hash first (lower pk) on their own recording.
        att_rec = baker.make("recordings.Recording", author=attacker)
        self._make_annotation(attacker, att_rec)
        vic_rec = baker.make("recordings.Recording", author=victim)
        vic_ann = self._make_annotation(victim, vic_rec)

        client.force_login(victim)
        resp = patch_json(
            client,
            f"{ANNOTATIONS_URL}{'A' * 32}",
            {"content": {"note": "updated"}},
        )
        assert resp.status_code == 200
        vic_ann.refresh_from_db()
        assert vic_ann.content == {"note": "updated"}
        # The attacker's row is untouched.
        att_ann = Annotation.objects.get(author=attacker)
        assert att_ann.content == {"note": "test"}

    def test_author_can_create(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        resp = post_json(c, ANNOTATIONS_URL, _base_payload(recording, content={"note": "hi"}))
        assert resp.status_code == 200
        # dict content is spread into the top-level response
        assert resp.json()["note"] == "hi"
        assert "content" not in resp.json()

    def test_non_dict_content_returned_as_value(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        resp = post_json(c, ANNOTATIONS_URL, _base_payload(recording, content=42))
        assert resp.status_code == 200
        assert resp.json()["value"] == 42

    def test_read_access_is_sufficient_to_create(self, client, user, make_user):
        from model_bakery import baker

        reader = make_user(username="reader")
        recording = baker.make("recordings.Recording", author=user)
        _grant_read(reader, recording, giver=user)
        client.force_login(reader)
        resp = post_json(client, ANNOTATIONS_URL, _base_payload(recording, content={"x": 1}))
        assert resp.status_code == 200

    def test_no_access_returns_403_on_create(self, client, user, make_user):
        from model_bakery import baker

        stranger = make_user(username="stranger")
        recording = baker.make("recordings.Recording", author=user)
        client.force_login(stranger)
        resp = post_json(client, ANNOTATIONS_URL, _base_payload(recording, content={"x": 1}))
        assert resp.status_code == 403

    def test_write_access_can_also_create(self, client, user, make_user):
        from model_bakery import baker

        writer = make_user(username="writer")
        recording = baker.make("recordings.Recording", author=user)
        _grant_write(writer, recording, giver=user)
        client.force_login(writer)
        resp = post_json(client, ANNOTATIONS_URL, _base_payload(recording, content="note"))
        assert resp.status_code == 200

    def test_list_requires_read_access(self, client, user, make_user):
        from model_bakery import baker

        reader = make_user(username="reader")
        recording = baker.make("recordings.Recording", author=user)
        client.force_login(reader)
        resp = client.get(_list_url(ANNOTATIONS_URL, recording))
        assert resp.status_code == 403

    def test_author_can_list_with_access_right(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        _grant_read(user, recording, giver=user)
        self._make_annotation(user, recording)
        resp = c.get(_list_url(ANNOTATIONS_URL, recording))
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_owner_can_update(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        ann = self._make_annotation(user, recording)
        resp = patch_json(c, f"{ANNOTATIONS_URL}{ann.object_hash}", {"content": {"updated": True}})
        assert resp.status_code == 200
        assert resp.json()["updated"] is True

    def test_other_user_cannot_update(self, client, user, make_user):
        from model_bakery import baker

        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=user)
        ann = self._make_annotation(user, recording)
        client.force_login(other)
        resp = patch_json(client, f"{ANNOTATIONS_URL}{ann.object_hash}", {"content": "hack"})
        assert resp.status_code == 403

    def test_update_nonexistent_returns_404(self, auth_client):
        c, _ = auth_client
        assert patch_json(c, f"{ANNOTATIONS_URL}{'Z' * 32}", {"content": "x"}).status_code == 404

    def test_owner_can_delete(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        ann = self._make_annotation(user, recording)
        resp = c.delete(f"{ANNOTATIONS_URL}{ann.object_hash}")
        assert resp.status_code == 200
        assert not Annotation.objects.filter(pk=ann.pk).exists()

    def test_other_user_cannot_delete(self, client, user, make_user):
        from model_bakery import baker

        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=user)
        ann = self._make_annotation(user, recording)
        client.force_login(other)
        assert client.delete(f"{ANNOTATIONS_URL}{ann.object_hash}").status_code == 403

    def test_delete_nonexistent_returns_404(self, auth_client):
        c, _ = auth_client
        assert c.delete(f"{ANNOTATIONS_URL}{'Z' * 32}").status_code == 404

    def test_mine_returns_only_caller_annotations(self, client, user, make_user):
        from model_bakery import baker

        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=user)
        mine = Annotation.objects.create(
            author=user,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="M" * 32,
            content={"note": "mine"},
        )
        theirs = Annotation.objects.create(
            author=other,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="T" * 32,
            content={"note": "theirs"},
        )
        client.force_login(user)
        resp = client.get(f"{ANNOTATIONS_URL}mine")
        assert resp.status_code == 200
        hashes = [a["object_hash"] for a in resp.json()]
        assert mine.object_hash in hashes
        assert theirs.object_hash not in hashes


# ── Event ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestEventCRUD:
    def _make_event(self, user, recording, timestamp=1.0, name="test-event"):
        return Event.objects.create(
            author=user,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="A" * 32,
            name=name,
            timestamp=timestamp,
        )

    def test_unauthenticated_create_returns_401(self, client, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        payload = _base_payload(recording, name="spike", timestamp=1.0)
        assert post_json(client, EVENTS_URL, payload).status_code == 401

    def test_author_can_create(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        payload = _base_payload(recording, name="spike", timestamp=1.5, duration=0.5)
        resp = post_json(c, EVENTS_URL, payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["timestamp"] == 1.5
        assert data["name"] == "spike"
        assert "codes" in data
        assert data["codes"] == []

    def test_response_includes_codes(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        ct = ContentType.objects.get_for_model(Event)
        Code.objects.create(content_type=ct, object_id=str(event.pk), standard="ICD10", value="G40.9")
        _grant_read(user, recording, giver=user)
        resp = c.get(_list_url(EVENTS_URL, recording))
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) == 1
        assert len(events[0]["codes"]) == 1
        assert events[0]["codes"][0]["standard"] == "ICD10"

    def test_owner_can_update(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        resp = patch_json(c, f"{EVENTS_URL}{event.object_hash}", {"name": "updated-name"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated-name"

    def test_other_user_cannot_update(self, client, user, make_user):
        from model_bakery import baker

        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        client.force_login(other)
        assert patch_json(client, f"{EVENTS_URL}{event.object_hash}", {"name": "hack"}).status_code == 403

    def test_owner_can_delete(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        resp = c.delete(f"{EVENTS_URL}{event.object_hash}")
        assert resp.status_code == 200
        assert not Event.objects.filter(pk=event.pk).exists()


# ── Interruption ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestInterruptionCRUD:
    def _make_interruption(self, user, recording, timestamp=2.0, duration=1.0):
        return Interruption.objects.create(
            author=user,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="A" * 32,
            timestamp=timestamp,
            duration=duration,
        )

    def test_author_can_create(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        payload = _base_payload(recording, timestamp=3.0, duration=1.0)
        resp = post_json(c, INTERRUPTIONS_URL, payload)
        assert resp.status_code == 200
        assert resp.json()["timestamp"] == 3.0
        assert "codes" in resp.json()

    def test_owner_can_update(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        interruption = self._make_interruption(user, recording)
        resp = patch_json(c, f"{INTERRUPTIONS_URL}{interruption.object_hash}", {"duration": 2.5})
        assert resp.status_code == 200
        assert resp.json()["duration"] == 2.5

    def test_owner_can_delete(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        interruption = self._make_interruption(user, recording)
        resp = c.delete(f"{INTERRUPTIONS_URL}{interruption.object_hash}")
        assert resp.status_code == 200
        assert not Interruption.objects.filter(pk=interruption.pk).exists()

    def test_other_user_cannot_delete(self, client, user, make_user):
        from model_bakery import baker

        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=user)
        interruption = self._make_interruption(user, recording)
        client.force_login(other)
        assert client.delete(f"{INTERRUPTIONS_URL}{interruption.object_hash}").status_code == 403


# ── Label ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestLabelCRUD:
    def _make_label(self, user, recording, name="default"):
        return Label.objects.create(
            author=user,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="A" * 32,
            name=name,
        )

    def test_author_can_create(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        payload = _base_payload(recording, name="seizure", value={"grade": 2})
        resp = post_json(c, LABELS_URL, payload)
        assert resp.status_code == 200
        assert resp.json()["name"] == "seizure"
        assert "codes" in resp.json()

    def test_owner_can_update(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        label = self._make_label(user, recording)
        resp = patch_json(c, f"{LABELS_URL}{label.object_hash}", {"name": "new-name"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "new-name"

    def test_owner_can_delete(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        label = self._make_label(user, recording)
        resp = c.delete(f"{LABELS_URL}{label.object_hash}")
        assert resp.status_code == 200
        assert not Label.objects.filter(pk=label.pk).exists()

    def test_mine_returns_labels(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        label = self._make_label(user, recording, name="mine")
        resp = c.get(f"{LABELS_URL}mine")
        assert resp.status_code == 200
        hashes = [lbl["object_hash"] for lbl in resp.json()]
        assert label.object_hash in hashes


# ── Code ──────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestCodeCRUD:
    def _make_event(self, user, recording):
        return Event.objects.create(
            author=user,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="A" * 32,
            name="test-event",
            timestamp=1.0,
        )

    def _code_payload(self, event, **kwargs):
        ct = ContentType.objects.get_for_model(Event)
        defaults = {
            "content_type_id": ct.pk,
            "object_id": str(event.pk),
            "standard": "ICD10",
            "value": "G40.9",
        }
        defaults.update(kwargs)
        return defaults

    def test_unauthenticated_create_returns_401(self, client, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        assert post_json(client, CODES_URL, self._code_payload(event)).status_code == 401

    def test_event_owner_can_add_code(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        resp = post_json(c, CODES_URL, self._code_payload(event))
        assert resp.status_code == 200
        data = resp.json()
        assert data["standard"] == "ICD10"
        assert data["value"] == "G40.9"
        assert Code.objects.filter(pk=data["id"]).exists()

    def test_other_user_cannot_add_code(self, client, user, make_user):
        from model_bakery import baker

        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        client.force_login(other)
        resp = post_json(client, CODES_URL, self._code_payload(event))
        assert resp.status_code == 403

    def test_invalid_parent_type_returns_400(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        # Try attaching a code to an Annotation (bundle), which is not allowed.
        ann = Annotation.objects.create(
            author=user,
            target_content_type=_recording_ct(recording),
            target_object_id=str(recording.pk),
            object_hash="A" * 32,
            content={"x": 1},
        )
        ann_ct = ContentType.objects.get_for_model(Annotation)
        payload = {
            "content_type_id": ann_ct.pk,
            "object_id": str(ann.pk),
            "standard": "ICD10",
            "value": "G40.9",
        }
        resp = post_json(c, CODES_URL, payload)
        assert resp.status_code == 400

    def test_owner_can_update_code(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        ct = ContentType.objects.get_for_model(Event)
        code = Code.objects.create(content_type=ct, object_id=str(event.pk), standard="ICD10", value="G40.9")
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"value": "G40.1"})
        assert resp.status_code == 200
        assert resp.json()["value"] == "G40.1"

    def test_other_user_cannot_update_code(self, client, user, make_user):
        from model_bakery import baker

        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        ct = ContentType.objects.get_for_model(Event)
        code = Code.objects.create(content_type=ct, object_id=str(event.pk), standard="ICD10", value="G40.9")
        client.force_login(other)
        resp = patch_json(client, f"{CODES_URL}{code.pk}", {"value": "hack"})
        assert resp.status_code == 403

    def test_owner_can_delete_code(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        event = self._make_event(user, recording)
        ct = ContentType.objects.get_for_model(Event)
        code = Code.objects.create(content_type=ct, object_id=str(event.pk), standard="ICD10", value="G40.9")
        resp = c.delete(f"{CODES_URL}{code.pk}")
        assert resp.status_code == 200
        assert not Code.objects.filter(pk=code.pk).exists()

    def test_delete_nonexistent_code_returns_404(self, auth_client):
        c, _ = auth_client
        assert c.delete(f"{CODES_URL}999999").status_code == 404


@pytest.mark.django_db
class TestAnnotationsAuditTrail:
    """Activity-row annotation contract for the annotations API.

    One representative test per endpoint (23 cases) — five each for
    Annotation, Event, Interruption, Label, plus three for Code. The
    list endpoints set ``target`` to the parent object being queried
    (not the annotation rows themselves), matching the pattern of
    ``recordings.annotations.list``; ``.mine`` variants carry no target
    since they span all parents the caller has authored against.
    """

    def _setup(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        _grant_write(user, recording, giver=user)
        return c, user, recording

    def _ct(self, model):
        return ContentType.objects.get_for_model(model, for_concrete_model=False)

    def _payload(self, recording, object_hash, **extra):
        ct = _recording_ct(recording)
        return {
            "target_content_type_id": ct.pk,
            "target_object_id": str(recording.pk),
            "object_hash": object_hash,
            **extra,
        }

    # ── Annotation bundle ──────────────────────────────────────────────────

    def test_annotation_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        resp = c.get(f"{ANNOTATIONS_URL}?target_content_type_id={ct.pk}&target_object_id={recording.pk}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.annotation.list").latest("created_at")
        assert activity.target_content_type_id == ct.pk
        assert activity.target_object_id == str(recording.pk)
        assert "returned_count" in activity.metadata

    def test_annotation_mine_records_verb(self, auth_client):
        from activity.models import Activity

        c, _ = auth_client
        resp = c.get(f"{ANNOTATIONS_URL}mine")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.annotation.mine").latest("created_at")
        assert "returned_count" in activity.metadata

    def test_annotation_create_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        payload = self._payload(recording, "A" * 32, content={"x": 1})
        resp = post_json(c, ANNOTATIONS_URL, payload)
        assert resp.status_code == 200
        ann = Annotation.objects.get(object_hash="A" * 32)
        activity = Activity.objects.filter(verb="annotations.annotation.create").latest("created_at")
        assert activity.target_content_type_id == self._ct(Annotation).pk
        assert activity.target_object_id == str(ann.pk)

    def test_annotation_update_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        ann = Annotation.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="b" * 32,
            content={"v": 1},
        )
        resp = patch_json(c, f"{ANNOTATIONS_URL}{ann.object_hash}", {"content": {"v": 2}})
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.annotation.update").latest("created_at")
        assert activity.target_object_id == str(ann.pk)
        assert activity.metadata["fields_updated"] == ["content"]

    def test_annotation_delete_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        ann = Annotation.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="c" * 32,
            content={},
        )
        ann_pk = ann.pk
        resp = c.delete(f"{ANNOTATIONS_URL}{ann.object_hash}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.annotation.delete").latest("created_at")
        assert activity.target_object_id == str(ann_pk)

    # ── Event ──────────────────────────────────────────────────────────────

    def test_event_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        resp = c.get(f"{EVENTS_URL}?target_content_type_id={ct.pk}&target_object_id={recording.pk}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.event.list").latest("created_at")
        assert activity.target_object_id == str(recording.pk)

    def test_event_mine_records_verb(self, auth_client):
        from activity.models import Activity

        c, _ = auth_client
        resp = c.get(f"{EVENTS_URL}mine")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.event.mine").latest("created_at")
        assert "returned_count" in activity.metadata

    def test_event_create_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        payload = self._payload(recording, "E" * 32, name="spike", timestamp=1.0, duration=0.1)
        resp = post_json(c, EVENTS_URL, payload)
        assert resp.status_code == 200
        event = Event.objects.get(object_hash="E" * 32)
        activity = Activity.objects.filter(verb="annotations.event.create").latest("created_at")
        assert activity.target_content_type_id == self._ct(Event).pk
        assert activity.target_object_id == str(event.pk)

    def test_event_update_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        event = Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="f" * 32,
            name="orig",
            timestamp=0.5,
        )
        resp = patch_json(c, f"{EVENTS_URL}{event.object_hash}", {"name": "new"})
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.event.update").latest("created_at")
        assert activity.target_object_id == str(event.pk)
        assert activity.metadata["fields_updated"] == ["name"]

    def test_event_delete_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        event = Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="0" * 32,
            name="x",
            timestamp=0.0,
        )
        event_pk = event.pk
        resp = c.delete(f"{EVENTS_URL}{event.object_hash}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.event.delete").latest("created_at")
        assert activity.target_object_id == str(event_pk)

    # ── Interruption ───────────────────────────────────────────────────────

    def test_interruption_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        resp = c.get(f"{INTERRUPTIONS_URL}?target_content_type_id={ct.pk}&target_object_id={recording.pk}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.interruption.list").latest("created_at")
        assert activity.target_object_id == str(recording.pk)

    def test_interruption_mine_records_verb(self, auth_client):
        from activity.models import Activity

        c, _ = auth_client
        resp = c.get(f"{INTERRUPTIONS_URL}mine")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.interruption.mine").latest("created_at")
        assert "returned_count" in activity.metadata

    def test_interruption_create_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        payload = self._payload(recording, "1" * 32, timestamp=2.0, duration=1.0)
        resp = post_json(c, INTERRUPTIONS_URL, payload)
        assert resp.status_code == 200
        intr = Interruption.objects.get(object_hash="1" * 32)
        activity = Activity.objects.filter(verb="annotations.interruption.create").latest("created_at")
        assert activity.target_content_type_id == self._ct(Interruption).pk
        assert activity.target_object_id == str(intr.pk)

    def test_interruption_update_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        intr = Interruption.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="2" * 32,
            timestamp=1.0,
            duration=0.5,
        )
        resp = patch_json(c, f"{INTERRUPTIONS_URL}{intr.object_hash}", {"duration": 0.8})
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.interruption.update").latest("created_at")
        assert activity.target_object_id == str(intr.pk)
        assert activity.metadata["fields_updated"] == ["duration"]

    def test_interruption_delete_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        intr = Interruption.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="3" * 32,
            timestamp=0.0,
            duration=0.1,
        )
        intr_pk = intr.pk
        resp = c.delete(f"{INTERRUPTIONS_URL}{intr.object_hash}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.interruption.delete").latest("created_at")
        assert activity.target_object_id == str(intr_pk)

    # ── Label ──────────────────────────────────────────────────────────────

    def test_label_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        resp = c.get(f"{LABELS_URL}?target_content_type_id={ct.pk}&target_object_id={recording.pk}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.label.list").latest("created_at")
        assert activity.target_object_id == str(recording.pk)

    def test_label_mine_records_verb(self, auth_client):
        from activity.models import Activity

        c, _ = auth_client
        resp = c.get(f"{LABELS_URL}mine")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.label.mine").latest("created_at")
        assert "returned_count" in activity.metadata

    def test_label_create_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        payload = self._payload(recording, "4" * 32, name="seizure", value="focal")
        resp = post_json(c, LABELS_URL, payload)
        assert resp.status_code == 200
        label = Label.objects.get(object_hash="4" * 32)
        activity = Activity.objects.filter(verb="annotations.label.create").latest("created_at")
        assert activity.target_content_type_id == self._ct(Label).pk
        assert activity.target_object_id == str(label.pk)

    def test_label_update_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        label = Label.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="5" * 32,
            name="orig",
        )
        resp = patch_json(c, f"{LABELS_URL}{label.object_hash}", {"name": "renamed"})
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.label.update").latest("created_at")
        assert activity.target_object_id == str(label.pk)
        assert activity.metadata["fields_updated"] == ["name"]

    def test_label_delete_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        label = Label.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="6" * 32,
            name="x",
        )
        label_pk = label.pk
        resp = c.delete(f"{LABELS_URL}{label.object_hash}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.label.delete").latest("created_at")
        assert activity.target_object_id == str(label_pk)

    # ── Code ───────────────────────────────────────────────────────────────

    def test_code_create_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        event = Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="7" * 32,
            name="seizure",
            timestamp=0.0,
        )
        payload = {
            "content_type_id": self._ct(Event).pk,
            "object_id": str(event.pk),
            "standard": "ICD10",
            "value": "G40.9",
        }
        resp = post_json(c, CODES_URL, payload)
        assert resp.status_code == 200
        code = Code.objects.get(value="G40.9")
        activity = Activity.objects.filter(verb="annotations.code.create").latest("created_at")
        assert activity.target_content_type_id == self._ct(Code).pk
        assert activity.target_object_id == str(code.pk)

    def test_code_update_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        event = Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="8" * 32,
            name="x",
            timestamp=0.0,
        )
        event_ct = self._ct(Event)
        code = Code.objects.create(
            content_type=event_ct,
            object_id=str(event.pk),
            standard="ICD10",
            value="G40.0",
        )
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"value": "G40.1"})
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.code.update").latest("created_at")
        assert activity.target_object_id == str(code.pk)
        assert activity.metadata["fields_updated"] == ["value"]

    def test_code_delete_records_verb(self, auth_client):
        from activity.models import Activity

        c, user, recording = self._setup(auth_client)
        ct = _recording_ct(recording)
        event = Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="9" * 32,
            name="x",
            timestamp=0.0,
        )
        event_ct = self._ct(Event)
        code = Code.objects.create(
            content_type=event_ct,
            object_id=str(event.pk),
            standard="ICD10",
            value="G40.2",
        )
        code_pk = code.pk
        resp = c.delete(f"{CODES_URL}{code_pk}")
        assert resp.status_code == 200
        activity = Activity.objects.filter(verb="annotations.code.delete").latest("created_at")
        assert activity.target_object_id == str(code_pk)
