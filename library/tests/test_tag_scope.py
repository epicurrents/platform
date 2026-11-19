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
class TestSearch:
    def test_matches_names_at_every_level_within_reach(self, world):
        client = _client_for(world["bob"])
        assert _ids(client.get(TAGS, {"q": "ROOT"})) == {world["alice_root"].pk, world["bob_root"].pk}
        assert _ids(client.get(TAGS, {"q": "child"})) == {world["alice_child"].pk}

    def test_leaves_out_what_is_out_of_reach(self, world):
        assert _ids(_client_for(world["carol"]).get(TAGS, {"q": "r"})) == {world["curated"].pk}

    def test_narrows_to_the_children_of_a_parent(self, world):
        client = _client_for(world["alice"])
        assert _ids(client.get(TAGS, {"q": "child", "parent_id": world["alice_root"].pk})) == {world["alice_child"].pk}
        assert _ids(client.get(TAGS, {"q": "root", "parent_id": world["alice_root"].pk})) == set()

    def test_a_blank_search_lists_the_roots(self, world):
        assert _ids(_client_for(world["alice"]).get(TAGS, {"q": "  "})) == {world["curated"].pk, world["alice_root"].pk}

    def test_the_audit_row_does_not_carry_the_search(self, world):
        from activity.models import Activity

        _client_for(world["alice"]).get(TAGS, {"q": "Alice"})
        row = Activity.objects.filter(verb="library.tag.list").latest("pk")
        assert row.metadata["searched"] is True
        assert "Alice" not in json.dumps(row.metadata)


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


def _hashed(user, index, **kwargs):
    return _recording(user, stored_name=f"{index:032X}.edf", **kwargs)


def _hash_of(recording) -> str:
    return recording.stored_name.split(".", 1)[0]


@pytest.mark.django_db
class TestTagsOnObjects:
    def test_order_is_curated_then_own_then_others_by_name(self, make_user):
        from library.tag_scope import tags_on_objects

        staff, reader, other = make_user(), make_user(), make_user()
        recording = _recording(reader)
        for author, name, curated in [
            (other, "a other", False),
            (reader, "b own", False),
            (staff, "z curated", True),
            (reader, "A own", False),
            (staff, "c curated", True),
        ]:
            _tag(recording, baker.make(Tag, author=author, name=name, curated=curated))
        tags = tags_on_objects(reader, _recording_ct(), [recording.pk])[str(recording.pk)]
        assert [tag.name for tag in tags] == ["c curated", "z curated", "A own", "b own", "a other"]

    def test_an_unreadable_object_and_an_untagged_one_are_both_absent(self, make_user):
        from library.tag_scope import tags_on_objects

        owner, reader = make_user(), make_user()
        unreadable, untagged = _recording(owner), _recording(reader)
        _tag(unreadable, baker.make(Tag, author=owner, name="curated", curated=True))
        assert tags_on_objects(reader, _recording_ct(), [unreadable.pk, untagged.pk]) == {}

    def test_a_readable_object_shows_another_users_private_tag(self, make_user):
        from library.tag_scope import tags_on_objects

        owner, reader = make_user(), make_user()
        recording = _recording(owner)
        _grant_read(recording, reader)
        _tag(recording, baker.make(Tag, author=owner, name="private"))
        assert [tag.name for tag in tags_on_objects(reader, _recording_ct(), [recording.pk])[str(recording.pk)]] == [
            "private"
        ]

    def test_a_failed_recording_of_another_author_is_not_read(self, make_user):
        from library.tag_scope import tags_on_objects

        owner, reader = make_user(), make_user()
        recording = _recording(owner)
        _grant_read(recording, reader)
        Recording.objects.filter(pk=recording.pk).update(status=Recording.Status.FAILED)
        _tag(recording, baker.make(Tag, author=owner, name="curated", curated=True))
        assert tags_on_objects(reader, _recording_ct(), [recording.pk]) == {}


@pytest.mark.django_db
class TestRecordingTagsEndpoint:
    URL = "/api/v1/library/recording-tags/"

    def _get(self, user, *hashes):
        return _client_for(user).get(self.URL, {"hash": list(hashes)})

    def test_lists_the_tags_of_readable_recordings_only(self, make_user):
        owner, reader = make_user(), make_user()
        readable, unreadable = _hashed(owner, 1), _hashed(owner, 2)
        _grant_read(readable, reader)
        curated = baker.make(Tag, author=owner, name="curated", curated=True)
        _tag(readable, curated)
        _tag(unreadable, curated)
        response = self._get(reader, _hash_of(readable), _hash_of(unreadable), "F" * 32, "not-a-hash")
        assert response.status_code == 200, response.content
        assert response.json() == [
            {"hash": _hash_of(readable), "tags": [{"id": curated.pk, "name": "curated", "curated": True}]}
        ]

    def test_a_trashed_recording_is_left_out(self, make_user):
        owner = make_user()
        recording = _hashed(owner, 3, deleted_at=timezone.now())
        _tag(recording, baker.make(Tag, author=owner, name="mine"))
        assert self._get(owner, _hash_of(recording)).json() == []

    def test_a_hidden_member_of_a_gated_dataset_is_left_out(self, make_user):
        from library.tests.test_release import _gated_dataset
        from library.tests.test_release import _grant as _object_grant

        manager, reader = make_user(), make_user()
        recording = _hashed(manager, 4)
        dataset, _ = _gated_dataset(manager, recording)
        _object_grant(dataset, manager, target=reader)
        _tag(recording, baker.make(Tag, author=manager, name="curated", curated=True))
        assert self._get(reader, _hash_of(recording)).json() == []
        assert [row["hash"] for row in self._get(manager, _hash_of(recording)).json()] == [_hash_of(recording)]

    def test_requires_an_account(self, make_user):
        assert Client().get(self.URL, {"hash": ["F" * 32]}).status_code == 401

    def test_refuses_too_many_hashes(self, make_user):
        assert self._get(make_user(), *(f"{i:032X}" for i in range(201))).status_code == 400

    def test_audit_row_counts_and_names_nothing(self, make_user):
        from activity.models import Activity

        owner = make_user()
        recording = _hashed(owner, 5)
        _tag(recording, baker.make(Tag, author=owner, name="mine"))
        self._get(owner, _hash_of(recording), "F" * 32)
        row = Activity.objects.filter(verb="library.tag.recording.list").latest("pk")
        assert row.metadata == {"requested_count": 2, "returned_count": 1}
