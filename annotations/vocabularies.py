"""Vocabulary registry for standardised annotation codes.

The registry follows the same pattern as ``register_read_permission_extension`` and
``register_csv_subconverter``. Core registers its own two vocabularies, the acquisition-scoped categories
of the sets the viewer ships (``annotations/core_vocabularies.py``), and no external one: a plugin or
project that owns a coding standard registers a validator from its ``AppConfig.ready()``, and a
``standard`` nobody registered is validated by nobody.

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
    """A registered coding standard: identifier, display label, version, its validator and its optional term names.

    ``term_name`` answers the display name of a value the validator accepts, or ``None`` where the standard gives
    none; a vocabulary registered without it names no terms. ``write_meta`` answers the ``meta`` a server-side
    writer stamps on a code it creates, or is ``None`` where the standard keeps no ``meta``. ``canonicalise`` rewrites
    a value into the one spelling the standard stores, before validation, or is ``None`` where every accepted value
    already has one.
    """

    standard: str
    label: str
    version: str
    validator: Callable[[str, Any], None]
    term_name: Callable[[str], str | None] | None = None
    closed: bool = False
    confined: bool = False
    write_meta: Callable[[], Any] | None = None
    canonicalise: Callable[[str], str] | None = None


_REGISTRY: dict[str, Vocabulary] = {}


def register_vocabulary(
    standard: str,
    *,
    label: str,
    validator: Callable[[str, Any], None],
    version: str = "",
    term_name: Callable[[str], str | None] | None = None,
    closed: bool = False,
    confined: bool = False,
    write_meta: Callable[[], Any] | None = None,
    canonicalise: Callable[[str], str] | None = None,
) -> None:
    """Register a validator for ``standard``; call from the owning ``AppConfig.ready()``.

    The validator receives ``(value, meta)`` for every API write carrying this ``standard`` and raises
    ``ValueError`` with a message naming the offending term when the pair violates the vocabulary.
    Re-registering the same ``standard`` replaces the earlier entry, which keeps ``ready()`` idempotent
    across repeated app loading in tests. ``term_name`` returns a term's display name, which ingest writes as the
    name of a row it creates from a code; without it the code itself is the name.

    ``closed`` declares that the validator accepts only an enumerated set of terms, never a placeholder a value can
    fill (HED's ``Description/<text>`` is the counter-example). Ingest accepts a code a file declares only under a
    closed vocabulary, because under an open one the file chooses the value, and code values are exempt from the
    annotation-text rule.

    ``confined`` declares less: every part of a value comes from a domain the validator declares — a term of the
    vocabulary, a canonical identifier from a closed set, or a number within a range declared for its placeholder, in
    a unit and at a precision declared with it — and a value is bounded in size. Typed text is never part of one. A
    closed vocabulary is confined, so ``closed`` implies it. A writer can still choose a plausible number, which is
    why ingest keeps requiring ``closed``; a gate taking codes a known person wrote may accept a confined one.

    ``write_meta`` returns the ``meta`` a server-side writer stamps on a code of this standard, for a vocabulary whose
    validator pins its ``meta`` (a schema release), so a row written without a client still validates. A client
    writing through the API sends its own.

    ``canonicalise`` takes a value and returns the spelling the standard stores, or raises ``ValueError`` where the
    value cannot be read at all; :func:`validate_code` applies it before the validator and returns its result, which
    is what every writer stores. It is for a standard whose grammar admits more than one spelling of the same
    meaning, such as HED's whitespace, repeated tags and redundant grouping: each spelling the writer may choose is
    a place to put something other than the meaning, and a confined vocabulary is one where no such place is left.
    The rules are the standard's own, since what is a redundant spelling differs by grammar.
    """
    _REGISTRY[standard] = Vocabulary(
        standard=standard,
        label=label,
        version=version,
        validator=validator,
        term_name=term_name,
        closed=closed,
        confined=confined or closed,
        write_meta=write_meta,
        canonicalise=canonicalise,
    )


def unregister_vocabulary(standard: str) -> None:
    """Remove a registered vocabulary; primarily test cleanup."""
    _REGISTRY.pop(standard, None)


def get_vocabulary(standard: str) -> Vocabulary | None:
    """The vocabulary registered for ``standard``, or ``None``."""
    return _REGISTRY.get(standard)


def registered_vocabularies() -> list[Vocabulary]:
    """Return the registered vocabularies sorted by standard identifier."""
    return sorted(_REGISTRY.values(), key=lambda v: v.standard)


def validate_code(standard: str, value: str, meta: Any) -> str:
    """Validate ``(value, meta)`` against the vocabulary registered for ``standard`` and return the value to store.

    Raises ``ValueError`` when the registered validator rejects the pair, or — only under
    ``ANNOTATION_CODE_STRICT_VOCABULARY`` — when no vocabulary is registered for ``standard``.
    Returns the value in the vocabulary's canonical spelling otherwise, which is ``value`` itself for a vocabulary
    that registers no ``canonicalise``; a writer stores what this returns, never what it was given.
    """
    from django.conf import settings

    vocabulary = _REGISTRY.get(standard)
    if vocabulary is None:
        if getattr(settings, "ANNOTATION_CODE_STRICT_VOCABULARY", False):
            raise ValueError(
                f"unknown coding standard {standard!r}: this deployment accepts only registered vocabularies"
            )
        return value
    value = canonical_value(vocabulary, value)
    vocabulary.validator(value, meta)
    return value


def canonical_value(vocabulary: Vocabulary, value: str) -> str:
    """The spelling of ``value`` that ``vocabulary`` stores: its ``canonicalise`` applied, or ``value`` without one.

    Raises ``ValueError`` as the hook does; the result is what the validator sees and what a writer stores.
    """
    if vocabulary.canonicalise is None:
        return value
    return vocabulary.canonicalise(value)
