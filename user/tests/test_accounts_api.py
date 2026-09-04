"""Contract tests for the account and group administration surface.

The surface exists because the Django admin produced no audit trail, so the
tests that matter most here are not the CRUD happy paths — they are:

- every write lands on the hash chain, group membership included, which is the
  one operation the signals cannot see;
- the staff / superuser split holds on every endpoint;
- the deployment cannot be locked out of its own account administration;
- a group carrying live access grants cannot be deleted out from under them.
"""

import json

import pytest
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType

from activity.derived_state import verify_derived_state
from activity.models import Activity, ObjectChangeLog
from conftest import delete_json, patch_json, post_json

ACCOUNTS = "/api/v1/user/admin/accounts"
GROUPS = "/api/v1/user/admin/groups"
ROLES = "/api/v1/user/admin/roles"


def put_json(client, url, data):
    """PUT JSON data and return the response."""
    return client.put(url, json.dumps(data), content_type="application/json")


@pytest.fixture
def su_client(superuser_client):
    """Superuser client, unpacked from conftest's ``(client, user)`` tuple."""
    return superuser_client[0]


@pytest.fixture
def plain_client(auth_client):
    """Ordinary-user client, unpacked from conftest's ``(client, user)`` tuple."""
    return auth_client[0]


def _changes_for(user):
    """Every ObjectChangeLog row targeting *user*."""
    content_type = ContentType.objects.get_for_model(user, for_concrete_model=False)
    return ObjectChangeLog.objects.filter(content_type=content_type, object_id=str(user.pk))


@pytest.mark.django_db
class TestAccessControl:
    """Staff reads, superuser writes. Checked per endpoint rather than once,
    because the guard is a call at the top of each function and omitting it is
    invisible until somebody exercises that one route."""

    def test_anonymous_is_rejected_everywhere(self, client, user):
        assert client.get(ACCOUNTS).status_code == 401
        assert client.get(f"{ACCOUNTS}/{user.pk}").status_code == 401
        assert client.get(GROUPS).status_code == 401
        assert client.get(ROLES).status_code == 401
        assert post_json(client, ACCOUNTS, {"username": "x", "password": "y"}).status_code == 401
        assert post_json(client, GROUPS, {"name": "x"}).status_code == 401

    def test_ordinary_user_is_rejected_everywhere(self, plain_client, user):
        assert plain_client.get(ACCOUNTS).status_code == 403
        assert plain_client.get(f"{ACCOUNTS}/{user.pk}").status_code == 403
        assert plain_client.get(GROUPS).status_code == 403
        assert plain_client.get(ROLES).status_code == 403
        assert post_json(plain_client, ACCOUNTS, {"username": "x", "password": "y"}).status_code == 403

    def test_staff_reads_but_cannot_write(self, client, make_user, user):
        staff = make_user(username="staffer", password="pw")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        client.force_login(staff)

        assert client.get(ACCOUNTS).status_code == 200
        assert client.get(f"{ACCOUNTS}/{user.pk}").status_code == 200
        assert client.get(GROUPS).status_code == 200
        assert client.get(ROLES).status_code == 200

        assert post_json(client, ACCOUNTS, {"username": "new", "password": "pw"}).status_code == 403
        assert patch_json(client, f"{ACCOUNTS}/{user.pk}", {"first_name": "X"}).status_code == 403
        assert post_json(client, f"{ACCOUNTS}/{user.pk}/password", {"new_password": "x"}).status_code == 403
        assert put_json(client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": []}).status_code == 403
        assert post_json(client, GROUPS, {"name": "New"}).status_code == 403

    def test_superuser_writes(self, su_client, user):
        assert patch_json(su_client, f"{ACCOUNTS}/{user.pk}", {"first_name": "Edited"}).status_code == 200


@pytest.mark.django_db
class TestNoUserDeletion:
    def test_there_is_no_delete_route_for_an_account(self, su_client, user):
        """erase_user is the sanctioned path — it unlinks owned recording and
        media files, which FK cascade never does. A CRUD delete here would strand
        PHI on disk exactly the way the admin's delete button does."""
        response = delete_json(su_client, f"{ACCOUNTS}/{user.pk}")
        assert response.status_code in (404, 405)


@pytest.mark.django_db
class TestAccountCreation:
    def test_creates_and_audits(self, su_client):
        response = post_json(
            su_client,
            ACCOUNTS,
            {"username": "newbie", "password": "Str0ng-Passphrase-42", "email": "n@example.com"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == "newbie"
        assert body["is_active"] is True

        from django.contrib.auth import get_user_model

        account = get_user_model().objects.get(username="newbie")
        assert account.check_password("Str0ng-Passphrase-42")
        assert _changes_for(account).filter(action=ObjectChangeLog.ACTION_CREATE).exists()
        assert Activity.objects.filter(verb="user.account.create").exists()

    def test_password_faces_the_deployment_validators(self, su_client):
        """Otherwise this endpoint becomes the way around AUTH_PASSWORD_VALIDATORS
        — a lower bar for an operator-set password than a user's own."""
        response = post_json(su_client, ACCOUNTS, {"username": "weak", "password": "123"})
        assert response.status_code == 400

    def test_duplicate_username_is_refused(self, su_client, user):
        response = post_json(su_client, ACCOUNTS, {"username": user.username, "password": "Str0ng-Pass-42"})
        assert response.status_code == 409

    def test_duplicate_username_is_refused_case_insensitively(self, su_client, user):
        response = post_json(su_client, ACCOUNTS, {"username": user.username.upper(), "password": "Str0ng-Pass-42"})
        assert response.status_code == 409

    def test_invalid_email_is_refused(self, su_client):
        response = post_json(
            su_client, ACCOUNTS, {"username": "bad", "password": "Str0ng-Pass-42", "email": "not-an-address"}
        )
        assert response.status_code == 400

    def test_a_rejected_password_leaves_no_account(self, su_client):
        from django.contrib.auth import get_user_model

        post_json(su_client, ACCOUNTS, {"username": "ghost", "password": "1"})
        assert not get_user_model().objects.filter(username="ghost").exists()


@pytest.mark.django_db
class TestAccountUpdate:
    def test_edits_fields_and_audits(self, su_client, user):
        response = patch_json(su_client, f"{ACCOUNTS}/{user.pk}", {"first_name": "Ada", "is_staff": True})
        assert response.status_code == 200
        user.refresh_from_db()
        assert user.first_name == "Ada"
        assert user.is_staff is True
        assert _changes_for(user).filter(action=ObjectChangeLog.ACTION_MODIFY).exists()

    def test_username_is_not_editable(self, su_client, user):
        original = user.username
        patch_json(su_client, f"{ACCOUNTS}/{user.pk}", {"username": "renamed"})
        user.refresh_from_db()
        assert user.username == original

    def test_unknown_account_is_404(self, su_client):
        assert patch_json(su_client, f"{ACCOUNTS}/99999", {"first_name": "X"}).status_code == 404


@pytest.mark.django_db
class TestLastSuperuserGuard:
    """The write half of this surface is superuser-only, so demoting or
    deactivating the last one locks every operator out of it. Recovery is a
    management command on the host, which needs shell access the operator may
    not have at the moment they need it."""

    def test_cannot_demote_the_last_superuser(self, su_client, superuser):
        response = patch_json(su_client, f"{ACCOUNTS}/{superuser.pk}", {"is_superuser": False})
        assert response.status_code == 409
        superuser.refresh_from_db()
        assert superuser.is_superuser is True

    def test_cannot_deactivate_the_last_superuser(self, su_client, superuser):
        response = patch_json(su_client, f"{ACCOUNTS}/{superuser.pk}", {"is_active": False})
        assert response.status_code == 409
        superuser.refresh_from_db()
        assert superuser.is_active is True

    def test_can_demote_when_another_active_superuser_remains(self, su_client, superuser, make_superuser):
        make_superuser(username="second_root")
        response = patch_json(su_client, f"{ACCOUNTS}/{superuser.pk}", {"is_superuser": False})
        assert response.status_code == 200

    def test_an_inactive_second_superuser_does_not_count(self, su_client, superuser, make_superuser):
        """A deactivated account cannot log in, so it is not a way back in."""
        spare = make_superuser(username="dormant_root")
        spare.is_active = False
        spare.save(update_fields=["is_active"])
        response = patch_json(su_client, f"{ACCOUNTS}/{superuser.pk}", {"is_superuser": False})
        assert response.status_code == 409

    def test_editing_an_ordinary_account_is_unaffected(self, su_client, user):
        assert patch_json(su_client, f"{ACCOUNTS}/{user.pk}", {"is_active": False}).status_code == 200


@pytest.mark.django_db
class TestSetPassword:
    def test_sets_and_audits(self, su_client, user):
        response = post_json(su_client, f"{ACCOUNTS}/{user.pk}/password", {"new_password": "An0ther-Str0ng-Pass"})
        assert response.status_code == 200
        user.refresh_from_db()
        assert user.check_password("An0ther-Str0ng-Pass")
        assert Activity.objects.filter(verb="user.account.password.set").exists()

    def test_validators_apply(self, su_client, user):
        assert post_json(su_client, f"{ACCOUNTS}/{user.pk}/password", {"new_password": "1"}).status_code == 400

    def test_the_hash_never_reaches_the_audit_payload(self, su_client, user):
        """user/apps.py masks `password`; this asserts the masking survives a
        write through this endpoint rather than trusting the registration."""
        post_json(su_client, f"{ACCOUNTS}/{user.pk}/password", {"new_password": "An0ther-Str0ng-Pass"})
        user.refresh_from_db()
        for change in _changes_for(user):
            assert user.password not in json.dumps(change.before_state)
            assert user.password not in json.dumps(change.changes or {})


@pytest.mark.django_db
class TestGroupMembershipIsAudited:
    """`groups.set()` fires m2m_changed, which activity/signals.py does not
    receive, and the M2M rows are not concrete fields — so without the explicit
    recorder this operation writes nothing to the chain."""

    def test_setting_groups_writes_a_change_row(self, su_client, user):
        group = Group.objects.create(name="Reviewers")
        before = _changes_for(user).count()
        response = put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group.pk]})
        assert response.status_code == 200
        assert _changes_for(user).count() == before + 1

    def test_the_row_carries_a_verifiable_membership_digest(self, su_client, user):
        group = Group.objects.create(name="Reviewers")
        put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group.pk]})
        change = _changes_for(user).latest("created_at")
        result = verify_derived_state(change)
        assert result.ok, result.digests

    def test_tampering_with_membership_afterwards_is_detectable(self, su_client, user):
        """The point of the digest. A direct edit of the M2M table leaves the
        stored digest describing a membership that no longer exists."""
        group = Group.objects.create(name="Reviewers")
        other = Group.objects.create(name="Smuggled")
        put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group.pk]})
        change = _changes_for(user).latest("created_at")

        user.groups.add(other)  # straight to the M2M table, no audit row
        result = verify_derived_state(change)
        assert not result.ok
        assert "mismatch" in result.digests.values()

    def test_renaming_a_group_does_not_look_like_tampering(self, su_client, user):
        """The digest is over primary keys precisely so a rename — which this
        surface offers — cannot invalidate every historical membership row."""
        group = Group.objects.create(name="Reviewers")
        put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group.pk]})
        change = _changes_for(user).latest("created_at")

        patch_json(su_client, f"{GROUPS}/{group.pk}", {"name": "Renamed"})
        assert verify_derived_state(change).ok

    def test_membership_is_replaced_not_merged(self, su_client, user):
        first = Group.objects.create(name="First")
        second = Group.objects.create(name="Second")
        put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [first.pk]})
        put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [second.pk]})
        assert sorted(user.groups.values_list("name", flat=True)) == ["Second"]

    def test_unknown_group_is_404_and_changes_nothing(self, su_client, user):
        response = put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [99999]})
        assert response.status_code == 404
        assert user.groups.count() == 0

    def test_setting_members_from_the_group_side_audits_each_account(self, su_client, make_user):
        """One row per affected account, not one for the group: erase_subject
        reaches audit rows through their target, so a single row targeting the
        group would put one account's membership history beyond the reach of
        every other account's erasure request."""
        group = Group.objects.create(name="Reviewers")
        first = make_user(username="member_a", password="pw")
        second = make_user(username="member_b", password="pw")

        response = put_json(su_client, f"{GROUPS}/{group.pk}/members", {"user_ids": [first.pk, second.pk]})
        assert response.status_code == 200
        assert response.json()["member_count"] == 2
        assert _changes_for(first).filter(action=ObjectChangeLog.ACTION_MODIFY).exists()
        assert _changes_for(second).filter(action=ObjectChangeLog.ACTION_MODIFY).exists()

    def test_unaffected_members_get_no_spurious_row(self, su_client, make_user):
        group = Group.objects.create(name="Reviewers")
        staying = make_user(username="stays", password="pw")
        joining = make_user(username="joins", password="pw")
        staying.groups.add(group)
        baseline = _changes_for(staying).count()

        put_json(su_client, f"{GROUPS}/{group.pk}/members", {"user_ids": [staying.pk, joining.pk]})
        assert _changes_for(staying).count() == baseline
        assert _changes_for(joining).filter(action=ObjectChangeLog.ACTION_MODIFY).exists()


@pytest.mark.django_db
class TestGroups:
    def test_create_rename_and_counts(self, su_client, user):
        created = post_json(su_client, GROUPS, {"name": "Reviewers"})
        assert created.status_code == 201
        group_id = created.json()["id"]

        put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group_id]})
        listing = su_client.get(GROUPS).json()
        entry = next(item for item in listing if item["id"] == group_id)
        assert entry["member_count"] == 1
        assert entry["grant_count"] == 0

        renamed = patch_json(su_client, f"{GROUPS}/{group_id}", {"name": "Assessors"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Assessors"

    def test_group_writes_reach_the_hash_chain(self, su_client):
        """Group is not in activity.signals.EXCLUDED_MODELS, so create / rename /
        delete are covered by the ordinary receivers. Asserted rather than
        assumed — the coverage comes from a set defined in another app."""
        group_ct = ContentType.objects.get_for_model(Group)
        created = post_json(su_client, GROUPS, {"name": "Audited"})
        group_id = created.json()["id"]
        rows = ObjectChangeLog.objects.filter(content_type=group_ct, object_id=str(group_id))
        assert rows.filter(action=ObjectChangeLog.ACTION_CREATE).exists()

        patch_json(su_client, f"{GROUPS}/{group_id}", {"name": "Audited2"})
        assert rows.filter(action=ObjectChangeLog.ACTION_MODIFY).exists()

        delete_json(su_client, f"{GROUPS}/{group_id}")
        assert rows.filter(action=ObjectChangeLog.ACTION_DELETE).exists()

    def test_duplicate_group_name_is_refused(self, su_client):
        post_json(su_client, GROUPS, {"name": "Reviewers"})
        assert post_json(su_client, GROUPS, {"name": "reviewers"}).status_code == 409

    def test_blank_name_is_refused(self, su_client):
        assert post_json(su_client, GROUPS, {"name": "   "}).status_code == 400

    def test_delete_removes_an_unused_group(self, su_client):
        group = Group.objects.create(name="Disposable")
        assert delete_json(su_client, f"{GROUPS}/{group.pk}").status_code == 200
        assert not Group.objects.filter(pk=group.pk).exists()

    def test_delete_is_refused_while_grants_target_the_group(self, su_client, user):
        """Deleting would cascade the AccessRight rows away, revoking access for
        everyone in the group at once and leaving no record of what was withdrawn."""
        from epicurrents.models import AccessRight

        group = Group.objects.create(name="Granted")
        # The grant's target object is irrelevant here — what blocks the delete
        # is that a row names the group.
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(user),
            object_id=str(user.pk),
            access_target_group=group,
            access_giver=user,
        )

        response = delete_json(su_client, f"{GROUPS}/{group.pk}")
        assert response.status_code == 409
        assert "1 access grant" in response.json()["detail"]
        assert Group.objects.filter(pk=group.pk).exists()


@pytest.mark.django_db
class TestListing:
    def test_inactive_accounts_are_included(self, su_client, make_user):
        """Unlike /search — an operator's first question about a failed login is
        usually whether the account is still active."""
        dormant = make_user(username="dormant", password="pw")
        dormant.is_active = False
        dormant.save(update_fields=["is_active"])
        names = [item["username"] for item in su_client.get(ACCOUNTS).json()]
        assert "dormant" in names

    def test_filters_by_term(self, su_client, make_user):
        make_user(username="findable_one", password="pw")
        make_user(username="unrelated_two", password="pw")
        names = [item["username"] for item in su_client.get(f"{ACCOUNTS}?q=findable").json()]
        assert names == ["findable_one"]

    def test_limit_is_bounded(self, su_client):
        assert su_client.get(f"{ACCOUNTS}?limit=100000").status_code == 400
        assert su_client.get(f"{ACCOUNTS}?limit=0").status_code == 400
        assert su_client.get(f"{ACCOUNTS}?offset=-1").status_code == 400


@pytest.mark.django_db
class TestRoleHook:
    """Core must not import a project to read or write its roles. These drive the
    registry directly rather than depending on which project is active in the
    test run. Roles ride on groups: writes land on ``PATCH /groups/{id}``
    and an account's roles are derived from its membership."""

    def _register(self, store):
        from user.roles import RoleProvider, register_role_provider

        register_role_provider(
            RoleProvider(
                key="test_role",
                label="Test role",
                choices=(("captain", "Captain"),),
                read_groups=lambda groups: {g.pk: store[g.pk] for g in groups if g.pk in store},
                write_group=lambda group, value: (
                    store.pop(group.pk, None) if value is None else store.__setitem__(group.pk, value)
                ),
            )
        )

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from user.roles import clear_role_providers, get_role_providers

        saved = get_role_providers()
        yield
        clear_role_providers()
        from user.roles import register_role_provider

        for provider in saved:
            register_role_provider(provider)

    def _group(self, name="Crew"):
        return Group.objects.create(name=name)

    def test_registered_roles_are_listed(self, su_client):
        self._register({})
        listing = su_client.get(ROLES).json()
        entry = next(item for item in listing if item["key"] == "test_role")
        assert entry["label"] == "Test role"
        assert ["captain", "Captain"] in entry["choices"]

    def test_a_role_is_written_onto_a_group_through_the_provider(self, su_client):
        store = {}
        self._register(store)
        group = self._group()
        response = patch_json(su_client, f"{GROUPS}/{group.pk}", {"roles": {"test_role": "captain"}})
        assert response.status_code == 200
        assert store[group.pk] == "captain"
        assert response.json()["roles"]["test_role"] == "captain"

    def test_a_null_value_clears_the_role(self, su_client):
        group = self._group()
        store = {group.pk: "captain"}
        self._register(store)
        response = patch_json(su_client, f"{GROUPS}/{group.pk}", {"roles": {"test_role": None}})
        assert response.status_code == 200
        assert group.pk not in store
        assert response.json()["roles"]["test_role"] is None

    def test_an_unknown_role_is_refused(self, su_client):
        self._register({})
        group = self._group()
        response = patch_json(su_client, f"{GROUPS}/{group.pk}", {"roles": {"nonexistent": "x"}})
        assert response.status_code == 400

    def test_a_value_outside_the_declared_choices_is_refused(self, su_client):
        self._register({})
        group = self._group()
        response = patch_json(su_client, f"{GROUPS}/{group.pk}", {"roles": {"test_role": "admiral"}})
        assert response.status_code == 400

    def test_an_account_inherits_roles_from_its_groups(self, su_client, user):
        group = self._group()
        store = {group.pk: "captain"}
        self._register(store)
        user.groups.add(group)
        response = su_client.get(f"{ACCOUNTS}/{user.pk}")
        assert response.status_code == 200
        assert response.json()["roles"]["test_role"] == ["captain"]

    def test_two_groups_carrying_the_same_role_read_as_one(self, su_client, user):
        first, second = self._group("First"), self._group("Second")
        store = {first.pk: "captain", second.pk: "captain"}
        self._register(store)
        user.groups.add(first, second)
        response = su_client.get(f"{ACCOUNTS}/{user.pk}")
        assert response.json()["roles"]["test_role"] == ["captain"]

    def test_the_group_listing_carries_the_roles(self, su_client):
        group = self._group()
        store = {group.pk: "captain"}
        self._register(store)
        listing = su_client.get(GROUPS).json()
        entry = next(item for item in listing if item["id"] == group.pk)
        assert entry["roles"]["test_role"] == "captain"
        bare = Group.objects.create(name="Bare")
        listing = su_client.get(GROUPS).json()
        entry = next(item for item in listing if item["id"] == bare.pk)
        assert entry["roles"]["test_role"] is None

    def test_a_provider_that_raises_on_read_does_not_break_the_listing(self, su_client, user):
        from user.roles import RoleProvider, register_role_provider

        def _explode(_groups):
            raise RuntimeError("project table not migrated")

        register_role_provider(
            RoleProvider(
                key="broken",
                label="Broken",
                choices=(("x", "X"),),
                read_groups=_explode,
                write_group=lambda g, v: None,
            )
        )
        user.groups.add(self._group())
        response = su_client.get(f"{ACCOUNTS}/{user.pk}")
        assert response.status_code == 200
        assert response.json()["roles"]["broken"] == []
        listing = su_client.get(GROUPS).json()
        assert all(entry["roles"]["broken"] is None for entry in listing)


class TestResponseFreshness:
    """The membership endpoints load the account with ``prefetch_related`` and
    serialize the same instance after writing it. That is safe — Django's related
    manager drops the prefetch cache in ``set()`` — but it is safe by virtue of
    someone else's implementation detail, so the outcome is pinned here rather
    than left resting on it.
    """

    def test_the_group_response_reflects_the_new_membership(self, su_client, user):
        group = Group.objects.create(name="Reviewers")
        response = put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group.pk]})
        assert [g["name"] for g in response.json()["groups"]] == ["Reviewers"]

    def test_clearing_membership_is_reflected_too(self, su_client, user):
        group = Group.objects.create(name="Reviewers")
        put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": [group.pk]})
        response = put_json(su_client, f"{ACCOUNTS}/{user.pk}/groups", {"group_ids": []})
        assert response.json()["groups"] == []
