"""Contract tests for the grant-delegation cap (``epicurrents.granting``).

Pins the no-amplification rule on the library grant endpoints — the only
grant-creation surface on existing objects — and the author-row protection on
the revoke endpoints. Extraction-plan §3.8 is the design record.
"""

import json
from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from model_bakery import baker

from epicurrents.models import AccessRight
from library.models import Dataset

pytestmark = pytest.mark.django_db


def _post_json(client, url, data):
    return client.post(url, json.dumps(data), content_type="application/json")


def _grant_url(dataset):
    return f"/api/v1/library/datasets/{dataset.pk}/access/"


def _revoke_url(dataset, right_id):
    return f"/api/v1/library/datasets/{dataset.pk}/access/{right_id}/"


def _make_dataset(author):
    return baker.make(Dataset, author=author)


def _make_right(obj, target, **flags):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    defaults = {"can_read": True, "can_write": False, "can_share": False, "apply_middleware": True}
    defaults.update(flags)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(obj.pk),
        access_giver=obj.author,
        access_target=target,
        **defaults,
    )


class TestGrantAmplificationCap:
    def test_share_only_holder_cannot_confer_write(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True)
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk, "can_write": True})
        assert resp.status_code == 403
        assert not AccessRight.objects.filter(access_target=third).exists()

    def test_share_holder_cannot_confer_raw_bytes_to_self(self, client, make_user):
        author, sharer = make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True, apply_middleware=True)
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": sharer.pk, "apply_middleware": False})
        assert resp.status_code == 403
        assert AccessRight.objects.filter(access_target=sharer).count() == 1

    def test_share_holder_with_raw_read_may_confer_raw(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True, apply_middleware=False)
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk, "apply_middleware": False})
        assert resp.status_code == 201

    def test_share_and_write_holder_may_confer_write(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True, can_write=True)
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk, "can_write": True})
        assert resp.status_code == 201

    def test_share_holder_may_confer_deidentified_read(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True)
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk})
        assert resp.status_code == 201

    def test_author_is_unrestricted(self, client, make_user):
        author, third = make_user(), make_user()
        dataset = _make_dataset(author)
        client.force_login(author)
        resp = _post_json(
            client,
            _grant_url(dataset),
            {"access_target_id": third.pk, "can_write": True, "can_share": True, "apply_middleware": False},
        )
        assert resp.status_code == 201

    def test_superuser_is_unrestricted(self, client, make_user, make_superuser):
        author, third = make_user(), make_user()
        dataset = _make_dataset(author)
        client.force_login(make_superuser())
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk, "can_write": True})
        assert resp.status_code == 201

    def test_group_share_rights_count_as_held(self, client, make_user):
        from django.contrib.auth.models import Group

        author, sharer, third = make_user(), make_user(), make_user()
        group = baker.make(Group)
        sharer.groups.add(group)
        dataset = _make_dataset(author)
        ct = ContentType.objects.get_for_model(dataset, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(dataset.pk),
            access_giver=author,
            access_target_group=group,
            can_read=True,
            can_share=True,
            can_write=True,
        )
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk, "can_write": True})
        assert resp.status_code == 201

    def test_expired_share_grant_does_not_qualify(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True, expires_at=timezone.now() - timedelta(days=1))
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk})
        assert resp.status_code == 403

    def test_conferred_expiry_capped_at_grantor_share_expiry(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        bound = timezone.now() + timedelta(days=7)
        _make_right(dataset, sharer, can_share=True, expires_at=bound)
        client.force_login(sharer)

        over = _post_json(
            client,
            _grant_url(dataset),
            {"access_target_id": third.pk, "expires_at": (bound + timedelta(days=1)).isoformat()},
        )
        assert over.status_code == 403

        unbounded = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk})
        assert unbounded.status_code == 403

        within = _post_json(
            client,
            _grant_url(dataset),
            {"access_target_id": third.pk, "expires_at": (bound - timedelta(days=1)).isoformat()},
        )
        assert within.status_code == 201

    def test_unbounded_share_grant_may_confer_unbounded(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True)
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk})
        assert resp.status_code == 201

    def test_share_token_grant_refuses_write_and_share(self, client, make_user):
        author = make_user()
        dataset = _make_dataset(author)
        client.force_login(author)
        resp = _post_json(
            client,
            _grant_url(dataset),
            {"public_share_token": "cap-test-token", "can_write": True},
        )
        assert resp.status_code == 400
        assert not AccessRight.objects.filter(public_share_token="cap-test-token").exists()

    def test_refusal_emits_security_event(self, client, make_user, caplog):
        import logging

        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True)
        client.force_login(sharer)
        with caplog.at_level(logging.WARNING, logger="epicurrents.security"):
            _post_json(client, _grant_url(dataset), {"access_target_id": third.pk, "can_write": True})
        events = [
            r
            for r in caplog.records
            if getattr(r, "security_event_type", "") == "permission.grant_amplification_refused"
        ]
        assert len(events) == 1
        assert events[0].reason == "can_write"
        assert events[0].actor_id == sharer.pk


class TestAuthorRowRevokeProtection:
    def test_share_holder_cannot_revoke_author_row(self, client, make_user):
        author, sharer = make_user(), make_user()
        dataset = _make_dataset(author)
        author_row = _make_right(dataset, author, can_write=True, can_share=True)
        _make_right(dataset, sharer, can_share=True)
        client.force_login(sharer)
        resp = client.delete(_revoke_url(dataset, author_row.pk))
        assert resp.status_code == 403
        assert AccessRight.objects.filter(pk=author_row.pk).exists()

    def test_share_holder_may_revoke_third_party_row(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_share=True)
        third_row = _make_right(dataset, third)
        client.force_login(sharer)
        resp = client.delete(_revoke_url(dataset, third_row.pk))
        assert resp.status_code == 200
        assert not AccessRight.objects.filter(pk=third_row.pk).exists()

    def test_author_may_revoke_own_row(self, client, make_user):
        author = make_user()
        dataset = _make_dataset(author)
        author_row = _make_right(dataset, author, can_write=True, can_share=True)
        client.force_login(author)
        resp = client.delete(_revoke_url(dataset, author_row.pk))
        assert resp.status_code == 200

    def test_superuser_may_revoke_author_row(self, client, make_user, make_superuser):
        author = make_user()
        dataset = _make_dataset(author)
        author_row = _make_right(dataset, author, can_write=True, can_share=True)
        client.force_login(make_superuser())
        resp = client.delete(_revoke_url(dataset, author_row.pk))
        assert resp.status_code == 200


class TestCleanSlateFindings:
    def test_sharer_without_read_cannot_confer_read(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        _make_right(dataset, sharer, can_read=False, can_share=True)
        client.force_login(sharer)
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk})
        assert resp.status_code == 403

    def test_naive_expiry_input_is_handled_not_crashed(self, client, make_user):
        author, sharer, third = make_user(), make_user(), make_user()
        dataset = _make_dataset(author)
        bound = timezone.now() + timedelta(days=7)
        _make_right(dataset, sharer, can_share=True, expires_at=bound)
        client.force_login(sharer)
        naive_within = (bound - timedelta(days=1)).replace(tzinfo=None).isoformat()
        resp = _post_json(client, _grant_url(dataset), {"access_target_id": third.pk, "expires_at": naive_within})
        assert resp.status_code == 201

    def test_expired_share_grant_does_not_qualify_on_recordings_manager(self, client, make_user):
        from recordings.models import Recording

        author, sharer = make_user(), make_user()
        recording = baker.make(Recording, author=author, status=Recording.Status.READY, stored_name="C" * 32 + ".edf")
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=author,
            access_target=sharer,
            can_read=True,
            can_share=True,
            expires_at=timezone.now() - timedelta(days=1),
        )
        client.force_login(sharer)
        resp = client.get(f"/recordings/api/v1/{'C' * 32}/access/")
        assert resp.status_code == 403
