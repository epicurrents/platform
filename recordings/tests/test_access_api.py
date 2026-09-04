"""Contract tests for recording access-right management.

Before these endpoints existed, a recording's grants were written once at upload
and could never be listed, changed or withdrawn through the API — sharing a
recording with the wrong person was permanent unless the grant happened to carry
an ``expires_at``. The Django admin was the only way to undo it, and it is being
retired.

The properties that matter:

- Only the data owner manages access, matching the collection and dataset rule.
  Not staff: who may see a recording is the author's call.
- A FAILED recording answers 404, so the access list cannot be used to discover
  that a failed upload exists.
- The author's own self-grant cannot be revoked. Reading resolves through
  ``AccessRight`` with no author fast-path, so deleting it would leave the author
  unable to read a recording they can still rename and delete.
"""

import pytest
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType

from activity.models import Activity
from conftest import delete_json
from epicurrents.models import AccessRight
from recordings.models import Recording

HASH = "ABCDEF1234567890ABCDEF1234567890"


def _make_recording(user, **kwargs):
    defaults = {
        "author": user,
        "original_name": "test.edf",
        "stored_name": f"{HASH}.edf",
        "file_extension": ".edf",
        "file_size": 1024,
        "file_path": "/tmp/test.edf",
        "file_hash": "a" * 64,
        "content_hash": "b" * 64,
        "status": Recording.Status.READY,
    }
    defaults.update(kwargs)
    return Recording.objects.create(**defaults)


def _grant(recording, giver, **kwargs):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        **kwargs,
    )


def _url(recording_hash=HASH, right_id=None):
    """Both routes carry the library convention's trailing slash.

    Not cosmetic: APPEND_SLASH will not rescue a DELETE, so a client that copies
    the collection / dataset call shape and lands on a slash-less route gets a
    404 with nothing to explain it.
    """
    base = f"/recordings/api/v1/{recording_hash}/access/"
    return base if right_id is None else f"{base}{right_id}/"


@pytest.fixture
def owner_client(auth_client):
    return auth_client[0]


@pytest.fixture
def owner(auth_client):
    return auth_client[1]


@pytest.mark.django_db
class TestWhoMayManageAccess:
    def test_anonymous_is_rejected(self, client, user):
        _make_recording(user)
        assert client.get(_url()).status_code == 401

    def test_the_author_may(self, owner_client, owner):
        _make_recording(owner)
        assert owner_client.get(_url()).status_code == 200

    def test_a_superuser_may(self, superuser_client, user):
        _make_recording(user)
        assert superuser_client[0].get(_url()).status_code == 200

    def test_a_plain_grantee_may_not(self, client, user, make_user):
        recording = _make_recording(user)
        grantee = make_user(username="reader", password="pw")
        _grant(recording, user, access_target=grantee, can_read=True)
        client.force_login(grantee)
        assert client.get(_url()).status_code == 403

    def test_a_can_share_grantee_may(self, client, user, make_user):
        recording = _make_recording(user)
        sharer = make_user(username="sharer", password="pw")
        _grant(recording, user, access_target=sharer, can_read=True, can_share=True)
        client.force_login(sharer)
        assert client.get(_url()).status_code == 200

    def test_a_can_share_grant_via_a_group_counts(self, client, user, make_user):
        recording = _make_recording(user)
        group = Group.objects.create(name="Sharers")
        member = make_user(username="grouped", password="pw")
        member.groups.add(group)
        _grant(recording, user, access_target_group=group, can_read=True, can_share=True)
        client.force_login(member)
        assert client.get(_url()).status_code == 200

    def test_staff_without_a_grant_may_not(self, client, user, make_user):
        """Deliberately not a staff surface — who may see a recording is the
        author's decision, not an operator's."""
        _make_recording(user)
        staff = make_user(username="nosy_staff", password="pw")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        client.force_login(staff)
        assert client.get(_url()).status_code == 403

    def test_an_unrelated_user_may_not(self, client, user, make_user):
        _make_recording(user)
        stranger = make_user(username="stranger", password="pw")
        client.force_login(stranger)
        assert client.get(_url()).status_code == 403


@pytest.mark.django_db
class TestFailedRecordingsStayHidden:
    def test_a_grantee_with_can_share_gets_404_on_a_failed_recording(self, client, user, make_user):
        """Answering 403 or 200 here would confirm a failed upload exists, which
        is what the FAILED-hidden rule withholds."""
        recording = _make_recording(user, status=Recording.Status.FAILED)
        sharer = make_user(username="failshare", password="pw")
        _grant(recording, user, access_target=sharer, can_read=True, can_share=True)
        client.force_login(sharer)
        assert client.get(_url()).status_code == 404

    def test_the_author_still_sees_it(self, owner_client, owner):
        _make_recording(owner, status=Recording.Status.FAILED)
        assert owner_client.get(_url()).status_code == 200


@pytest.mark.django_db
class TestListing:
    def test_lists_every_grant_kind(self, owner_client, owner, make_user):
        recording = _make_recording(owner)
        other = make_user(username="listed", password="pw")
        group = Group.objects.create(name="Listed")
        _grant(recording, owner, access_target=other, can_read=True)
        _grant(recording, owner, access_target_group=group, can_read=True)
        _grant(recording, owner, public_share_token="tok-123", can_read=True)

        body = owner_client.get(_url()).json()
        assert len(body) == 3
        assert {row["access_target_username"] for row in body} == {"listed", None}
        assert {row["access_target_group_name"] for row in body} == {"Listed", None}
        assert "tok-123" in {row["public_share_token"] for row in body}

    def test_the_listing_is_audited(self, owner_client, owner):
        _make_recording(owner)
        owner_client.get(_url())
        activity = Activity.objects.filter(verb="recordings.access.list").latest("created_at")
        assert activity.metadata["returned_count"] == 0

    def test_an_unknown_hash_is_404(self, owner_client, owner):
        _make_recording(owner)
        assert owner_client.get(_url("0" * 32)).status_code == 404

    def test_a_malformed_hash_is_400(self, owner_client, owner):
        _make_recording(owner)
        assert owner_client.get(_url("short")).status_code == 400

    def test_a_soft_deleted_recording_is_404(self, owner_client, owner):
        from django.utils import timezone

        _make_recording(owner, deleted_at=timezone.now())
        assert owner_client.get(_url()).status_code == 404


@pytest.mark.django_db
class TestRevoke:
    def test_revokes_and_audits(self, owner_client, owner, make_user):
        recording = _make_recording(owner)
        other = make_user(username="revokee", password="pw")
        right = _grant(recording, owner, access_target=other, can_read=True)

        response = delete_json(owner_client, _url(right_id=right.pk))
        assert response.status_code == 200
        assert not AccessRight.objects.filter(pk=right.pk).exists()
        activity = Activity.objects.filter(verb="recordings.access.revoke").latest("created_at")
        assert activity.metadata["target_kind"] == "user"

    def test_revokes_a_group_grant(self, owner_client, owner):
        recording = _make_recording(owner)
        group = Group.objects.create(name="Revoked")
        right = _grant(recording, owner, access_target_group=group, can_read=True)
        assert delete_json(owner_client, _url(right_id=right.pk)).status_code == 200
        assert not AccessRight.objects.filter(pk=right.pk).exists()

    def test_revokes_a_share_token(self, owner_client, owner):
        recording = _make_recording(owner)
        right = _grant(recording, owner, public_share_token="tok-abc", can_read=True)
        response = delete_json(owner_client, _url(right_id=right.pk))
        assert response.status_code == 200
        activity = Activity.objects.filter(verb="recordings.access.revoke").latest("created_at")
        assert activity.metadata["target_kind"] == "share_token"

    def test_the_authors_own_grant_cannot_be_revoked(self, owner_client, owner):
        """Reading resolves through AccessRight with no author fast-path, so this
        row is the author's only read access. Revoking it would leave them able to
        rename and delete the recording but not read it, with no way back."""
        recording = _make_recording(owner)
        self_grant = _grant(recording, owner, access_target=owner, can_read=True, can_write=True, can_share=True)

        response = delete_json(owner_client, _url(right_id=self_grant.pk))
        assert response.status_code == 409
        assert AccessRight.objects.filter(pk=self_grant.pk).exists()

    def test_a_superuser_cannot_revoke_it_either(self, superuser_client, user):
        """The hazard is about the author losing access, not about who asked."""
        recording = _make_recording(user)
        self_grant = _grant(recording, user, access_target=user, can_read=True)
        assert delete_json(superuser_client[0], _url(right_id=self_grant.pk)).status_code == 409

    def test_a_grant_on_another_recording_is_404(self, owner_client, owner):
        _make_recording(owner)
        other_recording = _make_recording(owner, stored_name="F" * 32 + ".edf", content_hash="c" * 64)
        foreign = _grant(other_recording, owner, public_share_token="tok-other", can_read=True)
        response = delete_json(owner_client, _url(right_id=foreign.pk))
        assert response.status_code == 404
        assert AccessRight.objects.filter(pk=foreign.pk).exists()

    def test_a_plain_grantee_cannot_revoke(self, client, user, make_user):
        recording = _make_recording(user)
        grantee = make_user(username="norevoke", password="pw")
        right = _grant(recording, user, access_target=grantee, can_read=True)
        client.force_login(grantee)
        assert delete_json(client, _url(right_id=right.pk)).status_code == 403
        assert AccessRight.objects.filter(pk=right.pk).exists()

    def test_an_unknown_right_is_404(self, owner_client, owner):
        _make_recording(owner)
        assert delete_json(owner_client, _url(right_id=999999)).status_code == 404

    def test_the_revocation_lands_on_the_hash_chain(self, owner_client, owner, make_user):
        """AccessRight is not in activity.signals.EXCLUDED_MODELS, so pre_delete
        covers this. Asserted because withdrawing access is exactly the kind of
        act the audit trail exists to make reconstructable."""
        from activity.models import ObjectChangeLog

        recording = _make_recording(owner)
        other = make_user(username="chained", password="pw")
        right = _grant(recording, owner, access_target=other, can_read=True)
        right_ct = ContentType.objects.get_for_model(right, for_concrete_model=False)

        delete_json(owner_client, _url(right_id=right.pk))
        assert ObjectChangeLog.objects.filter(
            content_type=right_ct,
            object_id=str(right.pk),
            action=ObjectChangeLog.ACTION_DELETE,
        ).exists()

    def test_the_matching_route_shape_is_the_one_with_a_trailing_slash(self, owner_client, owner, make_user):
        """Pins the convention the URL helper documents.

        The slash-less shape 404s rather than deleting anything. It used to
        answer 200 with index.html from the SPA catch-all, so a caller with a
        mis-copied revoke URL read success while the grant survived; that is
        fixed in epicurrents/urls.py, and this asserts the surviving half — the
        wrong shape still does not revoke.
        """
        recording = _make_recording(owner)
        other = make_user(username="slashy", password="pw")
        right = _grant(recording, owner, access_target=other, can_read=True)

        response = delete_json(owner_client, f"/recordings/api/v1/{HASH}/access/{right.pk}")
        assert response.status_code == 404
        assert AccessRight.objects.filter(pk=right.pk).exists()
