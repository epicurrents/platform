"""Vocabulary registry for standardised annotation codes.

Core ships the mechanism only — the same registry pattern as ``register_read_permission_extension`` and
``register_csv_subconverter`` — and contains zero vocabularies. A plugin or project that owns a coding
standard registers a validator from its ``AppConfig.ready()``; a deployment with nothing registered
validates nothing, unchanged from before the registry existed.

The validator is a callable rather than a term list because a vocabulary's rules are not always
membership: HED has value placeholders and group structure, ICD-10 has check-character rules. A list would
model none of them and would have to be replaced the first time a second vocabulary arrived.

Enforcement is at the API layer (``create_code`` / ``update_code``), not in ``Code.save()``, and that is a
choice: the untrusted writer in the threat model is a user reaching the API, while server-side writers —
ingest, management commands, fixtures — are the platform's own code. The control therefore supports the
claim "users cannot write non-conforming codes through the API", not the stronger "this deployment's
database contains only validated terms", which would need model-level enforcement.

Two enforcement modes, so the registry is adoptable without breaking deployments that already write codes:

* Default — an unregistered ``standard`` is accepted unvalidated (existing behaviour, existing rows, and
  project-local ``epicurrents.<project>.<concept>`` codes all keep working).
* ``ANNOTATION_CODE_STRICT_VOCABULARY = True`` — an unregistered ``standard`` is rejected. This converts
  convention into control and belongs in a project's settings, not in ``common``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Vocabulary:
    """A registered coding standard: identifier, display label, version, and its validator callable."""

    standard: str
    label: str
    version: str
    validator: Callable[[str, Any], None]


_REGISTRY: dict[str, Vocabulary] = {}


def register_vocabulary(
    standard: str,
    *,
    label: str,
    validator: Callable[[str, Any], None],
    version: str = "",
) -> None:
    """Register a validator for ``standard``; call from the owning ``AppConfig.ready()``.

    The validator receives ``(value, meta)`` for every API write carrying this ``standard`` and raises
    ``ValueError`` with a message naming the offending term when the pair violates the vocabulary.
    Re-registering the same ``standard`` replaces the earlier entry, which keeps ``ready()`` idempotent
    across repeated app loading in tests.
    """
    _REGISTRY[standard] = Vocabulary(standard=standard, label=label, version=version, validator=validator)


def unregister_vocabulary(standard: str) -> None:
    """Remove a registered vocabulary; primarily test cleanup."""
    _REGISTRY.pop(standard, None)


def registered_vocabularies() -> list[Vocabulary]:
    """Return the registered vocabularies sorted by standard identifier."""
    return sorted(_REGISTRY.values(), key=lambda v: v.standard)


def validate_code(standard: str, value: str, meta: Any) -> None:
    """Validate ``(value, meta)`` against the vocabulary registered for ``standard``.

    Raises ``ValueError`` when the registered validator rejects the pair, or — only under
    ``ANNOTATION_CODE_STRICT_VOCABULARY`` — when no vocabulary is registered for ``standard``.
    Returns silently otherwise.
    """
    from django.conf import settings

    vocabulary = _REGISTRY.get(standard)
    if vocabulary is None:
        if getattr(settings, "ANNOTATION_CODE_STRICT_VOCABULARY", False):
            raise ValueError(
                f"unknown coding standard {standard!r}: this deployment accepts only registered vocabularies"
            )
        return
    vocabulary.validator(value, meta)
