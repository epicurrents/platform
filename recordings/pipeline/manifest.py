"""The manifest: the content-addressed recipe that identifies a signal version.

A *manifest* is the ordered list of reconstruction-stage applications over an immutable
source. It is the durable, re-derivable recipe of ``signal-pipeline-plan.md`` §3.2: given
the source and the manifest, the served signal is reproducible, and the manifest's content
hash is the ``version_id`` the viewer overlays, the annotation cites, and the cache keys on.

Pure Python — no Django, no I/O. Persistence into the audit chain and materialisation into
the artifact cache wrap this core; they do not change how a version is *identified*.

What the ``version_id`` hashes, and what it does not:

* It hashes exactly what determines the output **bytes** — the ``source_hash`` and, in
  order, each stage's ``(stage_name, code_version, params_hash)``. Change any of these and
  the output could change, so the id must change.
* It does **not** hash ``reproducible`` or ``phase`` or any display label: those do not
  affect the produced bytes, so folding them in would make two byte-identical versions look
  different. Content addressing means the id follows the content, nothing else.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

#: The base version — the ingested file with no reconstruction applied. Not a manifest but
#: the *absence* of one; the API exposes it under this stable id (matching
#: ``version-fetch-contract.md``). An empty ``Manifest`` resolves to exactly this, so there
#: is one name for "source", not two.
SOURCE_VERSION_ID = "source"


@dataclass(frozen=True)
class StageApplication:
    """One stage's contribution to a manifest.

    ``params_hash`` is an opaque, stable digest of the stage's parameters, supplied by the
    caller (a stage knows how to hash its own config). The manifest treats it as an
    identifier: two applications with the same ``(stage_name, code_version, params_hash)``
    are the same operation and must produce the same bytes.
    """

    stage_name: str
    code_version: str
    params_hash: str


@dataclass(frozen=True)
class Manifest:
    """An ordered sequence of stage applications over a source.

    Frozen and hashable. Build incrementally with :meth:`with_stage`, which returns a new
    manifest rather than mutating — a manifest is a value, and its identity is its content.
    """

    source_hash: str
    stages: tuple[StageApplication, ...] = ()

    def with_stage(self, application: StageApplication) -> Manifest:
        """Return a new manifest with *application* appended. Does not mutate self."""
        return Manifest(self.source_hash, self.stages + (application,))

    def stage_names(self) -> tuple[str, ...]:
        return tuple(a.stage_name for a in self.stages)

    def canonical(self) -> bytes:
        """Canonical byte serialisation — the exact input to the hash.

        Stage **order is preserved** (the list is a sequence, not a set); ``sort_keys``
        canonicalises the keys *within* each object without reordering the list. Mirrors
        the canonical-JSON style used by the audit chain (``activity.audit``) so the two
        hash the same way.
        """
        payload = {
            "source_hash": self.source_hash,
            "stages": [
                {
                    "stage": a.stage_name,
                    "code_version": a.code_version,
                    "params_hash": a.params_hash,
                }
                for a in self.stages
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")

    @property
    def version_id(self) -> str:
        """Content-addressed identity of the version this manifest produces.

        An empty manifest (no stages) is the source, and resolves to
        :data:`SOURCE_VERSION_ID` rather than a hash — so "the original" has one canonical
        name. Any non-empty manifest is the full sha256 hex of :meth:`canonical`.
        """
        if not self.stages:
            return SOURCE_VERSION_ID
        return hashlib.sha256(self.canonical()).hexdigest()

    def is_source(self) -> bool:
        return not self.stages


def is_source_version(version_id: str) -> bool:
    """True when *version_id* names the base (unprocessed) version."""
    return version_id == SOURCE_VERSION_ID
