"""Tests for AccessRight per-target uniqueness and the deterministic resolver ordering built on it.

The three partial unique constraints on ``AccessRight.Meta`` enforce the invariant the
README states — one row per ``(content_type, object_id, target)`` grant — and the read resolvers
order the rows a caller can still match through several targets at once, so which grant's
``apply_middleware`` wins no longer depends on database row order.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from model_bakery import baker

from epicurrents.models import AccessRight
from epicurrents.permissions import get_federated_read_access_result, get_read_access_result


def _make_right(obj, giver, **kwargs):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(obj.pk),
        access_giver=giver,
        **kwargs,
    )


def _make_peer(user, url="https://peer.example.com"):
    from federation.models import FederatedPeer

    return FederatedPeer.objects.create(
        url=url,
        display_name="Test Peer",
        public_key="A" * 43,
        is_trusted=True,
        added_by=user,
    )


@pytest.mark.django_db
class TestPerTargetUniquenessConstraints:
    def test_duplicate_user_target_rejected(self, user, make_user):
        reader = make_user(username="reader")
        recording = baker.make("recordings.Recording", author=user, status="ready")
        _make_right(recording, user, access_target=reader, can_read=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_right(recording, user, access_target=reader, can_read=True, can_write=True)

    def test_duplicate_group_target_rejected(self, user):
        from django.contrib.auth.models import Group

        group = Group.objects.create(name="readers")
        recording = baker.make("recordings.Recording", author=user, status="ready")
        _make_right(recording, user, access_target_group=group, can_read=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_right(recording, user, access_target_group=group, can_read=True)

    def test_duplicate_federated_target_rejected(self, user):
        peer = _make_peer(user)
        recording = baker.make("recordings.Recording", author=user, status="ready")
        _make_right(recording, user, federated_peer=peer, remote_user_id="u1", can_read=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_right(recording, user, federated_peer=peer, remote_user_id="u1", can_read=True)

    def test_wildcard_and_exact_federated_rows_coexist(self, user):
        peer = _make_peer(user)
        recording = baker.make("recordings.Recording", author=user, status="ready")
        _make_right(recording, user, federated_peer=peer, remote_user_id="", can_read=True)
        _make_right(recording, user, federated_peer=peer, remote_user_id="u1", can_read=True)
        assert AccessRight.objects.filter(federated_peer=peer).count() == 2

    def test_two_share_token_rows_coexist_on_one_object(self, user):
        recording = baker.make("recordings.Recording", author=user, status="ready")
        _make_right(recording, user, public_share_token="token-one", can_read=True)
        _make_right(recording, user, public_share_token="token-two", can_read=True)
        assert AccessRight.objects.filter(object_id=str(recording.pk)).count() == 2

    def test_same_target_on_different_objects_coexists(self, user, make_user):
        reader = make_user(username="reader")
        rec_a = baker.make("recordings.Recording", author=user, status="ready")
        rec_b = baker.make("recordings.Recording", author=user, status="ready")
        _make_right(rec_a, user, access_target=reader, can_read=True)
        _make_right(rec_b, user, access_target=reader, can_read=True)
        assert AccessRight.objects.filter(access_target=reader).count() == 2


@pytest.mark.django_db
class TestResolverOrderingDeterminism:
    """A caller matching through several targets gets a defined winner, not row order."""

    def test_direct_user_row_wins_over_group_row(self, user, make_user):
        # Deliberate trade-off, not an oversight: the direct row wins even when
        # the group row sanitizes, per the explicit-grants-win rule (README
        # gotchas). Grant capping bounds it — only a raw-authorized grantor can
        # create the raw direct row.
        from django.contrib.auth.models import Group

        reader = make_user(username="reader")
        group = Group.objects.create(name="readers")
        reader.groups.add(group)
        recording = baker.make("recordings.Recording", author=user, status="ready")
        # Group row would de-identify; the direct row deliberately serves raw.
        _make_right(recording, user, access_target_group=group, can_read=True, apply_middleware=True)
        _make_right(recording, user, access_target=reader, can_read=True, apply_middleware=False)
        result = get_read_access_result(reader, recording)
        assert result.granted is True
        assert result.apply_middleware is False

    def test_group_rows_prefer_deidentifying_grant(self, user, make_user):
        from django.contrib.auth.models import Group

        reader = make_user(username="reader")
        group_a = Group.objects.create(name="group-a")
        group_b = Group.objects.create(name="group-b")
        reader.groups.add(group_a, group_b)
        recording = baker.make("recordings.Recording", author=user, status="ready")
        _make_right(recording, user, access_target_group=group_a, can_read=True, apply_middleware=False)
        _make_right(recording, user, access_target_group=group_b, can_read=True, apply_middleware=True)
        result = get_read_access_result(reader, recording)
        assert result.granted is True
        assert result.apply_middleware is True

    def test_exact_federated_row_wins_over_wildcard(self, user):
        peer = _make_peer(user)
        recording = baker.make("recordings.Recording", author=user, status="ready")
        # Wildcard de-identifies everyone; the exact grant deliberately serves raw.
        _make_right(recording, user, federated_peer=peer, remote_user_id="", can_read=True, apply_middleware=True)
        _make_right(recording, user, federated_peer=peer, remote_user_id="u1", can_read=True, apply_middleware=False)
        result = get_federated_read_access_result(peer, "u1", recording)
        assert result.granted is True
        assert result.apply_middleware is False
        # A different remote user only matches the wildcard row.
        other = get_federated_read_access_result(peer, "u2", recording)
        assert other.granted is True
        assert other.apply_middleware is True
