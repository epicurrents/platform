"""Tests for epicurrents.permissions — can_modify_object, can_read_object, can_write_object,
get_read_access_result, ReadAccessTerms."""

import pytest
from django.contrib.contenttypes.models import ContentType

from epicurrents.models import AccessRight
from epicurrents.permissions import (
    ReadAccessTerms,
    can_modify_object,
    can_read_object,
    can_write_object,
    get_federated_read_access_result,
    get_read_access_result,
)


def _make_access_right(recording, giver, *, target=None, group=None, token=None, **flags):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        access_target=target,
        access_target_group=group,
        public_share_token=token,
        **flags,
    )


@pytest.mark.django_db
class TestCanModifyObject:
    def test_superuser_always_allowed(self, superuser, make_user):
        other = make_user(username="other")
        # Create a dummy object whose author is `other`
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=other)
        assert can_modify_object(superuser, recording) is True

    def test_author_allowed(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        assert can_modify_object(user, recording) is True

    def test_non_author_denied(self, user, make_user):
        other = make_user(username="other")
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        assert can_modify_object(other, recording) is False

    def test_unauthenticated_denied(self):
        from unittest.mock import MagicMock

        anon = MagicMock()
        anon.is_authenticated = False
        # Use a simple object
        assert can_modify_object(None, object()) is False
        assert can_modify_object(anon, object()) is False

    def test_none_user_denied(self):
        assert can_modify_object(None, object()) is False


@pytest.mark.django_db
class TestCanReadObject:
    def test_superuser_always_allowed(self, superuser, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        assert can_read_object(superuser, recording) is True

    def test_user_with_read_access_right(self, user, make_user):
        reader = make_user(username="reader")
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, target=reader, can_read=True)
        assert can_read_object(reader, recording) is True

    def test_user_without_access_right_denied(self, user, make_user):
        reader = make_user(username="reader")
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        assert can_read_object(reader, recording) is False

    def test_expired_access_right_denied(self, user, make_user):
        from django.utils import timezone

        reader = make_user(username="reader")
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(
            recording,
            user,
            target=reader,
            can_read=True,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        assert can_read_object(reader, recording) is False

    def test_share_token_grants_read(self, user):
        """AccessRight.can_read_with_token is the correct API for token-based checks."""
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, token="mytoken123", can_read=True)
        assert AccessRight.can_read_with_token("mytoken123", recording) is True

    def test_wrong_token_denied(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, token="correcttoken", can_read=True)
        assert AccessRight.can_read_with_token("wrongtoken", recording) is False

    def test_group_access_right_grants_read(self, user, make_user):
        from django.contrib.auth.models import Group
        from model_bakery import baker

        reader = make_user(username="reader")
        group = Group.objects.create(name="readers")
        reader.groups.add(group)
        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, group=group, can_read=True)
        assert can_read_object(reader, recording) is True


@pytest.mark.django_db
class TestReadAccessTerms:
    """Unit tests for the ReadAccessTerms dataclass and get_read_access_result."""

    def test_bool_true_when_granted(self):
        assert bool(ReadAccessTerms(granted=True)) is True

    def test_bool_false_when_not_granted(self):
        assert bool(ReadAccessTerms(granted=False)) is False

    def test_defaults(self):
        result = ReadAccessTerms(granted=True)
        assert result.apply_middleware is False

    def test_superuser_always_granted_without_middleware(self, superuser):
        from model_bakery import baker

        recording = baker.make("recordings.Recording")
        result = get_read_access_result(superuser, recording)
        assert result.granted is True
        assert result.apply_middleware is False

    def test_no_access_right_returns_not_granted(self, user, make_user):
        reader = make_user()
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        result = get_read_access_result(reader, recording)
        assert result.granted is False

    def test_access_right_without_middleware_flag(self, user, make_user):
        reader = make_user()
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, target=reader, can_read=True, apply_middleware=False)
        result = get_read_access_result(reader, recording)
        assert result.granted is True
        assert result.apply_middleware is False

    def test_access_right_with_middleware_flag(self, user, make_user):
        reader = make_user()
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, target=reader, can_read=True, apply_middleware=True)
        result = get_read_access_result(reader, recording)
        assert result.granted is True
        assert result.apply_middleware is True

    def test_group_access_right_with_middleware_flag(self, user, make_user):
        from django.contrib.auth.models import Group
        from model_bakery import baker

        reader = make_user()
        group = Group.objects.create(name="mw_readers")
        reader.groups.add(group)
        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, group=group, can_read=True, apply_middleware=True)
        result = get_read_access_result(reader, recording)
        assert result.granted is True
        assert result.apply_middleware is True

    def test_share_token_with_middleware_flag(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, token="mwtoken", can_read=True, apply_middleware=True)
        result = get_read_access_result(None, recording, share_token="mwtoken")
        assert result.granted is True
        assert result.apply_middleware is True

    def test_expired_right_not_granted(self, user, make_user):
        from django.utils import timezone

        reader = make_user()
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(
            recording,
            user,
            target=reader,
            can_read=True,
            apply_middleware=True,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        result = get_read_access_result(reader, recording)
        assert result.granted is False

    def test_extension_grant_has_no_middleware(self, user, make_user):
        """Extensions always yield apply_middleware=False (no AccessRight row)."""
        from epicurrents.permissions import register_read_permission_extension

        reader = make_user()
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)

        def _always_grant(user, obj, share_token=None):
            return True

        register_read_permission_extension(_always_grant)
        try:
            result = get_read_access_result(reader, recording)
            assert result.granted is True
            assert result.apply_middleware is False
        finally:
            from epicurrents import permissions as _perm_mod

            _perm_mod._READ_PERMISSION_EXTENSIONS.remove(_always_grant)

    def test_extension_can_return_read_result_with_middleware(self, user, make_user):
        """Extensions returning ReadAccessTerms propagate apply_middleware to the caller."""
        from epicurrents.permissions import (
            ReadAccessTerms,
            register_read_permission_extension,
        )

        reader = make_user()
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)

        def _grant_with_middleware(user, obj, share_token=None):
            return ReadAccessTerms(granted=True, apply_middleware=True)

        register_read_permission_extension(_grant_with_middleware)
        try:
            result = get_read_access_result(reader, recording)
            assert result.granted is True
            assert result.apply_middleware is True
        finally:
            from epicurrents import permissions as _perm_mod

            _perm_mod._READ_PERMISSION_EXTENSIONS.remove(_grant_with_middleware)

    def test_extension_read_result_denied_does_not_grant(self, user, make_user):
        """Extensions returning ReadAccessTerms(granted=False) do not grant access."""
        from epicurrents.permissions import (
            ReadAccessTerms,
            register_read_permission_extension,
        )

        reader = make_user()
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)

        def _deny(user, obj, share_token=None):
            return ReadAccessTerms(granted=False)

        register_read_permission_extension(_deny)
        try:
            result = get_read_access_result(reader, recording)
            assert result.granted is False
        finally:
            from epicurrents import permissions as _perm_mod

            _perm_mod._READ_PERMISSION_EXTENSIONS.remove(_deny)

    def test_can_read_object_delegates_to_get_read_access_result(self, user, make_user):
        reader = make_user()
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, target=reader, can_read=True, apply_middleware=True)
        # can_read_object must still return True (bool wrapper)
        assert can_read_object(reader, recording) is True


@pytest.mark.django_db
class TestCanWriteObject:
    def test_author_can_write(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        assert can_write_object(user, recording) is True

    def test_expired_write_right_denied(self, user, make_user):
        """Expired AccessRight.can_write must not grant access (bug B1 regression guard)."""
        from django.utils import timezone
        from model_bakery import baker

        writer = make_user()
        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(
            recording,
            user,
            target=writer,
            can_read=True,
            can_write=True,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        assert can_write_object(writer, recording) is False

    def test_user_with_write_access_right(self, user, make_user):
        writer = make_user(username="writer")
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, target=writer, can_read=True, can_write=True)
        assert can_write_object(writer, recording) is True

    def test_user_with_read_only_cannot_write(self, user, make_user):
        reader = make_user(username="reader")
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_access_right(recording, user, target=reader, can_read=True)
        assert can_write_object(reader, recording) is False

    def test_superuser_can_write(self, superuser, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        assert can_write_object(superuser, recording) is True


@pytest.mark.django_db
class TestFederatedReadExtensions:
    """A peer must be able to reach objects it holds no row of its own on.

    Local read extensions cannot answer for a peer — they take a user, groups and
    a share token, none of which a peer has — so before this registry existed the
    federated resolver saw direct rows only. Sharing a dataset with a peer was
    accepted and conveyed nothing, on the platform's only sharing unit.
    """

    def _make_peer(self, user, url="https://ext-peer.example.com"):
        from federation.models import FederatedPeer

        return FederatedPeer.objects.create(url=url, public_key="B" * 43, is_trusted=True, added_by=user)

    def _register(self, check=None, visible_terms=None):
        """Register a pair, returning an undo that restores what was there before.

        Restores rather than clears: the registry is process-wide and already holds
        library's dataset extension from ``AppConfig.ready()``, so clearing it here
        would quietly disarm dataset sharing for every test that ran afterwards.
        """
        from epicurrents import permissions as _perm_mod
        from epicurrents.permissions import register_federated_read_extension

        previous = list(_perm_mod._FEDERATED_READ_EXTENSIONS)
        register_federated_read_extension(
            check=check or (lambda peer, remote_user_id, obj: None),
            visible_terms=visible_terms or (lambda peer, remote_user_id, content_type: {}),
        )

        def undo():
            _perm_mod._FEDERATED_READ_EXTENSIONS[:] = previous

        return undo

    def test_the_de_identifying_extension_wins_when_two_reach_the_same_object(self, user):
        # Registration order is whatever order AppConfig.ready() ran in, so
        # deciding the tie by it would make what a peer receives depend on
        # INSTALLED_APPS. The listing beside this resolves the same overlap toward
        # de-identification; disagreeing would advertise a de-identified object
        # and then serve the raw bytes.
        from model_bakery import baker

        from epicurrents.permissions import ReadAccessTerms

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        undo_raw = self._register(check=lambda p, r, o: ReadAccessTerms(granted=True, apply_middleware=False))
        undo_clean = self._register(check=lambda p, r, o: ReadAccessTerms(granted=True, apply_middleware=True))
        try:
            terms = get_federated_read_access_result(peer, "u1", recording)
            assert terms.granted is True
            assert terms.apply_middleware is True
        finally:
            undo_clean()
            undo_raw()

    def test_extension_grants_when_no_direct_row_exists(self, user):
        from model_bakery import baker

        from epicurrents.permissions import ReadAccessTerms

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        undo = self._register(check=lambda p, r, o: ReadAccessTerms(granted=True))
        try:
            assert get_federated_read_access_result(peer, "u1", recording).granted is True
        finally:
            undo()

    def test_extension_terms_carry_apply_middleware(self, user):
        # The flag decides whether the peer receives de-identified bytes, so an
        # extension that grants without carrying it silently serves raw PHI.
        from model_bakery import baker

        from epicurrents.permissions import ReadAccessTerms

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        undo = self._register(check=lambda p, r, o: ReadAccessTerms(granted=True, apply_middleware=True))
        try:
            assert get_federated_read_access_result(peer, "u1", recording).apply_middleware is True
        finally:
            undo()

    def test_a_direct_row_outranks_an_extension(self, user):
        # The sharer's specific decision about this object wins over anything
        # inherited, matching how the local resolver orders the two.
        from model_bakery import baker

        from epicurrents.permissions import ReadAccessTerms

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            federated_peer=peer,
            remote_user_id="",
            can_read=True,
            apply_middleware=True,
        )
        undo = self._register(check=lambda p, r, o: ReadAccessTerms(granted=True, apply_middleware=False))
        try:
            assert get_federated_read_access_result(peer, "u1", recording).apply_middleware is True
        finally:
            undo()

    def test_extension_returning_none_denies(self, user):
        from model_bakery import baker

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        undo = self._register(check=lambda p, r, o: None)
        try:
            assert get_federated_read_access_result(peer, "u1", recording).granted is False
        finally:
            undo()

    def test_visibility_gate_precedes_extensions(self, user):
        # A gate hides the object from every caller; an extension must not be able
        # to grant past one, as the direct-row path cannot.
        from model_bakery import baker

        from epicurrents.permissions import ReadAccessTerms

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user, status="failed")
        undo = self._register(check=lambda p, r, o: ReadAccessTerms(granted=True))
        try:
            assert get_federated_read_access_result(peer, "u1", recording).granted is False
        finally:
            undo()

    def test_visible_ids_aggregates_registered_extensions(self, user):
        from epicurrents.permissions import get_federated_visible_ids

        peer = self._make_peer(user)
        from epicurrents.permissions import ReadAccessTerms

        undo = self._register(
            visible_terms=lambda p, r, ct: {7: ReadAccessTerms(granted=True), "8": ReadAccessTerms(granted=True)}
        )
        try:
            assert {"7", "8"} <= get_federated_visible_ids(peer, "u1", None)
        finally:
            undo()

    def test_visible_ids_is_empty_without_a_peer(self, user):
        from epicurrents.permissions import ReadAccessTerms, get_federated_visible_ids

        undo = self._register(visible_terms=lambda p, r, ct: {"9": ReadAccessTerms(granted=True)})
        try:
            assert get_federated_visible_ids(None, "u1", None) == set()
        finally:
            undo()

    def test_registration_is_idempotent(self, user):
        from epicurrents import permissions as _perm_mod
        from epicurrents.permissions import register_federated_read_extension

        def check(peer, remote_user_id, obj):
            return None

        def visible_terms(peer, remote_user_id, content_type):
            return {}

        previous = list(_perm_mod._FEDERATED_READ_EXTENSIONS)
        try:
            register_federated_read_extension(check=check, visible_terms=visible_terms)
            register_federated_read_extension(check=check, visible_terms=visible_terms)
            assert len(_perm_mod._FEDERATED_READ_EXTENSIONS) == len(previous) + 1
        finally:
            _perm_mod._FEDERATED_READ_EXTENSIONS[:] = previous


class TestGetFederatedReadAccessResult:
    """Tests for get_federated_read_access_result."""

    def _make_peer(self, user):
        from federation.models import FederatedPeer

        return FederatedPeer.objects.create(
            url="https://peer.example.com",
            display_name="Test Peer",
            public_key="A" * 43,
            is_trusted=True,
            added_by=user,
        )

    def _make_federated_right(self, recording, giver, peer, remote_user_id="u1", **flags):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=giver,
            federated_peer=peer,
            remote_user_id=remote_user_id,
            can_read=True,
            **flags,
        )

    def test_granted_when_right_exists(self, user):
        from model_bakery import baker

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        self._make_federated_right(recording, user, peer)
        result = get_federated_read_access_result(peer, "u1", recording)
        assert result.granted is True

    def test_denied_when_no_right(self, user):
        from model_bakery import baker

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        result = get_federated_read_access_result(peer, "u1", recording)
        assert result.granted is False

    def test_apply_middleware_propagated(self, user):
        from model_bakery import baker

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        self._make_federated_right(recording, user, peer, apply_middleware=True)
        result = get_federated_read_access_result(peer, "u1", recording)
        assert result.granted is True
        assert result.apply_middleware is True

    def test_apply_middleware_false_by_default(self, user):
        from model_bakery import baker

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        self._make_federated_right(recording, user, peer, apply_middleware=False)
        result = get_federated_read_access_result(peer, "u1", recording)
        assert result.apply_middleware is False

    def test_wildcard_remote_user_grants_any_sub(self, user):
        from model_bakery import baker

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        self._make_federated_right(recording, user, peer, remote_user_id="")
        result = get_federated_read_access_result(peer, "any_user_at_all", recording)
        assert result.granted is True

    def test_wrong_remote_user_denied(self, user):
        from model_bakery import baker

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        self._make_federated_right(recording, user, peer, remote_user_id="alice")
        result = get_federated_read_access_result(peer, "bob", recording)
        assert result.granted is False

    def test_expired_right_denied(self, user):
        from django.utils import timezone
        from model_bakery import baker

        peer = self._make_peer(user)
        recording = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            federated_peer=peer,
            remote_user_id="u1",
            can_read=True,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        result = get_federated_read_access_result(peer, "u1", recording)
        assert result.granted is False

    def test_none_peer_denied(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        result = get_federated_read_access_result(None, "u1", recording)
        assert result.granted is False


@pytest.fixture
def gate_registry():
    """Snapshot and restore the module-global visibility-gate registry."""
    from epicurrents import permissions

    saved = {label: list(gates) for label, gates in permissions._READ_VISIBILITY_GATES.items()}
    yield permissions._READ_VISIBILITY_GATES
    permissions._READ_VISIBILITY_GATES.clear()
    permissions._READ_VISIBILITY_GATES.update(saved)


@pytest.mark.django_db
class TestReadVisibilityGates:
    """Registry mechanics of register_read_visibility_gate — tested on Dataset, which has no real gate."""

    def _hide_all(self, user, obj, share_token=None):
        return True

    def _make_dataset_with_grant(self, author, reader):
        from library.models import Dataset

        dataset = Dataset.objects.create(author=author, name="gated")
        ct = ContentType.objects.get_for_model(dataset, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(dataset.pk),
            access_giver=author,
            access_target=reader,
            can_read=True,
        )
        return dataset

    def test_gate_denies_despite_direct_access_right_row(self, user, make_user, gate_registry):
        from epicurrents.permissions import register_read_visibility_gate

        reader = make_user()
        dataset = self._make_dataset_with_grant(user, reader)
        assert can_read_object(reader, dataset) is True
        register_read_visibility_gate("library.dataset", self._hide_all)
        assert can_read_object(reader, dataset) is False

    def test_superuser_fast_path_precedes_gates(self, user, make_superuser, gate_registry):
        from epicurrents.permissions import register_read_visibility_gate
        from library.models import Dataset

        dataset = Dataset.objects.create(author=user, name="gated")
        register_read_visibility_gate("library.dataset", self._hide_all)
        assert can_read_object(make_superuser(), dataset) is True

    def test_gate_for_another_model_is_not_consulted(self, user, make_user, gate_registry):
        from epicurrents.permissions import register_read_visibility_gate

        reader = make_user()
        dataset = self._make_dataset_with_grant(user, reader)
        register_read_visibility_gate("library.collection", self._hide_all)
        assert can_read_object(reader, dataset) is True

    def test_gate_receives_caller_shape(self, user, make_user, gate_registry):
        from epicurrents.permissions import register_read_visibility_gate

        seen = []

        def recorder(user, obj, share_token=None):
            seen.append((user, share_token))
            return False

        reader = make_user()
        dataset = self._make_dataset_with_grant(user, reader)
        register_read_visibility_gate("library.dataset", recorder)
        can_read_object(reader, dataset, share_token="tok")
        assert seen == [(reader, "tok")]

    def test_registration_is_idempotent(self, gate_registry):
        from epicurrents.permissions import _READ_VISIBILITY_GATES, register_read_visibility_gate

        register_read_visibility_gate("library.dataset", self._hide_all)
        register_read_visibility_gate("library.dataset", self._hide_all)
        assert _READ_VISIBILITY_GATES["library.dataset"].count(self._hide_all) == 1


@pytest.mark.django_db
class TestRecordingVisibilityGate:
    """The real gate registered by recordings — FAILED hidden from grantees, trashed hidden from all."""

    def _failed_recording_with_grant(self, author, reader=None, token=None):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=author, status="failed")
        _make_access_right(recording, author, target=author, can_read=True)
        if reader is not None:
            _make_access_right(recording, author, target=reader, can_read=True)
        if token is not None:
            _make_access_right(recording, author, token=token, can_read=True)
        return recording

    def test_grantee_row_does_not_surface_failed_recording(self, user, make_user):
        reader = make_user()
        recording = self._failed_recording_with_grant(user, reader=reader)
        assert can_read_object(reader, recording) is False

    def test_share_token_does_not_surface_failed_recording(self, user):
        recording = self._failed_recording_with_grant(user, token="tok")
        assert can_read_object(None, recording, share_token="tok") is False

    def test_author_row_still_resolves_failed_recording(self, user):
        recording = self._failed_recording_with_grant(user)
        assert can_read_object(user, recording) is True

    def test_superuser_still_sees_failed_recording(self, user, make_superuser):
        recording = self._failed_recording_with_grant(user)
        assert can_read_object(make_superuser(), recording) is True

    def test_trashed_recording_hidden_from_grantee_and_author(self, user, make_user):
        from django.utils import timezone
        from model_bakery import baker

        reader = make_user()
        recording = baker.make("recordings.Recording", author=user, status="ready", deleted_at=timezone.now())
        _make_access_right(recording, user, target=user, can_read=True)
        _make_access_right(recording, user, target=reader, can_read=True)
        assert can_read_object(reader, recording) is False
        assert can_read_object(user, recording) is False

    def test_ready_recording_unaffected(self, user, make_user):
        from model_bakery import baker

        reader = make_user()
        recording = baker.make("recordings.Recording", author=user, status="ready")
        _make_access_right(recording, user, target=reader, can_read=True)
        assert can_read_object(reader, recording) is True

    def test_federated_resolver_consults_the_gate(self, user):
        from model_bakery import baker

        from federation.models import FederatedPeer

        peer = FederatedPeer.objects.create(
            url="https://peer.example.com",
            display_name="Gate Peer",
            public_key="A" * 43,
            is_trusted=True,
            added_by=user,
        )
        recording = baker.make("recordings.Recording", author=user, status="failed")
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            federated_peer=peer,
            remote_user_id="u1",
            can_read=True,
        )
        assert get_federated_read_access_result(peer, "u1", recording).granted is False

    def test_generic_annotations_surface_denies_failed_recording(self, user, make_user, auth_client):
        """The leak the gate closes: the annotations app's list endpoint checks only can_read_object."""
        from django.test import Client

        reader = make_user()
        recording = self._failed_recording_with_grant(user, reader=reader)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        client = Client()
        client.force_login(reader)
        response = client.get(
            "/annotations/api/v1/annotations/",
            {"target_content_type_id": ct.pk, "target_object_id": str(recording.pk)},
        )
        assert response.status_code == 403
