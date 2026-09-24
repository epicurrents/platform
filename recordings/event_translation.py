"""Translating the events a file arrived with into the platform's own vocabulary at ingest.

An event's free text never reaches a reader under a de-identifying grant, so an event whose meaning lives only in
its name reaches such a reader as a bare timestamp; a code from a registered vocabulary does cross that boundary.
And the strings themselves are the problem the translation exists for: a converter-derived event type is a
near-signature of the acquisition software and through it the acquiring laboratory, which is site metadata
surviving inside the annotations. Design and the term tables are in
``docs/engineering-notes/annotation-event-vocabulary.md``; the registry is the one designed in
``docs/engineering-notes/hed-score-integration.md`` section 8.

The rule is fail-closed. A source event a mapper translates to a term of a pinned vocabulary
(``annotations.core_vocabularies``) becomes an ``Event`` row named by the term, classed by the term and carrying a
``Code`` under the standard that owns the term. A source event nothing translates, or that a mapper translates to
a code no vocabulary has, becomes a placeholder ``Event`` that says only that the file had an event there: named
``Source event`` or ``Source annotation`` by its kind, timed, and carrying no text. The vendor string itself is
written nowhere but the raw record the two seams keep already, the ``"Original annotations"`` and
``"Source events"`` bundles, which follow the annotation-text rule like every other row. A silently wrong term is
worse than an untranslated event, so nothing here coerces a string to its nearest term.

Two kinds of mapper, tried in this order until one answers:

* Python mappers registered with :func:`register_event_translation` from a project's or plugin's
  ``AppConfig.ready()``, for translations that need logic.
* Translation tables, JSON files named in ``RECORDING_EVENT_TRANSLATIONS``, for the common case: a vendor's event
  type or label is a fixed string. A table is data, so the mapping for a converter the platform runs at arm's
  length ships beside that converter without the platform importing it. The format is in :func:`load_table`.

``RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS`` keeps its meaning of writing nothing from the file that carries text or
the vendor's vocabulary: under it the raw record and the placeholders are not written. Translated events are,
because a translated event carries the platform's own term and a timestamp and nothing from the file besides.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The placeholder names, by the kind of the source event: an untyped one is a text annotation, a typed one a
#: vendor event. Constants of the platform rather than anything from the file.
PLACEHOLDER_ANNOTATION_NAME = "Source annotation"
PLACEHOLDER_EVENT_NAME = "Source event"

#: The keys a table rule may carry.
_RULE_KEYS = {"type", "label", "pattern", "code", "meta"}
_MATCHER_KEYS = {"type", "label", "pattern"}
_GROUP_REF = re.compile(r"\{(\d+)\}")


class EventTranslationError(Exception):
    """A translation table is malformed: the system check reports it and the loader refuses it."""


@dataclass(frozen=True)
class SourceEvent:
    """One event as the file or the converter delivered it.

    ``type`` is the vendor's event type where the source distinguishes one (a converter sidecar's ``events``), and
    empty for a text annotation (an EDF+ TAL, a sidecar's ``annotations``); ``label`` is the text or the vendor
    label. ``onset`` is in the seam's time base, a data position for the TAL path.
    """

    onset: float
    duration: float | None
    label: str = ""
    type: str = ""


@dataclass(frozen=True)
class Translation:
    """What a mapper answers: a code of a pinned vocabulary and, optionally, the term's metadata."""

    code: str
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedTerm:
    """A translation resolved against the pinned vocabularies: what the ``Event`` and its ``Code`` are written from."""

    standard: str
    code: str
    name: str
    event_class: str
    meta: dict[str, Any] | None = None


Mapper = Callable[[SourceEvent], "Translation | str | None"]


@dataclass
class _Registration:
    name: str
    mapper: Mapper


_MAPPERS: list[_Registration] = []


def register_event_translation(mapper: Mapper, *, name: str) -> None:
    """Register *mapper* under *name*; call from the owning ``AppConfig.ready()``.

    The mapper receives a :class:`SourceEvent` and returns a :class:`Translation`, a bare code, or ``None`` for an
    event it does not know. Registering an existing *name* replaces it, which keeps ``ready()`` idempotent across
    repeated app loading in tests. A mapper that raises is logged and treated as not knowing the event.
    """
    unregister_event_translation(name)
    _MAPPERS.append(_Registration(name=name, mapper=mapper))


def unregister_event_translation(name: str) -> None:
    """Remove the mapper registered under *name*; primarily test cleanup."""
    _MAPPERS[:] = [entry for entry in _MAPPERS if entry.name != name]


def registered_event_translations() -> list[str]:
    """The names of the registered mappers, in the order they are consulted."""
    return [entry.name for entry in _MAPPERS]


# ── Translation tables ────────────────────────────────────────────────────────


def _normalise(text: str) -> str:
    """Casefold and collapse whitespace, so a table matches the string a vendor writes however it is spaced."""
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class _Rule:
    code: str
    type: str | None = None
    label: str | None = None
    pattern: re.Pattern | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def apply(self, source: SourceEvent) -> Translation | None:
        if self.type is not None and _normalise(source.type) != self.type:
            return None
        if self.label is not None and _normalise(source.label) != self.label:
            return None
        groups: tuple[str, ...] = ()
        if self.pattern is not None:
            match = self.pattern.fullmatch(" ".join(source.label.split()))
            if match is None:
                return None
            groups = match.groups()

        def fill(value: Any) -> Any:
            if not isinstance(value, str) or not groups:
                return value
            return _GROUP_REF.sub(
                lambda m: groups[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(groups) else "", value
            )

        return Translation(code=fill(self.code), meta={k: fill(v) for k, v in self.meta.items()} or None)


@dataclass(frozen=True)
class Table:
    """A loaded translation table: its rules in file order and the codes the system check can verify."""

    path: Path
    name: str
    rules: tuple[_Rule, ...]

    @property
    def literal_codes(self) -> list[str]:
        """The codes that carry no group reference, which is every code a check can resolve ahead of a match."""
        return [rule.code for rule in self.rules if not _GROUP_REF.search(rule.code)]


def _parse_rule(raw: Any, index: int, path: Path) -> _Rule:
    where = f"{path}: rule {index}"
    if not isinstance(raw, dict):
        raise EventTranslationError(f"{where} is not an object.")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise EventTranslationError(f"{where} has unknown keys {sorted(unknown)}.")
    if not isinstance(raw.get("code"), str) or not raw["code"]:
        raise EventTranslationError(f"{where} needs a non-empty string code.")
    if not _MATCHER_KEYS & set(raw):
        raise EventTranslationError(f"{where} matches nothing: give type, label or pattern.")
    if "label" in raw and "pattern" in raw:
        raise EventTranslationError(f"{where} gives both label and pattern; a rule matches the label one way.")
    for key in ("type", "label", "pattern"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key]):
            raise EventTranslationError(f"{where} key {key!r} must be a non-empty string.")
    if "meta" in raw and not isinstance(raw["meta"], dict):
        raise EventTranslationError(f"{where} key 'meta' must be an object.")
    pattern = None
    if "pattern" in raw:
        try:
            pattern = re.compile(raw["pattern"], re.IGNORECASE)
        except re.error as error:
            raise EventTranslationError(f"{where} pattern does not compile: {error}") from error
    return _Rule(
        code=raw["code"],
        type=_normalise(raw["type"]) if "type" in raw else None,
        label=_normalise(raw["label"]) if "label" in raw else None,
        pattern=pattern,
        meta=dict(raw.get("meta") or {}),
    )


def load_table(path: Path) -> Table:
    """Parse the translation table at *path*, raising :class:`EventTranslationError` on any malformation.

    The file is an object with an optional ``name`` and a ``rules`` list, tried in order until one matches, so a
    specific rule goes before a general one. A rule carries ``code`` and at least one matcher: ``type`` and
    ``label`` compare to the source's fields casefolded with whitespace collapsed, ``pattern`` is a regular
    expression the whole label must match, case-insensitively. With ``pattern``, ``{1}``, ``{2}``, … in ``code``
    and in string ``meta`` values are replaced by the groups, and a code assembled that way is checked against the
    vocabularies at match time rather than at deploy, failing closed. ``meta`` is an object stored on the code.

        {"name": "Vendor X events",
         "rules": [{"type": "Eyes closed", "code": "EEG_ACT_EC"},
                   {"type": "Photic", "pattern": r"(\\d+) ?hz", "code": "EEG_ACT_PHOTIC_{1}HZ"},
                   {"label": "Recording Paused", "code": "BIO_TECH_PAUSE"}]}
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EventTranslationError(f"{path}: cannot be read: {error.strerror or error}") from error
    except ValueError as error:
        raise EventTranslationError(f"{path}: is not valid JSON: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
        raise EventTranslationError(f"{path}: must be an object with a rules list.")
    name = raw.get("name", path.stem)
    if not isinstance(name, str):
        raise EventTranslationError(f"{path}: name must be a string.")
    rules = tuple(_parse_rule(rule, index, path) for index, rule in enumerate(raw["rules"]))
    return Table(path=path, name=name, rules=rules)


def table_paths() -> list[Path]:
    """The table files ``RECORDING_EVENT_TRANSLATIONS`` names, relative paths resolved against ``BASE_DIR``."""
    from django.conf import settings

    base = Path(getattr(settings, "BASE_DIR", "."))
    paths = []
    for entry in getattr(settings, "RECORDING_EVENT_TRANSLATIONS", None) or []:
        path = Path(entry)
        paths.append(path if path.is_absolute() else base / path)
    return paths


def load_tables() -> list[Table]:
    """Every configured table, parsed. Raises on the first malformed one, naming it."""
    return [load_table(path) for path in table_paths()]


@cache
def _cached_tables(key: tuple[str, ...]) -> list[Table]:
    """The parsed tables for one configuration, kept for the life of the process; a bad table is skipped and logged.

    The system check refuses a deployment whose table is malformed, so this branch is reached only by a table
    edited under a running worker. Skipping it fails closed: its events become placeholders.
    """
    tables = []
    for path in (Path(entry) for entry in key):
        try:
            tables.append(load_table(path))
        except EventTranslationError as error:
            logger.error("event translation: table refused, its events will not translate: %s", error)
    return tables


def _tables() -> list[Table]:
    return _cached_tables(tuple(str(path) for path in table_paths()))


# ── Translation ───────────────────────────────────────────────────────────────


def _resolve(translation: Translation | str | None, *, origin: str) -> ResolvedTerm | None:
    from annotations.core_vocabularies import find_acquisition_term

    if translation is None:
        return None
    if isinstance(translation, str):
        translation = Translation(code=translation)
    if not isinstance(translation, Translation) or not isinstance(translation.code, str):
        # The type name only: a mapper that answered the source event itself would otherwise log its label.
        logger.warning(
            "event translation: %s answered a %s rather than a Translation; ignored.",
            origin,
            type(translation).__name__,
        )
        return None
    term = find_acquisition_term(translation.code)
    if term is None:
        logger.warning(
            "event translation: %s answered %s, which no vocabulary has; ignored.", origin, _loggable(translation.code)
        )
        return None
    meta = dict(translation.meta) if translation.meta else None
    return ResolvedTerm(standard=term.standard, code=term.code, name=term.name, event_class=term.event_class, meta=meta)


_CODE_SHAPE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


def _loggable(code: str) -> str:
    """*code* if it has the shape of a vocabulary code, else a marker: a mapper's output is what the log names, and a
    mapper that echoed the vendor's label as its code would otherwise put that label into a permanent stream."""
    return repr(code) if _CODE_SHAPE.fullmatch(code) else "a value that is not shaped like a code"


def translate_source_event(source: SourceEvent) -> ResolvedTerm | None:
    """The term *source* translates to, or ``None`` when nothing registered or configured knows it.

    Registered mappers are asked first, in registration order, then the tables in configuration order, and the
    first answer that resolves to a term of a pinned vocabulary wins. Nothing here logs the source's text: a vendor
    string in a permanent log stream is the exposure the translation exists to prevent.
    """
    for entry in _MAPPERS:
        try:
            answer = entry.mapper(source)
        except Exception:
            logger.warning(
                "event translation: mapper %r raised; the event is left untranslated.", entry.name, exc_info=True
            )
            continue
        resolved = _resolve(answer, origin=f"mapper {entry.name!r}")
        if resolved is not None:
            return resolved
    for table in _tables():
        for rule in table.rules:
            resolved = _resolve(rule.apply(source), origin=f"table {table.name!r}")
            if resolved is not None:
                return resolved
    return None


# ── Writing ───────────────────────────────────────────────────────────────────


def annotation_hash(recording_pk: int, suffix: str) -> str:
    """A 32-character uppercase hex ``object_hash`` for a server-generated annotation row.

    Keyed on the recording's primary key rather than the file hash, so uploading the same file a second time yields
    a different set of hashes. *suffix* distinguishes sibling rows on one recording.
    """
    key = f"{recording_pk}:{suffix}"
    return hashlib.sha256(key.encode()).hexdigest()[:32].upper()


def placeholder_name(source: SourceEvent) -> str:
    """The name of the placeholder written for an untranslated *source*."""
    return PLACEHOLDER_EVENT_NAME if source.type else PLACEHOLDER_ANNOTATION_NAME


def write_source_events(recording, sources: list[SourceEvent], *, hash_prefix: str) -> int:
    """Write one ``Event`` row per source event on *recording*, translated where anything translates it.

    *hash_prefix* keeps the two seams' rows apart: the row for the event at *index* carries the hash suffix
    ``"<hash_prefix>:<index>"``, so a seam called once per ingest writes distinct hashes. Under
    ``RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS`` the placeholders are skipped and only translated events are written.
    Returns the number of translated events.
    """
    from django.conf import settings
    from django.contrib.contenttypes.models import ContentType

    from annotations.models import Code, Event
    from epicurrents.system_user import get_system_user

    discard = getattr(settings, "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS", False)
    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    event_ct = None
    system_user = None
    translated = 0
    for index, source in enumerate(sources):
        term = translate_source_event(source)
        if term is None and discard:
            continue
        if system_user is None:
            system_user = get_system_user()
        event = Event.objects.create(
            author=system_user,
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
            object_hash=annotation_hash(recording.pk, f"{hash_prefix}:{index}"),
            name=term.name if term else placeholder_name(source),
            event_class=term.event_class if term else "",
            timestamp=source.onset,
            duration=source.duration,
        )
        if term is None:
            continue
        if event_ct is None:
            event_ct = ContentType.objects.get_for_model(Event)
        Code.objects.create(
            content_type=event_ct, object_id=str(event.pk), standard=term.standard, value=term.code, meta=term.meta
        )
        translated += 1
    return translated
