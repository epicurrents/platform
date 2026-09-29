"""Tag reach per caller class, and the staff-only creation policy."""

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from django.utils import timezone
from model_bakery import baker

from epicurrents.models import AccessRight
from library.models import Dataset, DatasetItem, Tag, TaggedItem
from library.tag_scope import visible_tag_ids
from recordings.models import Recording

TAGS = "/api/v1/library/tags/"


def _client_for(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _post(client, url, body):
    return client.post(url, json.dumps(body), content_type="application/json")


def _patch(client, url, body):
    return client.patch(url, json.dumps(body), content_type="application/json")


def _recording(user, **kwargs):
    return baker.make(Recording, author=user, file_size=1, status=Recording.Status.READY, **kwargs)


def _recording_ct():
    return ContentType.objects.get_for_model(Recording, for_concrete_model=False)


def _tag(recording, tag):
    return TaggedItem.objects.create(tag=tag, content_type=_recording_ct(), object_id=str(recording.pk))


def _grant_read(recording, user):
    AccessRight.objects.create(
        content_type=_recording_ct(),
        object_id=str(recording.pk),
        access_giver=recording.author,
        access_target=user,
        can_read=True,
    )


def _ids(response) -> set[int]:
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()}


@pytest.fixture
def world(make_user, make_superuser):
    """Two users, a staff member, a superuser and a small tag tree.

    ``alice`` owns a private tag with a child, and a recording tagged with the
    child. ``bob`` owns a private tag of his own and holds a read grant on
    Alice's recording. ``carol`` holds nothing. ``staff`` created the curated
    tag.
    """
    staff = make_user(username="tagstaff")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    alice = make_user(username="alice")
    bob = make_user(username="bob")
    carol = make_user(username="carol")
    superuser = make_superuser()

    curated = baker.make(Tag, author=staff, name="Curated", curated=True)
    alice_root = baker.make(Tag, author=alice, name="Alice root")
    alice_child = baker.make(Tag, author=alice, name="Alice child", parent=alice_root)
    bob_root = baker.make(Tag, author=bob, name="Bob root")

    recording = _recording(alice)
    _tag(recording, alice_child)
    _grant_read(recording, bob)

    return {
        "staff": staff,
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "superuser": superuser,
        "curated": curated,
        "alice_root": alice_root,
        "alice_child": alice_child,
        "bob_root": bob_root,
        "recording": recording,
    }


@pytest.mark.django_db
class TestReach:
    def test_superuser_is_unrestricted(self, world):
        assert visible_tag_ids(world["superuser"]) is None
        listed = _ids(_client_for(world["superuser"]).get(TAGS))
        assert listed == {world["curated"].pk, world["alice_root"].pk, world["bob_root"].pk}

    def test_author_reaches_curated_and_own(self, world):
        listed = _ids(_client_for(world["alice"]).get(TAGS))
        assert listed == {world["curated"].pk, world["alice_root"].pk}

    def test_grantee_reaches_the_tag_on_the_readable_object_and_its_ancestors(self, world):
        client = _client_for(world["bob"])
        assert _ids(client.get(TAGS)) == {world["curated"].pk, world["alice_root"].pk, world["bob_root"].pk}
        assert _ids(client.get(TAGS, {"parent_id": world["alice_root"].pk})) == {world["alice_child"].pk}

    def test_stranger_reaches_only_curated(self, world):
        assert _ids(_client_for(world["carol"]).get(TAGS)) == {world["curated"].pk}

    def test_dataset_membership_conveys_reach(self, world):
        dataset = baker.make(Dataset, author=world["alice"])
        DatasetItem.objects.create(dataset=dataset, content_type=_recording_ct(), object_id=str(world["recording"].pk))
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(Dataset, for_concrete_model=False),
            object_id=str(dataset.pk),
            access_giver=world["alice"],
            access_target=world["carol"],
            can_read=True,
        )
        assert visible_tag_ids(world["carol"]) == {
            world["curated"].pk,
            world["alice_root"].pk,
            world["alice_child"].pk,
        }

    def test_trashed_object_conveys_nothing(self, world):
        world["recording"].deleted_at = timezone.now()
        world["recording"].save(update_fields=["deleted_at"])
        assert visible_tag_ids(world["bob"]) == {world["curated"].pk, world["bob_root"].pk}

    def test_revoked_grant_withdraws_reach(self, world):
        AccessRight.objects.filter(access_target=world["bob"]).delete()
        assert visible_tag_ids(world["bob"]) == {world["curated"].pk, world["bob_root"].pk}


@pytest.mark.django_db
class TestUnreachableTagsAreNotFound:
    """The same 404 as a missing tag, so the id space cannot be walked for names."""

    def test_detail(self, world):
        assert _client_for(world["carol"]).get(f"{TAGS}{world['alice_root'].pk}/").status_code == 404

    def test_children_listing(self, world):
        assert _client_for(world["carol"]).get(TAGS, {"parent_id": world["alice_root"].pk}).status_code == 404

    def test_items_listing(self, world):
        assert _client_for(world["carol"]).get(f"{TAGS}{world['alice_child'].pk}/items/").status_code == 404

    def test_applying_an_unreachable_tag(self, world):
        own = _recording(world["carol"])
        resp = _post(
            _client_for(world["carol"]),
            f"{TAGS}{world['alice_root'].pk}/items/",
            {"content_type_id": _recording_ct().pk, "object_id": str(own.pk)},
        )
        assert resp.status_code == 404
        assert not TaggedItem.objects.filter(tag=world["alice_root"]).exists()

    def test_applying_a_reachable_tag_still_works(self, world):
        own = _recording(world["carol"])
        resp = _post(
            _client_for(world["carol"]),
            f"{TAGS}{world['curated'].pk}/items/",
            {"content_type_id": _recording_ct().pk, "object_id": str(own.pk)},
        )
        assert resp.status_code == 201

    def test_creating_under_an_unreachable_parent(self, world, settings):
        settings.LIBRARY_TAG_CREATION_REQUIRES_STAFF = False
        resp = _post(_client_for(world["carol"]), TAGS, {"name": "Mine", "parent_id": world["alice_root"].pk})
        assert resp.status_code == 404

    def test_reparenting_under_an_unreachable_parent(self, world):
        resp = _patch(
            _client_for(world["bob"]),
            f"{TAGS}{world['bob_root'].pk}/",
            {"parent_id": world["curated"].pk},
        )
        assert resp.status_code == 200
        resp = _patch(
            _client_for(world["bob"]),
            f"{TAGS}{world['bob_root'].pk}/",
            {"parent_id": baker.make(Tag, author=world["alice"], name="Hidden").pk},
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestCreationPolicy:
    def test_default_reserves_creation_for_staff(self, world, settings):
        assert settings.LIBRARY_TAG_CREATION_REQUIRES_STAFF is True
        resp = _post(_client_for(world["alice"]), TAGS, {"name": "Mine"})
        assert resp.status_code == 403
        assert not Tag.objects.filter(name="Mine").exists()

    def test_staff_created_tag_is_curated(self, world):
        resp = _post(_client_for(world["staff"]), TAGS, {"name": "Vocabulary"})
        assert resp.status_code == 201
        assert resp.json()["curated"] is True
        assert Tag.objects.get(name="Vocabulary").curated is True

    def test_superuser_created_tag_is_curated(self, world):
        resp = _post(_client_for(world["superuser"]), TAGS, {"name": "Vocabulary"})
        assert resp.status_code == 201
        assert resp.json()["curated"] is True

    def test_setting_off_lets_anyone_create_an_uncurated_tag(self, world, settings):
        settings.LIBRARY_TAG_CREATION_REQUIRES_STAFF = False
        resp = _post(_client_for(world["alice"]), TAGS, {"name": "Mine"})
        assert resp.status_code == 201
        assert resp.json()["curated"] is False
        # Listed to its author, not to a stranger.
        assert Tag.objects.get(name="Mine").pk in visible_tag_ids(world["alice"])
        assert Tag.objects.get(name="Mine").pk not in visible_tag_ids(world["carol"])

    def test_curated_is_stamped_not_derived(self, world):
        """Demoting the author later does not hide the vocabulary they created."""
        world["staff"].is_staff = False
        world["staff"].save(update_fields=["is_staff"])
        assert world["curated"].pk in visible_tag_ids(world["carol"])
