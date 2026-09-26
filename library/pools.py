"""Submission pools: configuring a release-gated dataset to be fed through the submission gate, and the rules that lock it.

A *pool* is a release-gated dataset with a registered ingest profile and a dedicated contributor group; its members
arrive only through the validating submission path in ``recordings.submissions``. A contributor's *ledger* is the
record of their submissions to one pool (``recordings.SubmissionLedger``), created with their first accepted file.
A pool is *filling* once any ledger exists, which is the moment the pool's shape becomes part of what its members
were checked against.

The rules, in the order a pool meets them:

* A pool is configured on an empty dataset only. Its members must all have passed the same gate, and a member
  that arrived another way carries whatever its uploader's site left in it.
* Before it fills, the profile may change and the pool may be dissolved, which removes the configuration and the
  group and turns the gate off.
* Once filling, the profile, the group and the gate are fixed: every member was checked against them. Intake can
  still close and reopen. The pool is not dissolved and no member is added or removed by hand; withdrawal goes
  through the purge path (``purge_dataset_recordings``).

The group is created with the pool and exists for it alone. :func:`resolve_pool_groups` is registered with the
dedicated-group registry in ``user.dedicated_groups``, so the grant, role and group-delete endpoints refuse it.
The endpoints in ``library.api.v1.ninja`` call these functions and translate :class:`PoolError` into a response;
the functions themselves write inside the caller's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.contrib.auth.models import Group
from django.db.models import Count

POOL_GROUP_KIND = "submission_pool"

# Group.name is unique and at most 150 characters; the suffix and a collision tag fit in what is left.
_GROUP_NAME_STEM = 120


@dataclass
class PoolError(Exception):
    """A pool rule refused an operation. ``status`` is the HTTP status the endpoint answers with."""

    status: int
    message: str

    def __str__(self) -> str:
        return self.message


def is_pool(dataset: Any) -> bool:
    """True when ``dataset`` is configured as a submission pool."""
    return bool(getattr(dataset, "submission_profile", ""))


def is_filling(dataset: Any) -> bool:
    """True once a file has been accepted into any of the pool's ledgers."""
    from recordings.models import SubmissionLedger

    return SubmissionLedger.objects.filter(dataset=dataset).exists()


def has_members(dataset: Any) -> bool:
    """True when the dataset holds any item."""
    from library.models import DatasetItem

    return DatasetItem.objects.filter(dataset=dataset).exists()


def pool_totals(dataset: Any) -> dict[str, int]:
    """Files pending in the spool, failed at ingest, and ingested, over every ledger of the pool together.

    Totals only: a count per contributor would tell the pool's managers who sent what.
    """
    from recordings.models import SubmissionFile, SubmissionLedger

    by_status = dict(
        SubmissionFile.objects.filter(ledger__dataset=dataset)
        .values("status")
        .annotate(n=Count("id"))
        .values_list("status", "n")
    )
    ingested = sum(SubmissionLedger.objects.filter(dataset=dataset).values_list("ingested_count", flat=True))
    return {
        "pending": by_status.get(SubmissionFile.Status.PENDING, 0),
        "failed": by_status.get(SubmissionFile.Status.FAILED, 0),
        "ingested": ingested,
    }


def _lock(dataset: Any) -> None:
    """Take the dataset's row lock and re-read it, so two pool writes, or a write and a submission, do not interleave.

    Must be called inside the caller's transaction.
    """
    type(dataset).objects.select_for_update().filter(pk=dataset.pk).first()
    dataset.refresh_from_db()


def _registered_profile(profile_key: str):
    from recordings.submissions import get_ingest_profile

    profile = get_ingest_profile((profile_key or "").strip())
    if profile is None:
        raise PoolError(400, "Unknown ingest profile.")
    return profile


def _group_name(dataset: Any) -> str:
    stem = (dataset.name or "").strip()[:_GROUP_NAME_STEM] or "Dataset"
    name = f"{stem} — contributors"
    if Group.objects.filter(name__iexact=name).exists():
        name = f"{name} ({dataset.object_hash[:8].lower()})"
    return name


def configure_pool(dataset: Any, profile_key: str) -> Group:
    """Make an empty dataset a pool checked against ``profile_key``, with a new dedicated group and intake closed.

    Intake starts closed so the group can be filled before anyone submits. The dataset becomes release-gated.
    """
    _lock(dataset)
    if is_pool(dataset):
        raise PoolError(409, "The dataset is already a submission pool.")
    if has_members(dataset) or is_filling(dataset):
        raise PoolError(409, "A submission pool is configured on an empty dataset only.")
    profile = _registered_profile(profile_key)
    group = Group.objects.create(name=_group_name(dataset))
    dataset.submission_profile = profile.key
    dataset.submission_group = group
    dataset.submissions_open = False
    dataset.release_gated = True
    dataset.save(update_fields=["submission_profile", "submission_group", "submissions_open", "release_gated"])
    return group


def set_intake(dataset: Any, open_: bool) -> None:
    """Open or close the pool to new submissions. Allowed in every state; files already accepted are unaffected."""
    _lock(dataset)
    if not is_pool(dataset):
        raise PoolError(409, "The dataset is not a submission pool.")
    dataset.submissions_open = bool(open_)
    dataset.save(update_fields=["submissions_open"])


def change_profile(dataset: Any, profile_key: str) -> None:
    """Check the pool's future submissions against another profile; refused once the pool is filling."""
    _lock(dataset)
    if not is_pool(dataset):
        raise PoolError(409, "The dataset is not a submission pool.")
    profile = _registered_profile(profile_key)
    if profile.key == dataset.submission_profile:
        return
    if is_filling(dataset):
        raise PoolError(409, "The profile of a filling pool cannot change: its members were checked against it.")
    dataset.submission_profile = profile.key
    dataset.save(update_fields=["submission_profile"])


def dissolve_pool(dataset: Any) -> int:
    """Remove the pool's configuration and its group, and turn the gate off; refused once the pool is filling.

    Returns the number of accounts the deleted group held, for the audit row.
    """
    _lock(dataset)
    if not is_pool(dataset):
        raise PoolError(409, "The dataset is not a submission pool.")
    if is_filling(dataset):
        raise PoolError(409, "A filling pool cannot be dissolved; withdraw members through the purge path.")
    group = dataset.submission_group
    dataset.submission_profile = ""
    dataset.submission_group = None
    dataset.submissions_open = False
    dataset.release_gated = False
    dataset.save(update_fields=["submission_profile", "submission_group", "submissions_open", "release_gated"])
    member_count = 0
    if group is not None:
        member_count = group.user_set.count()
        group.delete()
    return member_count


def remove_group_with_dataset(sender, instance, **kwargs) -> None:
    """``post_delete`` receiver: a hard-deleted pool takes its group with it, which would otherwise outlive it undedicated.

    Once the dataset row is gone the resolver no longer knows the group, so it would become an ordinary group whose
    members could be granted access. Soft deletion keeps the row and the group.
    """
    if instance.submission_group_id is not None:
        Group.objects.filter(pk=instance.submission_group_id).delete()


def ensure_membership_editable(dataset: Any) -> None:
    """Refuse adding or removing a pool's members by hand: they arrive through the gate and leave through purge."""
    if is_pool(dataset):
        raise PoolError(
            409,
            "A submission pool's members arrive through the submission gate and are withdrawn through the purge path.",
        )


def ensure_deletable(dataset: Any) -> None:
    """Refuse deleting a pool that holds members or files waiting in the spool, or one that is not yet dissolved.

    A configured pool that has not filled is dissolved first, so its group goes with it. A filled pool is deleted
    once purge has withdrawn every member and the spool holds nothing for it.
    """
    if not is_pool(dataset):
        return
    if not is_filling(dataset):
        raise PoolError(409, "Dissolve the submission pool before deleting the dataset.")
    from recordings.models import SubmissionFile

    if has_members(dataset) or SubmissionFile.objects.filter(ledger__dataset=dataset).exists():
        raise PoolError(
            409,
            "A submission pool cannot be deleted while it has members or files waiting for ingest; "
            "withdraw members through the purge path.",
        )


def ensure_gate_unchanged(dataset: Any, release_gated: bool) -> None:
    """Refuse changing the gate of a pool, which the pool's configuration owns."""
    if is_pool(dataset) and release_gated != dataset.release_gated:
        raise PoolError(409, "A submission pool is always release-gated; dissolve the pool to change the gate.")


def resolve_pool_groups(group_ids: list[int]):
    """The pool each of ``group_ids`` belongs to, for the dedicated-group registry."""
    from library.models import Dataset
    from user.dedicated_groups import DedicatedGroup

    rows = Dataset.objects.filter(submission_group_id__in=group_ids).values_list(
        "submission_group_id", "object_hash", "name"
    )
    return {
        group_id: DedicatedGroup(kind=POOL_GROUP_KIND, object_hash=object_hash, name=name)
        for group_id, object_hash, name in rows
    }
