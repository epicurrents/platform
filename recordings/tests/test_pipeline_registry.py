"""Tests for the reconstruction-stage registry.

Pure Python — no Django, no DB — because the registry is a plain module. The tests use
tiny fake stages so they assert the ordering algebra, not any real correction.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from recordings.pipeline import (
    Phase,
    StageRegistryError,
    clear_reconstruction_stages,
    reconstruction_stages,
    register_reconstruction_stage,
)


@dataclass
class FakeStage:
    """Minimal ReconstructionStage for exercising ordering, without any signal work."""

    name: str
    requires: tuple[str, ...] = ()
    order_hint: int = 100
    enabled_by_default: bool = True
    code_version: str = "1"
    reproducible: bool = True
    phase: Phase = Phase.RECONSTRUCT

    def transform(self, header: bytes, signals: bytes) -> tuple[bytes, bytes]:
        return header, signals


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_reconstruction_stages()
    yield
    clear_reconstruction_stages()


def _names(stages):
    return [s.name for s in stages]


def test_requires_is_respected():
    # Register in the WRONG order on purpose: wqn before eog. The resolver must still
    # place eog first because wqn requires it.
    register_reconstruction_stage(FakeStage("wqn", requires=("eog",)))
    register_reconstruction_stage(FakeStage("eog"))
    assert _names(reconstruction_stages()) == ["eog", "wqn"]


def test_order_hint_breaks_ties_among_independent_stages():
    register_reconstruction_stage(FakeStage("b", order_hint=20))
    register_reconstruction_stage(FakeStage("a", order_hint=10))
    register_reconstruction_stage(FakeStage("c", order_hint=30))
    assert _names(reconstruction_stages()) == ["a", "b", "c"]


def test_name_breaks_ties_when_order_hint_is_equal():
    # Equal order_hint and no dependencies: the tie-break must be total, falling back to
    # name, so the result is deterministic rather than dict-order dependent.
    register_reconstruction_stage(FakeStage("zebra", order_hint=50))
    register_reconstruction_stage(FakeStage("alpha", order_hint=50))
    assert _names(reconstruction_stages()) == ["alpha", "zebra"]


def test_order_is_independent_of_registration_order():
    # The same set registered in two different orders must resolve identically.
    def build(order):
        clear_reconstruction_stages()
        specs = {
            "eog": FakeStage("eog", order_hint=10),
            "wqn": FakeStage("wqn", requires=("eog",), order_hint=20),
            "notch": FakeStage("notch", order_hint=5),
        }
        for name in order:
            register_reconstruction_stage(specs[name])
        return _names(reconstruction_stages())

    forward = build(["eog", "wqn", "notch"])
    reverse = build(["notch", "wqn", "eog"])
    assert forward == reverse
    # notch (hint 5, no deps) first; then eog; then wqn (requires eog).
    assert forward == ["notch", "eog", "wqn"]


def test_requires_beats_order_hint():
    # wqn has a smaller order_hint but requires eog: the dependency must win over the hint.
    register_reconstruction_stage(FakeStage("eog", order_hint=100))
    register_reconstruction_stage(FakeStage("wqn", requires=("eog",), order_hint=1))
    assert _names(reconstruction_stages()) == ["eog", "wqn"]


def test_unknown_dependency_raises_and_names_it():
    register_reconstruction_stage(FakeStage("wqn", requires=("does_not_exist",)))
    with pytest.raises(StageRegistryError) as exc:
        reconstruction_stages()
    assert "wqn" in str(exc.value) and "does_not_exist" in str(exc.value)


def test_cycle_raises_and_names_members():
    register_reconstruction_stage(FakeStage("a", requires=("b",)))
    register_reconstruction_stage(FakeStage("b", requires=("a",)))
    with pytest.raises(StageRegistryError) as exc:
        reconstruction_stages()
    msg = str(exc.value).lower()
    assert "cycle" in msg and "a" in msg and "b" in msg


def test_registration_is_idempotent_by_name():
    # Re-registering the same name overwrites rather than duplicating — so a repeated
    # AppConfig.ready during test bootstrap does not accumulate stale stages.
    register_reconstruction_stage(FakeStage("eog", code_version="1"))
    register_reconstruction_stage(FakeStage("eog", code_version="2"))
    stages = reconstruction_stages()
    assert len(stages) == 1
    assert stages[0].code_version == "2"


def test_non_reconstruct_phase_is_rejected():
    with pytest.raises(StageRegistryError):
        register_reconstruction_stage(FakeStage("derive_thing", phase=Phase.DERIVE))


def test_empty_registry_resolves_to_empty_list():
    assert reconstruction_stages() == []


def test_reproducible_flag_is_carried_through():
    # The registry must preserve reproducible as declared — it is the field the storage
    # lifecycle keys on (evictable cache vs retained archive), so it cannot be defaulted
    # or dropped in resolution.
    register_reconstruction_stage(FakeStage("pure"))  # default True
    register_reconstruction_stage(FakeStage("ml_model", reproducible=False))
    by_name = {s.name: s for s in reconstruction_stages()}
    assert by_name["pure"].reproducible is True
    assert by_name["ml_model"].reproducible is False


def test_diamond_dependency_resolves():
    # base → {left, right} → join. Both middle stages depend on base; join depends on
    # both. Tie-break orders left before right by name at equal hint.
    register_reconstruction_stage(FakeStage("join", requires=("left", "right")))
    register_reconstruction_stage(FakeStage("left", requires=("base",)))
    register_reconstruction_stage(FakeStage("right", requires=("base",)))
    register_reconstruction_stage(FakeStage("base"))
    order = _names(reconstruction_stages())
    assert order.index("base") < order.index("left") < order.index("join")
    assert order.index("base") < order.index("right") < order.index("join")
    assert order == ["base", "left", "right", "join"]
