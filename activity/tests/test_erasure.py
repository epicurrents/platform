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
    MASK_PREFIX,
    rollback_change,
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
        with pytest.raises(ValueError, match="subject-erased"):
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
