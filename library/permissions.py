"""Collection and Dataset permission helpers.

⚠️ LOAD-BEARING — access grants from Dataset sharing, and the author-only gate on Collections.
Three silent-failure classes are in scope:

1. **Dataset extension regression.** ``can_read_via_dataset`` is
   consulted by ``can_read_object`` for every read check that does not
   hit a direct ``AccessRight``; if it silently stops returning grants,
   dataset sharing breaks across the platform.
2. **Soft-delete check bypass.** The extension query gates on
   ``dataset__deleted_at__isnull=True``; silently removing that filter
   surfaces trashed datasets as if they were active.
3. **`apply_middleware` propagation.** The extension reads
   ``apply_middleware`` from the matching ``AccessRight`` and returns it
   in ``ReadAccessTerms``; dropping that propagation silently switches
   EDF serving from anonymised to raw (or vice versa).

See AGENTS.md → *Load-bearing files* before modifying. Contract tests
in `library/tests/test_permissions.py` cover all three classes,
including ``apply_middleware`` propagation for both true and false.

Collections are the author's private organisational tree: ``can_read_collection``
and ``can_write_collection`` grant only the author and superusers, and no
``AccessRight`` can target a collection. Datasets are the platform's only
sharing unit, and the Collection → Dataset export is the path for sharing
what a collection organises.

Dataset access rights propagate *downward* to contained items:
a ``can_read`` AccessRight on a Dataset grants read access to every item in it.
This is enforced transparently via the ``can_read_via_dataset`` extension
registered in ``library.apps.LibraryConfig.ready()``, so all existing API
endpoints honour Dataset membership without modification.
Write access is never inherited from Datasets — items need their own rights.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ninja.errors import HttpError

if TYPE_CHECKING:
    from library.models import Collection


# ---------------------------------------------------------------------------
# Collection permission helpers
# ---------------------------------------------------------------------------


def can_read_collection(user, collection: Collection) -> bool:
    """Return True if user can read the collection — the author and superusers only."""
    if getattr(user, "is_superuser", False):
        return True
    user_id = getattr(user, "pk", None)
    return user_id is not None and getattr(user, "is_authenticated", False) and collection.author_id == user_id


def ensure_can_read_collection(user, collection: Collection) -> None:
    """Raise 403 if user cannot read the collection."""
    if not can_read_collection(user=user, collection=collection):
        raise HttpError(403, "You do not have permission to view this collection")


def can_write_collection(user, collection: Collection) -> bool:
    """Return True if user can write to the collection — the same author-only gate as reads."""
    return can_read_collection(user=user, collection=collection)


def ensure_can_write_collection(user, collection: Collection) -> None:
    """Raise 403 if user cannot write to the collection."""
    if not can_write_collection(user=user, collection=collection):
        raise HttpError(403, "You do not have permission to modify this collection")


# ---------------------------------------------------------------------------
# Dataset permission extension
# ---------------------------------------------------------------------------


def can_read_via_dataset(user, obj, share_token: str | None = None):
    """Return a ReadAccessTerms if user can read ``obj`` via membership in an accessible Dataset.

    Called automatically by ``can_read_object`` through the extension registry
    registered in ``library.apps.LibraryConfig.ready()``.

    A user (or group, or share-token holder) can read an item via dataset when:
    - The item appears in at least one non-deleted Dataset as a DatasetItem.
    - That Dataset has a non-expired ``can_read=True`` AccessRight targeting
      the user, one of their groups, or the provided share token.

    Write access is never granted through Datasets.

    ``apply_middleware`` in the returned ``ReadAccessTerms`` reflects the matching
    Dataset ``AccessRight`` row, so EDF content is anonymised (or not) according
    to the sharer's choice — consistent with direct-right behaviour.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Q

    from epicurrents.models import AccessRight
    from epicurrents.permissions import ReadAccessTerms
    from library.models import Dataset, DatasetItem

    object_pk = getattr(obj, "pk", None)
    if object_pk is None:
        return ReadAccessTerms(granted=False)

    user_id = getattr(user, "pk", None)
    token_value = (share_token or "").strip()

    if user_id is None and not token_value:
        return ReadAccessTerms(granted=False)

    obj_ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)

    # Reverse lookup: which active datasets contain this object?
    dataset_ids = list(
        DatasetItem.objects.filter(
            content_type=obj_ct,
            object_id=str(object_pk),
            dataset__deleted_at__isnull=True,
        ).values_list("dataset_id", flat=True)
    )
    if not dataset_ids:
        return ReadAccessTerms(granted=False)

    dataset_ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)

    # Gate the user-id branch on is_authenticated — an unauthenticated caller
    # with a pk set (mock / stub) should never contribute to target_filter.
    target_filter = Q()
    if user_id is not None and getattr(user, "is_authenticated", False):
        target_filter |= Q(access_target_id=user_id)
        group_ids = list(user.groups.values_list("id", flat=True))
        if group_ids:
            target_filter |= Q(access_target_group_id__in=group_ids)
    if token_value:
        target_filter |= Q(public_share_token=token_value)

    if not target_filter:
        return ReadAccessTerms(granted=False)

    right = (
        AccessRight.objects.active()
        .filter(
            content_type=dataset_ct,
            object_id__in=[str(did) for did in dataset_ids],
            can_read=True,
        )
        .filter(target_filter)
        .only("apply_middleware")
        .first()
    )
    if right is None:
        return ReadAccessTerms(granted=False)
    return ReadAccessTerms(granted=True, apply_middleware=right.apply_middleware)


def can_read_via_dataset_federated(peer, remote_user_id: str, obj):
    """Return ReadAccessTerms when a peer reaches ``obj`` through a shared Dataset.

    The federated counterpart of :func:`can_read_via_dataset`, registered through
    ``register_federated_read_extension`` in ``library.apps.LibraryConfig.ready()``.
    It exists separately because a peer has no local user, group membership or
    share token to match an ``AccessRight`` target against — only the peer row and
    an opaque remote user id.

    A wildcard grant (``remote_user_id=""``) covers any user from that peer;
    an exact match covers one. ``apply_middleware`` comes from the dataset's grant
    row, so a sharer who chose de-identification keeps it on every recording the
    dataset carries.

    Returns ``None`` when no dataset grant reaches the object, which is how the
    resolver tells "not mine to grant" from "granted with these terms".
    """
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Q

    from epicurrents.models import AccessRight
    from epicurrents.permissions import ReadAccessTerms
    from library.models import Dataset, DatasetItem

    object_pk = getattr(obj, "pk", None)
    if object_pk is None or peer is None:
        return None

    obj_ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    dataset_ids = list(
        DatasetItem.objects.filter(
            content_type=obj_ct,
            object_id=str(object_pk),
            dataset__deleted_at__isnull=True,
        ).values_list("dataset_id", flat=True)
    )
    if not dataset_ids:
        return None

    dataset_ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
    right = (
        AccessRight.objects.active()
        .filter(
            content_type=dataset_ct,
            object_id__in=[str(did) for did in dataset_ids],
            can_read=True,
            federated_peer=peer,
        )
        .filter(Q(remote_user_id="") | Q(remote_user_id=remote_user_id))
        # Two tiers, as the direct-grant resolver orders them. An exact-user row
        # comes before the peer-wide wildcard, so the specific grant's terms win
        # whichever way it is set. Among rows of equal specificity the
        # de-identifying one wins — and unlike the direct-grant resolver, this
        # query spans every dataset holding the object, so equal-specificity ties
        # are ordinary here rather than impossible: one recording in two datasets
        # shared with the same peer produces two wildcard rows. Without the
        # second key the database's row order picks between them, and picking the
        # raw one serves the peer a file the sharer de-identified for them.
        .order_by("-remote_user_id", "-apply_middleware")
        .only("apply_middleware")
        .first()
    )
    if right is None:
        return None
    return ReadAccessTerms(granted=True, apply_middleware=right.apply_middleware)


def federated_dataset_visible_terms(peer, remote_user_id: str, content_type):
    """Objects of *content_type* a peer reaches through datasets shared with it, with terms.

    The listing half of the pair registered with
    ``register_federated_read_extension``. Two queries — the peer's dataset grants,
    then the items in those datasets — because listing endpoints resolve hundreds of
    rows and cannot call the per-object check on each.

    Terms rather than bare ids: ``apply_middleware`` lives on the dataset's grant
    row, and the federated listing also advertises a download size that depends on
    whether the bytes are transformed. An item in two shared datasets takes the
    de-identifying grant, the safe direction and the one the resolver picks among
    equals.

    Deleted datasets are excluded; the items' own visibility is the caller's
    business, as it is for directly granted ids.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Q

    from epicurrents.models import AccessRight
    from epicurrents.permissions import ReadAccessTerms
    from library.models import Dataset, DatasetItem

    if peer is None:
        return {}

    dataset_ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
    grants = (
        AccessRight.objects.active()
        .filter(content_type=dataset_ct, can_read=True, federated_peer=peer)
        .filter(Q(remote_user_id="") | Q(remote_user_id=remote_user_id))
        .values_list("object_id", "remote_user_id", "apply_middleware")
    )
    # Each grant is ranked the way the per-object query sorts: specificity first,
    # de-identification second. Comparing the ranks reproduces that ordering here
    # rather than restating it as a different rule, which is what let the two
    # halves disagree — the listing folded a dataset's wildcard and exact-user
    # rows together toward de-identification, where the per-object half lets the
    # exact row win outright. A peer then saw a size computed one way and
    # received bytes decided the other.
    rank_by_dataset: dict[int, tuple[int, int]] = {}
    for object_id, grant_user_id, apply_middleware in grants:
        if not str(object_id).isdigit():
            continue
        dataset_pk = int(object_id)
        rank = (1 if grant_user_id else 0, 1 if apply_middleware else 0)
        if rank_by_dataset.get(dataset_pk, (-1, -1)) < rank:
            rank_by_dataset[dataset_pk] = rank
    if not rank_by_dataset:
        return {}

    # An object in several shared datasets takes the highest-ranked of their
    # grants, which is the row the per-object query's `.first()` would return.
    rank_by_object: dict[str, tuple[int, int]] = {}
    items = DatasetItem.objects.filter(
        dataset_id__in=list(rank_by_dataset),
        dataset__deleted_at__isnull=True,
        content_type=content_type,
    ).values_list("object_id", "dataset_id")
    for object_id, dataset_id in items:
        rank = rank_by_dataset.get(dataset_id)
        if rank is None:
            continue
        key = str(object_id)
        if rank_by_object.get(key, (-1, -1)) < rank:
            rank_by_object[key] = rank
    return {key: ReadAccessTerms(granted=True, apply_middleware=bool(rank[1])) for key, rank in rank_by_object.items()}
