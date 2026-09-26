"""Registry for groups another feature owns: a group that exists for one purpose and must grant nothing else.

A submission pool's contributor group is the first. Its members may submit to one dataset, and the pool's
argument rests on that being all the group does: a grant or a project role on it would hand every contributor
something beyond the pool, and they joined the group to submit, not to read. An app that creates such a group
registers a resolver from its ``AppConfig.ready()``. Core then refuses the three ways a group acquires meaning
elsewhere, each at its own endpoint: an access grant targeting it, a project role written on it, and deleting it
while its owner exists. The group admin page shows what owns the group, so the operator managing its members knows
why the other controls are missing.

The registry answers for groups in batches, because the admin listing and the grant-target listing serialise every
group at once.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class DedicatedGroup:
    """What owns a dedicated group.

    ``kind`` is a stable token the frontend keys its wording on (``submission_pool``); ``object_hash`` and
    ``name`` identify the owning object for a link and a label.
    """

    kind: str
    object_hash: str
    name: str


Resolver = Callable[[list[int]], dict[int, DedicatedGroup]]

_RESOLVERS: dict[str, Resolver] = {}


def register_dedicated_group_resolver(kind: str, resolver: Resolver) -> None:
    """Register ``resolver`` for groups of ``kind``, replacing an earlier registration of the same kind.

    ``resolver`` receives group primary keys and returns the owner of each that it owns.
    """
    _RESOLVERS[kind] = resolver


def dedicated_groups(group_ids: Iterable[int]) -> dict[int, DedicatedGroup]:
    """The owner of each of ``group_ids`` that some feature owns."""
    ids = sorted({int(pk) for pk in group_ids if pk is not None})
    found: dict[int, DedicatedGroup] = {}
    if not ids:
        return found
    for resolver in _RESOLVERS.values():
        found.update(resolver(ids))
    return found


def dedicated_group(group_id: int | None) -> DedicatedGroup | None:
    """The owner of one group, or ``None`` when no feature owns it."""
    if group_id is None:
        return None
    return dedicated_groups([group_id]).get(int(group_id))
