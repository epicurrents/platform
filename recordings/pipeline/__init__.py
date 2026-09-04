"""Signal-processing pipeline: reconstruction-stage protocol and registry.

This package is the *core* half of the pipeline described in
``recordings/signal-pipeline-plan.md``. It knows about phases, stages, and their
ordering — but nothing about EEG, EOG correction, or any specific project. Projects
register concrete stages into it at ``AppConfig.ready`` time, exactly as apps register
derived-state digesters into ``activity.derived_state``.

Public surface:

* :class:`~recordings.pipeline.stages.Phase` — the fixed phase order.
* :class:`~recordings.pipeline.stages.ReconstructionStage` — the stage contract.
* :func:`~recordings.pipeline.registry.register_reconstruction_stage`
* :func:`~recordings.pipeline.registry.reconstruction_stages`
* :func:`~recordings.pipeline.registry.clear_reconstruction_stages` — test helper.
"""

from __future__ import annotations

from .manifest import (
    SOURCE_VERSION_ID,
    Manifest,
    StageApplication,
    is_source_version,
)
from .params import canonicalize, params_hash, stage_application
from .registry import (
    StageRegistryError,
    clear_reconstruction_stages,
    reconstruction_stages,
    register_reconstruction_stage,
)
from .stages import Phase, ReconstructionStage

__all__ = [
    "SOURCE_VERSION_ID",
    "Manifest",
    "Phase",
    "ReconstructionStage",
    "StageApplication",
    "StageRegistryError",
    "canonicalize",
    "clear_reconstruction_stages",
    "is_source_version",
    "params_hash",
    "reconstruction_stages",
    "register_reconstruction_stage",
    "stage_application",
]
