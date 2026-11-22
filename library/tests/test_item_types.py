"""The member type registry, and what the library does for a registered type.

Registration is idempotent for one model under the same terms and refuses a conflict; a type whose model is not
installed is not listed. A registered type is named by its hash alone on an item write, never by primary key; a member
that is not live is left out of a tag listing for a superuser too; a type serving no share token is listed to a
signed-in caller by their own access only, whatever token they present; a member its type withholds under
``apply_middleware`` is listed on the terms the caller reads that member by, and always to the dataset's author; a
row names the pass its type declares; and a dataset holding a member of a type nothing registers cannot be
release-gated.

Tested on ``Dataset`` registered as a member type, so the mechanics are seen apart from any plugin's model.
"""

from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from django.utils import timezone
from model_bakery import baker

from conftest import post_json
from library import item_types
from library.item_types import ItemType, register_item_type, registered_item_types
from library.models import Dataset, DatasetItem, Tag, TaggedItem
from library.release import gate_refusal
from library.tests.test_release import _add, _grant

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
TAGS = "/api/v1/library/tags"


def _type(**overrides):
    fields = {
        "object_type": "casefile",
        "model_label": "library.dataset",
        "hash_field": "object_hash",
        "display_name": lambda obj: obj.name,
        "live": lambda qs: qs.filter(deleted_at__isnull=True),
        "normalise_hash": lambda value: value.strip().upper(),
        "serves_share_tokens": False,
    }
    fields.update(overrides)
    return ItemType(**fields)


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(item_types, "_TYPES", {})


@pytest.fixture
def casefile(clean_registry):
    """``Dataset`` registered as a member type that serves no share token."""
    register_item_type(_type())
    return ContentType.objects.get_for_model(Dataset, for_concrete_model=False)


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _owned_dataset(author, name="holder"):
    dataset = Dataset.objects.create(author=author, name=name)
    _grant(dataset, author, target=author, can_write=True, can_share=True)
    return dataset


class TestRegistration:
    def test_the_same_type_registered_again_is_harmless(self, clean_registry):
        first = _type()
        register_item_type(first)
        # A second ready() builds its lambdas anew, so the two never compare equal as dataclasses.
        register_item_type(_type())
        assert registered_item_types() == [first]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"object_type": "other"},
            {"hash_field": "name"},
            {"release_gateable": True},
            {"serves_share_tokens": True},
            {"withheld_under_middleware": lambda qs: qs.none()},
        ],
    )
    def test_a_model_registered_again_under_other_terms_is_refused(self, clean_registry, overrides):
        register_item_type(_type())
        with pytest.raises(ValueError):
            register_item_type(_type(**overrides))

    def test_a_type_whose_model_is_not_installed_is_not_listed(self, clean_registry):
        register_item_type(_type(model_label="absent_plugin.thing"))
        register_item_type(_type())
        assert [t.model_label for t in registered_item_types()] == ["library.dataset"]


class TestHashOnly:
    def test_a_registered_type_is_added_by_its_hash(self, user, casefile):
        holder, member = _owned_dataset(user), _owned_dataset(user, "member")
        response = post_json(
            _client(user),
            f"{DATASETS}/{holder.object_hash}/items/",
            {"content_type_id": casefile.pk, "object_id": member.object_hash.lower()},
        )
        assert response.status_code == 201, response.content
        assert response.json()["object_hash"] == member.object_hash

    def test_a_primary_key_names_nothing_whether_or_not_it_is_live(self, user, make_user, casefile):
        holder = _owned_dataset(user)
        mine, foreign = _owned_dataset(user, "mine"), Dataset.objects.create(author=make_user(), name="foreign")
        client = _client(user)
        # The caller's own, somebody else's and no object at all answer alike, so the answer counts nothing.
        for key in (mine.pk, foreign.pk, foreign.pk + 1000):
            response = post_json(
                client,
                f"{DATASETS}/{holder.object_hash}/items/",
                {"content_type_id": casefile.pk, "object_id": str(key)},
            )
            assert response.status_code == 404
        assert not DatasetItem.objects.filter(dataset=holder).exists()


class TestTagListing:
    def test_a_trashed_member_is_not_listed_to_a_superuser(self, user, make_superuser, casefile):
        live, trashed = _owned_dataset(user, "live"), _owned_dataset(user, "trashed")
        tag = baker.make(Tag, author=user, name="cases")
        for member in (live, trashed):
            TaggedItem.objects.create(tag=tag, content_type=casefile, object_id=str(member.pk))
        Dataset.objects.filter(pk=trashed.pk).update(deleted_at=timezone.now())
        rows = _client(make_superuser()).get(f"{TAGS}/{tag.pk}/items/").json()
        assert [row["object_hash"] for row in rows] == [live.object_hash]


class TestTokenlessTypeInADatasetListing:
    """A type that serves no share token is listed by the session's own access, never through a presented token."""

    TOKEN = "T" * 32

    def _shared(self, author):
        holder, member = _owned_dataset(author), _owned_dataset(author, "member")
        _add(holder, member)
        _grant(holder, author, token=self.TOKEN)
        return holder, member

    def _listed(self, client, holder, **query):
        response = client.get(f"{DATASETS}/{holder.object_hash}/items/", query)
        assert response.status_code == 200, response.content
        return [row["object_hash"] for row in response.json()]

    def test_not_listed_to_a_token_reader_without_an_account(self, user, casefile):
        holder, _member = self._shared(user)
        assert self._listed(Client(), holder, share_token=self.TOKEN) == []

    def test_not_listed_to_a_signed_in_reader_who_holds_only_the_token(self, user, make_user, casefile):
        holder, _member = self._shared(user)
        assert self._listed(_client(make_user()), holder, share_token=self.TOKEN) == []

    def test_listed_to_a_signed_in_reader_with_access_of_their_own(self, user, make_user, casefile):
        holder, member = self._shared(user)
        reader = make_user()
        _grant(holder, user, target=reader)
        assert self._listed(_client(reader), holder, share_token=self.TOKEN) == [member.object_hash]
        assert self._listed(_client(reader), holder) == [member.object_hash]

    def test_the_author_presenting_a_token_still_reads_their_members(self, user, casefile):
        holder, member = self._shared(user)
        assert self._listed(_client(user), holder, share_token=self.TOKEN) == [member.object_hash]

    def test_a_type_that_serves_tokens_is_listed_through_one(self, user, make_user, clean_registry):
        register_item_type(_type(serves_share_tokens=True))
        holder, member = self._shared(user)
        assert self._listed(_client(make_user()), holder, share_token=self.TOKEN) == [member.object_hash]


class TestWithheldUnderMiddleware:
    """A member the type withholds under ``apply_middleware`` is listed as the per-object check would serve it."""

    RAW = "as uploaded"

    @pytest.fixture
    def withholding(self, clean_registry, monkeypatch):
        """``Dataset`` as a member type that withholds the members described as raw, with the matching refusal."""
        from epicurrents import permissions

        register_item_type(
            _type(
                serves_share_tokens=True,
                withheld_under_middleware=lambda qs: qs.filter(description=self.RAW),
                platform_pass=lambda obj: "none" if obj.description == self.RAW else "test-pass",
            )
        )
        monkeypatch.setattr(
            permissions, "_TERMS_REFUSALS", {"library.dataset": [lambda obj: obj.description == self.RAW]}
        )
        return ContentType.objects.get_for_model(Dataset, for_concrete_model=False)

    def _holder(self, author, member_author=None):
        """A dataset of *author*'s holding one raw and one passed member, both written by *member_author*."""
        holder = _owned_dataset(author)
        writer = member_author or author
        raw = Dataset.objects.create(author=writer, name="raw", description=self.RAW)
        passed = Dataset.objects.create(author=writer, name="passed")
        _add(holder, raw)
        _add(holder, passed)
        return holder, raw, passed

    def _listed(self, client, holder, **query) -> set[str]:
        response = client.get(f"{DATASETS}/{holder.object_hash}/items/", query)
        assert response.status_code == 200, response.content
        return {row["object_name"] for row in response.json()}

    def _agrees(self, reader, holder, members) -> set[str]:
        """What *reader* is listed, having checked each member against the per-object answer."""
        from epicurrents.permissions import can_read_object

        listed = self._listed(_client(reader), holder)
        assert listed == {member.name for member in members if can_read_object(reader, member)}
        return listed

    def test_a_de_identifying_reader_loses_the_raw_member(self, user, make_user, withholding):
        holder, raw, passed = self._holder(user)
        reader = make_user()
        _grant(holder, user, target=reader, apply_middleware=True)
        assert self._agrees(reader, holder, [raw, passed]) == {"passed"}

    def test_a_raw_reader_keeps_it(self, user, make_user, withholding):
        holder, raw, passed = self._holder(user)
        reader = make_user()
        _grant(holder, user, target=reader, apply_middleware=False)
        assert self._agrees(reader, holder, [raw, passed]) == {"raw", "passed"}

    def test_a_de_identifying_reader_keeps_a_member_a_raw_grant_of_their_own_reaches(
        self, user, make_user, withholding
    ):
        holder, raw, passed = self._holder(user)
        reader = make_user()
        _grant(holder, user, target=reader, apply_middleware=True)
        _grant(raw, user, target=reader, apply_middleware=False)
        assert self._agrees(reader, holder, [raw, passed]) == {"raw", "passed"}

    def test_a_raw_reader_loses_a_member_their_own_direct_row_de_identifies(self, user, make_user, withholding):
        holder, raw, passed = self._holder(user)
        reader = make_user()
        _grant(holder, user, target=reader, apply_middleware=False)
        _grant(raw, user, target=reader, apply_middleware=True)
        assert self._agrees(reader, holder, [raw, passed]) == {"passed"}

    def test_a_direct_user_row_outranks_a_group_row_as_in_the_per_object_check(self, user, make_user, withholding):
        from django.contrib.auth.models import Group

        holder, raw, passed = self._holder(user)
        reader = make_user()
        group = Group.objects.create(name="class")
        reader.groups.add(group)
        _grant(holder, user, target=reader, apply_middleware=True)
        _grant(raw, user, group=group, apply_middleware=True)
        _grant(raw, user, target=reader, apply_middleware=False)
        assert self._agrees(reader, holder, [raw, passed]) == {"raw", "passed"}

    def test_a_de_identifying_group_row_outranks_a_raw_one(self, user, make_user, withholding):
        from django.contrib.auth.models import Group

        holder, raw, passed = self._holder(user)
        reader = make_user()
        for name, apply_middleware in (("raw readers", False), ("class", True)):
            group = Group.objects.create(name=name)
            reader.groups.add(group)
            _grant(raw, user, group=group, apply_middleware=apply_middleware)
        _grant(holder, user, target=reader, apply_middleware=False)
        assert self._agrees(reader, holder, [raw, passed]) == {"passed"}

    def test_an_expired_raw_grant_reaches_nothing(self, user, make_user, withholding):
        from datetime import timedelta

        holder, raw, passed = self._holder(user)
        reader = make_user()
        _grant(holder, user, target=reader, apply_middleware=True)
        _grant(raw, user, target=reader, apply_middleware=False, expires_at=timezone.now() - timedelta(days=1))
        assert self._agrees(reader, holder, [raw, passed]) == {"passed"}

    def test_the_callers_own_member_is_listed_under_de_identifying_terms(self, user, make_user, withholding):
        reader = make_user()
        holder, raw, passed = self._holder(user, member_author=reader)
        _grant(holder, user, target=reader, apply_middleware=True)
        assert self._agrees(reader, holder, [raw, passed]) == {"raw", "passed"}

    def test_the_dataset_author_is_listed_their_own_raw_member(self, user, withholding):
        """The review reproduction: the author's own row on the dataset carries ``apply_middleware`` at its default."""
        holder, raw, passed = self._holder(user)
        assert self._agrees(user, holder, [raw, passed]) == {"raw", "passed"}

    def test_the_dataset_author_is_listed_a_raw_member_someone_else_wrote(self, user, make_user, withholding):
        """Reached through a raw grant of their own, or through nothing they could open it by: listed either way,
        with the keys that remove it."""
        other = make_user()
        holder, raw, _passed = self._holder(user, member_author=other)
        assert self._listed(_client(user), holder) == {"raw", "passed"}
        _grant(raw, other, target=user, apply_middleware=False)
        client = _client(user)
        rows = client.get(f"{DATASETS}/{holder.object_hash}/items/").json()
        row = next(row for row in rows if row["object_name"] == "raw")
        assert row["id"] is not None
        assert client.delete(f"{DATASETS}/{holder.object_hash}/items/{row['id']}/").status_code == 200
        assert self._listed(client, holder) == {"passed"}

    def test_a_superuser_is_listed_every_member(self, user, make_superuser, withholding):
        holder, _raw, _passed = self._holder(user)
        assert self._listed(_client(make_superuser()), holder) == {"raw", "passed"}

    def test_a_write_grantee_on_de_identifying_terms_still_loses_it(self, user, make_user, withholding):
        holder, raw, passed = self._holder(user)
        manager = make_user()
        _grant(holder, user, target=manager, can_write=True, apply_middleware=True)
        assert self._agrees(manager, holder, [raw, passed]) == {"passed"}

    def test_a_token_reader_is_decided_by_the_token_row(self, user, withholding):
        holder, _raw, _passed = self._holder(user)
        _grant(holder, user, token="D" * 32, apply_middleware=True)
        _grant(holder, user, token="R" * 32, apply_middleware=False)
        assert self._listed(Client(), holder, share_token="D" * 32) == {"passed"}
        assert self._listed(Client(), holder, share_token="R" * 32) == {"raw", "passed"}

    def test_the_decision_costs_no_query_per_member(self, user, make_user, withholding, django_assert_max_num_queries):
        holder = _owned_dataset(user)
        reader = make_user()
        _grant(holder, user, target=reader, apply_middleware=True)
        for index in range(3):
            _add(holder, Dataset.objects.create(author=user, name=f"raw {index}", description=self.RAW))
        client = _client(reader)
        with django_assert_max_num_queries(200) as few:
            client.get(f"{DATASETS}/{holder.object_hash}/items/")
        for index in range(3, 23):
            _add(holder, Dataset.objects.create(author=user, name=f"raw {index}", description=self.RAW))
        with django_assert_max_num_queries(len(few.captured_queries)):
            client.get(f"{DATASETS}/{holder.object_hash}/items/")

    def test_a_row_names_the_pass_its_type_declares(self, user, withholding):
        holder, _raw, _passed = self._holder(user)
        rows = _client(user).get(f"{DATASETS}/{holder.object_hash}/items/").json()
        assert {row["object_name"]: row["platform_pass"] for row in rows} == {"raw": "none", "passed": "test-pass"}

    def test_a_type_declaring_no_pass_and_a_recording_are_listed_with_null(self, user, casefile):
        from library.tests.test_release import _recording

        holder = _owned_dataset(user)
        _add(holder, _owned_dataset(user, "member"))
        _add(holder, _recording(user))
        rows = _client(user).get(f"{DATASETS}/{holder.object_hash}/items/").json()
        assert {row["object_type"]: row["platform_pass"] for row in rows} == {"casefile": None, "recording": None}


class TestGateRefusal:
    def test_a_member_of_a_type_nobody_registers_refuses_the_gate(self, user, clean_registry):
        """The rows of a disabled plugin: the type is gone from the registry and its members are still there."""
        holder = Dataset.objects.create(author=user, name="holder")
        _add(holder, Dataset.objects.create(author=user, name="orphan member"))
        assert gate_refusal(holder) == (
            "A dataset holding a member of a type the release gate does not cover cannot be release-gated."
        )

    def test_a_registered_ungateable_member_refuses_the_gate_by_name(self, user, casefile):
        holder = Dataset.objects.create(author=user, name="holder")
        _add(holder, Dataset.objects.create(author=user, name="member"))
        assert gate_refusal(holder) == "A dataset holding a casefile member cannot be release-gated."

    def test_a_registered_gateable_member_leaves_the_gate_open(self, user, clean_registry):
        register_item_type(_type(release_gateable=True))
        holder = Dataset.objects.create(author=user, name="holder")
        _add(holder, Dataset.objects.create(author=user, name="member"))
        assert gate_refusal(holder) is None

    def test_a_dataset_with_no_member_can_be_gated(self, user, clean_registry):
        assert gate_refusal(Dataset.objects.create(author=user, name="empty")) is None
