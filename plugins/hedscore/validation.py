"""The validator registered for the ``hed`` standard: a HED string written against the pinned SCORE schema.

A code's value is served to a reader under a de-identifying grant, because a value from a closed vocabulary names a
concept and cannot describe a subject. A HED string is closed only as far as this validator makes it so: the schema
lets a writer extend a branch, fill a text placeholder or define a term in prose, and each of those carries typed
text through that exemption. So the rules below are narrower than HED's own.

- **Tags are written in their short form,** exactly as the schema spells them. A long form or an extension is refused,
  since an extension is a term the writer made up.
- **A value is accepted only where the placeholder's value class is numeric:** a frequency, a count, an amplitude,
  with a unit of the placeholder's unit class when it names one. ``Description/#``, ``Label/#``, the ``Other-…/#``
  tags and every other text placeholder are refused with any value.
- **The one exception is ``Sensor-list``,** whose value lists channels by their canonical labels, separated by
  ``;``. A canonical label comes from a closed set, and a raw acquisition label would put the site's naming
  convention back into a surface no de-identification pass reads.
- **``ID`` takes a finding group's number,** a whole number from 100 to 999, which marks the events of one finding
  across its occurrences within a recording. A number names no concept, but it describes no subject either. 0 to 99
  are held back for a use not yet defined, and are refused so that no row takes on that meaning before it is.
- **Reserved and deprecated tags are refused.** The reserved ones carry HED's timing and definition machinery, and an
  event's onset and duration belong to the row, not the string.
- **A deployment may restrict the top-level branches** through ``HEDSCORE_ALLOWED_BRANCHES``; empty allows them all.
- **``meta`` is the schema pin**, exactly, so every stored row names the release it was validated against.

Groups are checked for balance and emptiness only. HED's group-level rules (``topLevelTagGroup``, ``unique``) are
for strings the platform did not author, and are left to ``hedtools`` if that dependency is ever taken
(``docs/engineering-notes/hed-score-integration.md`` §9).
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings

from plugins.hedscore.schema import SCHEMA_PIN, SchemaIndex, Term, load_schema
from recordings.processors.channel_labels import canonicalise_label

#: The longest string accepted. A grouped seizure description runs to a few hundred characters.
MAX_LENGTH = 2000

#: The deepest group nesting accepted. SCORE annotations nest a few levels; the cap keeps a crafted string from
#: reaching the recursion limit, which would answer a 500 instead of a 422.
MAX_DEPTH = 8

SENSOR_LIST = "Sensor-list"
FINDING_GROUP = "ID"
#: The numbers a finding group may take; the lower hundred is held back.
FINDING_GROUP_NUMBERS = range(100, 1000)
SENSOR_SEPARATOR = ";"
NUMERIC_CLASS = "numericClass"

_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _split_top_level(text: str) -> list[str]:
    """Split *text* at the commas outside parentheses, refusing unbalanced or empty parts."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses in the HED string")
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    if depth != 0:
        raise ValueError("unbalanced parentheses in the HED string")
    parts.append(text[start:])
    stripped = [part.strip() for part in parts]
    if any(not part for part in stripped):
        raise ValueError("the HED string has an empty tag or group")
    return stripped


def _tags(text: str, depth: int = 0) -> list[str]:
    """Every tag in *text*, groups flattened."""
    if depth > MAX_DEPTH:
        raise ValueError(f"groups nest at most {MAX_DEPTH} deep")
    tags = []
    for part in _split_top_level(text):
        if part.startswith("(") and part.endswith(")"):
            tags.extend(_tags(part[1:-1], depth + 1))
        elif "(" in part or ")" in part:
            raise ValueError(f"malformed group in the HED string near {part!r}")
        else:
            tags.append(part)
    return tags


def _check_numeric(term: Term, value: str, index: SchemaIndex) -> None:
    number, _, unit = value.partition(" ")
    if not _NUMBER.match(number):
        raise ValueError(f"{term.name} takes a number, not {value!r}")
    if not unit:
        return
    allowed = set().union(*(index.units.get(unit_class, frozenset()) for unit_class in term.unit_classes))
    if unit not in allowed:
        raise ValueError(f"{unit!r} is not a unit {term.name} accepts")


def _check_sensors(value: str) -> None:
    labels = value.split(SENSOR_SEPARATOR)
    for label in labels:
        if not label or canonicalise_label(label) != label:
            raise ValueError(f"{label!r} is not a canonical channel label")


def _check_finding_group(value: str) -> None:
    # Written exactly as the number, so one group has one spelling.
    if not (value.isascii() and value.isdigit()) or str(int(value)) != value or int(value) not in FINDING_GROUP_NUMBERS:
        raise ValueError(
            f"{FINDING_GROUP} takes a finding group's number from {FINDING_GROUP_NUMBERS.start}"
            f" to {FINDING_GROUP_NUMBERS.stop - 1}, not {value!r}"
        )


def _check_tag(tag: str, index: SchemaIndex, allowed_branches: set[str]) -> None:
    name, slash, value = tag.partition("/")
    term = index.terms.get(name)
    if term is None:
        raise ValueError(f"{name!r} is not a tag of HED-SCORE {SCHEMA_PIN.version}")
    if term.reserved:
        raise ValueError(f"{name} is a reserved HED tag and is not accepted")
    if term.deprecated:
        raise ValueError(f"{name} is deprecated in the pinned schema")
    if allowed_branches and term.branch not in allowed_branches:
        raise ValueError(f"{name} is in the {term.branch} branch, which this deployment does not accept")
    if not slash:
        if term.require_child:
            raise ValueError(f"{name} needs a value")
        return
    if not term.takes_value:
        raise ValueError(f"{tag!r} is not a tag: write tags in their short form, without extensions")
    if name == SENSOR_LIST:
        _check_sensors(value)
    elif name == FINDING_GROUP:
        _check_finding_group(value)
    elif NUMERIC_CLASS in term.value_classes:
        _check_numeric(term, value, index)
    else:
        raise ValueError(f"{name} takes a text value, which is not accepted")


def validate_hed(value: Any, meta: Any) -> None:
    """Raise ``ValueError`` naming the first rule *value* or *meta* breaks; the registered ``hed`` validator."""
    if meta != SCHEMA_PIN.as_meta():
        raise ValueError(f"meta must be the schema pin {SCHEMA_PIN.as_meta()}")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a HED code's value is a non-empty string")
    if len(value) > MAX_LENGTH:
        raise ValueError(f"a HED string is at most {MAX_LENGTH} characters")
    index = load_schema()
    allowed_branches = set(getattr(settings, "HEDSCORE_ALLOWED_BRANCHES", ()))
    for tag in _tags(value):
        _check_tag(tag, index, allowed_branches)
