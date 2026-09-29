"""Rollback guards for the pool and release models: the rollbacks that would restore state past a pool rule.

The activity rollback API restores a row's audited state directly, past every endpoint. The pool
rules in ``library.pools`` and the release record in ``library.release`` are enforced by the
endpoints and the release command, so each model they govern registers a guard here (through
``activity.audit.register_rollback_guard`` from ``LibraryConfig.ready()``) that refuses a rollback
restoring what those paths would refuse:

- a dataset rollback that changes the pool configuration or the gate (``submission_profile``,
  ``submission_group``, ``submissions_open``, ``release_gated``), and the rollback of a pool's
  creation, which would trash it past ``ensure_deletable``;
- any rollback of a release run, a run's sign-off or a curator's approval: each is a record of
  something that happened, and restoring or removing one rewrites what a release relied on;
- a membership rollback in a release-gated dataset or a pool, or one that moves a member's release
  pointer: members arrive through the gate or the add endpoint and leave through purge or veto;
- any rollback of a contributor's ledger or a spooled submission file;
- any rollback of a pooled (system-authored) recording, which would restore a withdrawn or vetoed
  member, or edit one past the pool's rules.

Guards apply to superusers too; they protect invariants rather than access.
"""

from __future__ import annotations

from typing import Any

_POOL_FIELDS = ("submission_profile", "submission_group_id", "submissions_open", "release_gated")
_POOL_DEFAULTS = {
    "submission_profile": "",
    "submission_group_id": None,
    "submissions_open": False,
    "release_gated": False,
}


def _restored_state(change) -> dict | None:
    """The state a rollback of *change* would write, or ``None`` for a creation rollback, which deletes."""
    from activity.models import ObjectChangeLog

    if change.action == ObjectChangeLog.ACTION_CREATE:
        return None
    return change.before_state or {}


def dataset_guard(change, existing_obj: Any) -> str | None:
    """Refuse a dataset rollback that would change the pool configuration or the gate, or trash a pool."""
    from library.pools import is_pool

    state = _restored_state(change)
    if state is None:
        if existing_obj is not None and is_pool(existing_obj):
            return "A submission pool is not removed by rolling back its creation; dissolve or purge it."
        return None
    for field in _POOL_FIELDS:
        current = getattr(existing_obj, field) if existing_obj is not None else _POOL_DEFAULTS[field]
        restored = state.get(field, current)
        if (restored or _POOL_DEFAULTS[field]) != (current or _POOL_DEFAULTS[field]):
            return "A rollback cannot change a dataset's submission pool or release gating."
    return None


def refuse_always(reason: str):
    """A guard refusing every rollback of its model, with *reason*."""

    def guard(change, existing_obj: Any) -> str | None:
        return reason

    return guard


def dataset_item_guard(change, existing_obj: Any) -> str | None:
    """Refuse a membership rollback in a gated dataset or a pool, or one that moves the release pointer."""
    from library.models import Dataset
    from library.pools import is_pool

    state = _restored_state(change) or {}
    dataset_ids = {state.get("dataset_id"), getattr(existing_obj, "dataset_id", None)} - {None}
    for dataset in Dataset.objects.filter(pk__in=dataset_ids):
        if dataset.release_gated or is_pool(dataset):
            return "The members of a release-gated dataset are not changed by rollback."
    if existing_obj is not None and "release_id" in state and state["release_id"] != existing_obj.release_id:
        return "A member's release is not changed by rollback."
    return None


def recording_guard(change, existing_obj: Any) -> str | None:
    """Refuse any rollback of a pooled (system-authored) recording."""
    from library.release import system_user_id

    system_id = system_user_id()
    if system_id is None:
        return None
    state = change.before_state or {}
    authors = {state.get("author_id"), getattr(existing_obj, "author_id", None)}
    if system_id in authors:
        return "A pooled recording is withdrawn through the purge path and is not restored by rollback."
    return None


ROLLBACK_GUARDS = {
    "library.dataset": dataset_guard,
    "library.datasetitem": dataset_item_guard,
    "library.datasetrelease": refuse_always("A release run is a record and is not rolled back."),
    "library.datasetreleasesignoff": refuse_always("A release run's sign-off is a record and is not rolled back."),
    "library.memberapproval": refuse_always("A curator's approval is withdrawn by its curator, not by rollback."),
    "recordings.submissionledger": refuse_always("A contributor's ledger is not rolled back."),
    "recordings.submissionfile": refuse_always("A spooled submission is not rolled back."),
    "recordings.recording": recording_guard,
}
