"""The validator registered for the ``hed`` standard: a HED string written against the pinned SCORE schema.

A code's value is served to a reader under a de-identifying grant, because a value from a closed vocabulary names a
concept and cannot describe a subject. A HED string is closed only as far as this validator makes it so: the schema
lets a writer extend a branch, fill a text placeholder or define a term in prose, and each of those carries typed
text through that exemption. So the rules below are narrower than HED's own.

- **Tags are written in their short form,** exactly as the schema spells them. A long form or an extension is refused,
  since an extension is a term the writer made up.
- **A value is accepted only where the placeholder's value class is numeric,** and only for a placeholder
  ``NUMERIC_DOMAINS`` declares a domain for: one unit, a range and a number of decimals. The number is written in
  plain decimal with a single spelling, so a reading has one form and cannot be padded or rescaled to carry digits
  that are not one. ``Description/#``, ``Label/#``, the ``Other-…/#`` tags and every other text placeholder are
  refused with any value, and so is a numeric placeholder without a domain, ``Age/#`` among them.
- **The one exception is ``Sensor-list``,** whose value lists channels by their canonical labels, separated by
  ``;``. A canonical label comes from a closed set, and a raw acquisition label would put the site's naming
  convention back into a surface no de-identification pass reads.
- **``ID`` takes a finding group's number,** a whole number from 100 to 999, which marks the events of one finding
  across its occurrences within a recording. A number names no concept, but it describes no subject either. 0 to 99
  are held back for a use not yet defined, and are refused so that no row takes on that meaning before it is.
- **Reserved and deprecated tags are refused.** The reserved ones carry HED's timing and definition machinery, and an
  event's onset and duration belong to the row, not the string.
- **A deployment may restrict the top-level branches** through ``HEDSCORE_ALLOWED_BRANCHES``; empty allows them all.
- **A string is bounded in size:** at most ``MAX_TAGS`` tags, ``MAX_NUMBERS`` numeric values and ``MAX_LENGTH``
  characters, and a sensor list names a channel once.
- **``meta`` is the schema pin**, exactly, so every stored row names the release it was validated against.
- **A string is stored in one spelling.** :func:`canonicalise_hed`, registered as the vocabulary's ``canonicalise``
  and applied before the validator, writes the tags of each group separated by ``, `` with no other whitespace,
  drops a tag or group repeated within its group, and unwraps a group that holds one member, so neither spacing,
  repetition nor redundant grouping can carry anything. The order of the tags within a group is kept as written:
  the viewer's picker writes one order, and the readable form follows it.

Together the rules make ``hed`` a confined vocabulary in the registry's sense: every part of a value comes from a
declared domain and none of it is typed text. A writer can still choose a plausible reading, and the domains bound
how much a reading can say rather than removing it, which is why ingest still takes no ``hed`` code from a file.

Groups are checked for balance and emptiness only. HED's group-level rules (``topLevelTagGroup``, ``unique``) are
for strings the platform did not author, and are left to ``hedtools`` if that dependency is ever taken
(``docs/engineering-notes/hed-score-integration.md`` §9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings

from plugins.hedscore.schema import SCHEMA_PIN, SchemaIndex, Term, load_schema
from recordings.processors.channel_labels import canonicalise_label

#: The longest string accepted. A grouped seizure description runs to a few hundred characters.
MAX_LENGTH = 2000

#: The most tags, and the most numeric values, one string may hold. A long seizure description has a few dozen tags.
MAX_TAGS = 64
MAX_NUMBERS = 16

#: The deepest group nesting accepted. SCORE annotations nest a few levels; the cap keeps a crafted string from
#: reaching the recursion limit, which would answer a 500 instead of a 422.
MAX_DEPTH = 8

SENSOR_LIST = "Sensor-list"
FINDING_GROUP = "ID"
#: The numbers a finding group may take; the lower hundred is held back.
FINDING_GROUP_NUMBERS = range(100, 1000)
SENSOR_SEPARATOR = ";"
NUMERIC_CLASS = "numericClass"

#: A non-negative number in its one spelling: no sign, exponent, leading zero or trailing fractional zero.
_NUMBER = re.compile(r"^(0|[1-9]\d*)(\.\d*[1-9])?$")


@dataclass(frozen=True)
class NumericDomain:
    """The values a numeric placeholder accepts: in *unit*, or unitless where it is ``None``, from *low* to *high*
    inclusive, with at most *decimals* fractional digits."""

    unit: str | None
    low: Decimal
    high: Decimal
    decimals: int


def _domain(unit: str | None, low: str, high: str, decimals: int = 0) -> NumericDomain:
    return NumericDomain(unit, Decimal(low), Decimal(high), decimals)


#: The numeric placeholders accepted, each with its domain. A placeholder missing here is refused, so a new schema
#: release adds a numeric placeholder to nothing until someone decides its domain. The unit of a placeholder without
#: a unit class is in the schema's description only: seconds for apnoea and hypopnoea, events per hour for the index,
#: cycles per minute for tachypnoea and per cent for saturation. Of the HED standard's own properties only two are
#: admitted, an interval between events and a share of the recording; the rest describe stimuli, displays and
#: subjects (``Age``, ``Weight``) rather than a signal.
NUMERIC_DOMAINS: dict[str, NumericDomain] = {
    "Feature-amplitude": _domain("uV", "0", "10000"),
    "RPP-absolute-amplitude": _domain("uV", "0", "10000"),
    "Feature-frequency": _domain("Hz", "0", "1000", 2),
    "Intermittent-photic-stimulation": _domain("Hz", "0", "100", 1),
    "Myoclonus-rhythmic": _domain("Hz", "0", "100", 1),
    "Number-of-RPP-phases": _domain(None, "1", "3"),
    "Episode-event-count": _domain(None, "0", "10000"),
    "Apnea": _domain(None, "0", "600"),
    "Hypopnea": _domain(None, "0", "600"),
    "Apnea-hypopnea-index": _domain(None, "0", "200", 1),
    "Tachypnea": _domain(None, "0", "150"),
    "Oxygen-saturation": _domain(None, "0", "100"),
    "ECG-QT-period": _domain("ms", "0", "2000"),
    "ECG-asystolia": _domain("s", "0", "600", 1),
    "Time-interval": _domain("s", "0", "3600", 2),
    "Percentage": _domain(None, "0", "100"),
}


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


#: A parsed HED string: a tag is a ``str``, a group the list of its members.
_Node = str | list


def _parse(text: str, depth: int = 0) -> list[_Node]:
    """The tags and groups of *text*, a tag's whitespace collapsed to single spaces."""
    if depth > MAX_DEPTH:
        raise ValueError(f"groups nest at most {MAX_DEPTH} deep")
    nodes: list[_Node] = []
    for part in _split_top_level(text):
        if part.startswith("(") and part.endswith(")"):
            nodes.append(_parse(part[1:-1], depth + 1))
        elif "(" in part or ")" in part:
            raise ValueError(f"malformed group in the HED string near {part!r}")
        else:
            nodes.append(" ".join(part.split()))
    return nodes


def _tags(text: str) -> list[str]:
    """Every tag in *text*, groups flattened."""
    tags: list[str] = []

    def walk(nodes: list[_Node]) -> None:
        for node in nodes:
            if isinstance(node, list):
                walk(node)
            else:
                tags.append(node)

    walk(_parse(text))
    return tags


def _canonical(nodes: list[_Node]) -> list[_Node]:
    """*nodes* with every group reduced to its one form: no repeated member, and no group holding a single member."""
    reduced: list[_Node] = []
    for node in nodes:
        if isinstance(node, list):
            node = _canonical(node)
            while isinstance(node, list) and len(node) == 1:
                node = node[0]
        if node not in reduced:
            reduced.append(node)
    return reduced


def _render(nodes: list[_Node]) -> str:
    return ", ".join(f"({_render(node)})" if isinstance(node, list) else node for node in nodes)


def canonicalise_hed(value: Any) -> str:
    """*value* in the one spelling the platform stores; the registered ``hed`` canonicaliser.

    Raises ``ValueError`` where the string cannot be read, as the validator would. Whitespace within a tag collapses
    to single spaces and the tags of a group are joined with ``, ``; a tag or group repeated within its group is
    dropped, keeping the first; a group holding one member is replaced by that member. Tags keep their written order.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a HED code's value is a non-empty string")
    if len(value) > MAX_LENGTH:
        raise ValueError(f"a HED string is at most {MAX_LENGTH} characters")
    return _render(_canonical(_parse(value)))


def _check_numeric(term: Term, value: str) -> None:
    domain = NUMERIC_DOMAINS.get(term.name)
    if domain is None:
        raise ValueError(f"{term.name} takes a number, which is not accepted")
    number, _, unit = value.partition(" ")
    if not _NUMBER.match(number):
        raise ValueError(f"{term.name} takes a number written plainly, without padding, not {value!r}")
    if (unit or None) != domain.unit:
        expected = f"in {domain.unit}" if domain.unit else "without a unit"
        raise ValueError(f"{term.name} is written {expected}, not {value!r}")
    _, _, fraction = number.partition(".")
    if len(fraction) > domain.decimals:
        raise ValueError(f"{term.name} takes at most {domain.decimals} decimals, not {number}")
    if not domain.low <= Decimal(number) <= domain.high:
        raise ValueError(f"{term.name} is from {domain.low} to {domain.high}, not {number}")


def _check_sensors(value: str) -> None:
    labels = value.split(SENSOR_SEPARATOR)
    for label in labels:
        if not label or canonicalise_label(label) != label:
            raise ValueError(f"{label!r} is not a canonical channel label")
    if len(set(labels)) != len(labels):
        raise ValueError("a sensor list names each channel once")


def _check_finding_group(value: str) -> None:
    # Written exactly as the number, so one group has one spelling.
    if not (value.isascii() and value.isdigit()) or str(int(value)) != value or int(value) not in FINDING_GROUP_NUMBERS:
        raise ValueError(
            f"{FINDING_GROUP} takes a finding group's number from {FINDING_GROUP_NUMBERS.start}"
            f" to {FINDING_GROUP_NUMBERS.stop - 1}, not {value!r}"
        )


def _check_tag(tag: str, index: SchemaIndex, allowed_branches: set[str]) -> bool:
    """Raise ``ValueError`` when *tag* breaks a rule; return whether it carries a numeric value."""
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
        return False
    if not term.takes_value:
        raise ValueError(f"{tag!r} is not a tag: write tags in their short form, without extensions")
    if name == SENSOR_LIST:
        _check_sensors(value)
    elif name == FINDING_GROUP:
        _check_finding_group(value)
    elif NUMERIC_CLASS in term.value_classes:
        _check_numeric(term, value)
        return True
    else:
        raise ValueError(f"{name} takes a text value, which is not accepted")
    return False


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
    tags = _tags(value)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"a HED string holds at most {MAX_TAGS} tags")
    numbers = sum(_check_tag(tag, index, allowed_branches) for tag in tags)
    if numbers > MAX_NUMBERS:
        raise ValueError(f"a HED string holds at most {MAX_NUMBERS} numeric values")
