"""Heuristic warnings for free-text labels that look like they identify a person.

``display_name``, collection, dataset and folder names, tag names and the
descriptions beside them are typed by a user and shown to every recipient of
the object they label. Free text is the classic identifier channel: the whole
de-identification pass is bypassed by an uploader who types the patient's name
or record number into the label. The platform cannot know what a label means,
so it warns rather than refuses — a false positive on a legitimate label must
not block work — and the write endpoints return the warnings beside their
result for the client to surface.

``looks_like_identifier`` names the shapes it recognises: a run of six or more
digits (record numbers, national identifiers), a date in the common clinical
spellings, a ``Surname, Firstname`` or ``Firstname Surname`` pair of capitalised
words, and whatever a deployment adds through ``TEXT_HYGIENE_PATTERNS``, a
mapping of kind to regular expression that a project settings module extends
with its own identifier formats. ``name_warnings`` applies it per field and
builds the response rows.
"""

from __future__ import annotations

import re
from functools import lru_cache

from django.conf import settings
from ninja import Schema

KIND_DIGIT_RUN = "digit_run"
KIND_DATE = "date"
KIND_PERSON_NAME = "person_name"

# Six or more consecutive digits: record numbers, national identifiers, and
# the date part of a Finnish personal identity code all have this shape.
_DIGIT_RUN = re.compile(r"\d{6,}")

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS = (
    # 2024-03-05, 2024/3/5, 2024.03.05
    re.compile(r"(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)"),
    # 05.03.2024, 5/3/24, 05-03-2024
    re.compile(r"(?<!\d)\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(?!\d)"),
    # 5 March 2024, 5 Mar 24
    re.compile(rf"(?<!\d)\d{{1,2}}\s+{_MONTH}\s+\d{{2,4}}(?!\d)", re.IGNORECASE),
    # March 5, 2024
    re.compile(rf"\b{_MONTH}\s+\d{{1,2}},?\s+\d{{2,4}}(?!\d)", re.IGNORECASE),
)

# One capitalised word: an upper-case letter followed by lower-case letters,
# or a compound joined by a hyphen or apostrophe (Smith-Jones, O'Brien).
# Latin-1 letters cover the scripts this platform's deployments label in. An
# all-caps token (EEG, MEG) is not a name token, which keeps "EEG Baseline"
# quiet.
_UPPER = "A-ZÀ-ÖØ-Þ"
_LOWER = "a-zà-öø-ÿ"
_NAME_TOKEN = rf"[{_UPPER}](?:[{_LOWER}]+|[{_LOWER}]*(?:[-'][{_UPPER}]?[{_LOWER}]+)+)"
_INITIAL = rf"[{_UPPER}]\.?"
_PERSON_NAME_PATTERNS = (
    # Doe, Jane / Doe, Jane Mary / Doe, J.
    re.compile(rf"^\s*{_NAME_TOKEN},\s*(?:{_NAME_TOKEN}(?:\s+{_NAME_TOKEN})?|{_INITIAL})\s*$"),
    # Jane Doe / Jane M. Doe
    re.compile(rf"^\s*{_NAME_TOKEN}(?:\s+{_INITIAL})?\s+{_NAME_TOKEN}\s*$"),
)

_MESSAGES = {
    KIND_DIGIT_RUN: "contains a run of six or more digits, the usual shape of a record number or personal identifier",
    KIND_DATE: "contains something that reads as a date",
    KIND_PERSON_NAME: "reads as a person's name",
}


class NameWarningOut(Schema):
    """One warning about a free-text field, returned beside a write endpoint's result.

    ``field`` is the request field the text came from, ``kind`` one of the
    ``KIND_*`` constants or a key of ``TEXT_HYGIENE_PATTERNS``, and ``message``
    an English sentence a client without its own wording can show.
    """

    field: str
    kind: str
    message: str


@lru_cache(maxsize=8)
def _compiled_deployment_patterns(items: tuple[tuple[str, str], ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((kind, re.compile(pattern)) for kind, pattern in items)


def deployment_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Return the compiled ``TEXT_HYGIENE_PATTERNS`` as ``(kind, pattern)`` pairs.

    The setting is validated at ``manage.py check`` (see ``epicurrents.checks``), so an
    entry that does not compile stops the boot rather than failing every rename.
    """
    configured = getattr(settings, "TEXT_HYGIENE_PATTERNS", None) or {}
    return _compiled_deployment_patterns(tuple(sorted(configured.items())))


def looks_like_identifier(text: str | None) -> list[str]:
    """Return the kinds of identifier *text* resembles, in a stable order; empty when none.

    The built-in shapes come first, then the deployment's own patterns by
    kind. A heuristic, deliberately: it exists to make a user look twice at a
    label, not to decide anything on their behalf.
    """
    if not text:
        return []
    kinds: list[str] = []
    if _DIGIT_RUN.search(text):
        kinds.append(KIND_DIGIT_RUN)
    if any(pattern.search(text) for pattern in _DATE_PATTERNS):
        kinds.append(KIND_DATE)
    if any(pattern.match(text) for pattern in _PERSON_NAME_PATTERNS):
        kinds.append(KIND_PERSON_NAME)
    for kind, pattern in deployment_patterns():
        if kind not in kinds and pattern.search(text):
            kinds.append(kind)
    return kinds


def name_warnings(**fields: str | None) -> list[dict[str, str]]:
    """Build warning rows for each keyword argument whose text looks like an identifier.

    Call it as ``name_warnings(display_name=..., description=...)``; the keyword
    is the ``field`` in each row, so the client can point at the input. Fields
    that are ``None`` or empty produce nothing.
    """
    rows: list[dict[str, str]] = []
    for field, text in fields.items():
        for kind in looks_like_identifier(text):
            detail = _MESSAGES.get(kind, f"matches this deployment's {kind} pattern")
            rows.append(
                {
                    "field": field,
                    "kind": kind,
                    "message": f"The {field.replace('_', ' ')} {detail}; recipients see it as typed.",
                }
            )
    return rows
