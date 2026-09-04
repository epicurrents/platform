"""Tests for annotation model classes — Annotation, Event, Interruption, Label, Code."""

import pytest
from django.contrib.contenttypes.models import ContentType

from annotations.models import Annotation, Code, Event, Interruption, Label


def _recording_ct(recording):
    return ContentType.objects.get_for_model(recording, for_concrete_model=False)


def _base_kwargs(user, recording, **kwargs):
    defaults = {
        "author": user,
        "target_content_type": _recording_ct(recording),
        "target_object_id": str(recording.pk),
        "object_hash": "a" * 32,
    }
    defaults.update(kwargs)
    return defaults


@pytest.mark.django_db
class TestAnnotationModel:
    def test_save_computes_content_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ann = Annotation(content={"key": "val"}, **_base_kwargs(user, recording))
        ann.save()
        assert ann.content_hash != ""
        assert len(ann.content_hash) == 64

    def test_content_hash_changes_on_content_update(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ann = Annotation(content={"v": 1}, **_base_kwargs(user, recording))
        ann.save()
        h1 = ann.content_hash
        ann.content = {"v": 2}
        ann.save()
        assert ann.content_hash != h1

    def test_object_hash_uppercased_on_save(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ann = Annotation(content="x", **_base_kwargs(user, recording, object_hash="a" * 32))
        ann.save()
        assert ann.object_hash == "A" * 32


@pytest.mark.django_db
class TestEventModel:
    def test_save_computes_content_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = Event(name="spike", timestamp=1.5, **_base_kwargs(user, recording))
        event.save()
        assert len(event.content_hash) == 64

    def test_hash_differs_for_different_timestamps(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        e1 = Event(
            name="spike",
            timestamp=1.0,
            **_base_kwargs(user, recording, object_hash="A" * 32),
        )
        e1.save()
        e2 = Event(
            name="spike",
            timestamp=2.0,
            **_base_kwargs(user, recording, object_hash="B" * 32),
        )
        e2.save()
        assert e1.content_hash != e2.content_hash

    def test_codes_included_in_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = Event(name="spike", timestamp=1.0, **_base_kwargs(user, recording))
        event.save()
        hash_without_codes = event.content_hash

        ct = ContentType.objects.get_for_model(Event)
        Code.objects.create(
            content_type=ct,
            object_id=str(event.pk),
            standard="ICD10",
            value="G40.9",
        )
        event.refresh_from_db()
        assert event.content_hash != hash_without_codes

    def test_code_deletion_updates_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = Event(name="spike", timestamp=1.0, **_base_kwargs(user, recording))
        event.save()
        ct = ContentType.objects.get_for_model(Event)
        code = Code.objects.create(
            content_type=ct,
            object_id=str(event.pk),
            standard="ICD10",
            value="G40.9",
        )
        event.refresh_from_db()
        hash_with_code = event.content_hash
        code.delete()
        event.refresh_from_db()
        assert event.content_hash != hash_with_code


@pytest.mark.django_db
class TestInterruptionModel:
    def test_save_computes_content_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        interruption = Interruption(timestamp=5.0, duration=1.0, **_base_kwargs(user, recording))
        interruption.save()
        assert len(interruption.content_hash) == 64

    def test_codes_included_in_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        interruption = Interruption(timestamp=5.0, duration=1.0, **_base_kwargs(user, recording))
        interruption.save()
        hash_before = interruption.content_hash

        ct = ContentType.objects.get_for_model(Interruption)
        Code.objects.create(
            content_type=ct,
            object_id=str(interruption.pk),
            standard="LOINC",
            value="12345-6",
        )
        interruption.refresh_from_db()
        assert interruption.content_hash != hash_before


@pytest.mark.django_db
class TestLabelModel:
    def test_save_computes_content_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        label = Label(name="seizure", **_base_kwargs(user, recording))
        label.save()
        assert len(label.content_hash) == 64

    def test_codes_included_in_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        label = Label(name="seizure", **_base_kwargs(user, recording))
        label.save()
        hash_before = label.content_hash

        ct = ContentType.objects.get_for_model(Label)
        Code.objects.create(
            content_type=ct,
            object_id=str(label.pk),
            standard="SNOMED",
            value="84757009",
        )
        label.refresh_from_db()
        assert label.content_hash != hash_before


@pytest.mark.django_db
class TestCodeModel:
    def test_code_saves_correctly(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = Event(timestamp=1.0, **_base_kwargs(user, recording))
        event.save()
        ct = ContentType.objects.get_for_model(Event)
        code = Code.objects.create(
            content_type=ct,
            object_id=str(event.pk),
            standard="ICD10",
            value="G40.9",
            meta="epilepsy",
        )
        assert code.pk is not None
        assert code.annotation == event

    def test_code_save_updates_parent_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = Event(name="spike", timestamp=2.0, **_base_kwargs(user, recording))
        event.save()
        hash_before = event.content_hash
        ct = ContentType.objects.get_for_model(Event)
        Code.objects.create(
            content_type=ct,
            object_id=str(event.pk),
            standard="ICD10",
            value="G40.9",
        )
        event.refresh_from_db()
        assert event.content_hash != hash_before

    def test_annotation_model_has_no_codes(self, user):
        """Annotation (bundle) does not have a GenericRelation to codes."""
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ann = Annotation(content={"x": 1}, **_base_kwargs(user, recording))
        ann.save()
        assert not hasattr(ann, "codes")


@pytest.mark.django_db
class TestContentHashTamperDetection:
    """``content_hash`` is the per-row integrity construct on every annotation type.

    Counterpart to ``activity.tests.test_audit.TestHashTamperDetection`` — same
    guarantees, applied to a different hash. Confirms (a) the hash round-trips
    through the JSONField storage layer for non-trivial payloads including
    nested dicts and Code rows with ``meta``, and (b) tampering with stored
    state via bulk DML produces a recomputed hash that diverges from the
    stored value.

    Does not test resistance against an attacker who can run
    ``build_content_hash`` — the hash is a fingerprint, not an HMAC. The same
    caveat applies as for the activity hash.
    """

    def test_hash_round_trips_through_jsonfield(self, user):
        """Recomputing the hash from a freshly-loaded row reproduces the stored value.

        Exercises the JSON canonicalisation path (sort_keys=True) plus the
        Code aggregation in _codes_hash_extra including a non-trivial ``meta``
        dict — the most common shape that could trip on dict-key ordering.
        """
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = Event(
            name="complex partial seizure",
            timestamp=12.5,
            duration=8.0,
            value={"focus": "left-temporal", "confidence": 0.9},
            event_class="seizure",
            **_base_kwargs(user, recording),
        )
        event.save()

        ct = ContentType.objects.get_for_model(Event)
        Code.objects.create(
            content_type=ct,
            object_id=str(event.pk),
            standard="ICD10",
            value="G40.209",
            meta={"reviewer": "Dr. X", "score": 0.85},
        )
        Code.objects.create(
            content_type=ct,
            object_id=str(event.pk),
            standard="SNOMED",
            value="84757009",
            meta={"variant": "focal"},
        )

        reloaded = Event.objects.get(pk=event.pk)
        recomputed = reloaded.build_content_hash(extra=reloaded._codes_hash_extra())
        assert recomputed == reloaded.content_hash

    def test_tampered_field_yields_different_hash(self, user):
        """Editing stored hashed-field state in the DB makes the recomputed hash diverge.

        Uses ``.update()`` to simulate bulk DML / direct SQL that bypasses the
        save() recompute path — i.e. the exact attack surface that the hash
        is supposed to make detectable.
        """
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = Event(name="spike", timestamp=2.0, **_base_kwargs(user, recording))
        event.save()
        stored_hash = event.content_hash

        Event.objects.filter(pk=event.pk).update(name="tampered")

        reloaded = Event.objects.get(pk=event.pk)
        recomputed = reloaded.build_content_hash(extra=reloaded._codes_hash_extra())
        assert recomputed != stored_hash
        # The stored value is the original hash — the tamper-detector confirms
        # mismatch by recomputing, not by trusting the stored field.
        assert reloaded.content_hash == stored_hash

    def test_tampered_code_meta_yields_different_hash(self, user):
        """Code.meta is part of the parent's hash — tampering with it must be detectable.

        Asserts the Code.meta inclusion that was added to _codes_hash_extra;
        a regression that drops meta from the payload would silently let any
        meta change pass tamper detection.
        """
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        event = Event(name="spike", timestamp=2.0, **_base_kwargs(user, recording))
        event.save()

        ct = ContentType.objects.get_for_model(Event)
        code = Code.objects.create(
            content_type=ct,
            object_id=str(event.pk),
            standard="ICD10",
            value="G40.9",
            meta={"reviewer": "Dr. X", "score": 0.85},
        )
        event.refresh_from_db()
        stored_hash = event.content_hash

        Code.objects.filter(pk=code.pk).update(meta={"reviewer": "Dr. X", "score": 0.10})

        reloaded = Event.objects.get(pk=event.pk)
        recomputed = reloaded.build_content_hash(extra=reloaded._codes_hash_extra())
        assert recomputed != stored_hash
