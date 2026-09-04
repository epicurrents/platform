"""Library API v1 — Collections, Datasets, and Tags with generic item membership.

Endpoints
---------
Collections
    POST   /collections/                     Create a collection.
    GET    /collections/                     List collections (root or children of parent).
    GET    /collections/{id}/                Retrieve a collection.
    PATCH  /collections/{id}/                Update name, description, or parent.
    DELETE /collections/{id}/                Soft-delete a collection.
    GET    /collections/{id}/items/          List items in the collection.
    POST   /collections/{id}/items/          Add an object to the collection.
    DELETE /collections/{id}/items/{iid}/   Remove an item from the collection.
    POST   /collections/{id}/export/         Export the subtree into a new dataset.

Datasets
    POST   /datasets/                        Create a dataset.
    GET    /datasets/                        List datasets.
    GET    /datasets/{id}/                   Retrieve a dataset.
    PATCH  /datasets/{id}/                   Update name, description, viewer config, licence.
    DELETE /datasets/{id}/                   Soft-delete a dataset.
    GET    /datasets/{id}/items/             List items in the dataset.
    POST   /datasets/{id}/items/             Add an object to the dataset.
    DELETE /datasets/{id}/items/{iid}/      Remove an item from the dataset.
    POST   /datasets/{id}/items/{iid}/move   Place an item in a folder (or the root).
    GET    /datasets/{id}/folders/           List the folder tree.
    POST   /datasets/{id}/folders/           Create a folder.
    PATCH  /datasets/{id}/folders/{fid}/     Rename, move, or reposition a folder.
    DELETE /datasets/{id}/folders/{fid}/     Delete a folder (items fall to the root).
    GET    /datasets/{id}/access/            List access rights.
    POST   /datasets/{id}/access/            Grant an access right.
    DELETE /datasets/{id}/access/{rid}/     Revoke an access right.
    POST   /datasets/{id}/snapshots/         Seal current membership (create-only).
    GET    /datasets/{id}/snapshots/         List snapshots, newest first.
    GET    /datasets/snapshots/{hash}/       One snapshot, manifest included.

Tags
    GET    /tags/                            List tags (root or children of parent).
    POST   /tags/                            Create a tag.
    GET    /tags/{id}/                       Retrieve a tag.
    PATCH  /tags/{id}/                       Update a tag.
    DELETE /tags/{id}/                       Delete a tag.
    GET    /tags/{id}/items/                 List tagged items (optionally with descendants).
    POST   /tags/{id}/items/                 Tag an object.
    DELETE /tags/{id}/items/{iid}/          Untag an object.
"""

import hashlib
import json
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone
from ninja import NinjaAPI, Query, Schema
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.api.schemas import AccessRightOut, access_right_out
from epicurrents.auth import enforce_session_csrf
from epicurrents.granting import ensure_can_confer, ensure_can_manage_access, ensure_can_revoke
from epicurrents.models import AccessRight
from epicurrents.permissions import can_modify_object
from library.models import (
    Collection,
    CollectionItem,
    Dataset,
    DatasetFolder,
    DatasetItem,
    DatasetMeta,
    DatasetSnapshot,
    Tag,
    TaggedItem,
)
from library.permissions import (
    ensure_can_read_collection,
    ensure_can_write_collection,
)

api = NinjaAPI(
    title="Library API",
    version="1",
    urls_namespace="library-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CollectionIn(Schema):
    """Payload for creating a collection."""

    name: str
    description: str = ""
    parent_id: int | None = None


class DatasetIn(Schema):
    """Payload for creating a dataset with an initial set of recordings."""

    name: str
    description: str = ""
    recording_hashes: list[str] = []


class CollectionPatchIn(Schema):
    """Payload for partial update of a collection."""

    name: str | None = None
    description: str | None = None
    parent_id: int | None = None
    # Datasets only — per-dataset viewer-config overrides. Ignored for collections.
    viewer_config: dict | None = None
    # Datasets only — the DatasetMeta licence pair. Ignored for collections;
    # empty string clears a previously declared value.
    license_spdx: str | None = None
    license_url: str | None = None


class CollectionOut(Schema):
    """Collection response."""

    id: int
    name: str
    description: str
    parent_id: int | None
    author_id: int
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None
    # Per-dataset viewer-config overrides; always empty for collections.
    viewer_config: dict = {}
    # Datasets only — opaque public identifier; null for collections.
    object_hash: str | None = None
    # Datasets only — the DatasetMeta licence pair; null for collections and
    # for datasets with nothing declared.
    license_spdx: str | None = None
    license_url: str | None = None


class CollectionItemIn(Schema):
    """Payload for adding an object to a collection."""

    content_type_id: int
    object_id: str


class MoveCollectionItemIn(Schema):
    """Payload for moving an item to a different collection."""

    target_collection_id: int


class CollectionItemOut(Schema):
    """Collection item response.

    ``object_name`` and ``object_hash`` are populated for known content types
    (``recordings.Recording`` and ``media.MediaFile`` today) so the frontend
    can display a human-readable label and navigate to the object without
    knowing its internal primary key.

    ``object_type`` is the lowercased model name — frontends dispatch on it
    to pick the right row component (e.g. ``"recording"`` vs ``"mediafile"``).

    ``media_type``, ``file_extension``, and ``is_supported`` are populated
    only for ``media.MediaFile`` items: ``is_supported`` reflects whether
    the file's extension is in the live ``MEDIA_ALLOWED_UPLOAD_EXTENSIONS``,
    so the frontend can grey out items the current project can no longer
    open without hiding them from the list.
    """

    id: int
    content_type_id: int
    object_id: str
    added_at: datetime
    # Dataset items only — the containing folder, or null for the dataset root.
    folder_id: int | None = None
    object_name: str | None = None
    object_hash: str | None = None
    object_type: str | None = None
    media_type: str | None = None
    file_extension: str | None = None
    is_supported: bool | None = None


class BulkRenameRecordingsIn(Schema):
    """Payload for the per-collection recording bulk-rename action."""

    prefix: str = "Recording"


class BulkRenameRecordingsOut(Schema):
    """Result of the per-collection recording bulk-rename action."""

    renamed: int
    skipped: int


class GrantAccessIn(Schema):
    """Payload for granting an access right on a collection or dataset."""

    access_target_id: int | None = None
    access_target_group_id: int | None = None
    public_share_token: str | None = None
    can_read: bool = True
    can_write: bool = False
    can_share: bool = False
    apply_middleware: bool = True
    expires_at: datetime | None = None


class TagIn(Schema):
    """Payload for creating a tag."""

    name: str
    description: str = ""
    parent_id: int | None = None


class TagPatchIn(Schema):
    """Payload for partial update of a tag."""

    name: str | None = None
    description: str | None = None
    parent_id: int | None = None


class TagOut(Schema):
    """Tag response."""

    id: int
    name: str
    description: str
    parent_id: int | None
    author_id: int
    created_at: datetime
    modified_at: datetime


class TaggedItemOut(Schema):
    """Tagged item response."""

    id: int
    tag_id: int
    content_type_id: int
    object_id: str
    tagged_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_auth(request):
    """Return authenticated user or raise 401."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


def _get_active_collection(collection_id: int) -> Collection:
    """Return active (non-deleted) collection or raise 404."""
    collection = Collection.objects.filter(pk=collection_id, deleted_at__isnull=True).first()
    if collection is None:
        raise HttpError(404, "Collection not found")
    return collection


def _subtree_collection_ids(root_id: int) -> set[int]:
    """Return the ids of *root_id* and every collection beneath it in the tree."""
    ids = {root_id}
    frontier = [root_id]
    while frontier:
        children = list(Collection.objects.filter(parent_id__in=frontier).values_list("pk", flat=True))
        frontier = [pk for pk in children if pk not in ids]
        ids.update(frontier)
    return ids


def _resolve_parent(parent_id: int | None, user) -> Collection | None:
    """Validate and return parent collection, or None for root."""
    if parent_id is None:
        return None
    parent = Collection.objects.filter(pk=parent_id, deleted_at__isnull=True).first()
    if parent is None:
        raise HttpError(404, f"Parent collection {parent_id} not found")
    # Collections are author-private: only the parent's author or a superuser
    # may place children in it.
    is_author = parent.author_id == getattr(user, "pk", None)
    if not is_author and not getattr(user, "is_superuser", False):
        ensure_can_read_collection(user=user, collection=parent)
    return parent


def _resolve_media_object_id(object_id: str) -> str:
    """Resolve a media-file content hash or integer PK string to an integer PK string.

    The media API exposes ``content_hash`` (not integer PKs) so callers
    adding media to collections or datasets pass the hash. Integer PK
    strings are also accepted for parity with the recording resolver
    and to keep internal callers (admin, management commands) simple.
    Raises HttpError(400/404) for invalid or missing values.
    """
    from media.models import MediaFile

    # Integer-PK shortcut: trust the value but verify the row exists.
    try:
        int(object_id)
        if not MediaFile.objects.filter(pk=object_id, deleted_at__isnull=True).exists():
            raise HttpError(404, "Media file not found")
        return object_id
    except ValueError:
        pass

    # Treat as a content_hash (case-insensitive hex).
    normalized = object_id.strip().upper()
    if not normalized or not normalized.isalnum():
        raise HttpError(400, "Invalid media identifier (expected content hash)")

    # Exact match, same as media's _get_media_or_404: values are written upper-case and normalised here.
    media = MediaFile.objects.filter(content_hash=normalized, deleted_at__isnull=True).only("id").first()
    if media is None:
        raise HttpError(404, "Media file not found")
    return str(media.pk)


def _resolve_recording_object_id(object_id: str) -> str:
    """Resolve a recording hash or integer PK string to an integer PK string.

    The recordings list API exposes hashes (not integer PKs) so callers
    adding recordings to collections or datasets pass the hash.  Integer PK
    strings are also accepted for backwards-compatibility and internal use.
    Raises HttpError(400/404) for invalid or missing values.
    """
    from recordings.models import Recording

    # If the value is an integer, validate the recording exists.
    try:
        int(object_id)
        if not Recording.objects.filter(pk=object_id, deleted_at__isnull=True).exists():
            raise HttpError(404, "Recording not found")
        return object_id
    except ValueError:
        pass

    # Treat as 32-character hex hash.
    normalized = object_id.strip().upper()
    if len(normalized) != 32 or not normalized.isalnum():
        raise HttpError(400, "Invalid recording identifier (expected 32-char hash)")

    rec = Recording.objects.filter(stored_name__startswith=f"{normalized}.", deleted_at__isnull=True).only("id").first()
    if rec is None:
        raise HttpError(404, "Recording not found")
    return str(rec.pk)


def _enrich_collection_items(items: list, user) -> list[dict]:
    """Attach resolved display metadata to a list of CollectionItem or DatasetItem rows.

    For ``recordings.Recording`` items the returned dicts include
    ``object_name`` (the display name resolved for *user*) and ``object_hash``
    (the public hash used in viewer URLs).

    For ``media.MediaFile`` items the dicts additionally include
    ``media_type``, ``file_extension``, and ``is_supported`` so the
    frontend can render a media-appropriate row and grey out items the
    current project can no longer open. Unsupported items are kept in the
    list (greyed on the frontend) — only soft-deleted ones are dropped.

    ``object_name`` is author-aware for recordings: the author and superusers
    see ``original_name`` until they set a custom ``display_name``, while
    grantees always get the resolved display name. The original filename can
    carry PHI, so it never reaches a non-author surface — the same gate as
    ``_can_see_original_name`` in the recordings API.

    Soft-deleted recordings and FAILED uploads are silently dropped from the
    enriched result — they are not surfaced through collection or dataset
    listings to anyone (the author retains visibility via the dedicated
    recordings list). Soft-deleted media files are dropped on the same
    grounds.
    """
    from media.api.v1.ninja import _is_supported
    from media.api.v1.ninja import _resolve_display_name as _media_display_name
    from media.models import MediaFile
    from recordings.api.v1.ninja import _can_see_original_name, _resolve_display_name
    from recordings.models import Recording

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    media_ct = ContentType.objects.get_for_model(MediaFile, for_concrete_model=False)

    recording_pks = {item.object_id for item in items if item.content_type_id == recording_ct.id}
    media_pks = {item.object_id for item in items if item.content_type_id == media_ct.id}

    recording_map: dict[str, tuple[str, str]] = {}
    if recording_pks:
        for rec in (
            Recording.objects.filter(pk__in=recording_pks, deleted_at__isnull=True)
            .exclude(status=Recording.Status.FAILED)
            .only("id", "display_name", "original_name", "author_id", "stored_name")
        ):
            # Author and superusers keep seeing the original filename until a
            # grantee-safe label is set; grantees (and any labeled recording)
            # get the resolved display name, so the filename's potential PHI
            # never reaches a non-author surface.
            if not (rec.display_name or "").strip() and _can_see_original_name(user, rec, None):
                name = rec.original_name
            else:
                name = _resolve_display_name(rec)
            recording_map[str(rec.pk)] = (
                name,
                rec.stored_name.split(".", 1)[0],
            )

    media_map: dict[str, dict] = {}
    if media_pks:
        for m in MediaFile.objects.filter(pk__in=media_pks, deleted_at__isnull=True).only(
            "id",
            "display_name",
            "stored_name",
            "content_hash",
            "media_type",
            "file_extension",
        ):
            media_map[str(m.pk)] = {
                "object_name": _media_display_name(m),
                "object_hash": m.content_hash,
                "media_type": m.media_type,
                "file_extension": m.file_extension,
                "is_supported": _is_supported(m),
            }

    result = []
    for item in items:
        base = {
            "id": item.id,
            "content_type_id": item.content_type_id,
            "object_id": item.object_id,
            "added_at": item.added_at,
            "folder_id": getattr(item, "folder_id", None),
            "object_name": None,
            "object_hash": None,
            "object_type": None,
            "media_type": None,
            "file_extension": None,
            "is_supported": None,
        }
        if item.content_type_id == recording_ct.id:
            entry = recording_map.get(item.object_id)
            if entry is None:
                # Recording is soft-deleted, FAILED, or missing — drop from response.
                continue
            base["object_name"], base["object_hash"] = entry
            base["object_type"] = "recording"
        elif item.content_type_id == media_ct.id:
            entry = media_map.get(item.object_id)
            if entry is None:
                # Media file is soft-deleted or missing — drop from response.
                continue
            base.update(entry)
            base["object_type"] = "mediafile"
        result.append(base)
    return result


# ---------------------------------------------------------------------------
# Collection CRUD
# ---------------------------------------------------------------------------


@api.post("/collections/", response={201: CollectionOut})
def create_collection(request, payload: CollectionIn):
    """Create a new collection.

    Collections are author-private; the author's access is implicit and no
    AccessRight row is created. Set ``parent_id`` to nest the collection
    inside an existing one.
    """
    user = _require_auth(request)
    parent = _resolve_parent(payload.parent_id, user)

    with transaction.atomic():
        collection = Collection.objects.create(
            author=user,
            name=payload.name,
            description=payload.description,
            parent=parent,
        )
        log_activity(verb="library.collection.create", target=collection)
        return 201, collection


@api.get("/collections/", response=list[CollectionOut])
def list_collections(
    request,
    parent_id: int | None = Query(None, description="List children of this collection. Omit for root collections."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    trash: bool = Query(False, description="List soft-deleted collections instead of active ones"),
):
    """List collections accessible to the caller.

    - Omit ``parent_id`` to browse root-level collections (no parent).
    - Pass ``parent_id`` to browse a collection's direct children; requires
      read access to the parent (its author or a superuser).
    - Pass ``trash=true`` to list your own soft-deleted collections.
    """
    user = _require_auth(request)

    if parent_id is not None:
        parent = Collection.objects.filter(pk=parent_id, deleted_at__isnull=True).first()
        if parent is None:
            raise HttpError(404, "Parent collection not found")
        ensure_can_read_collection(user=user, collection=parent)
        qs = Collection.objects.filter(
            parent_id=parent_id,
            deleted_at__isnull=not trash,
        ).order_by("name")
        visible = list(qs[offset : offset + limit])
    elif trash:
        qs = Collection.objects.filter(
            deleted_at__isnull=False,
            author=user,
        ).order_by("-deleted_at")
        visible = list(qs[offset : offset + limit])
    elif getattr(user, "is_superuser", False):
        qs = Collection.objects.filter(parent__isnull=True, deleted_at__isnull=True).order_by("name")
        visible = list(qs[offset : offset + limit])
    else:
        # Collections are author-private, so a plain author filter is the
        # complete visibility rule — no per-row permission walk.
        qs = Collection.objects.filter(parent__isnull=True, deleted_at__isnull=True, author=user).order_by("name")
        visible = list(qs[offset : offset + limit])

    log_activity(
        verb="library.collection.list",
        metadata={
            "parent_id": parent_id,
            "limit": limit,
            "offset": offset,
            "trash": trash,
            "returned_count": len(visible),
        },
    )
    return visible


@api.get("/collections/{collection_id}/", response=CollectionOut)
def get_collection(request, collection_id: int):
    """Retrieve a single collection by ID."""
    user = _require_auth(request)
    collection = _get_active_collection(collection_id)
    if not (getattr(user, "is_superuser", False) or collection.author_id == user.pk):
        ensure_can_read_collection(user=user, collection=collection)
    log_activity(verb="library.collection.read", target=collection)
    return collection


@api.patch("/collections/{collection_id}/", response=CollectionOut)
def update_collection(request, collection_id: int, payload: CollectionPatchIn):
    """Update collection name, description, or parent.

    Requires write access (author, superuser, or can_write AccessRight).
    Moving a collection to a new parent requires read access to the new parent.
    """
    user = _require_auth(request)
    collection = _get_active_collection(collection_id)
    ensure_can_write_collection(user=user, collection=collection)

    fields_updated: list[str] = []
    if payload.name is not None:
        collection.name = payload.name
        fields_updated.append("name")
    if payload.description is not None:
        collection.description = payload.description
        fields_updated.append("description")
    if "parent_id" in payload.model_fields_set:
        parent = _resolve_parent(payload.parent_id, user)
        if payload.parent_id is not None:
            _check_no_cycle(collection, payload.parent_id)
        collection.parent = parent
        fields_updated.append("parent_id")

    with transaction.atomic():
        collection.save()
        log_activity(
            verb="library.collection.update",
            target=collection,
            metadata={"fields_updated": fields_updated},
        )
    return collection


def _check_no_cycle(collection: Collection, new_parent_id: int):
    """Raise 400 if setting new_parent_id would create a cycle."""
    visited = {collection.pk}
    cursor_id = new_parent_id
    while cursor_id is not None:
        if cursor_id in visited:
            raise HttpError(400, "Cannot move a collection into one of its own descendants")
        visited.add(cursor_id)
        row = Collection.objects.filter(pk=cursor_id).values("parent_id").first()
        if row is None:
            break
        cursor_id = row["parent_id"]


@api.delete("/collections/{collection_id}/")
def delete_collection(request, collection_id: int):
    """Soft-delete a collection and everything filed under it.

    Recursively soft-deletes the collection, all its sub-collections, and every
    item membership beneath them — the whole subtree moves to the trash at once,
    sharing one ``deleted_at`` timestamp so a later restore lifts exactly the
    rows this action trashed. The referenced objects themselves (recordings,
    media) are untouched; only their membership is trashed, so a recording whose
    only collection is trashed reappears at the library root and stays a
    first-class, deletable object. Restore via
    ``POST /collections/{collection_id}/restore``.
    """
    user = _require_auth(request)
    collection = _get_active_collection(collection_id)
    ensure_can_write_collection(user=user, collection=collection)

    with transaction.atomic():
        now = timezone.now()
        subtree_ids = _subtree_collection_ids(collection.pk)
        collections = list(Collection.objects.filter(pk__in=subtree_ids, deleted_at__isnull=True))
        items = list(CollectionItem.objects.filter(collection_id__in=subtree_ids, deleted_at__isnull=True))
        # Per-object saves (not a bulk update) so the audit signals fire for
        # every trashed row.
        for col in collections:
            col.deleted_at = now
            col.save(update_fields=["deleted_at", "modified_at"])
        for item in items:
            item.deleted_at = now
            item.save(update_fields=["deleted_at"])
        log_activity(
            verb="library.collection.trash",
            target=collection,
            metadata={
                "collections_trashed": len(collections),
                "items_trashed": len(items),
            },
        )
    return {"status": "ok"}


@api.post("/collections/{collection_id}/restore")
def restore_collection(request, collection_id: int):
    """Restore a soft-deleted collection and the subtree trashed with it.

    Lifts the collection, its sub-collections, and their memberships back out of
    the trash — exactly the rows trashed together with it, matched by the shared
    ``deleted_at`` timestamp (so a sub-collection trashed separately, earlier,
    stays trashed). A membership is skipped when the object has since been filed
    into a live collection: an explicit re-filing wins over the restore, so the
    object stays where the user last put it.
    """
    user = _require_auth(request)
    collection = Collection.objects.filter(pk=collection_id, deleted_at__isnull=False).first()
    if collection is None:
        raise HttpError(404, "Trashed collection not found")
    ensure_can_write_collection(user=user, collection=collection)

    trashed_at = collection.deleted_at
    with transaction.atomic():
        subtree_ids = _subtree_collection_ids(collection.pk)
        collections = list(Collection.objects.filter(pk__in=subtree_ids, deleted_at=trashed_at))
        items = list(CollectionItem.objects.filter(collection_id__in=subtree_ids, deleted_at=trashed_at))
        for col in collections:
            col.deleted_at = None
            col.save(update_fields=["deleted_at", "modified_at"])
        restored_items = 0
        skipped_items = 0
        for item in items:
            # The object may have been re-filed into a live collection while this
            # one sat in the trash; un-trashing would then breach the one-active-
            # membership rule, so leave it trashed — the re-filing wins.
            if CollectionItem.objects.filter(
                content_type_id=item.content_type_id,
                object_id=item.object_id,
                deleted_at__isnull=True,
            ).exists():
                skipped_items += 1
                continue
            item.deleted_at = None
            item.save(update_fields=["deleted_at"])
            restored_items += 1
        log_activity(
            verb="library.collection.restore",
            target=collection,
            metadata={
                "collections_restored": len(collections),
                "items_restored": restored_items,
                "items_skipped": skipped_items,
            },
        )
    return {
        "status": "ok",
        "collections_restored": len(collections),
        "items_restored": restored_items,
        "items_skipped": skipped_items,
    }


# ---------------------------------------------------------------------------
# Item membership (generic)
# ---------------------------------------------------------------------------


@api.get("/collections/{collection_id}/items/", response=list[CollectionItemOut])
def list_items(
    request,
    collection_id: int,
    content_type_id: int | None = Query(None, description="Filter items by content type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List items in the collection.

    Requires read access to the collection. Use ``content_type_id`` to filter
    by object type (e.g. show only Recordings).
    """
    user = _require_auth(request)
    collection = _get_active_collection(collection_id)
    if not (getattr(user, "is_superuser", False) or collection.author_id == user.pk):
        ensure_can_read_collection(user=user, collection=collection)

    qs = CollectionItem.objects.filter(collection=collection, deleted_at__isnull=True)
    if content_type_id is not None:
        qs = qs.filter(content_type_id=content_type_id)

    # Superusers see everything without per-item checks.
    if getattr(user, "is_superuser", False):
        items = list(qs.order_by("added_at")[offset : offset + limit])
    else:
        items = _filter_readable(user, qs, order_by="added_at", offset=offset, limit=limit)
    log_activity(
        verb="library.collection.item.list",
        target=collection,
        metadata={
            "content_type_id": content_type_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(items),
        },
    )
    return _enrich_collection_items(items, user)


@api.post("/collections/{collection_id}/items/", response={201: CollectionItemOut})
def add_item(request, collection_id: int, payload: CollectionItemIn):
    """Add an object to the collection.

    Requires write access to the collection. The caller must also be able to
    read the referenced object (enforced per type via ``_check_item_readable``).
    ``content_type_id`` is a Django ``ContentType`` primary key — use
    ``GET /annotations/api/v1/content-types`` to look up type ids by app/model.
    ``object_id`` is the primary key of the target object (as a string).
    For ``recordings.Recording`` items the public hash (32-char hex) is also
    accepted and resolved to the internal primary key automatically.
    """
    user = _require_auth(request)
    collection = _get_active_collection(collection_id)
    ensure_can_write_collection(user=user, collection=collection)

    ct = ContentType.objects.filter(pk=payload.content_type_id).first()
    if ct is None:
        raise HttpError(404, f"ContentType {payload.content_type_id} not found")

    object_id = payload.object_id
    if ct.app_label == "recordings" and ct.model == "recording":
        object_id = _resolve_recording_object_id(object_id)
    elif ct.app_label == "media" and ct.model == "mediafile":
        object_id = _resolve_media_object_id(object_id)

    _check_item_readable(user=user, ct=ct, object_id=object_id)

    if CollectionItem.objects.filter(
        collection=collection,
        content_type=ct,
        object_id=object_id,
        deleted_at__isnull=True,
    ).exists():
        raise HttpError(409, "This object is already in the collection")

    try:
        with transaction.atomic():
            # A trashed membership in this same collection (e.g. skipped on a
            # restore) is revived rather than duplicated; otherwise a fresh one is
            # created. The partial unique-per-object constraint makes either fail
            # only when the object is already filed in another *live* collection.
            item = CollectionItem.objects.filter(
                collection=collection,
                content_type=ct,
                object_id=object_id,
                deleted_at__isnull=False,
            ).first()
            if item is not None:
                item.deleted_at = None
                item.save(update_fields=["deleted_at"])
            else:
                item = CollectionItem.objects.create(collection=collection, content_type=ct, object_id=object_id)
            log_activity(verb="library.collection.item.add", target=item)
    except IntegrityError:
        raise HttpError(409, "This object is already in another collection")

    return 201, _enrich_collection_items([item], user)[0]


@api.delete("/collections/{collection_id}/items/{item_id}/")
def remove_item(request, collection_id: int, item_id: int):
    """Remove an item from the collection.

    Requires write access to the collection.
    """
    user = _require_auth(request)
    collection = _get_active_collection(collection_id)
    ensure_can_write_collection(user=user, collection=collection)

    item = CollectionItem.objects.filter(pk=item_id, collection=collection).first()
    if item is None:
        raise HttpError(404, "Item not found in this collection")

    with transaction.atomic():
        log_activity(verb="library.collection.item.remove", target=item)
        item.delete()
    return {"status": "ok"}


@api.post(
    "/collections/{collection_id}/items/{item_id}/move",
    response=CollectionItemOut,
)
def move_item(request, collection_id: int, item_id: int, payload: MoveCollectionItemIn):
    """Move an item to a different collection.

    Requires write access on both the source and target collections.  The
    one-collection-per-recording invariant is preserved naturally by the
    single-row update — no remove/add window where the recording belongs
    to neither side.  Moving an item to its current collection is a no-op
    (returns the existing row unchanged) so the call is idempotent.
    """
    user = _require_auth(request)
    source = _get_active_collection(collection_id)
    ensure_can_write_collection(user=user, collection=source)

    item = CollectionItem.objects.filter(pk=item_id, collection=source).first()
    if item is None:
        raise HttpError(404, "Item not found in this collection")

    if payload.target_collection_id == source.id:
        return _enrich_collection_items([item], user)[0]

    target = _get_active_collection(payload.target_collection_id)
    ensure_can_write_collection(user=user, collection=target)

    with transaction.atomic():
        item.collection = target
        item.save(update_fields=["collection"])
        log_activity(
            verb="library.collection.item.move",
            target=item,
            metadata={
                "source_collection_id": source.id,
                "target_collection_id": target.id,
            },
        )

    return _enrich_collection_items([item], user)[0]


@api.post(
    "/collections/{collection_id}/recordings/bulk-rename",
    response=BulkRenameRecordingsOut,
)
def bulk_rename_recordings(request, collection_id: int, payload: BulkRenameRecordingsIn):
    """Assign sequential display names to recordings in this collection.

    Iterates the collection's recordings in ``added_at`` order and sets each
    writable recording's ``display_name`` to ``"{prefix} {n}"`` (n starts at
    1).  Recordings the caller cannot write are skipped without renumbering
    — the n counter only advances for renamed rows so the resulting display
    names form a contiguous 1..N sequence.

    Requires read access to the collection (the caller must be able to see
    its contents) and write access to each affected recording.  Other
    content-type items in the collection are ignored.

    Returns the count of renamed and skipped recording items.
    """
    from epicurrents.permissions import can_write_object
    from recordings.models import Recording

    user = _require_auth(request)
    collection = _get_active_collection(collection_id)
    if not (getattr(user, "is_superuser", False) or collection.author_id == user.pk):
        ensure_can_read_collection(user=user, collection=collection)

    prefix = (payload.prefix or "").strip() or "Recording"

    # Bound the prefix so {prefix} {counter} cannot exceed Recording.display
    # _name's max_length (255).  Reserve 10 chars for the " <counter>" suffix
    # — covers counters up to 999_999_999.
    _DISPLAY_NAME_MAX = 255
    _COUNTER_SUFFIX_BUDGET = 10
    if len(prefix) > _DISPLAY_NAME_MAX - _COUNTER_SUFFIX_BUDGET:
        raise HttpError(
            400,
            f"Prefix too long; must fit within {_DISPLAY_NAME_MAX - _COUNTER_SUFFIX_BUDGET} characters.",
        )

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    items = list(CollectionItem.objects.filter(collection=collection, content_type=recording_ct).order_by("added_at"))
    if not items:
        return {"renamed": 0, "skipped": 0}

    object_ids = [item.object_id for item in items]
    recordings = {
        str(rec.pk): rec
        for rec in Recording.objects.filter(pk__in=object_ids, deleted_at__isnull=True).exclude(
            status=Recording.Status.FAILED
        )
    }

    renamed = 0
    skipped = 0
    counter = 1
    renamed_pks: list[int] = []
    with transaction.atomic():
        for item in items:
            rec = recordings.get(item.object_id)
            # Soft-deleted / FAILED / missing rows are invisible to the
            # caller through every listing surface.  Excluding them from
            # the ``skipped`` counter avoids disclosing how many hidden
            # rows the collection contains.
            if rec is None:
                continue
            if not can_write_object(user=user, obj=rec):
                skipped += 1
                continue
            rec.display_name = f"{prefix} {counter}"
            rec.save(update_fields=["display_name", "modified_at"])
            counter += 1
            renamed += 1
            renamed_pks.append(rec.pk)

    log_activity(
        verb="library.collection.recordings.bulk_rename",
        target=collection,
        metadata={
            "renamed_count": renamed,
            "skipped_count": skipped,
            "renamed_recording_pks": renamed_pks,
            "prefix": prefix,
        },
    )

    return {"renamed": renamed, "skipped": skipped}


class CollectionExportIn(Schema):
    """Payload for exporting a collection subtree into a new dataset."""

    name: str | None = None
    description: str | None = None
    materialise_hierarchy: bool = True


class CollectionExportOut(Schema):
    """Result of a collection export: the created dataset plus copy counts."""

    dataset: CollectionOut
    exported_count: int
    skipped_count: int
    folder_count: int


@api.post("/collections/{collection_id}/export/", response={201: CollectionExportOut})
def export_collection_to_dataset(request, collection_id: int, payload: CollectionExportIn):
    """Export a collection's subtree into a new dataset owned by the caller.

    Copies every readable active item in the collection and its active sub-collections into a
    freshly created dataset, optionally materialising the sub-collection hierarchy as dataset
    folders (root-collection items land at the dataset root). Membership is copied, never moved
    — the source collection is left untouched. Items the caller cannot read, soft-deleted
    objects, and FAILED recordings are skipped and counted in ``skipped_count``, so no export
    can surface more than the caller already holds. Requires read access to the collection.
    """
    from epicurrents.permissions import can_read_object
    from recordings.models import Recording

    user = _require_auth(request)
    collection = _get_active_collection(collection_id)
    if not (collection.author_id == user.pk or getattr(user, "is_superuser", False)):
        ensure_can_read_collection(user=user, collection=collection)

    active_collections = {
        c.pk: c
        for c in Collection.objects.filter(pk__in=_subtree_collection_ids(collection.pk), deleted_at__isnull=True)
    }
    items = list(
        CollectionItem.objects.filter(
            collection_id__in=active_collections.keys(), deleted_at__isnull=True
        ).select_related("content_type")
    )

    exported = 0
    skipped = 0
    with transaction.atomic():
        dataset = Dataset.objects.create(
            author=user,
            name=(payload.name or "").strip() or collection.name,
            description=payload.description if payload.description is not None else collection.description,
        )
        AccessRight.objects.create(
            content_type=_dataset_ct(),
            object_id=str(dataset.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
            can_write=True,
            can_share=True,
        )

        # Materialise the sub-collection hierarchy breadth-first so every
        # folder's parent exists before its children. The root collection maps
        # to the dataset root, not a folder. A sub-collection whose parent is
        # trashed attaches to the root rather than being dropped.
        folder_by_collection: dict[int, DatasetFolder] = {}
        if payload.materialise_hierarchy:
            children_of: dict[int, list[Collection]] = {}
            for sub in active_collections.values():
                if sub.pk == collection.pk:
                    continue
                parent_pk = sub.parent_id if sub.parent_id in active_collections else collection.pk
                children_of.setdefault(parent_pk, []).append(sub)
            frontier = [collection.pk]
            while frontier:
                next_frontier = []
                for parent_pk in frontier:
                    siblings = sorted(children_of.get(parent_pk, []), key=lambda c: c.name.lower())
                    for position, sub in enumerate(siblings):
                        folder_by_collection[sub.pk] = DatasetFolder.objects.create(
                            dataset=dataset,
                            parent=folder_by_collection.get(parent_pk),
                            name=sub.name,
                            position=position,
                        )
                        next_frontier.append(sub.pk)
                frontier = next_frontier

        for item in items:
            model_class = item.content_type.model_class()
            obj = None
            if model_class is not None:
                qs = model_class.objects.filter(pk=item.object_id)
                if hasattr(model_class, "deleted_at"):
                    qs = qs.filter(deleted_at__isnull=True)
                obj = qs.first()
            if obj is None or (isinstance(obj, Recording) and obj.status == Recording.Status.FAILED):
                skipped += 1
                continue
            readable = (
                getattr(user, "is_superuser", False)
                or getattr(obj, "author_id", None) == user.pk
                or can_read_object(user=user, obj=obj)
            )
            if not readable:
                skipped += 1
                continue
            DatasetItem.objects.create(
                dataset=dataset,
                content_type=item.content_type,
                object_id=item.object_id,
                folder=folder_by_collection.get(item.collection_id),
            )
            exported += 1

        log_activity(
            verb="library.collection.export",
            target=dataset,
            metadata={
                "collection_id": collection.pk,
                "materialise_hierarchy": payload.materialise_hierarchy,
                "exported_count": exported,
                "skipped_count": skipped,
                "folder_count": len(folder_by_collection),
            },
        )

    return 201, {
        "dataset": _dataset_out(dataset),
        "exported_count": exported,
        "skipped_count": skipped,
        "folder_count": len(folder_by_collection),
    }


def _user_can_read_item(user, ct: ContentType, object_id: str) -> bool:
    """Return True if *user* may read the object referenced by *ct* + *object_id*.

    Fast paths (no AccessRight query):
    - superuser → always True
    - object author → True

    Falls back to ``can_read_object`` (which checks AccessRights and all
    registered extensions including Dataset membership).  Soft-deleted or
    missing objects are treated as unreadable and return False.
    """
    from epicurrents.permissions import can_read_object

    if getattr(user, "is_superuser", False):
        return True

    model_class = ct.model_class()
    if model_class is None:
        return False

    qs = model_class.objects.filter(pk=object_id)
    if hasattr(model_class, "deleted_at"):
        qs = qs.filter(deleted_at__isnull=True)
    obj = qs.first()
    if obj is None:
        return False

    if getattr(obj, "author_id", None) == user.pk:
        return True

    return can_read_object(user=user, obj=obj)


def _filter_readable(user, qs, order_by: str, offset: int, limit: int) -> list:
    """Iterate *qs* and return up to *limit* items the user can read.

    Handles pagination correctly even when items are filtered out: skips
    *offset* readable items before collecting up to *limit* readable items.
    ``select_related("content_type")`` is applied automatically to avoid
    an extra query per item for the CT FK.
    """
    visible = []
    skipped = 0
    for item in qs.select_related("content_type").order_by(order_by).iterator():
        if _user_can_read_item(user, item.content_type, item.object_id):
            if skipped < offset:
                skipped += 1
                continue
            visible.append(item)
        if len(visible) >= limit:
            break
    return visible


def _check_item_writable(user, ct: ContentType, object_id: str):
    """Raise 403/404 if the caller cannot write the referenced object."""
    from epicurrents.permissions import can_write_object

    model_class = ct.model_class()
    if model_class is None:
        raise HttpError(400, f"Cannot resolve model for content type {ct.pk}")

    qs = model_class.objects.filter(pk=object_id)
    if hasattr(model_class, "deleted_at"):
        qs = qs.filter(deleted_at__isnull=True)
    obj = qs.first()

    if obj is None:
        raise HttpError(404, f"{ct.model.capitalize()} {object_id} not found")

    if not can_write_object(user=user, obj=obj):
        raise HttpError(403, f"You do not have permission to modify this {ct.model}")


def _check_item_readable(user, ct: ContentType, object_id: str):
    """Raise 403/404/422 if the caller cannot read the referenced object.

    Only model types with a known permission pattern are accepted; unknown
    types are rejected with 400 to prevent accidental enumeration.
    """
    from epicurrents.permissions import can_read_object
    from recordings.models import Recording

    model_class = ct.model_class()
    if model_class is None:
        raise HttpError(400, f"Cannot resolve model for content type {ct.pk}")

    # Fetch the object — apply soft-delete filter where supported
    qs = model_class.objects.filter(pk=object_id)
    if hasattr(model_class, "deleted_at"):
        qs = qs.filter(deleted_at__isnull=True)
    obj = qs.first()

    if obj is None:
        raise HttpError(404, f"{ct.model.capitalize()} {object_id} not found")

    # FAILED recordings are filtered out of every collection / dataset
    # listing surface, so adding one would create an item that the caller
    # cannot subsequently see.  Reject with 422 — author and superuser are
    # included in the rejection because the surface itself is unusable.
    if isinstance(obj, Recording) and obj.status == Recording.Status.FAILED:
        raise HttpError(
            422,
            "Cannot add a recording that failed to process to a collection or dataset.",
        )

    author_id = getattr(obj, "author_id", None)
    if getattr(user, "is_superuser", False) or author_id == user.pk:
        return
    if not can_read_object(user=user, obj=obj):
        raise HttpError(403, f"You do not have permission to view this {ct.model}")


# ---------------------------------------------------------------------------
# Dataset CRUD
# ---------------------------------------------------------------------------


def _dataset_ct():
    return ContentType.objects.get_for_model(Dataset, for_concrete_model=False)


def _get_active_dataset(dataset_id: int | str) -> Dataset:
    """Return active (non-deleted) dataset or raise 404.

    Accepts the 32-character ``object_hash`` (the public identifier the frontend addresses
    datasets by) or the integer PK, which stays accepted for internal callers — the same dual
    resolution ``_resolve_recording_object_id`` gives recordings. Every malformed identifier
    collapses into the same 404 as a missing dataset.
    """
    qs = Dataset.objects.filter(deleted_at__isnull=True)
    identifier = str(dataset_id).strip()
    # Length decides the form: a 32-character value is always the hash, even
    # when it happens to be all digits (token_hex can produce one), and a PK
    # is never 32 digits long in practice.
    if len(identifier) == 32 and identifier.isalnum():
        dataset = qs.filter(object_hash=identifier.upper()).first()
    elif identifier.isdigit():
        dataset = qs.filter(pk=int(identifier)).first()
    else:
        raise HttpError(404, "Dataset not found")
    if dataset is None:
        raise HttpError(404, "Dataset not found")
    return dataset


@api.post("/datasets/", response={201: CollectionOut})
def create_dataset(request, payload: DatasetIn):
    """Create a new Dataset.

    The creator automatically receives full read/write/share access.
    If ``recording_hashes`` is provided, all recordings are added atomically —
    the dataset is not persisted at all if any hash is invalid or unreadable.
    """
    from recordings.models import Recording

    user = _require_auth(request)
    recording_ct = (
        ContentType.objects.get_for_model(Recording, for_concrete_model=False) if payload.recording_hashes else None
    )

    with transaction.atomic():
        dataset = Dataset.objects.create(
            author=user,
            name=payload.name,
            description=payload.description,
        )

        dataset_ct = _dataset_ct()
        AccessRight.objects.create(
            content_type=dataset_ct,
            object_id=str(dataset.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
            can_write=True,
            can_share=True,
        )

        for hash_ in payload.recording_hashes:
            object_id = _resolve_recording_object_id(hash_)
            _check_item_readable(user=user, ct=recording_ct, object_id=object_id)
            DatasetItem.objects.get_or_create(
                dataset=dataset,
                content_type=recording_ct,
                object_id=object_id,
            )

        log_activity(
            verb="library.dataset.create",
            target=dataset,
            metadata={"initial_item_count": len(payload.recording_hashes)},
        )
        return 201, {
            "id": dataset.pk,
            "name": dataset.name,
            "description": dataset.description,
            "parent_id": None,
            "author_id": dataset.author_id,
            "created_at": dataset.created_at,
            "modified_at": dataset.modified_at,
            "deleted_at": dataset.deleted_at,
        }


@api.get("/datasets/", response=list[CollectionOut])
def list_datasets(
    request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    trash: bool = Query(False, description="List soft-deleted datasets instead of active ones"),
):
    """List datasets accessible to the caller (authored or shared)."""
    from epicurrents.permissions import can_read_object

    user = _require_auth(request)

    if trash:
        qs = (
            Dataset.objects.filter(deleted_at__isnull=False, author=user).select_related("meta").order_by("-deleted_at")
        )
        visible = [_dataset_out(d) for d in qs[offset : offset + limit]]
    elif getattr(user, "is_superuser", False):
        qs = Dataset.objects.filter(deleted_at__isnull=True).select_related("meta").order_by("name")
        visible = [_dataset_out(d) for d in qs[offset : offset + limit]]
    else:
        visible = []
        skipped = 0
        for dataset in (
            Dataset.objects.filter(deleted_at__isnull=True).select_related("meta").order_by("name").iterator()
        ):
            if dataset.author_id == user.pk or can_read_object(user=user, obj=dataset):
                if skipped < offset:
                    skipped += 1
                    continue
                visible.append(_dataset_out(dataset))
            if len(visible) >= limit:
                break

    log_activity(
        verb="library.dataset.list",
        metadata={
            "limit": limit,
            "offset": offset,
            "trash": trash,
            "returned_count": len(visible),
        },
    )
    return visible


def _dataset_out(dataset: Dataset) -> dict:
    try:
        meta = dataset.meta
    except DatasetMeta.DoesNotExist:
        meta = None
    return {
        "id": dataset.pk,
        "name": dataset.name,
        "description": dataset.description,
        "parent_id": None,
        "author_id": dataset.author_id,
        "created_at": dataset.created_at,
        "modified_at": dataset.modified_at,
        "deleted_at": dataset.deleted_at,
        "viewer_config": dataset.viewer_config,
        "object_hash": dataset.object_hash,
        "license_spdx": meta.license_spdx if meta else None,
        "license_url": meta.license_url if meta else None,
    }


@api.get("/datasets/{dataset_id}/", response=CollectionOut)
def get_dataset(request, dataset_id: str, share_token: str | None = None):
    """Retrieve a single dataset by ID.

    Unauthenticated access is allowed when a valid *share_token* is provided.
    """
    from epicurrents.permissions import can_read_object

    user = getattr(request, "user", None)
    if not (user and getattr(user, "is_authenticated", False)):
        if not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")
        user = None
    dataset = _get_active_dataset(dataset_id)
    if not (
        (user and getattr(user, "is_superuser", False))
        or (user and dataset.author_id == user.pk)
        or can_read_object(user=user, obj=dataset, share_token=share_token)
    ):
        raise HttpError(403, "You do not have permission to view this dataset")
    log_activity(
        verb="library.dataset.read",
        target=dataset,
        metadata={"share_token_used": bool((share_token or "").strip())},
    )
    return _dataset_out(dataset)


@api.patch("/datasets/{dataset_id}/", response=CollectionOut)
def update_dataset(request, dataset_id: str, payload: CollectionPatchIn):
    """Update dataset name or description.

    Requires write access (author, superuser, or can_write AccessRight).
    ``parent_id`` in the payload is ignored.
    """
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to modify this dataset")

    fields_updated: list[str] = []
    if payload.name is not None:
        dataset.name = payload.name
        fields_updated.append("name")
    if payload.description is not None:
        dataset.description = payload.description
        fields_updated.append("description")
    if payload.viewer_config is not None:
        # The CollectionPatchIn schema types this as a dict, and JSON object keys
        # are always strings, so the flat-map shape is already guaranteed here.
        dataset.viewer_config = payload.viewer_config
        fields_updated.append("viewer_config")

    meta_updates: dict[str, str] = {}
    if payload.license_spdx is not None:
        meta_updates["license_spdx"] = payload.license_spdx
        fields_updated.append("license_spdx")
    if payload.license_url is not None:
        meta_updates["license_url"] = payload.license_url
        fields_updated.append("license_url")

    with transaction.atomic():
        dataset.save()
        if meta_updates:
            DatasetMeta.objects.update_or_create(dataset=dataset, defaults=meta_updates)
        log_activity(
            verb="library.dataset.update",
            target=dataset,
            metadata={"fields_updated": fields_updated},
        )
    return _dataset_out(dataset)


@api.delete("/datasets/{dataset_id}/")
def delete_dataset(request, dataset_id: str):
    """Soft-delete a dataset.

    Sets ``deleted_at`` to now. Items are not deleted; they remain in the
    dataset but lose their inherited access (since the dataset is inactive).
    Restore via ``POST /api/v1/activity/rollback/{change_id}``.
    """
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to delete this dataset")

    with transaction.atomic():
        dataset.deleted_at = timezone.now()
        dataset.save(update_fields=["deleted_at", "modified_at"])
        log_activity(verb="library.dataset.trash", target=dataset)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dataset item membership
# ---------------------------------------------------------------------------


@api.get("/datasets/{dataset_id}/items/", response=list[CollectionItemOut])
def list_dataset_items(
    request,
    dataset_id: str,
    content_type_id: int | None = Query(None, description="Filter by content type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    share_token: str | None = None,
):
    """List items in the dataset.

    Requires read access to the dataset.  Unauthenticated access is allowed
    when a valid *share_token* is provided.
    """
    from epicurrents.permissions import can_read_object

    user = getattr(request, "user", None)
    if not (user and getattr(user, "is_authenticated", False)):
        if not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")
        user = None
    dataset = _get_active_dataset(dataset_id)
    if not (
        (user and getattr(user, "is_superuser", False))
        or (user and dataset.author_id == user.pk)
        or can_read_object(user=user, obj=dataset, share_token=share_token)
    ):
        raise HttpError(403, "You do not have permission to view this dataset")

    qs = DatasetItem.objects.filter(dataset=dataset).order_by("added_at")
    if content_type_id is not None:
        qs = qs.filter(content_type_id=content_type_id)
    items = list(qs[offset : offset + limit])
    log_activity(
        verb="library.dataset.item.list",
        target=dataset,
        metadata={
            "content_type_id": content_type_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(items),
            "share_token_used": bool((share_token or "").strip()),
        },
    )
    return _enrich_collection_items(items, user)


@api.post("/datasets/{dataset_id}/items/", response={201: CollectionItemOut})
def add_dataset_item(request, dataset_id: str, payload: CollectionItemIn):
    """Add an object to the dataset.

    Requires write access to the dataset. The caller must also be able to
    read the referenced object.
    For ``recordings.Recording`` items the public hash (32-char hex) is also
    accepted and resolved to the internal primary key automatically.
    """
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to modify this dataset")

    ct = ContentType.objects.filter(pk=payload.content_type_id).first()
    if ct is None:
        raise HttpError(404, f"ContentType {payload.content_type_id} not found")

    object_id = payload.object_id
    if ct.app_label == "recordings" and ct.model == "recording":
        object_id = _resolve_recording_object_id(object_id)
    elif ct.app_label == "media" and ct.model == "mediafile":
        object_id = _resolve_media_object_id(object_id)

    _check_item_readable(user=user, ct=ct, object_id=object_id)

    with transaction.atomic():
        item, created = DatasetItem.objects.get_or_create(
            dataset=dataset,
            content_type=ct,
            object_id=object_id,
        )
        if not created:
            raise HttpError(409, "This object is already in the dataset")
        log_activity(verb="library.dataset.item.add", target=item)

    return 201, _enrich_collection_items([item], user)[0]


@api.delete("/datasets/{dataset_id}/items/{item_id}/")
def remove_dataset_item(request, dataset_id: str, item_id: int):
    """Remove an item from the dataset.

    Requires write access to the dataset.
    """
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to modify this dataset")

    item = DatasetItem.objects.filter(pk=item_id, dataset=dataset).first()
    if item is None:
        raise HttpError(404, "Item not found in this dataset")

    with transaction.atomic():
        log_activity(verb="library.dataset.item.remove", target=item)
        item.delete()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dataset folders and item placement
# ---------------------------------------------------------------------------


class DatasetFolderIn(Schema):
    """Payload for creating a dataset folder."""

    name: str
    parent_id: int | None = None
    position: int = 0


class DatasetFolderPatchIn(Schema):
    """Payload for partial update of a dataset folder.

    ``parent_id`` distinguishes absent (leave in place) from explicit null (move to the dataset
    root) via ``exclude_unset``, like the other patch schemas.
    """

    name: str | None = None
    parent_id: int | None = None
    position: int | None = None


class DatasetFolderOut(Schema):
    """Dataset folder response."""

    id: int
    dataset_id: int
    parent_id: int | None
    name: str
    position: int
    created_at: datetime
    modified_at: datetime


class MoveDatasetItemIn(Schema):
    """Payload for placing a dataset item in a folder; null means the dataset root."""

    folder_id: int | None = None


def _get_dataset_folder(dataset: Dataset, folder_id: int) -> DatasetFolder:
    """Return the folder scoped to *dataset* or raise 404."""
    folder = DatasetFolder.objects.filter(pk=folder_id, dataset=dataset).first()
    if folder is None:
        raise HttpError(404, "Folder not found in this dataset")
    return folder


def _subtree_folder_ids(root_id: int) -> set[int]:
    """Return the ids of *root_id* and every folder beneath it in the tree."""
    ids = {root_id}
    frontier = [root_id]
    while frontier:
        children = list(DatasetFolder.objects.filter(parent_id__in=frontier).values_list("pk", flat=True))
        frontier = [pk for pk in children if pk not in ids]
        ids.update(frontier)
    return ids


@api.get("/datasets/{dataset_id}/folders/", response=list[DatasetFolderOut])
def list_dataset_folders(request, dataset_id: str, share_token: str | None = None):
    """List the dataset's folder tree as a flat list.

    Ordered by (parent, position, name) so siblings arrive in display order; the frontend
    assembles the tree from ``parent_id``. Read access mirrors the dataset's, share tokens
    included.
    """
    from epicurrents.permissions import can_read_object

    user = getattr(request, "user", None)
    if not (user and getattr(user, "is_authenticated", False)):
        if not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")
        user = None
    dataset = _get_active_dataset(dataset_id)
    if not (
        (user and getattr(user, "is_superuser", False))
        or (user and dataset.author_id == user.pk)
        or can_read_object(user=user, obj=dataset, share_token=share_token)
    ):
        raise HttpError(403, "You do not have permission to view this dataset")
    folders = list(DatasetFolder.objects.filter(dataset=dataset).order_by("parent_id", "position", "name"))
    log_activity(
        verb="library.dataset.folder.list",
        target=dataset,
        metadata={
            "returned_count": len(folders),
            "share_token_used": bool((share_token or "").strip()),
        },
    )
    return folders


@api.post("/datasets/{dataset_id}/folders/", response={201: DatasetFolderOut})
def create_dataset_folder(request, dataset_id: str, payload: DatasetFolderIn):
    """Create a folder in the dataset's tree. Requires write access to the dataset."""
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to modify this dataset")

    name = (payload.name or "").strip()
    if not name:
        raise HttpError(422, "Folder name cannot be empty")
    parent = _get_dataset_folder(dataset, payload.parent_id) if payload.parent_id is not None else None

    with transaction.atomic():
        folder = DatasetFolder.objects.create(
            dataset=dataset,
            parent=parent,
            name=name,
            position=payload.position,
        )
        log_activity(verb="library.dataset.folder.create", target=folder)
    return 201, folder


@api.patch("/datasets/{dataset_id}/folders/{folder_id}/", response=DatasetFolderOut)
def update_dataset_folder(request, dataset_id: str, folder_id: int, payload: DatasetFolderPatchIn):
    """Rename, move, or reposition a folder. Requires write access to the dataset."""
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to modify this dataset")
    folder = _get_dataset_folder(dataset, folder_id)

    patch = payload.model_dump(exclude_unset=True)
    if "name" in patch:
        name = (patch["name"] or "").strip()
        if not name:
            raise HttpError(422, "Folder name cannot be empty")
        folder.name = name
    if "parent_id" in patch:
        if patch["parent_id"] is None:
            folder.parent = None
        else:
            new_parent = _get_dataset_folder(dataset, patch["parent_id"])
            if new_parent.pk in _subtree_folder_ids(folder.pk):
                raise HttpError(400, "Cannot move a folder inside itself or its descendants")
            folder.parent = new_parent
    if "position" in patch:
        # position is non-nullable — an explicit null would otherwise be
        # silently ignored, which reads as success to the caller.
        if patch["position"] is None:
            raise HttpError(422, "position cannot be null")
        folder.position = patch["position"]

    with transaction.atomic():
        folder.save()
        log_activity(
            verb="library.dataset.folder.update",
            target=folder,
            metadata={"fields_updated": sorted(patch.keys())},
        )
    return folder


@api.delete("/datasets/{dataset_id}/folders/{folder_id}/")
def delete_dataset_folder(request, dataset_id: str, folder_id: int):
    """Delete a folder and its sub-folders; the items inside fall back to the dataset root.

    Membership is never touched — only the presentation tree. The item updates run per row
    rather than through the cascade's bulk SET_NULL, so each placement change lands in the
    audit trail.
    """
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to modify this dataset")
    folder = _get_dataset_folder(dataset, folder_id)

    subtree = _subtree_folder_ids(folder.pk)
    with transaction.atomic():
        moved = 0
        for item in DatasetItem.objects.filter(folder_id__in=subtree):
            item.folder = None
            item.save(update_fields=["folder"])
            moved += 1
        log_activity(
            verb="library.dataset.folder.delete",
            target=folder,
            metadata={"folder_count": len(subtree), "items_moved_to_root": moved},
        )
        folder.delete()
    return {"status": "ok"}


@api.post("/datasets/{dataset_id}/items/{item_id}/move", response=CollectionItemOut)
def move_dataset_item(request, dataset_id: str, item_id: int, payload: MoveDatasetItemIn):
    """Place an item in one of the dataset's folders, or back at the root.

    Presentation only — membership and access are untouched. Requires write access to the
    dataset. Idempotent: re-placing an item where it already is returns the row unchanged.
    """
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to modify this dataset")

    item = DatasetItem.objects.filter(pk=item_id, dataset=dataset).first()
    if item is None:
        raise HttpError(404, "Item not found in this dataset")

    folder = _get_dataset_folder(dataset, payload.folder_id) if payload.folder_id is not None else None
    target_folder_id = folder.pk if folder is not None else None
    if item.folder_id == target_folder_id:
        return _enrich_collection_items([item], user)[0]

    with transaction.atomic():
        item.folder = folder
        item.save(update_fields=["folder"])
        log_activity(verb="library.dataset.item.move", target=item)
    return _enrich_collection_items([item], user)[0]


# ---------------------------------------------------------------------------
# Dataset snapshots
# ---------------------------------------------------------------------------


class DatasetSnapshotIn(Schema):
    """Payload for creating a snapshot."""

    label: str = ""


class DatasetSnapshotOut(Schema):
    """Snapshot response. ``manifest`` is included on the detail endpoint only."""

    object_hash: str
    dataset_id: int
    dataset_object_hash: str
    author_id: int
    label: str
    manifest_hash: str
    member_count: int
    created_at: datetime
    manifest: list[dict] | None = None


def _canonical_manifest(dataset: Dataset) -> list[dict]:
    """Build the canonically-ordered member manifest for *dataset*.

    Identity per member: ``content_hash`` for recordings and media, ``object_hash`` for the
    annotation types, and ``pk:<id>`` as the documented last resort for models with no hash
    identity. Soft-deleted and FAILED recordings are excluded — the manifest pins the set every
    serving surface actually offers, and a member nobody can read would make the snapshot
    unsatisfiable from birth. Ordering is (content_type label, identity), so equal membership
    always serialises to equal bytes.
    """
    from recordings.models import Recording

    entries: list[dict] = []
    # Per-item content_object resolution is one query per member — the same
    # accepted N+1 as the per-item access checks (see README → Gotchas).
    # Snapshot creation is a rare governance act, not a hot path; the batched
    # rewrite rides the ROADMAP's batched-permission-helper entry if it comes.
    items = DatasetItem.objects.filter(dataset=dataset).select_related("content_type")
    for item in items:
        obj = item.content_object
        if obj is None:
            continue
        # Generic soft-delete check: a trashed member of any type is off every
        # serving surface, so sealing it would make the snapshot unsatisfiable
        # from birth.
        if getattr(obj, "deleted_at", None) is not None:
            continue
        if isinstance(obj, Recording):
            if obj.status == Recording.Status.FAILED:
                continue
            identity = obj.content_hash
        else:
            identity = getattr(obj, "content_hash", "") or getattr(obj, "object_hash", "") or f"pk:{obj.pk}"
        entries.append(
            {
                "content_type": f"{item.content_type.app_label}.{item.content_type.model}",
                "identity": str(identity),
            }
        )
    entries.sort(key=lambda entry: (entry["content_type"], entry["identity"]))
    return entries


def _manifest_hash(manifest: list[dict]) -> str:
    """SHA-256 hex over the canonical JSON serialisation of *manifest*."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_out(snapshot: DatasetSnapshot, *, include_manifest: bool = False) -> dict:
    return {
        "object_hash": snapshot.object_hash,
        "dataset_id": snapshot.dataset_id,
        "dataset_object_hash": snapshot.dataset.object_hash,
        "author_id": snapshot.author_id,
        "label": snapshot.label,
        "manifest_hash": snapshot.manifest_hash,
        "member_count": len(snapshot.manifest),
        "created_at": snapshot.created_at,
        "manifest": snapshot.manifest if include_manifest else None,
    }


@api.post("/datasets/{dataset_id}/snapshots/", response={201: DatasetSnapshotOut})
def create_dataset_snapshot(request, dataset_id: str, payload: DatasetSnapshotIn):
    """Seal the dataset's current membership as a create-only snapshot.

    Requires write access to the dataset. There is no update or delete counterpart by design —
    a snapshot is a claim about what the set was, and rows are only ever added.
    """
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to snapshot this dataset")

    manifest = _canonical_manifest(dataset)
    with transaction.atomic():
        snapshot = DatasetSnapshot.objects.create(
            dataset=dataset,
            author=user,
            label=(payload.label or "").strip(),
            manifest=manifest,
            manifest_hash=_manifest_hash(manifest),
        )
        log_activity(
            verb="library.dataset.snapshot.create",
            target=snapshot,
            metadata={"dataset_id": dataset.pk, "member_count": len(manifest)},
        )
    return 201, _snapshot_out(snapshot, include_manifest=True)


@api.get("/datasets/{dataset_id}/snapshots/", response=list[DatasetSnapshotOut])
def list_dataset_snapshots(request, dataset_id: str, share_token: str | None = None):
    """List the dataset's snapshots, newest first. Read access mirrors the dataset's."""
    from epicurrents.permissions import can_read_object

    user = getattr(request, "user", None)
    if not (user and getattr(user, "is_authenticated", False)):
        if not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")
        user = None
    dataset = _get_active_dataset(dataset_id)
    if not (
        (user and getattr(user, "is_superuser", False))
        or (user and dataset.author_id == user.pk)
        or can_read_object(user=user, obj=dataset, share_token=share_token)
    ):
        raise HttpError(403, "You do not have permission to view this dataset")
    snapshots = DatasetSnapshot.objects.filter(dataset=dataset).select_related("dataset").order_by("-created_at")
    log_activity(
        verb="library.dataset.snapshot.list",
        target=dataset,
        metadata={
            "returned_count": snapshots.count(),
            "share_token_used": bool((share_token or "").strip()),
        },
    )
    return [_snapshot_out(snapshot) for snapshot in snapshots]


@api.get("/datasets/snapshots/{snapshot_hash}/", response=DatasetSnapshotOut)
def get_dataset_snapshot(request, snapshot_hash: str, share_token: str | None = None):
    """Retrieve one snapshot, manifest included. Read access mirrors its dataset's."""
    from epicurrents.permissions import can_read_object

    user = getattr(request, "user", None)
    if not (user and getattr(user, "is_authenticated", False)):
        if not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")
        user = None
    normalized = (snapshot_hash or "").strip().upper()
    snapshot = DatasetSnapshot.objects.filter(object_hash=normalized).select_related("dataset").first()
    if snapshot is None or snapshot.dataset.deleted_at is not None:
        raise HttpError(404, "Snapshot not found")
    dataset = snapshot.dataset
    if not (
        (user and getattr(user, "is_superuser", False))
        or (user and dataset.author_id == user.pk)
        or can_read_object(user=user, obj=dataset, share_token=share_token)
    ):
        raise HttpError(404, "Snapshot not found")
    log_activity(
        verb="library.dataset.snapshot.read",
        target=snapshot,
        metadata={"share_token_used": bool((share_token or "").strip())},
    )
    return _snapshot_out(snapshot, include_manifest=True)


# ---------------------------------------------------------------------------
# Dataset access rights
# ---------------------------------------------------------------------------


@api.get("/datasets/{dataset_id}/access/", response=list[AccessRightOut])
def list_dataset_access_rights(request, dataset_id: str):
    """List access rights for the dataset. Requires write access."""
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)
    if not can_write_object(user=user, obj=dataset):
        raise HttpError(403, "You do not have permission to manage this dataset")

    rights = list(
        AccessRight.objects.filter(content_type=_dataset_ct(), object_id=str(dataset.pk))
        .exclude(access_target_id=dataset.author_id)
        .select_related("access_target", "access_target_group")
        .order_by("id")
    )
    log_activity(
        verb="library.dataset.access.list",
        target=dataset,
        metadata={"returned_count": len(rights)},
    )
    return [access_right_out(r) for r in rights]


@api.post("/datasets/{dataset_id}/access/", response={201: AccessRightOut})
def grant_dataset_access(request, dataset_id: str, payload: GrantAccessIn):
    """Grant an access right on the dataset.

    Exactly one of ``access_target_id``, ``access_target_group_id``, or
    ``public_share_token`` must be provided. Requires the author, a
    superuser, or an active ``can_share`` grant — and a delegated grant may
    confer only rights the grantor holds (see ``epicurrents.granting``).

    Granting ``can_read`` on a Dataset implicitly grants read access to all
    items currently and future in the dataset (via the permission extension).
    """
    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)

    dataset_ct = _dataset_ct()
    rights = ensure_can_manage_access(user, dataset, object_label="dataset", action="share")

    targets = [
        payload.access_target_id,
        payload.access_target_group_id,
        payload.public_share_token,
    ]
    if sum(t is not None for t in targets) != 1:
        raise HttpError(
            400,
            "Provide exactly one of: access_target_id, access_target_group_id, public_share_token",
        )
    if not (payload.can_read or payload.can_write or payload.can_share):
        raise HttpError(400, "At least one permission flag must be true")

    ensure_can_confer(
        request,
        user,
        dataset,
        rights,
        can_read=payload.can_read,
        can_write=payload.can_write,
        can_share=payload.can_share,
        apply_middleware=payload.apply_middleware,
        expires_at=payload.expires_at,
        share_token=payload.public_share_token is not None,
    )

    UserModel = get_user_model()
    kwargs: dict = {
        "content_type": dataset_ct,
        "object_id": str(dataset.pk),
        "access_giver": user,
        "can_read": payload.can_read,
        "can_write": payload.can_write,
        "can_share": payload.can_share,
        "apply_middleware": payload.apply_middleware,
        "expires_at": payload.expires_at,
    }

    if payload.access_target_id is not None:
        if not UserModel.objects.filter(pk=payload.access_target_id).exists():
            raise HttpError(400, f"User {payload.access_target_id} not found")
        kwargs["access_target_id"] = payload.access_target_id

    elif payload.access_target_group_id is not None:
        if not Group.objects.filter(pk=payload.access_target_group_id).exists():
            raise HttpError(400, f"Group {payload.access_target_group_id} not found")
        kwargs["access_target_group_id"] = payload.access_target_group_id

    else:
        token = (payload.public_share_token or "").strip()
        if not token:
            raise HttpError(400, "public_share_token must not be empty")
        kwargs["public_share_token"] = token

    try:
        with transaction.atomic():
            if (
                "public_share_token" in kwargs
                and AccessRight.objects.filter(public_share_token=kwargs["public_share_token"]).exists()
            ):
                raise HttpError(409, "This share token is already in use")
            target_lookup = {
                key: kwargs[key] for key in ("access_target_id", "access_target_group_id") if key in kwargs
            }
            if target_lookup and (
                AccessRight.objects.filter(content_type=dataset_ct, object_id=str(dataset.pk), **target_lookup).exists()
            ):
                raise HttpError(409, "This target already has an access right on the dataset. Revoke it first.")
            right = AccessRight.objects.create(**kwargs)
            log_activity(verb="library.dataset.access.grant", target=right)
    except IntegrityError as exc:
        # Race backstop: two concurrent grants for the same target pass the
        # pre-check together; the per-target uniqueness constraint rejects the
        # loser, which is the same conflict the pre-check reports.
        raise HttpError(409, "This target already has an access right on the dataset. Revoke it first.") from exc
    right = AccessRight.objects.select_related("access_target", "access_target_group").get(pk=right.pk)
    return 201, access_right_out(right)


@api.delete("/datasets/{dataset_id}/access/{right_id}/")
def revoke_dataset_access(request, dataset_id: str, right_id: int):
    """Revoke an access right from the dataset.

    Requires the author, a superuser, or an active ``can_share`` grant; the
    author's own row is revocable only by the author or a superuser.
    """
    user = _require_auth(request)
    dataset = _get_active_dataset(dataset_id)

    dataset_ct = _dataset_ct()
    rights = ensure_can_manage_access(user, dataset, object_label="dataset", action="manage access for")

    right = AccessRight.objects.filter(
        pk=right_id,
        content_type=dataset_ct,
        object_id=str(dataset.pk),
    ).first()
    if right is None:
        raise HttpError(404, "Access right not found")

    ensure_can_revoke(request, user, dataset, right, rights)

    with transaction.atomic():
        log_activity(verb="library.dataset.access.revoke", target=right)
        right.delete()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def _get_tag(tag_id: int) -> Tag:
    """Return tag or raise 404."""
    tag = Tag.objects.filter(pk=tag_id).first()
    if tag is None:
        raise HttpError(404, "Tag not found")
    return tag


def _get_tag_subtree_ids(root_id: int) -> list[int]:
    """Return IDs of root tag and all descendants.

    Loads the full tag table once (a single query) and traverses in memory
    with BFS, so depth has no effect on the number of queries.
    """
    all_tags = list(Tag.objects.values("id", "parent_id"))
    children_map: dict[int | None, list[int]] = {}
    for t in all_tags:
        pid = t["parent_id"]
        if pid not in children_map:
            children_map[pid] = []
        children_map[pid].append(t["id"])
    result = []
    queue = [root_id]
    while queue:
        current = queue.pop()
        result.append(current)
        queue.extend(children_map.get(current, []))
    return result


def _check_no_tag_cycle(tag: Tag, new_parent_id: int):
    """Raise 400 if setting new_parent_id would create a cycle."""
    visited = {tag.pk}
    cursor_id = new_parent_id
    while cursor_id is not None:
        if cursor_id in visited:
            raise HttpError(400, "Cannot move a tag into one of its own descendants")
        visited.add(cursor_id)
        row = Tag.objects.filter(pk=cursor_id).values("parent_id").first()
        if row is None:
            break
        cursor_id = row["parent_id"]


@api.get("/tags/", response=list[TagOut])
def list_tags(
    request,
    parent_id: int | None = Query(None, description="List children of this tag. Omit for root tags."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List tags.

    - Omit ``parent_id`` to list root-level tags (no parent).
    - Pass ``parent_id`` to list direct children of a tag.
    """
    _require_auth(request)
    if parent_id is not None:
        if not Tag.objects.filter(pk=parent_id).exists():
            raise HttpError(404, "Tag not found")
        qs = Tag.objects.filter(parent_id=parent_id).order_by("name")
    else:
        qs = Tag.objects.filter(parent__isnull=True).order_by("name")
    tags = list(qs[offset : offset + limit])
    log_activity(
        verb="library.tag.list",
        metadata={
            "parent_id": parent_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(tags),
        },
    )
    return tags


@api.post("/tags/", response={201: TagOut})
def create_tag(request, payload: TagIn):
    """Create a new tag.

    Any authenticated user may create tags. Optionally nest under an existing
    tag by providing ``parent_id``.
    """
    user = _require_auth(request)
    parent = None
    if payload.parent_id is not None:
        parent = Tag.objects.filter(pk=payload.parent_id).first()
        if parent is None:
            raise HttpError(404, f"Parent tag {payload.parent_id} not found")
    tag = Tag.objects.create(
        author=user,
        name=payload.name,
        description=payload.description,
        parent=parent,
    )
    log_activity(verb="library.tag.create", target=tag)
    return 201, tag


@api.get("/tags/{tag_id}/", response=TagOut)
def get_tag_detail(request, tag_id: int):
    """Retrieve a single tag by ID."""
    _require_auth(request)
    tag = _get_tag(tag_id)
    log_activity(verb="library.tag.read", target=tag)
    return tag


@api.patch("/tags/{tag_id}/", response=TagOut)
def update_tag(request, tag_id: int, payload: TagPatchIn):
    """Update a tag's name, description, or parent.

    Requires the caller to be the tag author or a superuser.
    Moving a tag to a new parent triggers cycle detection to prevent loops.
    Pass ``parent_id: null`` explicitly to promote a tag to root level.
    """
    user = _require_auth(request)
    tag = _get_tag(tag_id)
    if not can_modify_object(user=user, obj=tag):
        raise HttpError(403, "You do not have permission to modify this tag")

    fields_updated: list[str] = []
    if payload.name is not None:
        tag.name = payload.name
        fields_updated.append("name")
    if payload.description is not None:
        tag.description = payload.description
        fields_updated.append("description")
    if "parent_id" in payload.model_fields_set:
        if payload.parent_id is not None:
            parent = Tag.objects.filter(pk=payload.parent_id).first()
            if parent is None:
                raise HttpError(404, f"Parent tag {payload.parent_id} not found")
            _check_no_tag_cycle(tag, payload.parent_id)
            tag.parent = parent
        else:
            tag.parent = None
        fields_updated.append("parent_id")

    with transaction.atomic():
        tag.save()
        log_activity(
            verb="library.tag.update",
            target=tag,
            metadata={"fields_updated": fields_updated},
        )
    return tag


@api.delete("/tags/{tag_id}/")
def delete_tag(request, tag_id: int):
    """Delete a tag and all its tagged-item associations.

    Requires the caller to be the tag author or a superuser.
    Child tags are not deleted — they become root-level (``parent`` SET_NULL).
    """
    user = _require_auth(request)
    tag = _get_tag(tag_id)
    if not can_modify_object(user=user, obj=tag):
        raise HttpError(403, "You do not have permission to delete this tag")

    with transaction.atomic():
        log_activity(verb="library.tag.delete", target=tag)
        tag.delete()
    return {"status": "ok"}


@api.get("/tags/{tag_id}/items/", response=list[TaggedItemOut])
def list_tagged_items(
    request,
    tag_id: int,
    include_children: bool = Query(True, description="Include items tagged with descendant tags"),
    content_type_id: int | None = Query(None, description="Filter by content type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List items tagged with this tag.

    By default (``include_children=true``) results include items tagged with
    any descendant tag, enabling hierarchical browsing of the taxonomy.
    Set ``include_children=false`` to retrieve items with this exact tag only.
    """
    user = _require_auth(request)
    tag = _get_tag(tag_id)

    if include_children:
        tag_ids = _get_tag_subtree_ids(tag_id)
        qs = TaggedItem.objects.filter(tag_id__in=tag_ids)
    else:
        qs = TaggedItem.objects.filter(tag_id=tag_id)

    if content_type_id is not None:
        qs = qs.filter(content_type_id=content_type_id)

    # FAILED recordings are hidden from every tag-item surface — matching the
    # rule applied to recording listings and collection / dataset items.  Done
    # at the queryset level with a Django subquery so the DB joins on FAILED
    # status rather than loading every FAILED recording PK platform-wide into
    # Python on each request.  Cast to text since ``TaggedItem.object_id`` is
    # a CharField and PostgreSQL won't compare varchar against integer
    # implicitly.
    from django.db.models import CharField
    from django.db.models.functions import Cast

    from recordings.models import Recording

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    failed_object_ids = (
        Recording.objects.filter(status=Recording.Status.FAILED)
        .annotate(pk_text=Cast("pk", CharField()))
        .values("pk_text")
    )
    qs = qs.exclude(
        content_type_id=recording_ct.id,
        object_id__in=failed_object_ids,
    )

    # Superusers see everything without per-item checks.
    if getattr(user, "is_superuser", False):
        items = list(qs.order_by("tagged_at")[offset : offset + limit])
    else:
        items = _filter_readable(user, qs, order_by="tagged_at", offset=offset, limit=limit)
    log_activity(
        verb="library.tag.item.list",
        target=tag,
        metadata={
            "include_children": include_children,
            "content_type_id": content_type_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(items),
        },
    )
    return items


@api.post("/tags/{tag_id}/items/", response={201: TaggedItemOut})
def tag_item(request, tag_id: int, payload: CollectionItemIn):
    """Apply a tag to an object.

    The caller must have write access to the referenced object.
    ``content_type_id`` is a Django ``ContentType`` PK; ``object_id`` is the
    target object's PK as a string.
    """
    user = _require_auth(request)
    tag = _get_tag(tag_id)

    ct = ContentType.objects.filter(pk=payload.content_type_id).first()
    if ct is None:
        raise HttpError(404, f"ContentType {payload.content_type_id} not found")

    _check_item_writable(user=user, ct=ct, object_id=payload.object_id)

    with transaction.atomic():
        item, created = TaggedItem.objects.get_or_create(
            tag=tag,
            content_type=ct,
            object_id=payload.object_id,
        )
        if not created:
            raise HttpError(409, "This object already has this tag")
        log_activity(verb="library.tag.item.add", target=item)

    return 201, item


@api.delete("/tags/{tag_id}/items/{item_id}/")
def untag_item(request, tag_id: int, item_id: int):
    """Remove a tag from an object.

    The caller must either have write access to the tagged object or be the
    tag's author (or a superuser).
    """
    user = _require_auth(request)
    tag = _get_tag(tag_id)

    item = TaggedItem.objects.filter(pk=item_id, tag=tag).first()
    if item is None:
        raise HttpError(404, "Tagged item not found")

    # Tag author (or superuser) may always untag; otherwise check object write access
    if not can_modify_object(user=user, obj=tag):
        _check_item_writable(user=user, ct=item.content_type, object_id=item.object_id)

    with transaction.atomic():
        log_activity(verb="library.tag.item.remove", target=item)
        item.delete()
    return {"status": "ok"}
