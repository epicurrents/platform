"""Activity v1 REST endpoints — change-log listing, single + bulk rollback."""

from datetime import datetime

from django.conf import settings
from django.db import transaction
from ninja import NinjaAPI, Query, Schema
from ninja.errors import HttpError

from activity.audit import (
    ChangeHashMismatch,
    can_rollback_change,
    log_activity,
    rollback_change,
    verify_change_hash,
)
from activity.models import ObjectChangeLog
from epicurrents.auth import enforce_session_csrf

api = NinjaAPI(
    title="Activity API",
    version="1",
    urls_namespace="activity-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)


class RollbackOut(Schema):
    """Response payload returned after a successful single rollback."""

    model: str
    object_id: str  # stringified PK of the target object
    change_id: int
    status: str  # currently always the literal "rolled_back"; reserved for future expansion


class BulkRollbackIn(Schema):
    """Request body for the bulk-rollback endpoint."""

    change_ids: list[int]


class BulkRollbackItemOut(Schema):
    """Per-item result within a bulk rollback response."""

    change_id: int
    model: str
    object_id: str  # stringified PK of the target object
    status: str  # currently always the literal "rolled_back"; reserved for future expansion


class BulkRollbackOut(Schema):
    """Response payload returned after a successful bulk rollback."""

    rolled_back: int  # number of change log entries successfully rolled back
    results: list[BulkRollbackItemOut]


class ChangeLogOut(Schema):
    """List item schema for rollback-discoverable change log entries."""

    id: int
    action: str
    model: str
    object_id: str  # stringified PK of the target object
    project: str
    performed_by: int | None = None  # PK of the User who performed the change; null for system/unauthenticated actions
    activity_id: int | None = None  # PK of the parent Activity row, if the change was made within an API request
    after_hash: str  # 32-char integrity hash from audit.compute_audit_hash
    verified: bool  # True when after_hash matches a fresh recompute of the row's contents; False on tamper / corruption
    erased: bool  # True when the row was subject-erased (GDPR Art. 17); payload is scrubbed and rollback is refused
    created_at: datetime


def _require_auth(request):
    """Return authenticated user or raise 401 for unauthenticated requests."""

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


@api.post("/rollback/bulk", response=BulkRollbackOut)
def rollback_bulk_endpoint(request, payload: BulkRollbackIn):
    """Roll back a list of change log entries atomically.

    All rollbacks succeed or none do — the entire operation runs inside a
    single database transaction.  A pre-flight pass validates existence and
    permission for every ID before any data is touched; the first failure
    aborts with the appropriate HTTP error.

    This is the intended recovery path after ``generate_epochs --clear
    --clear-labels``, where each deleted label and epoch has its own change
    log entry.  Retrieve the relevant IDs via ``GET /changes/?model=label``
    (or ``model=recordingepoch``) filtered by project and time, then pass
    them here.

    Rolling back a CREATE entry deletes the object; rolling back a DELETE
    entry recreates it.  Each rollback is itself logged as ACTION_ROLLBACK,
    so the bulk operation is fully undoable entry by entry.
    """
    user = _require_auth(request)

    if not payload.change_ids:
        return BulkRollbackOut(rolled_back=0, results=[])

    # ── Pre-flight: fetch all entries and check permissions ───────────────
    # Do this before touching any data so a single bad ID aborts cleanly.
    # Integrity is also verified up front: any tampered / corrupted row in
    # the batch aborts the whole bulk operation before the transaction opens.
    changes: dict[int, ObjectChangeLog] = {}
    for change_id in payload.change_ids:
        try:
            change = ObjectChangeLog.objects.select_related("content_type").get(pk=change_id)
        except ObjectChangeLog.DoesNotExist:
            raise HttpError(404, f"Change log entry {change_id} not found.")
        if not can_rollback_change(user, change):
            raise HttpError(
                403,
                f"You do not have permission to rollback change log entry {change_id}.",
            )
        changes[change_id] = change

    # ── Execute all rollbacks in one transaction ──────────────────────────
    results: list[dict] = []
    try:
        with transaction.atomic():
            for change_id in payload.change_ids:
                restored = rollback_change(user=user, change_id=change_id)
                change = changes[change_id]
                if restored is not None:
                    results.append(
                        {
                            "change_id": change_id,
                            "model": restored._meta.label,
                            "object_id": str(restored.pk),
                            "status": "rolled_back",
                        }
                    )
                else:
                    # CREATE rollback: object was deleted; use stored identifiers.
                    results.append(
                        {
                            "change_id": change_id,
                            "model": change.content_type.model,
                            "object_id": change.object_id,
                            "status": "rolled_back",
                        }
                    )
    except ChangeHashMismatch as exc:
        # rollback_change emits the security event before raising; the
        # transaction has already been rolled back by exit. Surface a 409
        # so the caller sees "row integrity issue, can't apply this rollback".
        raise HttpError(409, str(exc))
    except ValueError as exc:
        # Non-restorable entry in the batch (subject-erased row, erasure
        # record, vanished model); the transaction rolled back on exit.
        raise HttpError(400, str(exc))

    log_activity(
        verb="activity.rollback.bulk",
        metadata={
            "change_ids": payload.change_ids,
            "rolled_back_count": len(results),
        },
    )

    return BulkRollbackOut(rolled_back=len(results), results=results)


@api.post("/rollback/{change_id}", response=RollbackOut)
def rollback_change_endpoint(request, change_id: int):
    """Rollback object state using a specific change log entry id.

    Rolling back a CREATE entry deletes the created object — this is a
    destructive action.  The deletion is itself logged as ACTION_ROLLBACK with
    the deleted state captured in ``before_state``, so it can be undone by
    rolling back that rollback entry.
    """
    user = _require_auth(request)

    try:
        restored = rollback_change(user=user, change_id=change_id)
    except ObjectChangeLog.DoesNotExist:
        raise HttpError(404, "Change log entry not found")
    except PermissionError:
        raise HttpError(403, "You do not have permission to rollback this object state")
    except ChangeHashMismatch as exc:
        raise HttpError(409, str(exc))
    except ValueError as exc:
        raise HttpError(400, str(exc))

    # ``change`` is needed both for the audit-row target and (in the
    # CREATE-rollback branch) to resolve the response identifiers, since
    # the underlying object was deleted by rollback_change.
    change = ObjectChangeLog.objects.select_related("content_type").get(pk=change_id)

    if restored is not None:
        model_label = restored._meta.label
        target_object_id = str(restored.pk)
    else:
        model_label = change.content_type.model
        target_object_id = change.object_id

    # target=change records the rolled-back ObjectChangeLog; the underlying
    # restored / deleted object is captured by the linked ObjectChangeLog
    # row produced by rollback_change itself, joinable via activity_id.
    log_activity(verb="activity.rollback", target=change)

    return {
        "model": model_label,
        "object_id": target_object_id,
        "change_id": change_id,
        "status": "rolled_back",
    }


@api.get("/changes/", response=list[ChangeLogOut])
def list_change_logs(
    request,
    limit: int = Query(50, ge=1, le=200),
    action: str | None = Query(None),
    model: str | None = Query(None),
    activity_id: int | None = Query(None),
):
    """List recent change logs that the caller is allowed to rollback.

    Query parameters:
        limit: maximum number of entries to return (1-200, default 50).
        action: filter by change action — one of ``create``, ``modify``,
            ``delete``, ``rollback``. Case-insensitive; omit for no filter.
        model: filter by target model name as stored on ``ContentType.model``
            (e.g. ``recording``, ``label``, ``recordingepoch``). Case-insensitive;
            omit for no filter.
        activity_id: filter to entries belonging to a single ``Activity`` row.
            Useful for discovering the full set of rows produced by a cascade
            deletion, which all share the same parent ``Activity``.
    """

    user = _require_auth(request)

    queryset = ObjectChangeLog.objects.select_related("content_type", "performed_by", "activity").order_by(
        "-created_at"
    )
    if action:
        normalized_action = action.strip().lower()
        if normalized_action not in {
            ObjectChangeLog.ACTION_CREATE,
            ObjectChangeLog.ACTION_MODIFY,
            ObjectChangeLog.ACTION_DELETE,
            ObjectChangeLog.ACTION_ROLLBACK,
        }:
            raise HttpError(
                400,
                "Invalid action filter. Use 'create', 'modify', 'delete', or 'rollback'.",
            )
        queryset = queryset.filter(action=normalized_action)

    if model:
        queryset = queryset.filter(content_type__model=model.strip().lower())

    if activity_id is not None:
        queryset = queryset.filter(activity_id=activity_id)

    if not getattr(user, "is_superuser", False):
        queryset = [change for change in queryset[:1000] if can_rollback_change(user, change)]
        queryset = queryset[:limit]
    else:
        queryset = list(queryset[:limit])

    log_activity(
        verb="activity.changelog.list",
        metadata={
            "limit": limit,
            "action_filter": action,
            "model_filter": model,
            "activity_id_filter": activity_id,
            "returned_count": len(queryset),
        },
    )

    return [
        {
            "id": change.pk,
            "action": change.action,
            "model": change.content_type.model,
            "object_id": change.object_id,
            "project": change.project,
            "performed_by": change.performed_by_id,
            "activity_id": change.activity_id,
            "after_hash": change.after_hash,
            "verified": verify_change_hash(change),
            "erased": change.erased_at is not None,
            "created_at": change.created_at,
        }
        for change in queryset
    ]
