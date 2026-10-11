"""Contract tests for GDPR Art. 17 subject erasure across the audit trail.

Backstops the load-bearing contracts in ``activity/erasure.py`` (scrub
completeness + chain integrity), the ``Session`` exclusion in
``activity/signals.py``, and the credential masking in
``activity/audit.serialize_instance``.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session

from activity.audit import (
    _WITHHELD_FIELDS,
    MASK_PREFIX,
    WITHHELD_SENTINEL,
    registered_masked_fields,
    rollback_change,
    serialize_instance,
    verify_chain,
    verify_change_hash,
)
from activity.erasure import ERASED_SENTINEL, erase_subject
from activity.models import Activity, ObjectChangeLog
from activity.system_activity import with_system_activity


def _user_ct():
    return ContentType.objects.get_for_model(get_user_model())


def _login(client, username, password="testpass123"):
    response = client.post(
        "/api/v1/user/login",
        json.dumps({"username": username, "password": password}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response


@pytest.mark.django_db
class TestSessionExclusion:
    def test_session_writes_produce_no_change_log(self, client, make_user):
        make_user(username="sessionprobe")
        _login(client, "sessionprobe")
        session_ct = ContentType.objects.get_for_model(Session)
        assert not ObjectChangeLog.objects.filter(content_type=session_ct).exists()

    def test_user_model_writes_still_audited(self, client, make_user):
        # Coverage-retention check: excluding Session from the signal gate
        # must not strip auditing from any other model. The login's
        # last_login update on the User row is the canary.
        make_user(username="sessionprobe2")
        _login(client, "sessionprobe2")
        assert ObjectChangeLog.objects.filter(content_type=_user_ct()).exists()


@pytest.mark.django_db
class TestCredentialMasking:
    def test_login_modify_row_masks_password(self, client, make_user):
        user = make_user(username="maskprobe")
        _login(client, "maskprobe")
        row = ObjectChangeLog.objects.filter(content_type=_user_ct(), object_id=str(user.pk)).latest("created_at")
        assert row.before_state["password"].startswith(MASK_PREFIX)
        assert user.password not in json.dumps(row.before_state)

    def test_password_change_produces_masked_diff(self, make_user):
        user = make_user(username="maskdiff")
        with with_system_activity("tests.password.change", interface=Activity.Interface.COMMAND):
            user.set_password("newpass456!")
            user.save()
        row = ObjectChangeLog.objects.filter(
            content_type=_user_ct(),
            object_id=str(user.pk),
            action=ObjectChangeLog.ACTION_MODIFY,
        ).latest("created_at")
        delta = row.changes["password"]
        assert delta["from"].startswith(MASK_PREFIX)
        assert delta["to"].startswith(MASK_PREFIX)
        assert delta["from"] != delta["to"]

    def test_push_subscription_keys_masked(self, make_user):
        from model_bakery import baker

        user = make_user()
        with with_system_activity("tests.subscription.create", interface=Activity.Interface.COMMAND):
            subscription = baker.make(
                "notifications.PushSubscription",
                user=user,
                endpoint="https://push.example/ep1",
                p256dh="p256dh-key-material",
                auth="auth-secret",
            )
        ct = ContentType.objects.get_for_model(subscription.__class__)
        row = ObjectChangeLog.objects.get(content_type=ct, object_id=str(subscription.pk))
        assert row.before_state["p256dh"].startswith(MASK_PREFIX)
        assert row.before_state["auth"].startswith(MASK_PREFIX)
        # The endpoint is a per-device identifier, not a secret — it stays
        # readable for audit reconstruction and is scrubbed only on erasure.
        assert row.before_state["endpoint"] == "https://push.example/ep1"

    def test_withheld_field_is_one_constant_whatever_its_value(self, make_user, monkeypatch):
        # A mask is a digest of the value, so a copy of the value recomputes it; a withheld
        # field must not be recomputable, and two different values must read the same.
        from model_bakery import baker

        monkeypatch.setitem(_WITHHELD_FIELDS, "notifications.pushsubscription", frozenset({"endpoint"}))
        user = make_user()
        first, second = (
            baker.prepare("notifications.PushSubscription", user=user, endpoint=endpoint, p256dh="key", auth="secret")
            for endpoint in ("https://push.example/a", "https://push.example/b")
        )
        assert serialize_instance(first)["endpoint"] == serialize_instance(second)["endpoint"] == WITHHELD_SENTINEL
        # The masks registered for the same model still apply beside it.
        assert serialize_instance(first)["auth"].startswith(MASK_PREFIX)
        assert serialize_instance(first)["auth"] != WITHHELD_SENTINEL
        first.endpoint = ""
        assert serialize_instance(first)["endpoint"] == ""
        assert {"endpoint", "auth", "p256dh"} <= registered_masked_fields("notifications.pushsubscription")

    def test_rollback_does_not_clobber_password_with_mask(self, make_user, superuser):
        user = make_user(username="maskroll")
        with with_system_activity("tests.profile.update", interface=Activity.Interface.COMMAND):
            user.first_name = "Changed"
            user.save()
        row = ObjectChangeLog.objects.filter(
            content_type=_user_ct(),
            object_id=str(user.pk),
            action=ObjectChangeLog.ACTION_MODIFY,
        ).latest("created_at")
        rollback_change(user=superuser, change_id=row.pk)
        user.refresh_from_db()
        assert user.first_name == ""
        assert user.check_password("testpass123")


def _build_subject_history(make_user):
    """Create a user and a spread of audit rows carrying their PII."""
    from model_bakery import baker

    user = make_user(
        username="erasetarget",
        email="erase.target@example.com",
        first_name="Erase",
        last_name="Target",
    )
    with with_system_activity("tests.subject.history", interface=Activity.Interface.COMMAND):
        user.first_name = "Erased-To-Be"
        user.save()
        identity = baker.make(
            "user.ExternalIdentity",
            user=user,
            subject="oidc-subject-xyz",
            email="erase.target@example.com",
        )
        subscription = baker.make(
            "notifications.PushSubscription",
            user=user,
            endpoint="https://push.example/erase-target",
            p256dh="keymaterial",
            auth="authsecret",
        )
    activity = Activity.objects.create(
        verb="user.password.reset.request",
        target_content_type=_user_ct(),
        target_object_id=str(user.pk),
        metadata={"email_hash": "cafe" * 8, "found": True},
    )
    return user, identity, subscription, activity


@pytest.mark.django_db
class TestEraseSubject:
    def test_scrubs_all_registered_pii(self, make_user):
        user, identity, subscription, _ = _build_subject_history(make_user)
        user_pk = user.pk
        with with_system_activity("tests.subject.delete", interface=Activity.Interface.COMMAND):
            user.delete()

        erase_subject(user_pk)

        for row in ObjectChangeLog.objects.filter(content_type=_user_ct(), object_id=str(user_pk)).exclude(
            action=ObjectChangeLog.ACTION_ERASE
        ):
            payload = json.dumps([row.before_state, row.changes])
            assert "erasetarget" not in payload
            assert "erase.target@example.com" not in payload
            assert row.erased_at is not None
            assert row.before_state["username"] == ERASED_SENTINEL

        identity_ct = ContentType.objects.get(app_label="user", model="externalidentity")
        for row in ObjectChangeLog.objects.filter(content_type=identity_ct):
            assert row.before_state["subject"] == ERASED_SENTINEL
            assert row.before_state["email"] == ERASED_SENTINEL

        subscription_ct = ContentType.objects.get(app_label="notifications", model="pushsubscription")
        for row in ObjectChangeLog.objects.filter(content_type=subscription_ct):
            assert row.before_state["endpoint"] == ERASED_SENTINEL

    def test_erased_rows_verify_and_chain_stays_intact(self, make_user):
        user, identity, subscription, _ = _build_subject_history(make_user)
        user_pk = user.pk
        with with_system_activity("tests.subject.delete", interface=Activity.Interface.COMMAND):
            user.delete()

        erase_subject(user_pk)

        for content_type in (
            _user_ct(),
            ContentType.objects.get(app_label="user", model="externalidentity"),
            ContentType.objects.get(app_label="notifications", model="pushsubscription"),
        ):
            for row in ObjectChangeLog.objects.filter(content_type=content_type):
                assert verify_change_hash(row), f"row {row.pk} failed verification"
            result = verify_chain(content_type)
            assert result.ok, repr(result)

    def test_appends_sealed_erase_record(self, make_user):
        user, *_ = _build_subject_history(make_user)
        user_pk = user.pk
        erase_subject(user_pk)
        erase_row = ObjectChangeLog.objects.get(
            content_type=_user_ct(),
            object_id=str(user_pk),
            action=ObjectChangeLog.ACTION_ERASE,
        )
        assert erase_row.before_state["reason"] == "gdpr_art17"
        assert erase_row.before_state["erased"]["user.user"] >= 1
        assert erase_row.sequence_no is not None
        assert verify_change_hash(erase_row)

    def test_strips_activity_metadata_pii(self, make_user):
        user, _, _, activity = _build_subject_history(make_user)
        erase_subject(user.pk)
        activity.refresh_from_db()
        assert activity.metadata["email_hash"] == ERASED_SENTINEL
        assert activity.metadata["found"] is True

    def test_second_run_is_idempotent(self, make_user):
        user, *_ = _build_subject_history(make_user)
        user_pk = user.pk
        erase_subject(user_pk)
        sealed = {
            row.pk: (row.erased_at, row.erased_hash) for row in ObjectChangeLog.objects.filter(erased_at__isnull=False)
        }
        summary = erase_subject(user_pk)
        assert all(count == 0 for count in summary.values())
        for row in ObjectChangeLog.objects.filter(pk__in=sealed):
            assert (row.erased_at, row.erased_hash) == sealed[row.pk]

    def test_post_erasure_tampering_is_detected(self, make_user):
        user, *_ = _build_subject_history(make_user)
        user_pk = user.pk
        erase_subject(user_pk)
        row = ObjectChangeLog.objects.filter(
            content_type=_user_ct(),
            object_id=str(user_pk),
            erased_at__isnull=False,
        ).first()
        row.before_state["email"] = "forged@example.com"
        row.save(update_fields=["before_state"])
        assert not verify_change_hash(row)

    def test_rollback_refused_on_erased_and_erase_rows(self, make_user, superuser):
        user, *_ = _build_subject_history(make_user)
        user_pk = user.pk
        erase_subject(user_pk)
        erased = ObjectChangeLog.objects.filter(content_type=_user_ct(), erased_at__isnull=False).first()
        with pytest.raises(ValueError, match="was erased"):
            rollback_change(user=superuser, change_id=erased.pk)
        erase_record = ObjectChangeLog.objects.get(content_type=_user_ct(), action=ObjectChangeLog.ACTION_ERASE)
        with pytest.raises(ValueError, match="cannot be rolled back"):
            rollback_change(user=superuser, change_id=erase_record.pk)

    def test_other_subjects_untouched(self, make_user):
        bystander = make_user(username="bystander", email="bystander@example.com")
        with with_system_activity("tests.bystander.update", interface=Activity.Interface.COMMAND):
            bystander.first_name = "Still"
            bystander.save()
        user, *_ = _build_subject_history(make_user)
        erase_subject(user.pk)
        row = ObjectChangeLog.objects.filter(content_type=_user_ct(), object_id=str(bystander.pk)).latest("created_at")
        assert row.erased_at is None
        assert row.before_state["username"] == "bystander"


@pytest.mark.django_db
class TestContentTypeProtection:
    """ObjectChangeLog.content_type is PROTECT — a shard must survive its ContentType.

    The row that matters is a *stale* one: a historical erasure registration
    scrubs a dropped model's audit rows through the ContentType that outlives
    it, and ``remove_stale_contenttypes`` offers to delete exactly that row.
    Under CASCADE a confirmed run would destroy the shard outside the
    sanctioned tombstoning path and silently unhook the scrub.
    """

    def test_a_content_type_with_change_rows_cannot_be_deleted(self, make_user):
        from django.db.models.deletion import ProtectedError
        from django.utils import timezone

        from activity.audit import create_chained_change_log, hash_payload_state

        user = make_user()
        ct, _created = ContentType.objects.get_or_create(app_label="gone", model="retiredmodel")
        before = {"id": 1, "user_id": user.pk, "secret": "value"}
        create_chained_change_log(
            content_type=ct,
            object_id="1",
            action=ObjectChangeLog.ACTION_MODIFY,
            performed_by=None,
            before_state=before,
            changes=None,
            hash_payload=hash_payload_state(ObjectChangeLog.ACTION_MODIFY, before, None),
            timestamp=timezone.now(),
        )

        with pytest.raises(ProtectedError):
            ct.delete()
        assert ObjectChangeLog.objects.filter(content_type=ct).exists()


def _coded_events(make_user, count=2):
    """Audited events with a code each, created, modified and deleted the way a round leaves them."""
    from model_bakery import baker

    from annotations.models import Code, Event

    author = make_user()
    events, codes = [], []
    with with_system_activity("tests.review.annotate", interface=Activity.Interface.COMMAND):
        for index in range(count):
            event = baker.make(
                Event,
                author=author,
                target_content_type=_user_ct(),
                target_object_id=str(author.pk),
                name=f"spike {index}",
                timestamp=12.5 + index,
                duration=0.25,
                object_hash=f"{index:032x}",
            )
            event.timestamp += 1
            event.save()
            events.append(event)
            codes.append(
                baker.make(
                    Code,
                    content_type=ContentType.objects.get_for_model(Event),
                    object_id=str(event.pk),
                    standard="epicurrents.eeg",
                    value="spike",
                )
            )
    return events, codes


def _rows(model, objects):
    ct = ContentType.objects.get_for_model(model)
    return ObjectChangeLog.objects.filter(content_type=ct, object_id__in=[str(obj.pk) for obj in objects]).exclude(
        action=ObjectChangeLog.ACTION_ERASE
    )


@pytest.mark.django_db
class TestScrubRows:
    def _scrub(self, events, codes, reason="review.handover"):
        from activity.erasure import RowScrub, scrub_rows

        return scrub_rows(
            [
                RowScrub("annotations.event", [e.pk for e in events], {"timestamp", "duration", "object_hash"}),
                RowScrub("annotations.code", [c.pk for c in codes], {"value"}),
            ],
            reason=reason,
        )

    def test_scrubs_the_named_fields_of_the_named_objects_only(self, make_user):
        from annotations.models import Code, Event

        events, codes = _coded_events(make_user, count=3)
        with with_system_activity("tests.review.handover", interface=Activity.Interface.COMMAND):
            summary = self._scrub(events[:2], codes[:2])

        assert summary == {"annotations.event": 4, "annotations.code": 2}
        for row in _rows(Event, events[:2]):
            assert row.erased_at is not None
            assert row.before_state["timestamp"] == ERASED_SENTINEL
            assert row.before_state["object_hash"] == ERASED_SENTINEL
            assert row.before_state["name"].startswith("spike")
            if row.changes:
                assert row.changes["timestamp"] == {"from": ERASED_SENTINEL, "to": ERASED_SENTINEL}
        for row in _rows(Code, codes[:2]):
            assert row.before_state["value"] == ERASED_SENTINEL
            assert row.before_state["standard"] == "epicurrents.eeg"
        for row in [*_rows(Event, events[2:]), *_rows(Code, codes[2:])]:
            assert row.erased_at is None

    def test_a_delete_in_the_same_transaction_is_scrubbed(self, make_user):
        from django.db import transaction

        from annotations.models import Event

        events, codes = _coded_events(make_user, count=1)
        with with_system_activity("tests.review.handover", interface=Activity.Interface.COMMAND):
            with transaction.atomic():
                Event.objects.get(pk=events[0].pk).delete()
                self._scrub(events, codes)

        deleted = _rows(Event, events).get(action=ObjectChangeLog.ACTION_DELETE)
        assert deleted.before_state["timestamp"] == ERASED_SENTINEL
        assert verify_change_hash(deleted)

    def test_scrubbed_rows_verify_and_the_chains_stay_intact(self, make_user):
        from annotations.models import Code, Event

        events, codes = _coded_events(make_user)
        self._scrub(events, codes)
        for model in (Event, Code):
            ct = ContentType.objects.get_for_model(model)
            for row in ObjectChangeLog.objects.filter(content_type=ct):
                assert verify_change_hash(row), f"row {row.pk} failed verification"
            assert verify_chain(ct).ok

    def test_the_run_is_recorded_on_each_shard_and_names_no_object(self, make_user):
        from annotations.models import Code, Event

        events, codes = _coded_events(make_user)
        self._scrub(events, codes)
        records = list(ObjectChangeLog.objects.filter(action=ObjectChangeLog.ACTION_ERASE))
        assert {record.content_type for record in records} == {
            ContentType.objects.get_for_model(Event),
            ContentType.objects.get_for_model(Code),
        }
        assert len({record.object_id for record in records}) == 1
        for record in records:
            assert record.before_state == {
                "erased": {"annotations.code": 2, "annotations.event": 4},
                "fields": {
                    "annotations.code": ["value"],
                    "annotations.event": ["duration", "object_hash", "timestamp"],
                },
                "reason": "review.handover",
            }
            assert verify_change_hash(record)
            payload = json.dumps(record.before_state)
            for obj in [*events, *codes]:
                assert f'"{obj.pk}"' not in payload

    def test_a_second_run_changes_no_seal(self, make_user):
        events, codes = _coded_events(make_user)
        self._scrub(events, codes)
        sealed = {
            row.pk: (row.erased_at, row.erased_hash) for row in ObjectChangeLog.objects.filter(erased_at__isnull=False)
        }
        assert self._scrub(events, codes) == {"annotations.event": 0, "annotations.code": 0}
        for row in ObjectChangeLog.objects.filter(pk__in=sealed):
            assert (row.erased_at, row.erased_hash) == sealed[row.pk]

    def test_object_ids_beyond_one_query_are_all_scrubbed(self, make_user, monkeypatch):
        from activity import erasure
        from annotations.models import Event

        monkeypatch.setattr(erasure, "_ID_CHUNK", 1)
        events, codes = _coded_events(make_user, count=3)
        assert self._scrub(events, codes)["annotations.event"] == 6
        assert not _rows(Event, events).filter(erased_at__isnull=True).exists()

    @pytest.mark.parametrize(
        "label,fields,reason",
        [
            ("annotations.nosuchmodel", {"value"}, "review.handover"),
            ("annotations.code", {"valeu"}, "review.handover"),
            ("annotations.code", set(), "review.handover"),
            ("annotations.code", {"value"}, "Handed over by the curator"),
        ],
    )
    def test_a_scrub_that_cannot_be_what_was_meant_writes_nothing(self, make_user, label, fields, reason):
        from activity.erasure import RowScrub, scrub_rows

        events, codes = _coded_events(make_user)
        before = ObjectChangeLog.objects.count()
        with pytest.raises(ValueError, match="scrub_rows"):
            scrub_rows(
                [
                    RowScrub("annotations.event", [e.pk for e in events], {"timestamp"}),
                    RowScrub(label, [c.pk for c in codes], fields),
                ],
                reason=reason,
            )
        assert ObjectChangeLog.objects.count() == before
        assert not ObjectChangeLog.objects.filter(erased_at__isnull=False).exists()

    @pytest.mark.parametrize("object_ids,fields", [("42", {"value"}), (["42"], "value")])
    def test_a_bare_string_is_not_read_as_a_collection(self, object_ids, fields):
        from activity.erasure import RowScrub

        with pytest.raises(TypeError):
            RowScrub("annotations.code", object_ids, fields)

    def test_rollback_is_refused_on_a_scrubbed_row(self, make_user, superuser):
        from annotations.models import Event

        events, codes = _coded_events(make_user)
        self._scrub(events, codes)
        row = _rows(Event, events).filter(action=ObjectChangeLog.ACTION_MODIFY).first()
        with pytest.raises(ValueError, match="was erased"):
            rollback_change(user=superuser, change_id=row.pk)
