"""Parameter hashing: turning a stage's configuration into a stable digest.

The manifest identifies a stage application by ``(stage_name, code_version, params_hash)``
(see :mod:`recordings.pipeline.manifest`). ``code_version`` covers the *algorithm*; this
module covers the *parameters* — the knobs a stage was configured with that change its output
bytes for the same input.

Design choice: hashing is **over-sensitive by construction**. A params hash folds in every
serialisable parameter a stage reports, even ones a particular input might not exercise. The
safe failure here is a spurious cache miss — rebuild identical bytes, waste a little compute.
The *unsafe* failure is two runs whose output differs colliding on one ``version_id``, so an
annotation ends up pointing at bytes that are not what was scored. When in doubt we therefore
hash **more**, never less; a stage that wants a tighter cache narrows what :meth:`params`
returns, but must never omit something that affects the bytes.

Canonicalisation mirrors the manifest's (sorted keys, tight separators) so identical params
yield identical bytes regardless of dict order or how the mapping was built. Dataclass configs
are walked field-by-field, honouring ``field(compare=False)`` — the codebase's own marker for
"not part of this value's identity" (e.g. the ``BandPowerIndex.fn`` lambda) — so those fields
are excluded automatically rather than crashing the hash.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .manifest import StageApplication

if TYPE_CHECKING:
    from .stages import ReconstructionStage


def canonicalize(obj: Any) -> Any:
    """Reduce *obj* to a JSON-canonicalisable structure of plain types.

    Accepts ``None``, ``bool``/``int``/``float``/``str``, mappings, ``list``/``tuple``,
    :class:`enum.Enum`, and dataclass instances (walked by field, skipping
    ``field(compare=False)``). ``tuple`` and ``list`` reduce to the same list, so a
    parameter's *sequence order* matters but its container type does not.

    Raises
    ------
    TypeError
        For any type that has no canonical form (a ``set``, a bare object, ``bytes``, a
        callable, …). Failing loudly is deliberate: a parameter we cannot hash is a
        parameter we cannot promise to detect a change in, and silently dropping it would
        reintroduce exactly the version-collision this module exists to prevent.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        # bool is an int subclass but json renders it as true/false, so True and 1 stay
        # distinct — a desirable property, they are different parameter values.
        return obj
    if isinstance(obj, enum.Enum):
        # Tagged so an enum member cannot collide with a plain value that happens to equal
        # its ``.value``.
        return {"__enum__": type(obj).__name__, "value": canonicalize(obj.value)}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: canonicalize(getattr(obj, f.name)) for f in dataclasses.fields(obj) if f.compare}
    if isinstance(obj, Mapping):
        return {str(k): canonicalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [canonicalize(v) for v in obj]
    raise TypeError(
        f"cannot canonicalize {type(obj).__name__} for a params hash; a stage's params() "
        "must contain only primitives, mappings, sequences, enums, or dataclasses"
    )


def params_hash(params: Any) -> str:
    """Full sha256 hex of the canonical serialisation of *params*.

    Deterministic across processes and independent of mapping insertion order. An empty
    mapping hashes stably (to the digest of ``{}``), so a parameterless stage still gets a
    well-defined, non-empty ``params_hash``.
    """
    payload = json.dumps(
        canonicalize(params),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stage_application(stage: ReconstructionStage) -> StageApplication:
    """Build the :class:`StageApplication` that records *stage* in a manifest.

    Bridges the stage side (which knows its ``name``, ``code_version``, and ``params()``) to
    the manifest's identity tuple. The dispatcher appends the result to the manifest as each
    enabled stage is applied.
    """
    return StageApplication(
        stage_name=stage.name,
        code_version=stage.code_version,
        params_hash=params_hash(stage.params()),
    )
