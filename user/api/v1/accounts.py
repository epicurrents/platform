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

Every write that could hand someone a way in asks the caller for step-up
confirmation (user/stepup.py) first: creating an account with a password or a
staff tier, changing an account's tier, activation or email, setting a
password, removing a second factor, adding group members, and giving a group a
project role. A superuser session cookie is otherwise enough to mint a new
superuser, or to strip the caller's own second factor and set a new password,
which turns one stolen cookie into a permanent account. Setting a password or
resetting the second factor on the caller's own account is refused outright:
the profile flows exist for that, and they ask for the current credential.
Each of these writes is also reported to the security log.
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
from user.dedicated_groups import dedicated_group, dedicated_groups
from user.identity import is_externally_authenticated, provider_label
from user.roles import get_role_providers, read_group_roles, read_roles, write_group_role
from user.stepup import confirm_step_up
from user.tasks import mail_deliverable, send_welcome_email
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
    #: The provider this account signs in through, or null for a local account.
    #: Set only when the account has no local password of its own; see
    #: user/identity.py for why holding an identity is not by itself enough.
    external_provider: str | None
    #: The account was created without a password and can still be invited.
    is_invite_pending: bool


class AccountCreatedOut(AccountOut):
    """A newly created account, and whether an invitation was actually mailed.

    ``invitation_sent`` is false for an account created with a password, one
    created deactivated, and any account on a deployment with no mail backend
    configured — where sending would print the link to the container log.
    """

    invitation_sent: bool


class AccountCreateIn(Schema):
    """New-account payload. Only ``username`` is required.

    Omitting ``password`` is the ordinary way to add someone: the account is
    created with no usable password and an invitation carrying a set-password
    link is mailed to ``email``, which is then required. The alternative leaves
    an operator holding a credential they have to convey out of band.

    ``password`` here is the new account's. The caller's step-up credentials are
    ``current_password`` and ``totp_code``, required when ``password`` is
    supplied or either staff tier is set.
    """

    username: str
    password: str | None = None
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    is_active: bool = True
    is_staff: bool = False
    is_superuser: bool = False
    current_password: str | None = None
    totp_code: str | None = None


class StepUpIn(Schema):
    """The caller's step-up credentials: their own password, and a code when they have a second factor."""

    password: str | None = None
    totp_code: str | None = None


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
    #: Step-up credentials, required when the edit changes ``is_staff``,
    #: ``is_superuser`` or ``email``, or activates the account.
    password: str | None = None
    totp_code: str | None = None


class SetPasswordIn(StepUpIn):
    """Operator-set password for another account, with the caller's step-up credentials."""

    new_password: str


class GroupMembershipIn(StepUpIn):
    """Replacement membership, from either direction. Step-up is required when it adds anyone."""

    group_ids: list[int] | None = None
    user_ids: list[int] | None = None


class GroupIn(Schema):
    """Group create payload."""

    name: str


class GroupUpdateIn(StepUpIn):
    """Partial group edit. Omitted fields are left alone.

    ``roles`` is a partial map: an absent key is untouched, so a client that
    does not know a project's role exists cannot clear it. An explicit ``null``
    value clears that role. Setting any role to a value needs step-up; clearing
    one does not.
    """

    name: str | None = None
    roles: dict[str, str | None] | None = None


class DedicatedGroupOut(Schema):
    """What owns a dedicated group: a kind token and the owning object's hash and name."""

    kind: str
    object_hash: str
    name: str


class GroupDetailOut(Schema):
    """A group with its project roles and the two counts that decide whether it can be deleted.

    ``dedicated_to`` names the feature that owns the group (``user.dedicated_groups``); such a group carries no
    grant or role and is not deleted here.
    """

    id: int
    name: str
    member_count: int
    grant_count: int
    roles: dict[str, str | None]
    dedicated_to: DedicatedGroupOut | None = None


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
    """Serialize one account to an ``AccountOut`` dict.

    Both identity-derived fields read ``external_identities``, so a caller
    serializing a page of accounts prefetches it or pays a query per row.
    """
    external = is_externally_authenticated(user)
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
        "external_provider": provider_label(user) if external else None,
        "is_invite_pending": not external and not user.has_usable_password(),
    }


def _step_up(request, actor, *, password: str | None, totp_code: str | None) -> None:
    """Confirm the caller's own credentials before a write that could hand someone a way in."""
    confirm_step_up(request, actor, password=password, totp_code=totp_code)


def _refuse_self(actor, account, what: str) -> None:
    """Refuse an operator credential action aimed at the caller's own account."""
    if account.pk == actor.pk:
        raise HttpError(
            409,
            f"Use your profile page to {what} on your own account; it asks for your current credentials.",
        )


def _dedicated_out(owner) -> dict | None:
    if owner is None:
        return None
    return {"kind": owner.kind, "object_hash": owner.object_hash, "name": owner.name}


def _serialize_group(group, *, member_count: int, grant_count: int) -> dict:
    """Serialize one group to a ``GroupDetailOut`` dict."""
    return {
        "id": group.pk,
        "name": group.name,
        "member_count": member_count,
        "grant_count": grant_count,
        "roles": read_group_roles([group])[group.pk],
        "dedicated_to": _dedicated_out(dedicated_group(group.pk)),
    }


def _get_account(account_id: int):
    """Fetch an account by primary key or raise 404."""
    User = get_user_model()
    user = (
        User.objects.filter(pk=account_id)
        .select_related("two_factor")
        .prefetch_related("groups", "external_identities")
        .first()
    )
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
        accounts.order_by("username")
        .select_related("two_factor")
        .prefetch_related("groups", "external_identities")[offset : offset + limit]
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


@router.post("/accounts", response={201: AccountCreatedOut})
def create_account(request, payload: AccountCreateIn):
    """Create an account, by invitation unless a password is supplied.

    Without a password the account gets an unusable one and an invitation
    carrying a set-password link, so the credential is chosen by the person who
    will use it and passes through nobody else. That needs an address to send
    to, which is why ``email`` stops being optional in that case.

    A supplied password is validated against ``AUTH_PASSWORD_VALIDATORS`` before
    anything is written, so a rejected password leaves no half-made account.

    Step-up (``current_password``, ``totp_code``) is required when a password is
    supplied or either staff tier is set. Both leave the caller holding a way in
    that outlives their session: a staff or superuser account is one directly,
    and so is any account whose password they chose. An invited ordinary
    account needs none, since its credential reaches only the invited address.
    """
    actor = _require_superuser(request)
    username = payload.username.strip()
    if not username:
        raise HttpError(400, "Username is required.")

    User = get_user_model()
    if User.objects.filter(username__iexact=username).exists():
        raise HttpError(409, "An account with that username already exists.")

    email = _validated_email(payload.email)
    invite = not payload.password
    if invite and not email:
        raise HttpError(400, "An account created without a password needs an email address to send the invitation to.")
    if not invite:
        # An unsaved instance is enough context for UserAttributeSimilarityValidator,
        # which is what stops "alice" from setting her password to "alice".
        _validated_password(payload.password, user=User(username=username, email=email))
    if not invite or payload.is_staff or payload.is_superuser:
        _step_up(request, actor, password=payload.current_password, totp_code=payload.totp_code)

    account = User(
        username=username,
        email=email,
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        is_active=payload.is_active,
        is_staff=payload.is_staff,
        is_superuser=payload.is_superuser,
    )
    if invite:
        account.set_unusable_password()
    else:
        account.set_password(payload.password)

    # An account created deactivated is not invited yet. The task would refuse
    # it anyway — an invitation to an account that cannot sign in is worth
    # nothing — but it would refuse silently, leaving an operator who prepared
    # the account ahead of time believing the mail went out. The resend action
    # appears the moment the account is activated.
    # Nor is it invited while no mail backend is configured: the task would
    # decline rather than print the link to the container log, and the response
    # says so instead of letting the operator believe it went.
    invite_now = invite and account.is_active and mail_deliverable()
    with transaction.atomic():
        account.save()
        if invite_now:
            # Dispatched on commit so the worker cannot read the row before it
            # exists. Only the primary key crosses the broker; see user/tasks.py.
            transaction.on_commit(lambda: send_welcome_email.delay(account.pk))

    log_activity(
        verb="user.account.create",
        target=account,
        metadata={
            "is_staff": account.is_staff,
            "is_superuser": account.is_superuser,
            "is_active": account.is_active,
            "invited": invite,
            "invitation_sent": invite_now,
        },
    )
    log_security_event(
        "admin.account_created",
        ip=get_client_ip(request),
        actor_id=actor.pk,
        target_id=account.pk,
        is_staff=account.is_staff,
        is_superuser=account.is_superuser,
        invited=invite,
    )
    return 201, {**_serialize_account(account), "invitation_sent": invite_now}


@router.post("/accounts/{account_id}/invite", response=dict)
def resend_account_invitation(request, account_id: int):
    """Send the set-password invitation again, for someone who missed the first one.

    The link expires after three days, which an invitation sent before a holiday
    routinely outlives, and there is no other way into an account that has never
    had a password.

    Refused rather than repurposed once the account has a password of its own:
    this would otherwise be a way for an operator to mail a password link to any
    account from the roster, which is the user's own request to make through
    password reset.

    Answers ``{"status": "sent", "invitation_sent": true}``, or ``"not_sent"``
    and ``false`` when the deployment has no mail backend configured, in which
    case nothing is queued.
    """
    actor = _require_superuser(request)
    account = _get_account(account_id)
    if is_externally_authenticated(account):
        raise HttpError(
            409,
            f"This account signs in through {provider_label(account)} and does not use a password on this platform.",
        )
    if account.has_usable_password():
        raise HttpError(409, "This account already has a password. The account holder can request a reset themselves.")
    if not account.is_active:
        raise HttpError(409, "This account is deactivated. Reactivate it before inviting the account holder in.")
    if not account.email:
        raise HttpError(409, "This account has no email address to send the invitation to.")

    sent = mail_deliverable()
    if sent:
        send_welcome_email.delay(account.pk)
    log_activity(verb="user.account.invite.resend", target=account, metadata={"invitation_sent": sent})
    log_security_event(
        "admin.invitation_sent", ip=get_client_ip(request), actor_id=actor.pk, target_id=account.pk, sent=sent
    )
    return {"status": "sent" if sent else "not_sent", "invitation_sent": sent}


@router.patch("/accounts/{account_id}", response=AccountOut)
def update_account(request, account_id: int, payload: AccountUpdateIn):
    """Edit an account's fields and project roles.

    Username is not editable. It is the identifier the audit trail, the session
    store and every ``AccessRight`` grant already reference by primary key, but
    it is also what an operator recognises an account by in a log line; renaming
    silently rewrites the meaning of every historical line that names it.

    Step-up (``password``, ``totp_code``) is required when the edit changes
    ``is_staff``, ``is_superuser`` or ``email``, or activates the account. A
    tier change is privilege by definition; activation restores a way in that
    was closed; and the address is where a password reset goes, so changing it
    is a takeover by another name. Values equal to the current ones are not
    changes and need nothing.
    """
    actor = _require_superuser(request)
    account = _get_account(account_id)

    is_active = payload.is_active if payload.is_active is not None else account.is_active
    is_superuser = payload.is_superuser if payload.is_superuser is not None else account.is_superuser
    _guard_last_superuser(account, is_active=is_active, is_superuser=is_superuser)

    new_email = _validated_email(payload.email) if payload.email is not None else account.email
    email_changed = new_email != account.email
    privilege_changed = [
        field
        for field, value in (("is_staff", payload.is_staff), ("is_superuser", payload.is_superuser))
        if value is not None and value != getattr(account, field)
    ]
    if payload.is_active is True and not account.is_active:
        privilege_changed.append("is_active")
    if email_changed or privilege_changed:
        _step_up(request, actor, password=payload.password, totp_code=payload.totp_code)

    changed: list[str] = []
    if payload.email is not None:
        account.email = new_email
        if email_changed:
            # An operator set it, so a verified-email link may use it again.
            account.email_self_asserted = False
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
    if privilege_changed:
        log_security_event(
            "admin.account_privilege_changed",
            ip=get_client_ip(request),
            actor_id=actor.pk,
            target_id=account.pk,
            fields=sorted(privilege_changed),
            is_staff=account.is_staff,
            is_superuser=account.is_superuser,
            is_active=account.is_active,
        )
    if email_changed:
        log_security_event(
            "admin.account_email_changed", ip=get_client_ip(request), actor_id=actor.pk, target_id=account.pk
        )
    return _serialize_account(account)


@router.post("/accounts/{account_id}/password", response=dict)
def set_account_password(request, account_id: int, payload: SetPasswordIn):
    """Set another account's password.

    Sessions are not flushed. An operator setting a password is usually helping
    somebody back in rather than responding to a compromise, and silently
    signing the account out of a viewer session mid-review is its own harm. For
    a compromise, deactivate the account — that does end its sessions.

    Needs step-up (``password``, ``totp_code`` — the caller's own). Refused with
    409 on the caller's own account, where change-password is the route and asks
    for the current password, and on an account that signs in through an
    identity provider, for the reason every other password surface refuses it:
    a local password there answers to neither the tenant nor the domain gate.
    """
    actor = _require_superuser(request)
    account = _get_account(account_id)
    _refuse_self(actor, account, "change the password")
    if is_externally_authenticated(account):
        raise HttpError(
            409,
            f"This account signs in through {provider_label(account)}. Its password is managed there, not here.",
        )
    _validated_password(payload.new_password, user=account)
    _step_up(request, actor, password=payload.password, totp_code=payload.totp_code)
    account.set_password(payload.new_password)
    account.save(update_fields=["password"])
    log_activity(verb="user.account.password.set", target=account)
    log_security_event("admin.password_set", ip=get_client_ip(request), actor_id=actor.pk, target_id=account.pk)
    return {"status": "ok"}


@router.delete("/accounts/{account_id}/2fa", response=dict)
def reset_account_two_factor(request, account_id: int, payload: StepUpIn | None = None):
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

    Needs step-up (``password``, ``totp_code`` in the request body), and is
    refused with 409 on the caller's own account: stripping one's own factor
    here is exactly the first step of turning a stolen session into a stolen
    account, and the self-service disable endpoint asks for the password.
    """
    actor = _require_superuser(request)
    account = _get_account(account_id)
    _refuse_self(actor, account, "remove the second factor")
    credential = getattr(account, "two_factor", None)
    if credential is None:
        raise HttpError(409, "This account does not have two-factor authentication set up.")
    # Optional so a bodiless DELETE still reaches the auth check and answers
    # 401 / 403 rather than a schema error; it then fails step-up with 400.
    payload = payload or StepUpIn()
    _step_up(request, actor, password=payload.password, totp_code=payload.totp_code)

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

    Adding the account to any group needs step-up (``password``, ``totp_code``).
    A group carries access grants and project roles, including ones added after
    the membership, so an addition is a grant; removals need nothing.
    """
    actor = _require_superuser(request)
    account = _get_account(account_id)
    if payload.group_ids is None:
        raise HttpError(400, "group_ids is required.")

    groups = list(Group.objects.filter(pk__in=payload.group_ids))
    missing = set(payload.group_ids) - {group.pk for group in groups}
    if missing:
        raise HttpError(404, f"No such group: {sorted(missing)}.")

    before_ids = set(account.groups.values_list("pk", flat=True))
    added = {group.pk for group in groups} - before_ids
    if added:
        _step_up(request, actor, password=payload.password, totp_code=payload.totp_code)

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
    if added:
        log_security_event(
            "admin.group_membership_granted",
            ip=get_client_ip(request),
            actor_id=actor.pk,
            target_id=account.pk,
            group_ids=sorted(added),
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
    owners = dedicated_groups(group.pk for group in groups)
    log_activity(verb="user.group.list", metadata={"returned_count": len(groups)})
    return [
        {
            "id": group.pk,
            "name": group.name,
            "member_count": group.members,
            "grant_count": grants.get(group.pk, 0),
            "roles": roles[group.pk],
            "dedicated_to": _dedicated_out(owners.get(group.pk)),
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

    Setting a role to a value needs step-up (``password``, ``totp_code``), since
    it grants that role to every member at once; clearing one does not.
    """
    actor = _require_superuser(request)
    group = Group.objects.filter(pk=group_id).first()
    if group is None:
        raise HttpError(404, "Group not found.")

    granted_roles = sorted(key for key, value in (payload.roles or {}).items() if value is not None)
    if granted_roles:
        _step_up(request, actor, password=payload.password, totp_code=payload.totp_code)

    changed: list[str] = []
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HttpError(400, "Group name is required.")
        if Group.objects.filter(name__iexact=name).exclude(pk=group.pk).exists():
            raise HttpError(409, "A group with that name already exists.")
        group.name = name
        changed.append("name")

    # A dedicated group means one thing; a role would give its members another.
    # Clearing stays allowed, so a role that predates the group's owner can go.
    if dedicated_group(group.pk) is not None and any(value is not None for value in (payload.roles or {}).values()):
        raise HttpError(409, "This group exists for one purpose and carries no project role.")

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
    if granted_roles:
        log_security_event(
            "admin.group_roles_granted",
            ip=get_client_ip(request),
            actor_id=actor.pk,
            group_id=group.pk,
            roles=granted_roles,
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
    if dedicated_group(group.pk) is not None:
        raise HttpError(409, "This group belongs to another feature and is removed with it, not here.")

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

    Adding any member needs step-up (``password``, ``totp_code``), for the
    reason ``set_account_groups`` gives.
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
    added = after_members - before_members
    if added:
        _step_up(request, actor, password=payload.password, totp_code=payload.totp_code)
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
    if added:
        log_security_event(
            "admin.group_membership_granted",
            ip=get_client_ip(request),
            actor_id=actor.pk,
            group_id=group.pk,
            added_count=len(added),
        )
    return _serialize_group(
        group,
        member_count=len(after_members),
        grant_count=AccessRight.objects.filter(access_target_group=group).count(),
    )
