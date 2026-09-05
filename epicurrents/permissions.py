"""Object-level permission resolution for the platform.

⚠️ LOAD-BEARING — object-level access control.
Every endpoint that touches a user-owned object calls into the
``can_*_object`` / ``ensure_*_object`` functions here.  Changing the
resolution order, the return shape, or the extension-registry semantics
changes security behaviour repo-wide.  See AGENTS.md → *Load-bearing
files* before modifying; the contract test is
``epicurrents/tests/test_permissions.py``.

Every other app calls into the four ``can_*_object`` functions (and their
``ensure_*`` raise-on-deny counterparts) defined here.  Behind them sits a
three-layer model:

1. ``AccessRight`` rows in ``epicurrents_accessright`` — the canonical
   per-object grants (one row per user/group/token/peer × object).
2. The permission functions in this file, which read those rows together
   with author / superuser fast paths.
3. Permission extensions registered via
   :func:`register_read_permission_extension`, consulted only when no
   direct ``AccessRight`` row matches.  Library uses this to grant read
   access via Dataset membership.

Above all three sits the restrictive counterpart: read-visibility gates
registered via :func:`register_read_visibility_gate`. A gate hides an
object from grant resolution entirely — consulted after the superuser
fast-path but before rows and extensions, so no grant of any kind can
surface an object its model's gate hides. Recordings register a gate
that hides FAILED recordings from everyone but the author and hides
trashed recordings from every caller.

The README (``epicurrents/README.md`` → *Permissions*) is the long-form
description of grants, resolution order, and the ``apply_middleware``
metadata flag; this docstring just sets the orientation for code readers.
"""

from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import F, Q
from django.utils import timezone
from ninja.errors import HttpError

from epicurrents.security_log import log_security_event


def _log_permission_denied(permission: str, user: Any, obj: Any) -> None:
    """Emit a structured ``permission.denied`` security log entry.

    Centralised so every ``ensure_*`` helper logs the same shape — extending
    the security event taxonomy or adding fields lifts every call site at
    once.
    """
    log_security_event(
        "permission.denied",
        permission=permission,
        actor_id=getattr(user, "pk", None) if user is not None else None,
        object_type=type(obj).__name__ if obj is not None else None,
        object_id=str(getattr(obj, "pk", "")) or None,
    )


READ_PERMISSION_MODEL_APP_LABEL = "epicurrents"
READ_PERMISSION_MODEL_NAME = "AccessRight"

# Registry of extra read-permission checkers.  Each callable receives
# (user, obj, share_token) and returns bool.  Register from app.ready()
# via register_read_permission_extension() to avoid circular imports.
_READ_PERMISSION_EXTENSIONS: list = []

# Registry of federated read extensions: pairs of callables that answer the same
# question for one object and for a listing. Separate from the registry above
# because the caller shape differs — a peer holds no local user, no groups and no
# share token, so a local checker cannot answer for one. Register from app.ready()
# via register_federated_read_extension() to avoid circular imports.
_FEDERATED_READ_EXTENSIONS: list = []

# Registry of read-visibility gates, keyed by model label ("app.model").
# Each gate receives (user, obj, share_token) and returns True when the
# object must be treated as invisible to that caller. Register from
# app.ready() via register_read_visibility_gate() to avoid circular imports.
_READ_VISIBILITY_GATES: dict = {}


@dataclass
class ReadAccessTerms:
    """Result of a read-permission check, carrying access-right metadata.

    ``granted`` is always set.  Additional fields reflect properties of the
    matching ``AccessRight`` row and can be used by callers to customise how
    they serve the object — e.g. ``apply_middleware`` signals that EDF file
    content should be piped through the configured middleware pipeline before
    being sent to the recipient.

    Extension-granted access carries the terms of the row the grant came from:
    Dataset membership resolves the ``AccessRight`` on the dataset and returns
    its ``apply_middleware``, so a sharer's choice to de-identify survives being
    inherited. An extension with no backing row leaves the default.
    """

    granted: bool
    apply_middleware: bool = False

    def __bool__(self) -> bool:
        return self.granted


def register_read_visibility_gate(model_label: str, gate) -> None:
    """Register a restrictive read-visibility gate for one model.

    Signature: ``gate(user, obj, share_token=None) -> bool`` — return True when
    *obj* must be treated as invisible to this caller, in which case grant
    resolution stops and the read is denied regardless of any ``AccessRight``
    row or extension grant. ``model_label`` is the lower-case Django label
    (``"recordings.recording"``); gates are only consulted for instances of
    that model.

    A gate must treat ``user=None`` as a fully unprivileged caller: the
    federated resolver consults gates with ``user=None`` because peer callers
    hold no local identity. Superusers never reach gates — the resolver's
    fast-path precedes them. Idempotent — registering the same callable twice
    has no effect.
    """
    gates = _READ_VISIBILITY_GATES.setdefault(model_label.lower(), [])
    if gate not in gates:
        gates.append(gate)


def _hidden_by_visibility_gate(user: Any, obj: Any, share_token: str | None = None) -> bool:
    """Return True when a registered gate hides *obj* from this caller."""
    label = getattr(getattr(obj, "_meta", None), "label_lower", None)
    if label is None:
        return False
    for gate in _READ_VISIBILITY_GATES.get(label, ()):
        if gate(user=user, obj=obj, share_token=share_token):
            return True
    return False


def register_read_permission_extension(checker) -> None:
    """Register an extra callable for can_read_object fallback checks.

    Signature: ``checker(user, obj, share_token=None) -> bool``

    Checkers are called in registration order after the standard
    AccessRight lookup fails.  Idempotent — registering the same
    callable twice has no effect.
    """
    if checker not in _READ_PERMISSION_EXTENSIONS:
        _READ_PERMISSION_EXTENSIONS.append(checker)


@dataclass(frozen=True)
class _FederatedReadExtension:
    """One extension's two answers: for a single object, and for a listing."""

    check: Any
    visible_terms: Any


def register_federated_read_extension(*, check, visible_terms) -> None:
    """Register a path by which a peer reaches objects it holds no direct grant on.

    Local read extensions cannot serve here. They take ``(user, obj, share_token)``,
    and a federated caller has none of those — it is a peer plus an opaque remote
    user id — so dataset membership and anything like it is invisible to a peer
    unless something answers in these terms.

    The two callables are registered together because a listing that disagrees with
    the per-object check is the failure this shape exists to prevent: one grants
    access nobody can discover, the other advertises objects that then 404.

    ``check(peer, remote_user_id, obj) -> ReadAccessTerms | None``
        Terms for one object, or ``None`` when this extension does not grant it.
        Carry ``apply_middleware`` from the row the grant came from — a federated
        grant de-identifies by default, and dropping the flag while inheriting it
        serves raw PHI to a peer nobody granted that to.

    ``visible_terms(peer, remote_user_id, content_type) -> Mapping[object id, ReadAccessTerms]``
        Every object of that content type the peer reaches this way, with the terms
        it reaches each one on. Listing endpoints resolve many objects at once and
        cannot afford a per-row ``check``; returning terms rather than bare ids
        means a caller that needs ``apply_middleware`` — the download-size
        computation does — gets it from the same query.

    Idempotent — registering the same pair twice has no effect.
    """
    extension = _FederatedReadExtension(check=check, visible_terms=visible_terms)
    if extension not in _FEDERATED_READ_EXTENSIONS:
        _FEDERATED_READ_EXTENSIONS.append(extension)


def get_federated_visible_terms(peer, remote_user_id: str, content_type) -> dict:
    """Terms on which a peer reaches each *content_type* object, through extensions.

    The listing counterpart to the extension consultation in
    :func:`get_federated_read_access_result`. Keys are strings, matching the
    ``AccessRight.object_id`` column the direct-grant query reads.

    Where two extensions reach the same object, the de-identifying terms win. That
    matches how the direct-row resolver breaks a tie, and errs the safe way: the
    cost of applying the middleware to a caller who could have had raw bytes is a
    transformation nobody needed, and the cost of the reverse is PHI.

    Callers still apply their own visibility rules to the rows they resolve — this
    answers which objects an extension reaches, not whether they may be shown.
    """
    terms: dict = {}
    if peer is None:
        return terms
    for extension in _FEDERATED_READ_EXTENSIONS:
        for object_id, object_terms in extension.visible_terms(peer, remote_user_id, content_type).items():
            key = str(object_id)
            existing = terms.get(key)
            if existing is None or (object_terms.apply_middleware and not existing.apply_middleware):
                terms[key] = object_terms
    return terms


def get_federated_visible_ids(peer, remote_user_id: str, content_type) -> set[str]:
    """Ids of *content_type* objects a peer reaches through registered extensions."""
    return set(get_federated_visible_terms(peer, remote_user_id, content_type))


def _resolve_read_permission_model():
    """Resolve configured read-permission model class lazily."""

    try:
        return apps.get_model(READ_PERMISSION_MODEL_APP_LABEL, READ_PERMISSION_MODEL_NAME)
    except LookupError:
        return None


def can_modify_object(user: Any, obj: Any, author_field: str = "author") -> bool:
    """Return True when user is superuser or object author for writes."""

    if not user or not getattr(user, "is_authenticated", False):
        return False

    if getattr(user, "is_superuser", False):
        return True

    author = getattr(obj, author_field, None)
    if author is None:
        return False

    author_id = getattr(obj, f"{author_field}_id", None)
    if author_id is not None:
        return author_id == user.pk

    return author == user


def get_read_access_result(user: Any, obj: Any, share_token: str | None = None) -> ReadAccessTerms:
    """Return a ReadAccessTerms describing whether user/token may read *obj*.

    Carries access-right metadata from the matching ``AccessRight`` row so
    callers can act on it (e.g. ``apply_middleware``) without a second query.

    Checks in order:
    1. Superuser → granted, default metadata.
    2. Read-visibility gates for the object's model — a gate that hides the
       object ends resolution with a denial before any grant is consulted.
    3. Direct ``AccessRight`` query (user, group, or share-token target).
       Returns metadata from the first matching row.
    4. Registered extension checkers (e.g. Dataset membership).
       Extensions are only consulted when no direct ``AccessRight`` matched;
       they always yield default metadata (``apply_middleware=False``).
    """

    if user and getattr(user, "is_superuser", False):
        return ReadAccessTerms(granted=True)

    if _hidden_by_visibility_gate(user=user, obj=obj, share_token=share_token):
        return ReadAccessTerms(granted=False)

    permission_model = _resolve_read_permission_model()
    if permission_model is None:
        return ReadAccessTerms(granted=False)

    token_value = (share_token or "").strip()
    object_pk = getattr(obj, "pk", None)

    if object_pk is not None:
        content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
        now = timezone.now()
        base_qs = permission_model.objects.filter(
            content_type=content_type,
            object_id=str(object_pk),
            can_read=True,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))

        target_filter = Q()
        user_id = getattr(user, "pk", None)
        if user_id is not None and getattr(user, "is_authenticated", False):
            target_filter |= Q(access_target_id=user_id)
            group_ids = list(user.groups.values_list("id", flat=True))
            if group_ids:
                target_filter |= Q(access_target_group_id__in=group_ids)
        if token_value:
            target_filter |= Q(public_share_token=token_value)

        # An empty Q() (anonymous caller, no groups, no token) is falsy via
        # Django's tree.Node.__bool__; the guard skips the query in that case
        # so we never accidentally match rows on the can_read=True filter alone.
        if target_filter:
            # Per-target uniqueness (AccessRight.Meta.constraints) means at most
            # one row per target can match, but a caller can still match
            # through several targets at once — their own user row plus group
            # rows, or a group row plus a presented share token. The ordering
            # makes which row wins deterministic, in two tiers. The caller's
            # direct user row wins outright, whatever its apply_middleware —
            # the same explicit-grants-win rule that already lets a direct row
            # beat a more-sanitizing extension grant (see the README gotcha).
            # That precedence can serve raw where a group row would sanitize,
            # and it is bounded by the grant capping in epicurrents/granting.py:
            # only a grantor whose own read access is already raw (author,
            # superuser, raw-holding sharer) can create a raw direct row.
            # Among the remaining group and token rows, the de-identifying row
            # (apply_middleware=True) wins, so within that tier an ambiguous
            # overlap never serves raw bytes a stricter grant would have
            # sanitized.
            right = (
                base_qs.filter(target_filter)
                .order_by(F("access_target_id").asc(nulls_last=True), "-apply_middleware")
                .only("apply_middleware")
                .first()
            )
            if right is not None:
                return ReadAccessTerms(granted=True, apply_middleware=right.apply_middleware)

    # Extension fallback — only reached when no direct AccessRight matched.
    # Extensions may return either a plain bool or a ReadAccessTerms.  A bool True
    # is treated as ReadAccessTerms(granted=True) with default metadata
    # (apply_middleware=False).  Returning a ReadAccessTerms lets the extension
    # propagate access-right metadata such as apply_middleware to the caller.
    for checker in _READ_PERMISSION_EXTENSIONS:
        result = checker(user=user, obj=obj, share_token=share_token)
        if isinstance(result, ReadAccessTerms):
            if result.granted:
                return result
        elif result:
            return ReadAccessTerms(granted=True)

    return ReadAccessTerms(granted=False)


def get_federated_read_access_result(peer, remote_user_id: str, obj) -> ReadAccessTerms:
    """Return a ReadAccessTerms for a federated peer's user accessing *obj*.

    Matches ``AccessRight`` rows where ``federated_peer`` equals *peer* and
    ``remote_user_id`` is blank (wildcard — grants access to any authenticated
    user from that peer) or exactly matches *remote_user_id*.

    Returns ``ReadAccessTerms`` with ``apply_middleware`` from the matching row, or
    ``granted=False`` when no matching row exists.  Expired grants are excluded.

    Unlike :func:`get_read_access_result`, there is no superuser or author
    fast-path: federated peers are always governed by explicit grants.
    Read-visibility gates apply here too, consulted with ``user=None``.
    """
    permission_model = _resolve_read_permission_model()
    if permission_model is None or peer is None:
        return ReadAccessTerms(granted=False)

    object_pk = getattr(obj, "pk", None)
    if object_pk is None:
        return ReadAccessTerms(granted=False)

    # Peer callers hold no local identity, so gates see the fully
    # unprivileged caller shape.
    if _hidden_by_visibility_gate(user=None, obj=obj):
        return ReadAccessTerms(granted=False)

    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    now = timezone.now()

    right = (
        permission_model.objects.filter(
            content_type=content_type,
            object_id=str(object_pk),
            can_read=True,
            federated_peer=peer,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .filter(Q(remote_user_id="") | Q(remote_user_id=remote_user_id))
        # A wildcard row (remote_user_id="") and an exact-user row may both
        # match; uniqueness guarantees at most one of each. Descending order
        # puts the exact row first, so the specific grant's terms win over the
        # peer-wide default.
        .order_by("-remote_user_id")
        .only("apply_middleware")
        .first()
    )

    if right is not None:
        return ReadAccessTerms(granted=True, apply_middleware=right.apply_middleware)

    # Extensions last, and only on a miss, mirroring the local resolver: a direct
    # row is the sharer's specific decision about this object and outranks anything
    # inherited. Without this loop a peer reaches only objects granted one by one,
    # which is why sharing a dataset with a peer conveyed nothing at all.
    #
    # Every extension is consulted rather than the first grant returned, because
    # two of them reaching the same object is a tie, and the de-identifying terms
    # win it — the same rule get_federated_visible_terms applies to the listing
    # beside this. Returning early would decide the tie by registration order,
    # which is the order AppConfig.ready() happened to run in, and would let the
    # listing advertise a de-identified object this path then serves raw.
    granted = None
    for extension in _FEDERATED_READ_EXTENSIONS:
        terms = extension.check(peer, remote_user_id, obj)
        if terms is None or not terms.granted:
            continue
        if granted is None or (terms.apply_middleware and not granted.apply_middleware):
            granted = terms
    return granted if granted is not None else ReadAccessTerms(granted=False)


def can_read_object(user: Any, obj: Any, share_token: str | None = None) -> bool:
    """Return True when object read permission exists for user/group/token target.

    Thin wrapper around :func:`get_read_access_result` for callers that only
    need a boolean answer.  Use ``get_read_access_result`` directly when
    access-right metadata (e.g. ``apply_middleware``) is also needed.
    """
    return get_read_access_result(user=user, obj=obj, share_token=share_token).granted


def can_write_object(user: Any, obj: Any) -> bool:
    """Return True when user may write the object.

    Grants access to superusers and the object author (via can_modify_object),
    and also to any user or group that holds an active (non-expired) can_write
    AccessRight for the object.
    """

    if can_modify_object(user=user, obj=obj):
        return True

    permission_model = _resolve_read_permission_model()
    if permission_model is None:
        return False

    manager = getattr(permission_model, "objects", None)
    if manager is None:
        return False

    object_pk = getattr(obj, "pk", None)
    if object_pk is None:
        return False

    user_id = getattr(user, "pk", None)
    if user_id is None or not getattr(user, "is_authenticated", False):
        return False

    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    now = timezone.now()

    target_filter = Q(access_target_id=user_id)
    group_ids = list(user.groups.values_list("id", flat=True))
    if group_ids:
        target_filter |= Q(access_target_group_id__in=group_ids)

    return (
        manager.filter(
            content_type=content_type,
            object_id=str(object_pk),
            can_write=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .filter(target_filter)
        .exists()
    )


def can_annotate_object(
    user: Any,
    obj: Any,
    share_token: str | None = None,
    annotator: str | None = None,
) -> bool:
    """Return True when user may add personal annotations to obj.

    Lower bar than write access: object authorship or any read right suffices.
    Superusers and object authors are always granted (via can_modify_object).

    Share-token access additionally requires a non-empty *annotator* identifier
    so that annotations can be attributed to a named person rather than the
    anonymous token holder.  If a share_token is supplied but annotator is
    absent or blank the function returns False.

    No annotation endpoint accepts ``share_token`` today, so the token branch
    is currently unreachable from the live API; the validation stays in place
    as the contract for the eventual share-token annotation flow.  See
    ROADMAP — *Annotations — wire share-token attribution end-to-end*.
    """
    if share_token and not (annotator or "").strip():
        return False
    return can_modify_object(user=user, obj=obj) or can_read_object(user=user, obj=obj, share_token=share_token)


def ensure_can_annotate_object(
    user: Any,
    obj: Any,
    share_token: str | None = None,
    annotator: str | None = None,
) -> None:
    """Raise HttpError(400/403) if user may not annotate obj.

    400 when a share_token is provided without an annotator identifier.
    403 when access is simply not granted.
    """
    if share_token and not (annotator or "").strip():
        raise HttpError(
            400,
            "An annotator identifier is required when accessing via a share token",
        )
    if not can_annotate_object(user=user, obj=obj, share_token=share_token, annotator=annotator):
        _log_permission_denied("annotate", user, obj)
        raise HttpError(403, "You do not have permission to annotate this object")


def ensure_can_modify_object(user: Any, obj: Any, author_field: str = "author") -> None:
    """Raise 403 if user is not allowed to modify the object."""

    if not can_modify_object(user=user, obj=obj, author_field=author_field):
        _log_permission_denied("modify", user, obj)
        raise HttpError(403, "You do not have permission to modify this object")


def ensure_can_write_object(user: Any, obj: Any) -> None:
    """Raise 403 if user does not have write access to the object."""

    if not can_write_object(user=user, obj=obj):
        _log_permission_denied("write", user, obj)
        raise HttpError(403, "You do not have permission to modify this object")


def ensure_can_read_object(user: Any, obj: Any, share_token: str | None = None) -> None:
    """Raise 403 if user/token target is not allowed to read the object."""

    if not can_read_object(user=user, obj=obj, share_token=share_token):
        _log_permission_denied("read", user, obj)
        raise HttpError(403, "You do not have permission to view this object")
