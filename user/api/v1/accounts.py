"""Account and group administration, replacing the Django admin's user surface.

Mounted at ``/api/v1/user/admin/``. Everything here lives under ``/api/v1/``
deliberately: the path matches ``_API_PATH_RE``, so every request gets an
``Activity`` row and every model write inside it produces an ``ObjectChangeLog``
entry on the hash chain. That is the whole reason this exists — the same
operations through ``/admin/`` produced neither.

Two rules shape the surface:

- **Staff reads, superuser writes**, per the staff-vs-superuser tier in
  AGENTS.md. A staff account can see the roster and diagnose an access problem;
  changing who someone is takes superuser.
- **No user deletion.** ``erase_user`` is the sanctioned path — it unlinks owned
  recording and media files, flushes sessions, deletes the account and scrubs
  the audit trail. A plain CRUD delete would leave stranded PHI files on disk,
  which is what the admin's delete button does today.

Group membership is written through the explicit audit recorders rather than
left to the signals. ``user.groups.set()`` emits ``m2m_changed``, which
``activity/signals.py`` does not listen for, and the M2M table is not among the
user's concrete fields — so without an explicit ``record_modify_change``, the
single most consequential operation here would be the one that left no trace.
The resulting membership rides on the row's hash as a recomputable digest; see
user/audit_digests.py.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Q
from ninja import Router, Schema
from ninja.errors import HttpError

from activity.audit import log_activity, record_modify_change, serialize_instance
from epicurrents.auth import enforce_session_csrf
from epicurrents.models import AccessRight
from epicurrents.security_log import get_client_ip, log_security_event
from user.audit_digests import GROUP_MEMBERSHIP_DIGEST_KEY, compute_group_membership_digest
from user.roles import get_role_providers, read_group_roles, read_roles, write_group_role
from user.two_factor import active_credential

router = Router()

#: Upper bound on a roster page. The account list is an operator tool on a
#: deployment with a bounded user count, not a public listing.
_MAX_PAGE = 500


class GroupRef(Schema):
    """A group as it appears inside an account payload."""

    id: int
    name: str


class AccountOut(Schema):
    """One account, as the administration surface sees it.

    Carries the fields the admin's user form carried, plus group membership and
    any project-supplied roles. ``email`` is here where ``UserSearchOut``
    withholds it: this endpoint requires staff, and an operator resetting an
    account needs to see which address it belongs to.
    """

    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool
    is_staff: bool
    is_superuser: bool
    is_2fa_enabled: bool
    date_joined: str
    last_login: str | None
    groups: list[GroupRef]
    roles: dict[str, list[str]]


class AccountCreateIn(Schema):
    """New-account payload. Everything but ``username`` and ``password`` optional."""

    username: str
    password: str
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    is_active: bool = True
    is_staff: bool = False
    is_superuser: bool = False


class AccountUpdateIn(Schema):
    """Partial account edit. Omitted fields are left alone.

    Roles are not written here: a role belongs to a group, so assigning one is
    a membership change (``PUT /accounts/{id}/groups``) or a group edit
    (``PATCH /groups/{id}``), never a per-account field.
    """

    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    is_active: bool | None = None
    is_staff: bool | None = None
    is_superuser: bool | None = None


class SetPasswordIn(Schema):
    """Operator-set password for another account."""

    new_password: str


class GroupMembershipIn(Schema):
    """Replacement membership, from either direction."""

    group_ids: list[int] | None = None
    user_ids: list[int] | None = None


class GroupIn(Schema):
    """Group create payload."""

    name: str


class GroupUpdateIn(Schema):
    """Partial group edit. Omitted fields are left alone.

    ``roles`` is a partial map: an absent key is untouched, so a client that
    does not know a project's role exists cannot clear it. An explicit ``null``
    value clears that role.
    """

    name: str | None = None
    roles: dict[str, str | None] | None = None


class GroupDetailOut(Schema):
    """A group with its project roles and the two counts that decide whether it can be deleted."""

    id: int
    name: str
    member_count: int
    grant_count: int
    roles: dict[str, str | None]


class RoleProviderOut(Schema):
    """A project-supplied role and the values it accepts."""

    key: str
    label: str
    choices: list[list[str]]


def _require_auth(request):
    """Return the authenticated user or raise 401.

    Routes the request through the session-CSRF chokepoint; see AGENTS.md →
    *Session-authenticated write CSRF*.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Not authenticated")
    enforce_session_csrf(request)
    return user


def _require_staff(request):
    """Return an authenticated staff (or superuser) user or raise 403."""
    user = _require_auth(request)
    if not (user.is_staff or user.is_superuser):
        raise HttpError(403, "Staff access required.")
    return user


def _require_superuser(request):
    """Return an authenticated superuser or raise 403."""
    user = _require_auth(request)
    if not user.is_superuser:
        raise HttpError(403, "Superuser access required.")
    return user


def _serialize_account(user) -> dict:
    """Serialize one account to an ``AccountOut`` dict."""
    return {
        "id": user.pk,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "is_2fa_enabled": active_credential(user) is not None,
        "date_joined": user.date_joined.isoformat(),
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "groups": [{"id": group.pk, "name": group.name} for group in user.groups.all()],
        "roles": read_roles(user),
    }


def _serialize_group(group, *, member_count: int, grant_count: int) -> dict:
    """Serialize one group to a ``GroupDetailOut`` dict."""
    return {
        "id": group.pk,
        "name": group.name,
        "member_count": member_count,
        "grant_count": grant_count,
        "roles": read_group_roles([group])[group.pk],
    }


def _get_account(account_id: int):
    """Fetch an account by primary key or raise 404."""
    User = get_user_model()
    user = User.objects.filter(pk=account_id).select_related("two_factor").prefetch_related("groups").first()
    if user is None:
        raise HttpError(404, "Account not found.")
    return user


def _validated_password(raw: str, user=None) -> str:
    """Return *raw* after running the deployment's password validators.

    Without this the account surface becomes the way around
    ``AUTH_PASSWORD_VALIDATORS`` — an operator-set password would face a lower
    bar than the one a user sets for themselves through ``change-password``.
    """
    try:
        validate_password(raw, user=user)
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages)) from exc
    return raw


def _validated_email(raw: str) -> str:
    """Return a normalised address, or raise 400. Blank is allowed and passes through."""
    address = (raw or "").strip()
    if not address:
        return ""
    try:
        validate_email(address)
    except ValidationError as exc:
        raise HttpError(400, "Enter a valid email address.") from exc
    return address


def _guard_last_superuser(account, *, is_active: bool, is_superuser: bool) -> None:
    """Refuse a change that would leave the deployment with no active superuser.

    The account surface is superuser-only for writes, so demoting or
    deactivating the last one locks every operator out of it. Recovery is the
    ``createadmin`` management command on the host, which needs shell access an
    operator may not have at the moment they need it. Cheaper to refuse.

    Only checked when the change actually removes a superuser: promoting or
    editing an unrelated account cannot reduce the count.
    """
    if not account.is_superuser or not account.is_active:
        return
    if is_superuser and is_active:
        return
    User = get_user_model()
    remaining = User.objects.filter(is_superuser=True, is_active=True).exclude(pk=account.pk).count()
    if remaining == 0:
        raise HttpError(
            409,
            "This is the last active superuser. Promote another account first, or the deployment loses "
            "access to account administration entirely.",
        )


@router.get("/roles", response=list[RoleProviderOut])
def list_role_providers(request):
    """List the project-supplied roles this deployment defines.

    Empty on a deployment whose active project registers none, which is the
    normal case. The group form uses this to know which role selectors to
    render and what to put in them; roles are assigned to groups, and accounts
    inherit them through membership.
    """
    _require_staff(request)
    providers = get_role_providers()
    log_activity(verb="user.role.list", metadata={"returned_count": len(providers)})
    return [
        {"key": p.key, "label": p.label, "choices": [[value, label] for value, label in p.choices]} for p in providers
    ]


@router.get("/accounts", response=list[AccountOut])
def list_accounts(request, q: str = "", limit: int = 100, offset: int = 0):
    """List accounts, optionally filtered by username, name or email.

    Unlike ``/search``, inactive accounts are included — an operator's first
    question about a login failure is often whether the account is still active.
    """
    _require_staff(request)
    if limit < 1 or limit > _MAX_PAGE:
        raise HttpError(400, f"limit must be between 1 and {_MAX_PAGE}.")
    if offset < 0:
        raise HttpError(400, "offset must not be negative.")

    User = get_user_model()
    accounts = User.objects.all()
    term = q.strip()
    if term:
        accounts = accounts.filter(
            Q(username__icontains=term)
            | Q(first_name__icontains=term)
            | Q(last_name__icontains=term)
            | Q(email__icontains=term)
        )
    # select_related on the credential, not just the group prefetch: without it
    # the is_2fa_enabled column costs one query per row on a 500-row page.
    page = list(
        accounts.order_by("username").select_related("two_factor").prefetch_related("groups")[offset : offset + limit]
    )
    log_activity(
        verb="user.account.list",
        metadata={"returned_count": len(page), "filtered": bool(term), "offset": offset},
    )
    return [_serialize_account(user) for user in page]


@router.get("/accounts/{account_id}", response=AccountOut)
def get_account(request, account_id: int):
    """Fetch one account."""
    _require_staff(request)
    account = _get_account(account_id)
    log_activity(verb="user.account.read", target=account)
    return _serialize_account(account)


@router.post("/accounts", response={201: AccountOut})
def create_account(request, payload: AccountCreateIn):
    """Create an account.

    The password is validated against ``AUTH_PASSWORD_VALIDATORS`` before
    anything is written, so a rejected password leaves no half-made account.
    """
    _require_superuser(request)
    username = payload.username.strip()
    if not username:
        raise HttpError(400, "Username is required.")

    User = get_user_model()
    if User.objects.filter(username__iexact=username).exists():
        raise HttpError(409, "An account with that username already exists.")

    email = _validated_email(payload.email)
    # An unsaved instance is enough context for UserAttributeSimilarityValidator,
    # which is what stops "alice" from setting her password to "alice".
    _validated_password(payload.password, user=User(username=username, email=email))

    account = User(
        username=username,
        email=email,
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        is_active=payload.is_active,
        is_staff=payload.is_staff,
        is_superuser=payload.is_superuser,
    )
    account.set_password(payload.password)
    account.save()

    log_activity(
        verb="user.account.create",
        target=account,
        metadata={
            "is_staff": account.is_staff,
            "is_superuser": account.is_superuser,
            "is_active": account.is_active,
        },
    )
    return 201, _serialize_account(account)


@router.patch("/accounts/{account_id}", response=AccountOut)
def update_account(request, account_id: int, payload: AccountUpdateIn):
    """Edit an account's fields and project roles.

    Username is not editable. It is the identifier the audit trail, the session
    store and every ``AccessRight`` grant already reference by primary key, but
    it is also what an operator recognises an account by in a log line; renaming
    silently rewrites the meaning of every historical line that names it.
    """
    _require_superuser(request)
    account = _get_account(account_id)

    is_active = payload.is_active if payload.is_active is not None else account.is_active
    is_superuser = payload.is_superuser if payload.is_superuser is not None else account.is_superuser
    _guard_last_superuser(account, is_active=is_active, is_superuser=is_superuser)

    changed: list[str] = []
    if payload.email is not None:
        account.email = _validated_email(payload.email)
        changed.append("email")
    if payload.first_name is not None:
        account.first_name = payload.first_name.strip()
        changed.append("first_name")
    if payload.last_name is not None:
        account.last_name = payload.last_name.strip()
        changed.append("last_name")
    for field, value in (("is_active", payload.is_active), ("is_staff", payload.is_staff)):
        if value is not None:
            setattr(account, field, value)
            changed.append(field)
    if payload.is_superuser is not None:
        account.is_superuser = payload.is_superuser
        changed.append("is_superuser")

    account.save()

    log_activity(
        verb="user.account.update",
        target=account,
        metadata={"fields": sorted(changed)},
    )
    return _serialize_account(account)


@router.post("/accounts/{account_id}/password", response=dict)
def set_account_password(request, account_id: int, payload: SetPasswordIn):
    """Set another account's password.

    Sessions are not flushed. An operator setting a password is usually helping
    somebody back in rather than responding to a compromise, and silently
    signing the account out of a viewer session mid-review is its own harm. For
    a compromise, deactivate the account — that does end its sessions.
    """
    _require_superuser(request)
    account = _get_account(account_id)
    _validated_password(payload.new_password, user=account)
    account.set_password(payload.new_password)
    account.save(update_fields=["password"])
    log_activity(verb="user.account.password.set", target=account)
    return {"status": "ok"}


@router.delete("/accounts/{account_id}/2fa", response=dict)
def reset_account_two_factor(request, account_id: int):
    """Remove an account's second factor, for the operator recovery case.

    Someone loses the phone holding their authenticator and has spent their
    recovery codes; without this the account is unreachable and the only way
    back is a shell on the host. That makes a lost phone an incident, which is
    the wrong shape for a routine event — so it is an audited, superuser-only
    endpoint instead.

    The counterpart risk is that this is a way to strip a second factor off an
    account, so it emits a security event as well as the audit row: an operator
    session used to disarm accounts is exactly what an alert rule should see.
    Deliberately not self-service — the account's own disable endpoint requires
    the password, which someone who has lost only their phone still has.
    """
    actor = _require_superuser(request)
    account = _get_account(account_id)
    credential = getattr(account, "two_factor", None)
    if credential is None:
        raise HttpError(409, "This account does not have two-factor authentication set up.")

    was_active = credential.confirmed_at is not None
    credential.delete()
    log_activity(
        verb="user.account.2fa.reset",
        target=account,
        metadata={"was_active": was_active},
    )
    log_security_event(
        "auth.2fa_reset",
        ip=get_client_ip(request),
        actor_id=actor.pk,
        target_id=account.pk,
    )
    return {"status": "reset"}


@router.put("/accounts/{account_id}/groups", response=AccountOut)
def set_account_groups(request, account_id: int, payload: GroupMembershipIn):
    """Replace an account's group membership.

    Recorded through ``record_modify_change`` rather than left to the signals:
    ``groups.set()`` fires ``m2m_changed``, which the audit receivers do not
    listen for, and the M2M rows are not concrete fields on the user, so the
    before / after membership rides in ``extra_payload``.
    """
    actor = _require_superuser(request)
    account = _get_account(account_id)
    if payload.group_ids is None:
        raise HttpError(400, "group_ids is required.")

    groups = list(Group.objects.filter(pk__in=payload.group_ids))
    missing = set(payload.group_ids) - {group.pk for group in groups}
    if missing:
        raise HttpError(404, f"No such group: {sorted(missing)}.")

    before_state = serialize_instance(account)
    before = sorted(account.groups.values_list("name", flat=True))
    with transaction.atomic():
        account.groups.set(groups)
        after = sorted(group.name for group in groups)
        record_modify_change(
            actor=actor,
            obj=account,
            before_state=before_state,
            extra_payload={GROUP_MEMBERSHIP_DIGEST_KEY: compute_group_membership_digest(account)},
        )

    log_activity(
        verb="user.account.groups.set",
        target=account,
        metadata={"groups_before": before, "groups_after": after},
    )
    return _serialize_account(account)


@router.get("/groups", response=list[GroupDetailOut])
def list_group_details(request):
    """List groups with member and grant counts.

    ``grant_count`` is what makes a group deletable or not, so it belongs in the
    listing rather than only in the error message that refuses the delete.
    """
    _require_staff(request)
    groups = list(Group.objects.annotate(members=Count("user", distinct=True)).order_by("name"))
    grants = dict(
        AccessRight.objects.filter(access_target_group__isnull=False)
        .values_list("access_target_group_id")
        .annotate(total=Count("id"))
    )
    # Batched through the registry so the listing costs one provider query,
    # not one per group.
    roles = read_group_roles(groups)
    log_activity(verb="user.group.list", metadata={"returned_count": len(groups)})
    return [
        {
            "id": group.pk,
            "name": group.name,
            "member_count": group.members,
            "grant_count": grants.get(group.pk, 0),
            "roles": roles[group.pk],
        }
        for group in groups
    ]


@router.post("/groups", response={201: GroupDetailOut})
def create_group(request, payload: GroupIn):
    """Create a group."""
    _require_superuser(request)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "Group name is required.")
    if Group.objects.filter(name__iexact=name).exists():
        raise HttpError(409, "A group with that name already exists.")
    group = Group.objects.create(name=name)
    log_activity(verb="user.group.create", target=group)
    return 201, _serialize_group(group, member_count=0, grant_count=0)


@router.patch("/groups/{group_id}", response=GroupDetailOut)
def update_group(request, group_id: int, payload: GroupUpdateIn):
    """Rename a group and/or set its project roles.

    Renaming is safe where renaming a user is not: grants reference the group by
    primary key and nothing in the platform gates on a group's name, so the name
    is a label rather than an identifier.

    Roles are written here — on the group — because a role belongs to the group
    and its members inherit it; there is no per-account role write. The
    provider's model write happens inside the request scope, so it lands on the
    audit trail through the ordinary signals.
    """
    _require_superuser(request)
    group = Group.objects.filter(pk=group_id).first()
    if group is None:
        raise HttpError(404, "Group not found.")

    changed: list[str] = []
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HttpError(400, "Group name is required.")
        if Group.objects.filter(name__iexact=name).exclude(pk=group.pk).exists():
            raise HttpError(409, "A group with that name already exists.")
        group.name = name
        changed.append("name")

    roles_changed: list[str] = []
    with transaction.atomic():
        if changed:
            group.save()
        for key, value in (payload.roles or {}).items():
            try:
                write_group_role(group, key, value)
            except KeyError as exc:
                raise HttpError(400, f"Unknown role '{key}' for this deployment.") from exc
            except ValueError as exc:
                raise HttpError(400, f"'{value}' is not an accepted value for role '{key}'.") from exc
            roles_changed.append(key)

    log_activity(
        verb="user.group.update",
        target=group,
        metadata={"fields": sorted(changed), "roles": sorted(roles_changed)},
    )
    return _serialize_group(
        group,
        member_count=group.user_set.count(),
        grant_count=AccessRight.objects.filter(access_target_group=group).count(),
    )


@router.delete("/groups/{group_id}", response=dict)
def delete_group(request, group_id: int):
    """Delete a group, unless access grants still name it.

    Deleting a group that grants stand on would revoke access for everyone in it
    at once, and the ``AccessRight`` rows would cascade away with it — so the
    operator would not be able to see afterwards what had been revoked. Refuse,
    and report the count so the decision can be made deliberately.
    """
    _require_superuser(request)
    group = Group.objects.filter(pk=group_id).first()
    if group is None:
        raise HttpError(404, "Group not found.")

    grant_count = AccessRight.objects.filter(access_target_group=group).count()
    if grant_count:
        raise HttpError(
            409,
            f"{grant_count} access grant(s) still target this group. Revoke them first — deleting the "
            "group would remove them silently, leaving no record of what access was withdrawn.",
        )

    member_count = group.user_set.count()
    name = group.name
    group.delete()
    log_activity(
        verb="user.group.delete",
        metadata={"group_name": name, "member_count": member_count},
    )
    return {"status": "deleted"}


@router.put("/groups/{group_id}/members", response=GroupDetailOut)
def set_group_members(request, group_id: int, payload: GroupMembershipIn):
    """Replace a group's membership — the same operation from the other side.

    Audited per affected account rather than once for the group: the change is
    to each user's membership, and ``erase_subject`` reaches audit rows through
    their target, so a single row targeting the group would put one user's
    membership history out of reach of every other user's erasure request.
    """
    actor = _require_superuser(request)
    group = Group.objects.filter(pk=group_id).first()
    if group is None:
        raise HttpError(404, "Group not found.")
    if payload.user_ids is None:
        raise HttpError(400, "user_ids is required.")

    User = get_user_model()
    users = list(User.objects.filter(pk__in=payload.user_ids))
    missing = set(payload.user_ids) - {user.pk for user in users}
    if missing:
        raise HttpError(404, f"No such account: {sorted(missing)}.")

    before_members = set(group.user_set.values_list("pk", flat=True))
    after_members = {user.pk for user in users}
    affected = User.objects.filter(pk__in=before_members ^ after_members)

    with transaction.atomic():
        states = {user.pk: (user, serialize_instance(user)) for user in affected}
        group.user_set.set(users)
        for account, before_state in states.values():
            record_modify_change(
                actor=actor,
                obj=account,
                before_state=before_state,
                extra_payload={GROUP_MEMBERSHIP_DIGEST_KEY: compute_group_membership_digest(account)},
            )

    log_activity(
        verb="user.group.members.set",
        target=group,
        metadata={"member_count_before": len(before_members), "member_count_after": len(after_members)},
    )
    return _serialize_group(
        group,
        member_count=len(after_members),
        grant_count=AccessRight.objects.filter(access_target_group=group).count(),
    )
