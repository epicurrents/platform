"""Import a published dataset's subject table onto the subject fields of the dataset its recordings were imported into.

A published dataset ships its subject facts as a table, one row per file or per participant (a BIDS
``participants.tsv`` is the common case). The table's columns hold what the original's authors recorded, exact
ages and local codes among them, which a field of choices does not admit. A *mapping file* says how
each declared field is read from the table: which column, and how a cell becomes one of the field's choices,
through a table of values, a list of numeric bands, or both. A number field names its column alone, and a cell is
stored as the number it reads as. Every column the mapping does not name is dropped, and a cell the mapping does
not cover, or that is not a finite number for a number field, is counted and never stored.

Rows are paired with recordings by the original's file name, which reaches the platform only in the
author-private ``Recording.original_name``, or by the file's bytes, through the server-only ``Recording.file_hash``,
when the deployment discards names. The import reads either to match and stores nothing of it, and nothing it
reports names a recording, a file or a value: the run is described by counts and by the table's line numbers.
Pairing by position is not offered, because one file missing from the import would shift every later row onto the
wrong recording with nothing to show it.

The mapping file is JSON::

    {
      "match": {"column": "participant_id", "on": "prefix"},
      "fields": {
        "age": {"column": "age", "bands": [{"below": 18, "choice": "0-17"},
                                           {"from": 18, "below": 65, "choice": "18-64"},
                                           {"from": 65, "choice": "65+"}],
                "values": {"89+": "65+"}},
        "sex": {"column": "sex", "values": {"F": "female", "M": "male"}},
        "age_years": {"column": "age"}
      }
    }

where ``age_years`` is a number field.

``match.on`` is ``name`` (the cell's file name equals the recording's), ``stem`` (the same, ignoring the
extension, for a file a converter renamed) or ``prefix`` (the recording's file name starts with the cell followed
by a character that is not a letter or a digit, so ``sub-1`` does not match ``sub-10_task-rest_eeg.edf``) or
``content`` (the cell names a file under a source folder the caller gives, whose SHA-256 equals the recording's
``file_hash``). ``content`` is the mode for a deployment running ``RECORDINGS_DISCARD_ORIGINAL_NAME``, where no name
was kept to match; it pairs a native EDF or BDF file only, since a converted file's digest is of the EDF the
converter wrote. Rows naming byte-identical files that would write the same values share one outcome, and a
recording whose bytes another member shares takes those values with it. ``files_not_read`` counts the rows whose cell
is empty or names no readable file in the folder, which tells an operator's mistake from a file whose bytes match no
recording. A band is half-open, ``from`` inclusive and ``below`` exclusive, either end optional, and bands may not
overlap. ``values`` is consulted before ``bands``. A cell that is empty or ``n/a`` is *missing*, not unmapped.

The rules of ``library.subjects`` hold: the values are checked against the schema under the dataset's row lock,
a released member of a release-gated dataset keeps its values, and the audit trail withholds them. A submission
pool is refused, because its members' values arrive with their submissions and its members carry no original
file name.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any

MATCH_MODES = ("name", "stem", "prefix", "content")
_MISSING = {"", "n/a", "na"}
# What a missing cell writes in ``_outcome``: a tuple, which no choice and no number can equal.
_WRITES_NOTHING = ("missing",)


class ImportRefused(Exception):
    """The table, the mapping or the dataset cannot be imported; nothing has been written."""


@dataclass
class FieldMap:
    """How one declared field is read from the table."""

    key: str
    column: str
    values: dict[str, str]
    bands: list[tuple[float, float, str]]
    number: bool = False

    def choice_for(self, cell: str) -> str | float | None:
        """The choice *cell* maps to, or for a number field the number it reads as; ``None`` when the mapping does not
        cover it."""
        if self.number:
            from library.subjects import is_number

            try:
                value = float(cell)
            except ValueError:
                return None
            return value if is_number(value) else None
        if cell in self.values:
            return self.values[cell]
        if not self.bands:
            return None
        try:
            number = float(cell)
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        for low, high, choice in self.bands:
            if low <= number < high:
                return choice
        return None


@dataclass
class Mapping:
    """A validated mapping file."""

    match_column: str
    match_on: str
    fields: list[FieldMap]


@dataclass
class ImportPlan:
    """What an import would write, and the counts that describe it. Holds no file name and no cell."""

    rows: int = 0
    matched_rows: int = 0
    unmatched_lines: list[int] = field(default_factory=list)
    ambiguous_lines: list[int] = field(default_factory=list)
    members: int = 0
    members_matched: int = 0
    members_released: int = 0
    members_not_controlled: int = 0
    members_changed: int = 0
    files_not_read: int = 0
    mapped: Counter = field(default_factory=Counter)
    unmapped: Counter = field(default_factory=Counter)
    missing: Counter = field(default_factory=Counter)
    writes: dict[int, dict] = field(default_factory=dict)

    def report(self) -> dict:
        """The plan as counts and line numbers, the shape the command prints and the audit row keeps in part."""
        keys = sorted(set(self.mapped) | set(self.unmapped) | set(self.missing))
        return {
            "rows": self.rows,
            "matched_rows": self.matched_rows,
            "unmatched_rows": len(self.unmatched_lines),
            "ambiguous_rows": len(self.ambiguous_lines),
            "unmatched_lines": self.unmatched_lines,
            "ambiguous_lines": self.ambiguous_lines,
            "members": self.members,
            "members_matched": self.members_matched,
            "members_released": self.members_released,
            "members_not_controlled": self.members_not_controlled,
            "members_changed": self.members_changed,
            "files_not_read": self.files_not_read,
            "fields": {
                key: {"mapped": self.mapped[key], "unmapped": self.unmapped[key], "missing": self.missing[key]}
                for key in keys
            },
        }

    def audit_metadata(self) -> dict:
        """The counts the audit row carries: no line numbers, which say where in someone's table a subject sits."""
        report = self.report()
        for key in ("unmatched_lines", "ambiguous_lines"):
            report.pop(key)
        return report


def read_table(path: Path) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    """The table's header and its rows with their line numbers; a ``.tsv`` or ``.tab`` file is tab-separated."""
    delimiter = "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh, delimiter=delimiter)
            header = next(reader, None)
            if not header:
                raise ImportRefused("The table has no header row.")
            header = [column.strip() for column in header]
            if len(set(header)) != len(header):
                raise ImportRefused("The table's header names a column twice.")
            rows = []
            for row in reader:
                if not any(cell.strip() for cell in row):
                    continue
                if len(row) > len(header):
                    raise ImportRefused(f"Line {reader.line_num} has more cells than the header.")
                padded = row + [""] * (len(header) - len(row))
                rows.append((reader.line_num, {column: cell.strip() for column, cell in zip(header, padded)}))
    except UnicodeDecodeError as exc:
        raise ImportRefused("The table is not UTF-8 text.") from exc
    except csv.Error as exc:
        raise ImportRefused(f"The table cannot be read as delimited text: {exc}") from exc
    return header, rows


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ImportRefused(f"{what} must be a finite number.")
    return float(value)


def load_mapping(raw: Any, schema: list[dict], columns: list[str]) -> Mapping:
    """Validate a parsed mapping file against the dataset's schema and the table's columns, or raise."""
    if not isinstance(raw, dict) or set(raw) != {"match", "fields"}:
        raise ImportRefused("The mapping must be an object with exactly the keys match and fields.")
    match = raw["match"]
    if not isinstance(match, dict) or not isinstance(match.get("column"), str) or set(match) - {"column", "on"}:
        raise ImportRefused("match must be an object naming a column, with an optional on.")
    match_on = match.get("on", "name")
    if match_on not in MATCH_MODES:
        raise ImportRefused(f"match.on must be one of {', '.join(MATCH_MODES)}.")
    if match["column"] not in columns:
        raise ImportRefused("The table has no column named by match.column.")
    from library.subjects import is_number_field

    choices = {entry["key"]: entry.get("choices", []) for entry in schema}
    numbers = {entry["key"] for entry in schema if is_number_field(entry)}
    if not isinstance(raw["fields"], dict) or not raw["fields"]:
        raise ImportRefused("fields must be a non-empty object of subject field keys.")
    fields = []
    for key, spec in raw["fields"].items():
        if key not in choices:
            raise ImportRefused(f"The dataset declares no subject field {key}.")
        if not isinstance(spec, dict) or not isinstance(spec.get("column"), str):
            raise ImportRefused(f"The mapping of {key} must be an object naming a column.")
        if key in numbers:
            if set(spec) != {"column"}:
                raise ImportRefused(f"{key} is a number field; its mapping names a column and nothing else.")
            if spec["column"] not in columns:
                raise ImportRefused(f"The table has no column named by the mapping of {key}.")
            fields.append(FieldMap(key=key, column=spec["column"], values={}, bands=[], number=True))
            continue
        if set(spec) - {"column", "values", "bands"} or not ({"values", "bands"} & set(spec)):
            raise ImportRefused(f"The mapping of {key} takes a column and values, bands or both.")
        if spec["column"] not in columns:
            raise ImportRefused(f"The table has no column named by the mapping of {key}.")
        values = spec.get("values", {})
        if not isinstance(values, dict):
            raise ImportRefused(f"The values of {key} must be an object.")
        for choice in values.values():
            if choice not in choices[key]:
                raise ImportRefused(f"A value of {key} maps to something that is not one of its choices.")
        if not isinstance(spec.get("bands", []), list):
            raise ImportRefused(f"The bands of {key} must be a list.")
        bands = []
        for band in spec.get("bands", []):
            if not isinstance(band, dict) or "choice" not in band or set(band) - {"from", "below", "choice"}:
                raise ImportRefused(f"A band of {key} must be an object with a choice and from, below or both.")
            if band["choice"] not in choices[key]:
                raise ImportRefused(f"A band of {key} maps to something that is not one of its choices.")
            if "from" not in band and "below" not in band:
                raise ImportRefused(f"A band of {key} needs from, below or both.")
            low = _number(band["from"], f"A band's from in {key}") if "from" in band else -math.inf
            high = _number(band["below"], f"A band's below in {key}") if "below" in band else math.inf
            if low >= high:
                raise ImportRefused(f"A band of {key} is empty: from must be below below.")
            bands.append((low, high, band["choice"]))
        bands.sort()
        for (_, high, _), (low, _, _) in pairwise(bands):
            if low < high:
                raise ImportRefused(f"The bands of {key} overlap.")
        fields.append(FieldMap(key=key, column=spec["column"], values=dict(values), bands=bands))
    return Mapping(match_column=match["column"], match_on=match_on, fields=fields)


def hash_files(rows: list[tuple[int, dict[str, str]]], column: str, root: Path) -> dict[int, str | None]:
    """The SHA-256 of the file each row's *column* names under *root*, by line.

    ``None`` for a cell that is empty, names a path outside *root* as written (an absolute path or a ``..`` escape),
    or names nothing that can be read as a regular file, so such a row is unmatched rather than read from elsewhere.
    Containment is checked on the path as written and links are followed after it, because the escape the check
    exists for comes from the table, and a DataLad or git-annex tree keeps every file behind a link into its annex.
    A read error is a ``None`` too, so the run reports the row instead of a traceback naming the file.
    """
    root = root.resolve()
    digests: dict[int, str | None] = {}
    for line, row in rows:
        cell = row[column].replace("\\", "/")
        digests[line] = None
        if not cell:
            continue
        path = Path(os.path.normpath(root / cell))
        if not path.is_relative_to(root):
            continue
        try:
            if not path.is_file():
                continue
            hasher = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    hasher.update(chunk)
        except (OSError, ValueError):
            continue
        digests[line] = hasher.hexdigest()
    return digests


def _stem(name: str) -> str:
    """*name* without its extension; a suffix that does not start with a letter (``sub-1.2``) is not one."""
    path = Path(name)
    return path.stem if path.suffix[1:2].isalpha() else name


def _matches(original_name: str, cell: str, match_on: str) -> bool:
    name = PurePosixPath(original_name.replace("\\", "/")).name
    if match_on == "prefix":
        if not cell or not name.startswith(cell):
            return False
        rest = name[len(cell) :]
        return rest == "" or not rest[0].isalnum()
    wanted = PurePosixPath(cell.replace("\\", "/")).name
    if match_on == "stem":
        return bool(wanted) and _stem(name) == _stem(wanted)
    return bool(wanted) and name == wanted


def _outcome(mapping: Mapping, row: dict[str, str]) -> tuple:
    """What *row* would write for each mapped field, so ``30`` and ``30.0``, or ``n/a`` and an empty cell, agree."""
    return tuple(
        _WRITES_NOTHING if row[field_map.column].lower() in _MISSING else field_map.choice_for(row[field_map.column])
        for field_map in mapping.fields
    )


def plan_import(
    dataset: Any,
    schema: list[dict],
    mapping: Mapping,
    rows: list[tuple[int, dict[str, str]]],
    digests: dict[int, str | None] | None = None,
) -> ImportPlan:
    """Pair the table's rows with the dataset's recording members and work out each member's new values.

    A member matched by more than one row is matched by none, and every row that matched it is reported as
    ambiguous: two rows disagreeing about one recording is not a choice the import makes. A row matching several
    members is fine under ``prefix``, a participant with several recordings, and under ``content``, recordings with
    the same bytes; it is ambiguous otherwise. ``content`` needs *digests*, from ``hash_files``.
    """
    from django.contrib.contenttypes.models import ContentType

    from library.models import DatasetItem, DatasetItemSubject
    from library.subjects import apply_values
    from recordings.models import Recording

    plan = ImportPlan(rows=len(rows))
    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    items = list(DatasetItem.objects.filter(dataset=dataset, content_type=recording_ct))
    live = Recording.objects.filter(
        pk__in=[int(item.object_id) for item in items if item.object_id.isdigit()], deleted_at__isnull=True
    )
    # Only the dataset author's own recordings: another author's original name is theirs, and pairing it with a
    # row of the table would tell the dataset's author which subject their recording is.
    by_content = mapping.match_on == "content"
    if by_content and digests is None:
        raise ImportRefused("Matching on content needs the files the table names.")
    names = dict(
        live.filter(author_id=dataset.author_id).values_list("pk", "file_hash" if by_content else "original_name")
    )
    plan.members_not_controlled = live.exclude(author_id=dataset.author_id).count()
    members = [
        (item, names[int(item.object_id)])
        for item in items
        if item.object_id.isdigit() and int(item.object_id) in names
    ]
    plan.members = len(members)

    # Rows naming byte-identical files that would write the same values follow the first of them.
    duplicate_of: dict[int, int] = {}
    if by_content:
        plan.files_not_read = sum(1 for line, _row in rows if digests.get(line) is None)
        groups: dict[tuple, list[int]] = {}
        for line, row in rows:
            if digests.get(line):
                groups.setdefault((digests[line], _outcome(mapping, row)), []).append(line)
        duplicate_of = {line: lines[0] for lines in groups.values() for line in lines[1:]}

    hits: dict[int, list[int]] = {}
    row_members: dict[int, list] = {}
    for line, row in rows:
        if line in duplicate_of:
            continue
        if by_content:
            digest = digests.get(line)
            matched = [item for item, file_hash in members if digest and file_hash == digest]
        else:
            cell = row[mapping.match_column]
            matched = [
                item
                for item, original_name in members
                if original_name and _matches(original_name, cell, mapping.match_on)
            ]
        row_members[line] = matched
        for item in matched:
            hits.setdefault(item.pk, []).append(line)

    ambiguous = {line for lines in hits.values() if len(lines) > 1 for line in lines}
    if mapping.match_on not in ("prefix", "content"):
        ambiguous |= {line for line, matched in row_members.items() if len(matched) > 1}
    current = dict(
        DatasetItemSubject.objects.filter(item__in=[item for item, _ in members]).values_list("item_id", "values")
    )

    for line, row in rows:
        if line in duplicate_of:
            if duplicate_of[line] in ambiguous:
                plan.ambiguous_lines.append(line)
            elif row_members[duplicate_of[line]]:
                plan.matched_rows += 1
            else:
                plan.unmatched_lines.append(line)
            continue
        matched = row_members[line]
        if line in ambiguous:
            plan.ambiguous_lines.append(line)
            continue
        if not matched:
            plan.unmatched_lines.append(line)
            continue
        plan.matched_rows += 1
        changes = {}
        for field_map in mapping.fields:
            cell = row[field_map.column]
            if cell.lower() in _MISSING:
                plan.missing[field_map.key] += len(matched)
                continue
            choice = field_map.choice_for(cell)
            if choice is None:
                plan.unmapped[field_map.key] += len(matched)
                continue
            plan.mapped[field_map.key] += len(matched)
            changes[field_map.key] = choice
        for item in matched:
            plan.members_matched += 1
            if dataset.release_gated and item.release_id is not None:
                plan.members_released += 1
                continue
            before = current.get(item.pk) or {}
            after = apply_values(schema, before, changes)
            if after != before:
                plan.members_changed += 1
                plan.writes[item.pk] = after
    return plan


def apply_plan(plan: ImportPlan) -> None:
    """Write the plan's values. The caller holds the dataset's row lock, and made the plan under it."""
    from library.models import DatasetItemSubject

    existing = {row.item_id: row for row in DatasetItemSubject.objects.filter(item_id__in=list(plan.writes))}
    for item_id, values in plan.writes.items():
        row = existing.get(item_id)
        if row is None:
            DatasetItemSubject.objects.create(item_id=item_id, values=values)
        else:
            row.values = values
            row.save(update_fields=["values", "modified_at"])
