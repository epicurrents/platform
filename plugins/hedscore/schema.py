"""The vendored HED-SCORE schema, verified against its pin and parsed into a term index.

The schema is the merged XML of the SCORE library and the HED standard it is partnered with, so one file holds every
tag a string may use. It is read from ``schemas/`` and never fetched: a validator whose answer depends on reaching
GitHub is not a control. The file's SHA-256 is checked before it is parsed, which is what lets the standard library's
parser read it; a file that fails the check, or is missing, raises ``ImproperlyConfigured`` from ``ready()`` and the
platform does not start.

The viewer generates its picker's term index from the same release, and records the version and digest it was
generated from beside it, so ``SCHEMA_PIN`` is the one statement of which release both sides speak. Design in
``docs/engineering-notes/hed-score-integration.md``.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


@dataclass(frozen=True)
class SchemaPin:
    """Which schema release the plugin validates against, and the digest its vendored file must carry."""

    file: str
    library: str
    version: str
    with_standard: str
    sha256: str

    def as_meta(self) -> dict:
        """The pin as a code's ``meta`` carries it, so a stored row names the release it was validated against."""
        return {"schema": {"library": self.library, "version": self.version, "with_standard": self.with_standard}}


SCHEMA_PIN = SchemaPin(
    file="HED_score_2.1.0.xml",
    library="score",
    version="2.1.0",
    with_standard="8.4.0",
    sha256="71b54cdfa1877365ec23b30e276fe42d035c2e9cb7071dc286718c90e67b6089",
)


@dataclass(frozen=True)
class Term:
    """One schema node, keyed in the index by its short name, which HED keeps unique across the merged schema."""

    name: str
    #: The node's long form, top-level branch first.
    path: tuple[str, ...]
    hed_id: str
    description: str
    #: Whether the node is from the SCORE library rather than the partnered standard.
    in_library: bool
    #: The value classes of the node's ``#`` child; empty when the node takes no value.
    value_classes: frozenset[str]
    #: The unit classes of the node's ``#`` child.
    unit_classes: frozenset[str]
    takes_value: bool
    require_child: bool
    reserved: bool
    deprecated: bool

    @property
    def branch(self) -> str:
        """The top-level branch the node sits under."""
        return self.path[0]


@dataclass(frozen=True)
class SchemaIndex:
    """The parsed schema: every node by short name, the top-level branches, and the unit strings per unit class."""

    terms: dict[str, Term]
    branches: tuple[str, ...]
    units: dict[str, frozenset[str]]

    def library_term_count(self) -> int:
        """How many nodes the SCORE library contributes, value placeholders excluded."""
        return sum(1 for term in self.terms.values() if term.in_library)


def _attributes(node: ET.Element) -> dict[str, tuple[str, ...]]:
    return {
        attribute.findtext("name"): tuple(value.text or "" for value in attribute.findall("value"))
        for attribute in node.findall("attribute")
    }


def _children(node: ET.Element) -> list[ET.Element]:
    return node.findall("node")


def _section(root: ET.Element, tag: str) -> list[ET.Element]:
    """The entries of a top-level definitions section, or none. An element's truth value is not its presence."""
    section = root.find(tag)
    return list(section) if section is not None else []


def _units(root: ET.Element) -> dict[str, frozenset[str]]:
    """Every unit string a value may carry, per unit class, SI prefixes applied where the schema allows them.

    A unit marked ``SIUnit`` takes the SI modifiers, the symbol modifiers (``k``, ``m``, ``u``) when the unit is
    itself a symbol and the word modifiers (``kilo``, ``milli``) when it is a word. Plural unit names are not
    accepted; the picker writes the symbol.
    """
    modifiers: dict[str, list[str]] = {"symbol": [], "word": []}
    for modifier in _section(root, "unitModifierDefinitions"):
        kinds = _attributes(modifier)
        if "SIUnitSymbolModifier" in kinds:
            modifiers["symbol"].append(modifier.findtext("name"))
        if "SIUnitModifier" in kinds:
            modifiers["word"].append(modifier.findtext("name"))

    units: dict[str, frozenset[str]] = {}
    for unit_class in _section(root, "unitClassDefinitions"):
        allowed: set[str] = set()
        for unit in unit_class.findall("unit"):
            name = unit.findtext("name")
            allowed.add(name)
            kinds = _attributes(unit)
            if "SIUnit" in kinds:
                prefixes = modifiers["symbol"] if "unitSymbol" in kinds else modifiers["word"]
                allowed.update(prefix + name for prefix in prefixes)
        units[unit_class.findtext("name")] = frozenset(allowed)
    return units


def parse_schema(data: bytes) -> SchemaIndex:
    """Parse the merged schema XML into an index. Trusted input only: call it on bytes that passed the pin."""
    root = ET.fromstring(data)
    terms: dict[str, Term] = {}

    def walk(node: ET.Element, path: tuple[str, ...]) -> None:
        name = node.findtext("name")
        attributes = _attributes(node)
        placeholder = next((child for child in _children(node) if child.findtext("name") == "#"), None)
        value_attributes = _attributes(placeholder) if placeholder is not None else {}
        terms[name] = Term(
            name=name,
            path=path,
            hed_id=attributes.get("hedId", ("",))[0],
            description=node.findtext("description") or "",
            in_library="inLibrary" in attributes,
            value_classes=frozenset(value_attributes.get("valueClass", ())),
            unit_classes=frozenset(value_attributes.get("unitClass", ())),
            takes_value=placeholder is not None,
            require_child="requireChild" in attributes,
            reserved="reserved" in attributes,
            deprecated="deprecatedFrom" in attributes,
        )
        for child in _children(node):
            if child.findtext("name") != "#":
                walk(child, (*path, child.findtext("name")))

    branches = []
    for top in _children(root.find("schema")):
        branches.append(top.findtext("name"))
        walk(top, (top.findtext("name"),))
    return SchemaIndex(terms=terms, branches=tuple(branches), units=_units(root))


def read_pinned_schema(pin: SchemaPin = SCHEMA_PIN, directory: Path = SCHEMA_DIR) -> bytes:
    """The vendored file's bytes, or ``ImproperlyConfigured`` when it is missing or does not match its pin."""
    path = directory / pin.file
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ImproperlyConfigured(f"hedscore: the vendored schema {path} is missing") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != pin.sha256:
        raise ImproperlyConfigured(f"hedscore: {path} has SHA-256 {digest}, expected {pin.sha256}")
    return data


@cache
def load_schema() -> SchemaIndex:
    """The index of the pinned schema, parsed once per process."""
    return parse_schema(read_pinned_schema())
