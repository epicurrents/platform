"""Tests for parameter hashing.

Pure Python — no Django, no EEG. These pin what ``params_hash`` responds to (every
serialisable parameter, by value, order-independent for mappings) and what it refuses
(unhashable types, loudly), plus the ``compare=False`` dataclass carve-out and the
``stage_application`` bridge to the manifest.
"""

from __future__ import annotations

import dataclasses

import pytest

from recordings.pipeline import (
    StageApplication,
    canonicalize,
    params_hash,
    stage_application,
)

# ── Determinism and independence ───────────────────────────────────────────────────────


def test_same_params_same_hash():
    assert params_hash({"a": 1, "b": [1, 2, 3]}) == params_hash({"a": 1, "b": [1, 2, 3]})


def test_mapping_key_order_does_not_matter():
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})


def test_full_sha256_hex():
    vid = params_hash({"a": 1})
    assert len(vid) == 64 and all(c in "0123456789abcdef" for c in vid)


def test_empty_params_are_stable_and_nonempty():
    assert params_hash({}) == params_hash({})
    assert len(params_hash({})) == 64


# ── Value sensitivity ──────────────────────────────────────────────────────────────────


def test_value_change_changes_hash():
    assert params_hash({"x": 1}) != params_hash({"x": 2})


def test_sequence_order_matters():
    assert params_hash({"x": [1, 2]}) != params_hash({"x": [2, 1]})


def test_bool_and_int_are_distinct():
    # json renders True as ``true`` and 1 as ``1`` — they are different parameter values
    # and must not collide.
    assert params_hash({"x": True}) != params_hash({"x": 1})


def test_missing_key_changes_hash():
    assert params_hash({"a": 1}) != params_hash({"a": 1, "b": 2})


# ── Container-type agnosticism (documented behaviour) ──────────────────────────────────


def test_tuple_and_list_hash_the_same():
    # canonicalize reduces both to a list: sequence *order* is significant, container
    # *type* is not. A stage must not rely on tuple-vs-list to distinguish params.
    assert params_hash({"x": (1, 2)}) == params_hash({"x": [1, 2]})


# ── Dataclass handling ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class _Inner:
    k: int = 1


@dataclasses.dataclass
class _Cfg:
    a: int = 1
    b: str = "x"
    note: object = dataclasses.field(default=None, compare=False)
    inner: _Inner = dataclasses.field(default_factory=_Inner)


def test_dataclass_compared_field_changes_hash():
    assert params_hash({"c": _Cfg(a=1)}) != params_hash({"c": _Cfg(a=2)})


def test_dataclass_compare_false_field_is_ignored():
    # ``note`` is field(compare=False) — the codebase's marker for "not part of identity"
    # (mirrors BandPowerIndex.fn). Even set to an otherwise-unhashable value it must not
    # affect, or crash, the hash.
    assert params_hash({"c": _Cfg(note=lambda z: z)}) == params_hash({"c": _Cfg(note=None)})


def test_nested_dataclass_change_changes_hash():
    assert params_hash({"c": _Cfg(inner=_Inner(k=1))}) != params_hash({"c": _Cfg(inner=_Inner(k=2))})


def test_enum_is_hashable_and_distinct():
    import enum

    class E(enum.Enum):
        A = "a"
        B = "b"

    assert params_hash({"e": E.A}) == params_hash({"e": E.A})
    assert params_hash({"e": E.A}) != params_hash({"e": E.B})


# ── Loud refusal ───────────────────────────────────────────────────────────────────────


def test_unhashable_type_raises():
    # A set has no canonical order we would trust; rather than guess, refuse. Silently
    # dropping an un-canonicalisable param is exactly the version-collision risk to avoid.
    with pytest.raises(TypeError):
        params_hash({"x": {1, 2, 3}})


def test_bare_object_raises():
    with pytest.raises(TypeError):
        canonicalize(object())


# ── stage_application bridge ───────────────────────────────────────────────────────────


class _FakeStage:
    name = "fake"
    code_version = "7"

    def __init__(self, **cfg):
        self._cfg = cfg

    def params(self):
        return self._cfg


def test_stage_application_carries_name_version_and_params_hash():
    stage = _FakeStage(alpha=1, beta="two")
    app = stage_application(stage)
    assert isinstance(app, StageApplication)
    assert app.stage_name == "fake"
    assert app.code_version == "7"
    assert app.params_hash == params_hash({"alpha": 1, "beta": "two"})


def test_stage_application_reflects_param_changes():
    assert stage_application(_FakeStage(x=1)).params_hash != stage_application(_FakeStage(x=2)).params_hash
