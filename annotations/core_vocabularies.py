"""The platform's own annotation vocabularies: the acquisition-scoped categories of the sets the viewer ships.

The viewer is where events are made and read, so it is the source of truth for the terms; each of its packages
ships a vocabulary file, and this module registers pinned copies of them from ``annotations/vocabulary/``.
``VOCABULARY_PINS`` names the version and the SHA-256 digest each copy is expected to carry, and
``annotations/tests/test_core_vocabularies.py`` fails when a copy drifts from its pin or, where the viewer checkout
is present beside the platform, from the viewer's file. Both directions matter: a term added in the viewer and not
copied here is a 422 for a client using it, and a term edited here alone is a value the viewer cannot name.

``epicurrents.biosignal`` accepts every term of the shared set: what was done, given and observed during a
recording. ``epicurrents.eeg`` accepts the EEG set's acquisition categories and delegates a value it does not know
to the shared set, so an EEG event may carry a shared term under either standard. The EEG file also carries the
finding categories the viewer names, scoped ``finding``; those are not registered, because findings are standardised
through an external vocabulary rather than the platform's own, and the category filter is the one place to widen
that. A code's ``meta`` is not validated: the file names the keys a term expects as documentation for the writer,
and the redaction rule treats the metadata as text whatever it holds.

Why a term is worth more than a name: under a grant carrying ``apply_middleware`` the redaction rule withholds an
event's name and a code's metadata, and serves a code's ``standard`` and ``value``. A coded event therefore explains
the signal to every reader; a named one only to its author. Design and term tables in
``docs/engineering-notes/annotation-event-vocabulary.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from annotations.vocabularies import register_vocabulary

VOCABULARY_DIR = Path(__file__).resolve().parent / "vocabulary"

#: The scope a category must carry for its terms to be accepted as annotation codes.
ACQUISITION_SCOPE = "acquisition"


@dataclass(frozen=True)
class VocabularyPin:
    """Where a vocabulary's pinned copy lives and what the copy is expected to be."""

    file: str
    label: str
    version: str
    sha256: str
    #: The standard a value is looked up in when this one does not know it.
    fallback: str | None = None


VOCABULARY_PINS: dict[str, VocabularyPin] = {
    "epicurrents.biosignal": VocabularyPin(
        file="biosignal-events.json",
        label="Epicurrents biosignal acquisition events",
        version="1.1",
        sha256="145dd6b63ad810dd81955bb9d95f1b01d650bb3fd73a79fbf9b425c91bfc1720",
    ),
    "epicurrents.eeg": VocabularyPin(
        file="eeg-events.json",
        label="Epicurrents EEG events",
        version="1.0",
        sha256="9297d71a478026e7225a3297122c2de04d32d4ff412e8b6530dc7d65ebd70f7b",
        fallback="epicurrents.biosignal",
    ),
}


@cache
def load_vocabulary(standard: str) -> dict:
    """The parsed pinned copy for *standard*."""
    return json.loads((VOCABULARY_DIR / VOCABULARY_PINS[standard].file).read_text(encoding="utf-8"))


@cache
def acquisition_codes(standard: str) -> frozenset[str]:
    """The codes of *standard*'s acquisition-scoped categories: the values the registered validator accepts."""
    codes: set[str] = set()
    for category in load_vocabulary(standard)["categories"].values():
        if category.get("scope") != ACQUISITION_SCOPE:
            continue
        codes.update(term["code"] for term in category["events"].values())
    return frozenset(codes)


def accepted_codes(standard: str) -> frozenset[str]:
    """Every value the validator for *standard* accepts, its fallback's included."""
    pin = VOCABULARY_PINS[standard]
    codes = acquisition_codes(standard)
    return codes | accepted_codes(pin.fallback) if pin.fallback else codes


@dataclass(frozen=True)
class AcquisitionTerm:
    """One acquisition-scoped term and the standard that owns it: what an ingest translation resolves a code to."""

    standard: str
    code: str
    name: str
    #: The viewer's event class for the term (``technical``, ``activation``, ``trigger``, ``event``), or ``""``.
    event_class: str


@cache
def _acquisition_terms() -> dict[str, AcquisitionTerm]:
    """Every acquisition-scoped term of every pinned vocabulary, by code. The prefixes keep the codes disjoint."""
    terms: dict[str, AcquisitionTerm] = {}
    for standard in VOCABULARY_PINS:
        for category in load_vocabulary(standard)["categories"].values():
            if category.get("scope") != ACQUISITION_SCOPE:
                continue
            for term in category["events"].values():
                terms[term["code"]] = AcquisitionTerm(
                    standard=standard, code=term["code"], name=term["name"], event_class=term.get("class", "")
                )
    return terms


def find_acquisition_term(code: str) -> AcquisitionTerm | None:
    """The term *code* names in the pinned vocabularies, under the standard that owns it, or ``None``.

    A finding-scoped term answers ``None`` like an unknown code: ingest writes acquisition events only.
    """
    return _acquisition_terms().get(code)


def _validator(standard: str):
    """A membership validator over *standard*'s accepted codes, naming the term it rejects."""

    def validate(value: Any, meta: Any) -> None:  # the metadata is documented by the file, not checked
        if not isinstance(value, str) or value not in accepted_codes(standard):
            raise ValueError(f"term {value!r} is not in the {standard} vocabulary")

    return validate


def _term_name(code: str) -> str | None:
    """The name of the acquisition term *code* names, under whichever standard owns it."""
    term = find_acquisition_term(code)
    return term.name if term else None


def register_core_vocabularies() -> None:
    """Register the pinned vocabularies; called from ``AnnotationsConfig.ready()``."""
    for standard, pin in VOCABULARY_PINS.items():
        register_vocabulary(
            standard, label=pin.label, version=pin.version, validator=_validator(standard), term_name=_term_name
        )
