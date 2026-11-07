"""The DERIVE phase's job registry: computations that read a recording's default version and write model rows.

A derive job is the read-only counterpart of a reconstruction stage (``recordings.pipeline``). Where a stage turns
signal bytes into signal bytes, a job reads the version the reconstruction phase settled on and writes rows of its
own — an impedance mask, a table of indices. It never writes signal, so the jobs of one recording may run in
parallel, and a recording is openable (``AVAILABLE``) while they do.

Jobs are registered from an ``AppConfig.ready()``, as stages are, and the lifecycle provider in
:mod:`compute.lifecycle` plans a :class:`~compute.models.DeriveJob` row for each one that applies to a recording. The
registry keys on the job's name, so a repeated ``ready()`` replaces rather than duplicates.

Annotation-producing analysis — a detector writing events — is not a derive job: it is an ``AnalysisRun``, with the
identity, segmentation and conformance that need (``recordings/analysis-execution-plan.md``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class DeriveJobSpec(Protocol):
    """One read-only computation over a recording's default version.

    Attributes
    ----------
    name:
        Stable identifier, unique across the registry. It is the ledger key, so it must not change once recordings
        have rows under it.
    code_version:
        Opaque tag for the job's algorithm, recorded on its ledger row so a row says which code produced the rows the
        job wrote. Bump it when the output could change for the same input.
    """

    name: str
    code_version: str

    def applies_to(self, recording) -> bool:
        """Whether this recording gets the job. Read at planning, once its ingest has finished."""
        ...

    def run(self, recording, signal_path: Path, version_id: str) -> None:
        """Compute from the EDF at *signal_path*, the recording's version *version_id*, and write the results.

        Must only read *signal_path*: it is the source file or a cached version, and neither may change. Writes are
        the job's own model rows; the call runs inside an audited scope, so they land in the trail like any other.
        Raising marks the job failed with the exception's message, which the recording's jobs show to its author and
        superusers — so the message should not quote personal data.
        """
        ...


_JOBS: dict[str, DeriveJobSpec] = {}


def register_derive_job(job: DeriveJobSpec) -> None:
    """Register (or replace) a derive job, from an ``AppConfig.ready()``."""
    if not isinstance(job, DeriveJobSpec):
        raise TypeError(f"{job!r} does not implement name, code_version, applies_to and run")
    _JOBS[job.name] = job


def derive_jobs() -> list[DeriveJobSpec]:
    """The registered jobs, sorted by name: they run in parallel, so the order is only for a stable listing."""
    return [_JOBS[name] for name in sorted(_JOBS)]


def get_derive_job(name: str) -> DeriveJobSpec | None:
    """The registered job called *name*, or ``None`` when no app registers it any more."""
    return _JOBS.get(name)


def clear_derive_jobs() -> None:
    """Empty the registry. Test helper."""
    _JOBS.clear()
