"""Affiliations: groups that record where their members belong and grant nothing.

The properties that matter are the ones that keep an affiliation from becoming a permission: no access grant can
target it, it stays out of the grant-target listings, only a role scoped to affiliations can land on it, and a
rollback cannot turn it into an ordinary group. The rest is the account surface treating it as a group: created and
deleted there, its membership set there, and its name shown to the account holder.
"""

import json

import pytest
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from activity.audit import rollback_refusal
from activity.models import ObjectChangeLog
from conftest import delete_json, patch_json, post_json
from epicurrents.models import AccessRight
from library.models import Dataset
from user.affiliations import AFFILIATION_KIND, affiliation_names
from user.models import Affiliation
from user.roles import (
    RoleNotApplicable,
    RoleProvider,
    clear_role_providers,
    get_role_providers,
    register_role_provider,
    write_group_role,
)

ACCOUNTS = "/api/v1/user/admin/accounts"
GROUPS = "/api/v1/user/admin/groups"
ROLES = "/api/v1/user/admin/roles"
STEP = {"password": "adminpass123"}


@pytest.fixture
def su_client(superuser_client):
    return superuser_client[0]


@pytest.fixture
def affiliation(su_client):
    created = post_json(su_client, GROUPS, {"name": "Northern Hospital", "affiliation": True})
    assert created.status_code == 201, created.content
    return Group.objects.get(pk=created.json()["id"])


@pytest.fixture
def scoped_roles():
    """Two test providers, one for ordinary groups and one scoped to affiliations, each writing into its store."""
    saved = get_role_providers()
    clear_role_providers()
    stores = {"plain": {}, "scoped": {}}

    def _provider(key, store, kind):
        return RoleProvider(
            key=key,
            label=key.title(),
            choices=(("yes", "Yes"),),
            read_groups=lambda groups: {g.pk: store[g.pk] for g in groups if g.pk in store},
            write_group=lambda group, value: (
                store.pop(group.pk, None) if value is None else store.__setitem__(group.pk, value)
            ),
            group_kind=kind,
        )

    register_role_provider(_provider("plain", stores["plain"], None))
    register_role_provider(_provider("scoped", stores["scoped"], AFFILIATION_KIND))
    yield stores
    clear_role_providers()
    for provider in saved:
        register_role_provider(provider)


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
class TestCreation:
    def test_an_affiliation_is_a_dedicated_group_of_its_own(self, su_client, affiliation):
        assert Affiliation.objects.filter(group=affiliation).exists()
        entry = next(row for row in su_client.get(GROUPS).json() if row["id"] == affiliation.pk)
        assert entry["dedicated_to"] == {"kind": AFFILIATION_KIND, "object_hash": "", "name": "Northern Hospital"}

    def test_a_plain_group_is_no_affiliation(self, su_client):
        created = post_json(su_client, GROUPS, {"name": "Readers"})
        assert created.json()["dedicated_to"] is None
        assert not Affiliation.objects.exists()

    def test_the_marker_reaches_the_hash_chain(self, affiliation):
        content_type = ContentType.objects.get_for_model(Affiliation)
        assert ObjectChangeLog.objects.filter(content_type=content_type, action=ObjectChangeLog.ACTION_CREATE).exists()

    def test_deleting_it_removes_the_marker(self, su_client, affiliation):
        assert delete_json(su_client, f"{GROUPS}/{affiliation.pk}").status_code == 200
        assert not Group.objects.filter(pk=affiliation.pk).exists()
        assert not Affiliation.objects.exists()


@pytest.mark.django_db
class TestGrantsNothing:
    def test_no_access_grant_targets_it(self, make_user, affiliation):
        author = make_user()
        dataset = Dataset.objects.create(author=author, name="Shared")
        response = _client(author).post(
            f"/api/v1/library/datasets/{dataset.object_hash}/access/",
            json.dumps({"access_target_group_id": affiliation.pk, "can_read": True}),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert not AccessRight.objects.filter(access_target_group=affiliation).exists()

    def test_left_out_of_the_grant_target_listing(self, user, affiliation):
        Group.objects.create(name="Readers")
        names = [row["name"] for row in _client(user).get("/api/v1/user/groups").json()]
        assert "Readers" in names
        assert affiliation.name not in names


@pytest.mark.django_db
class TestScopedRoles:
    def test_the_role_listing_carries_the_scope(self, su_client, scoped_roles):
        kinds = {row["key"]: row["group_kind"] for row in su_client.get(ROLES).json()}
        assert kinds == {"plain": None, "scoped": AFFILIATION_KIND}

    def test_a_scoped_role_is_written_on_an_affiliation(self, su_client, scoped_roles, affiliation):
        response = patch_json(su_client, f"{GROUPS}/{affiliation.pk}", {"roles": {"scoped": "yes"}, **STEP})
        assert response.status_code == 200, response.content
        assert scoped_roles["scoped"] == {affiliation.pk: "yes"}

    def test_an_unscoped_role_is_refused_on_an_affiliation(self, su_client, scoped_roles, affiliation):
        response = patch_json(su_client, f"{GROUPS}/{affiliation.pk}", {"roles": {"plain": "yes"}, **STEP})
        assert response.status_code == 409
        assert scoped_roles["plain"] == {}

    def test_a_scoped_role_is_refused_on_an_ordinary_group(self, su_client, scoped_roles):
        group = Group.objects.create(name="Readers")
        response = patch_json(su_client, f"{GROUPS}/{group.pk}", {"roles": {"scoped": "yes"}, **STEP})
        assert response.status_code == 409
        assert scoped_roles["scoped"] == {}

    def test_the_refusal_comes_before_step_up(self, su_client, scoped_roles, affiliation):
        response = patch_json(su_client, f"{GROUPS}/{affiliation.pk}", {"roles": {"plain": "yes"}})
        assert response.status_code == 409

    def test_clearing_is_allowed_on_any_group(self, su_client, scoped_roles, affiliation):
        scoped_roles["plain"][affiliation.pk] = "yes"
        response = patch_json(su_client, f"{GROUPS}/{affiliation.pk}", {"roles": {"plain": None}})
        assert response.status_code == 200
        assert scoped_roles["plain"] == {}

    def test_the_registry_refuses_a_misplaced_write_itself(self, scoped_roles, affiliation):
        with pytest.raises(RoleNotApplicable):
            write_group_role(affiliation, "plain", "yes")
        with pytest.raises(RoleNotApplicable):
            write_group_role(Group.objects.create(name="Readers"), "scoped", "yes")

    def test_a_pool_group_still_takes_no_role(self, scoped_roles):
        from user.dedicated_groups import _RESOLVERS, DedicatedGroup, register_dedicated_group_resolver

        group = Group.objects.create(name="Pool")
        register_dedicated_group_resolver("test_kind", lambda ids: {group.pk: DedicatedGroup("test_kind", "", "Pool")})
        try:
            for key in ("plain", "scoped"):
                with pytest.raises(RoleNotApplicable):
                    write_group_role(group, key, "yes")
        finally:
            _RESOLVERS.pop("test_kind")


@pytest.mark.django_db
class TestRollback:
    def _changes(self):
        return ObjectChangeLog.objects.filter(content_type=ContentType.objects.get_for_model(Affiliation))

    def test_no_creation_or_deletion_of_the_marker_is_rolled_back(self, su_client, affiliation):
        delete_json(su_client, f"{GROUPS}/{affiliation.pk}")
        actions = set(self._changes().values_list("action", flat=True))
        assert {ObjectChangeLog.ACTION_CREATE, ObjectChangeLog.ACTION_DELETE} <= actions
        for change in self._changes():
            assert rollback_refusal(change) is not None


@pytest.mark.django_db
class TestVisibility:
    def test_the_holder_sees_their_affiliations(self, su_client, affiliation, make_user):
        member = make_user()
        response = su_client.put(
            f"{ACCOUNTS}/{member.pk}/groups",
            json.dumps({"group_ids": [affiliation.pk], **STEP}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert _client(member).get("/api/v1/user/me").json()["user"]["affiliations"] == ["Northern Hospital"]

    def test_an_ordinary_group_is_no_affiliation(self, user):
        user.groups.add(Group.objects.create(name="Readers"))
        assert affiliation_names(user) == []
