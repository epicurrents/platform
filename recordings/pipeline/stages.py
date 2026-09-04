"""The reconstruction-stage contract.

A *stage* is one signal modifier — EOG correction, WQN repair, a future denoiser. Stages
live in a *phase* whose position is fixed; their order within a phase is resolved from
declared dependencies (see :mod:`recordings.pipeline.registry`).

Nothing here imports EEG or project code. A stage is a small metadata record plus a
``transform`` callable; concrete stages are defined by projects and registered at
``AppConfig.ready``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Protocol, runtime_checkable


class Phase(enum.IntEnum):
    """Coarse processing classes, in fixed order.

    ``IntEnum`` so phases sort naturally and the fixed order is expressed once, here,
    rather than re-encoded at every call site. The value *is* the order.

    The boundary that matters: ``RECONSTRUCT`` is the only phase that rewrites signal, so
    it is the one that gates a recording's openability (``AVAILABLE`` in the lifecycle
    doc). ``DERIVE`` runs strictly after it and only reads.
    """

    INGEST = 0
    RECONSTRUCT = 1
    DERIVE = 2


@runtime_checkable
class ReconstructionStage(Protocol):
    """One signal-modifying stage in the ``RECONSTRUCT`` phase.

    The registry uses the metadata fields to enumerate and order stages; the dispatcher
    (a later step) calls :meth:`transform`. Keeping both on one object means the thing
    that declares "I run after EOG" is the same thing that does the work — they cannot
    drift.

    Attributes
    ----------
    name:
        Stable identifier, e.g. ``"eog_regression"``. Used as the manifest stage name,
        the ledger key, and the target of another stage's ``requires``. Must be unique
        across the registry and must not change once recordings reference it.
    phase:
        Which phase the stage belongs to. Reconstruction stages are ``Phase.RECONSTRUCT``.
    order_hint:
        Default position within the phase, used only to break ties between stages that
        have no dependency relation. Lower runs earlier. It is a *hint*: declared
        ``requires`` always wins over it.
    requires:
        Names of stages that must run before this one. The registry resolves a
        topological order from these; a cycle or an unknown name is a registration-time
        error, never a silent misorder.
    enabled_by_default:
        Whether the stage runs unless a per-recording override disables it. This is the
        single source of truth for enablement, replacing the config flags that never
        gated the modifying task.
    code_version:
        Opaque version tag for the stage's *algorithm*. It enters the manifest so that a
        change in behaviour — not just parameters — invalidates downstream cached output
        by hash. Bump it whenever the transform's output could change for the same input.
        See ``signal-pipeline-plan.md`` §3.3: reproducible stages must be deterministic,
        and a code change is the one legitimate way their output may differ. For an
        external tool, ``code_version`` should pin the tool's identity as tightly as
        possible — e.g. the container image *digest*, not a mutable ``latest`` tag.
    reproducible:
        Whether the stage's output can be rebuilt **bit-exact** from the same input. This
        is the axis that decides the storage lifecycle of the version the stage produces,
        and it is *orthogonal* to whether the stage is in-process or external — a
        pinned-digest deterministic container is reproducible; a pure-Python stage using
        RNG is not.

        ``True`` (the default, and the only value a pure deterministic stage needs): the
        output is an **evictable cache** entry — it may be discarded and rebuilt from
        source + manifest, because a rebuild yields the same bytes.

        ``False``: the output is **retained, not evicted** — a rebuild is not guaranteed
        to reproduce it, so the bytes a rater scored must be kept, not regenerated. An ML
        model with GPU nondeterminism or sampling sets this. A non-reproducible stage does
        not fit the "cache is a pure function of source + manifest" property and its
        version must be archived instead; see ``signal-pipeline-plan.md`` §3.5.
    """

    name: str
    phase: Phase
    order_hint: int
    requires: tuple[str, ...]
    enabled_by_default: bool
    code_version: str
    reproducible: bool

    def params(self) -> Mapping[str, object]:
        """The output-affecting parameters this stage is configured with.

        Returns a canonicalisable mapping — primitives, mappings, ``list``/``tuple``,
        enums, or dataclasses — that :func:`recordings.pipeline.params.params_hash` digests
        into the manifest's ``params_hash``. ``code_version`` already covers the algorithm;
        this covers the knobs. Include everything that changes the transform's output for
        the same input, and nothing that does not affect the bytes (labels, logging). A
        parameterless stage returns an empty mapping. Omitting a parameter that affects the
        output makes two different results share a ``version_id`` — the one failure the
        hashing is built to prevent — so when unsure, include it.
        """
        ...

    def transform(self, header: bytes, signals: bytes) -> tuple[bytes, bytes]:
        """Apply the stage to raw EDF bytes, returning new ``(header, signals)``.

        Idempotent in the sense that the pipeline never relies on applying it twice. A
        stage may implement this by computing locally **or** by marshalling the bytes to
        an external tool (a Docker-hosted model, a remote service) and blocking on the
        reply — from the pipeline's view it is still input-bytes to output-bytes, just
        I/O-bound. The transport for that round trip is deliberately unspecified here and
        deferred; it lives inside the stage's own ``transform``.

        A ``reproducible`` stage must additionally be **deterministic**: the same input
        yields byte-identical output on every run and every runtime. Determinism is what
        lets a manifest be a durable, re-derivable recipe rather than a one-time claim. A
        stage that cannot promise this declares ``reproducible = False`` and its output is
        archived rather than treated as rebuildable cache — it does not get to silently
        break addressability for the rest of the pipeline.
        """
        ...
