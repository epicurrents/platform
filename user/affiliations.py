"""Affiliations: auth groups that record the organisation their members belong to, and grant nothing.

An affiliation is a fact about an account, not a permission. Keeping it a group reuses what groups already have:
membership is managed and audited on the account surface, exported to the subject and erased with the account, and
a project can give an affiliation a meaning through a role provider scoped to affiliations
(``RoleProvider.group_kind``), the way edu marks the one that counts as an account's institution.

An affiliation is a dedicated group of kind ``affiliation``, so an access grant targeting it is refused, a role not
scoped to affiliations is refused on it, and it is left out of the grant-target listings. Unlike other dedicated
groups it has no owner but itself, so the account surface deletes it like an ordinary group.

Only the account holder and the administration surface see an account's affiliations.
"""

from user.dedicated_groups import DedicatedGroup

AFFILIATION_KIND = "affiliation"


def resolve_affiliations(group_ids: list[int]) -> dict[int, DedicatedGroup]:
    """The dedicated-group resolver for affiliations: each of ``group_ids`` that is one.

    An affiliation owns itself, so the owner it reports is its own group's name, with no object hash to link to.
    """
    from user.models import Affiliation

    rows = Affiliation.objects.filter(group_id__in=group_ids).select_related("group")
    return {row.group_id: DedicatedGroup(kind=AFFILIATION_KIND, object_hash="", name=row.group.name) for row in rows}


def affiliation_names(user) -> list[str]:
    """The names of the affiliations *user* belongs to, sorted."""
    return sorted(user.groups.filter(affiliation__isnull=False).values_list("name", flat=True))


def refuse_affiliation_rollback(change, existing_obj) -> str:
    """Rollback guard: no rollback creates, removes or changes an affiliation marker.

    The endpoints make a group an affiliation only when it is created and unmake it only by deleting it. Rolling
    back the marker's creation would turn an affiliation into an ordinary group that grants can target, with any
    affiliation-scoped role still on it; restoring a deleted one would mark a group the operator never created as an
    affiliation.
    """
    return "An affiliation is made when its group is created and removed with it; a rollback cannot change that."
