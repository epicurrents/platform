"""Reconstruction-stage registry with dependency-resolved ordering.

Mirrors ``activity.derived_state``: apps register stages at ``AppConfig.ready`` time;
the resolved, ordered list is read at dispatch. Registration is idempotent per stage
name so app ``ready()`` re-runs during test bootstrap don't accumulate duplicates.

The resolved order is a topological sort of the ``requires`` graph, with a **total**
tie-break — ``(order_hint, name)`` — so the same set of registered stages always yields
the same order. Determinism matters here for the same reason it matters in the stages
themselves (``signal-pipeline-plan.md`` §3.3): the stage order is part of what a manifest
records, so it must not depend on registration order, dict iteration, or anything else
incidental.
"""

from __future__ import annotations

from .stages import Phase, ReconstructionStage

# Keyed by stage name so re-registration overwrites rather than duplicates.
_STAGES: dict[str, ReconstructionStage] = {}


class StageRegistryError(Exception):
    """A stage graph that cannot be resolved: unknown dependency or a cycle."""


def register_reconstruction_stage(stage: ReconstructionStage) -> None:
    """Register (or replace) a reconstruction stage.

    Idempotent by ``stage.name``: registering the same name twice overwrites, so a repeated
    ``AppConfig.ready`` during test setup does not accumulate stale stages. Validation of
    the *graph* (unknown ``requires``, cycles) happens at resolution time in
    :func:`reconstruction_stages`, not here — a stage may legitimately be registered before
    the stage it requires, as long as both exist by the time the order is resolved.
    """
    if stage.phase is not Phase.RECONSTRUCT:
        raise StageRegistryError(
            f"stage {stage.name!r} has phase {stage.phase!r}; the registry holds RECONSTRUCT stages only"
        )
    _STAGES[stage.name] = stage


def clear_reconstruction_stages() -> None:
    """Empty the registry. Test helper; not used in production."""
    _STAGES.clear()


def reconstruction_stages() -> list[ReconstructionStage]:
    """Return all registered stages in a deterministic, dependency-respecting order.

    Order is a topological sort of the ``requires`` graph. Among stages with no ordering
    relation between them, the tie-break is ``(order_hint, name)`` — a **total** order, so
    the result never depends on registration or dict order.

    Raises
    ------
    StageRegistryError
        If a stage requires a name that is not registered, or if the ``requires`` edges
        contain a cycle. Both are configuration errors that must fail loudly at resolution
        rather than silently drop or misorder a stage.
    """
    stages = dict(_STAGES)  # snapshot; do not mutate the registry

    # Validate dependencies exist before attempting the sort, so the error names the
    # offending stage rather than surfacing as a confusing missing-node later.
    for stage in stages.values():
        for dep in stage.requires:
            if dep not in stages:
                raise StageRegistryError(f"stage {stage.name!r} requires {dep!r}, which is not registered")

    # Kahn's algorithm with a deterministic frontier. At each step we take the ready node
    # (all dependencies already emitted) that sorts first by (order_hint, name). Using a
    # repeatedly-scanned frontier rather than a heap keeps it obviously correct; the stage
    # count is tiny (a handful), so efficiency is irrelevant and clarity wins.
    emitted: list[str] = []
    emitted_set: set[str] = set()
    remaining = set(stages)

    def sort_key(name: str) -> tuple[int, str]:
        s = stages[name]
        return (s.order_hint, s.name)

    while remaining:
        ready = sorted(
            (name for name in remaining if all(dep in emitted_set for dep in stages[name].requires)),
            key=sort_key,
        )
        if not ready:
            # Nothing is ready but stages remain ⇒ every remaining stage is waiting on
            # another remaining stage: a cycle. Name the cycle members so it is fixable.
            raise StageRegistryError("cycle in stage 'requires' graph among: " + ", ".join(sorted(remaining)))
        pick = ready[0]
        emitted.append(pick)
        emitted_set.add(pick)
        remaining.discard(pick)

    return [stages[name] for name in emitted]
