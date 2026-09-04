"""Tests for the content-addressed manifest.

Pure Python — no Django. The manifest is the identity core of the versioning model, so the
tests pin exactly what the ``version_id`` responds to (source, stage order, each stage's
code_version and params_hash) and what it must ignore.
"""

from __future__ import annotations

from recordings.pipeline import (
    SOURCE_VERSION_ID,
    Manifest,
    StageApplication,
    is_source_version,
)


def _app(name, code="1", params="p0"):
    return StageApplication(stage_name=name, code_version=code, params_hash=params)


def _manifest(*apps, source="sha256:src"):
    return Manifest(source_hash=source, stages=tuple(apps))


def test_empty_manifest_is_the_source_version():
    m = Manifest(source_hash="sha256:src")
    assert m.version_id == SOURCE_VERSION_ID
    assert m.is_source()
    assert is_source_version(m.version_id)


def test_version_id_is_deterministic():
    a = _manifest(_app("eog"), _app("wqn"))
    b = _manifest(_app("eog"), _app("wqn"))
    assert a.version_id == b.version_id
    # And stable across process assumptions: canonical bytes match too.
    assert a.canonical() == b.canonical()


def test_stage_order_changes_the_version_id():
    forward = _manifest(_app("eog"), _app("wqn"))
    reverse = _manifest(_app("wqn"), _app("eog"))
    assert forward.version_id != reverse.version_id, (
        "order must matter — eog-then-wqn is a different signal from wqn-then-eog"
    )


def test_params_hash_changes_the_version_id():
    base = _manifest(_app("eog", params="p0"))
    changed = _manifest(_app("eog", params="p1"))
    assert base.version_id != changed.version_id


def test_code_version_changes_the_version_id():
    base = _manifest(_app("eog", code="1"))
    changed = _manifest(_app("eog", code="2"))
    assert base.version_id != changed.version_id, (
        "a code change must invalidate the version even at identical params — this is what "
        "catches silent algorithm drift"
    )


def test_source_hash_changes_the_version_id():
    a = _manifest(_app("eog"), source="sha256:aaa")
    b = _manifest(_app("eog"), source="sha256:bbb")
    assert a.version_id != b.version_id


def test_with_stage_is_immutable_and_appends():
    base = Manifest(source_hash="sha256:src")
    one = base.with_stage(_app("eog"))
    two = one.with_stage(_app("wqn"))
    # Originals unchanged.
    assert base.stages == ()
    assert one.stage_names() == ("eog",)
    assert two.stage_names() == ("eog", "wqn")
    # Built incrementally == built at once.
    assert two.version_id == _manifest(_app("eog"), _app("wqn")).version_id


def test_non_empty_version_id_is_a_full_sha256_hex():
    m = _manifest(_app("eog"))
    vid = m.version_id
    assert vid != SOURCE_VERSION_ID
    assert len(vid) == 64 and all(c in "0123456789abcdef" for c in vid)


def test_frozen_and_hashable():
    m = _manifest(_app("eog"))
    # Frozen dataclasses are hashable; usable as dict keys / set members (cache keying).
    assert {m: 1}[m] == 1
    assert _manifest(_app("eog")) in {m}
