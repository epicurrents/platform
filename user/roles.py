"""Registry for project-supplied roles carried by auth groups.

A project may give groups a meaning core knows nothing about — an instructor /
student distinction, say. The design puts the role on the **group**: each group
carries at most one role per provider, and a user holds whatever roles the
groups they belong to carry. That makes membership the single assignment
mechanism (the account surface already manages it, with explicit audit), lets
one user hold several roles at once through several groups, and lets a group
scope a role — a per-course group can both receive access grants and mark its
members as that course's students.

A project registers one provider per role concept from its ``AppConfig.ready()``.
Core calls :func:`read_roles` when serialising a user, :func:`read_group_roles`
when serialising groups, and :func:`write_group_role` when the account surface
assigns or clears a group's role; core never knows what a role means.

The registry is deliberately thin. It holds no state beyond the providers, does
no validation of its own beyond the declared choices, and has no opinion about
who may set a role — the endpoint's ``_require_superuser`` guard is the access
control, and the provider's own model writes land on the audit trail through
the ordinary signals, so a provider that misbehaves cannot escape either.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

_ROLE_PROVIDERS: dict[str, "RoleProvider"] = {}


@dataclass(frozen=True)
class RoleProvider:
    """One project-defined role concept, and how groups carry it.

    ``key`` is the name the role is serialised under and the name the account
    surface addresses it by. ``choices`` are ``(value, label)`` pairs in the
    project's own vocabulary and the complete set of accepted values — a write
    of anything else is refused before the provider is called; "no role" is the
    absence of a value, not a sentinel choice. ``read_groups`` takes the groups
    of interest in one call (so a roster page costs one provider query, not one
    per group) and returns ``{group_pk: value}`` for the groups that carry a
    role. ``write_group`` persists or clears one group's role; the value has
    already been checked against ``choices``, and ``None`` means clear.
    """

    key: str
    label: str
    choices: tuple[tuple[str, str], ...]
    read_groups: Callable[[Sequence[Any]], dict[int, str]]
    write_group: Callable[[Any, str | None], None]


def register_role_provider(provider: RoleProvider) -> None:
    """Register a project role, replacing any earlier one with the same key.

    Replacement rather than refusal because ``ready()`` can run more than once
    in a test process, and a stale provider closing over a reloaded model class
    is worse than a duplicate registration.
    """
    _ROLE_PROVIDERS[provider.key] = provider


def clear_role_providers() -> None:
    """Drop every registered provider. For tests that register their own."""
    _ROLE_PROVIDERS.clear()


def get_role_providers() -> list[RoleProvider]:
    """Every registered provider, in key order."""
    return [_ROLE_PROVIDERS[key] for key in sorted(_ROLE_PROVIDERS)]


def read_roles(user: Any) -> dict[str, list[str]]:
    """The roles *user* inherits through group membership, keyed by role key.

    Each value is the sorted, de-duplicated set of roles the user's groups
    carry for that provider — several groups carrying the same role read as one.
    A provider that raises is reported as an empty list rather than propagating:
    the caller is usually serialising a user for a response, and a project whose
    role table has not migrated yet should not turn every user lookup into a 500.
    """
    groups = list(user.groups.all())
    values: dict[str, list[str]] = {}
    for provider in get_role_providers():
        try:
            by_group = provider.read_groups(groups)
            values[provider.key] = sorted({value for value in by_group.values() if value})
        except Exception:
            values[provider.key] = []
    return values


def read_group_roles(groups: Sequence[Any]) -> dict[int, dict[str, str | None]]:
    """Each group's role per provider, keyed by group primary key.

    Every registered key is present for every group, with ``None`` where the
    group carries no role — so a serializer or form can render the full selector
    set without special-casing absence. Raise-swallowing as in
    :func:`read_roles`, for the same reason.
    """
    result: dict[int, dict[str, str | None]] = {group.pk: {} for group in groups}
    for provider in get_role_providers():
        try:
            by_group = provider.read_groups(groups)
        except Exception:
            by_group = {}
        for group in groups:
            result[group.pk][provider.key] = by_group.get(group.pk)
    return result


def write_group_role(group: Any, key: str, value: str | None) -> None:
    """Set or clear one registered role on *group*. ``None`` clears.

    Raises ``KeyError`` when no provider owns *key* and ``ValueError`` when
    *value* is outside the provider's declared choices. Both are the endpoint's
    to translate into a 400 — validating here rather than in the endpoint keeps
    a project from having to re-check what it already declared.
    """
    provider = _ROLE_PROVIDERS.get(key)
    if provider is None:
        raise KeyError(key)
    if value is not None and value not in {choice for choice, _label in provider.choices}:
        raise ValueError(value)
    provider.write_group(group, value)
