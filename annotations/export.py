"""Bulk export of Event and Label annotations as JSON or CSV.

The per-target list endpoints in :mod:`annotations.api.v1.ninja` answer "what is on this object";
this module answers "give me the rows, across targets, attributable per annotator". It exists for
the research / QA workflow where someone needs the rater output off the platform and into pandas or
R, and needs to keep apart whose output is whose.

Access follows the staff tier from AGENTS.md: a staff (or superuser) caller exports across all
authors, anyone else is restricted to their own rows. The restriction is applied to the queryset,
not checked afterwards, so a non-staff caller cannot widen it through any filter combination.

Two rules from AGENTS.md are enforced on the way out, both concerning the *target* rather than the
annotation:

- FAILED-hidden recordings are dropped, reusing ``_failed_hidden_for_caller`` from the recordings
  API rather than restating the rule. Soft-deleted recordings are dropped for the same reason — a
  trashed recording should not come back through an export.
- ``Recording.original_name`` never appears. Targets are identified by ``content_hash`` and
  labelled with the grantee-visible display name.

Annotators are identified by their numeric user id only, per row and in the metadata roster. No
username or real name enters the file: personal data leaves the platform's erasure reach the moment
a file is written, so the id-to-identity mapping stays behind authentication instead —
:func:`list_annotators` backs the staff-only roster endpoint the exporter reads it from. See
annotations/README.md for the operator note.

Projects extend the rows rather than replacing the endpoint: :func:`register_export_extension`
lets a plugin complement rows targeting its own models with target-derived columns while the
access tiers, target hiding, and de-identification above keep applying unchanged.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from ninja.errors import HttpError

from annotations.models import Event, Label

#: Bumped when the emitted field set changes in a way a downstream parser could trip over.
#: Additive changes (a new column at the end, a new metadata key) do not bump it.
#: Version 2 replaced ``author_username`` with ``author_id`` and stripped names and usernames from
#: the metadata header — annotator identity resolves via the in-platform roster endpoint instead.
FORMAT_VERSION = 2

#: Annotation types the export understands, in the order they appear in a JSON payload.
EXPORTABLE_TYPES = ("events", "labels")

#: A ``since``/``until`` value carrying no time component, which is widened to cover the whole day.
_DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_MODELS = {"events": Event, "labels": Label}

#: Columns per type. Order is the CSV column order, with the ``value``/``codes`` pair last so a
#: reader scanning left to right meets the scalar fields first.
#:
#: ``created_at`` / ``modified_at`` are deliberately absent: AGENTS.md → De-identification requires
#: annotation-type responses to omit them, and a bulk export is the last surface that should carry
#: fields the narrower per-target endpoints withhold. Row order still encodes the sequence, since
#: the queryset sorts on ``created_at`` before serialising.
#:
#: ``author_id`` is the only annotator identifier — no username or name appears anywhere in the
#: file. The staff-only roster endpoint (:func:`list_annotators`) maps ids to identities inside the
#: platform, so an exported file carries attribution without carrying personal data.
_COLUMNS = {
    "events": (
        "object_hash",
        "content_hash",
        "author_id",
        "target_type",
        "target_ref",
        "target_label",
        "version_id",
        "name",
        "event_class",
        "timestamp",
        "duration",
        "value",
        "codes",
    ),
    "labels": (
        "object_hash",
        "content_hash",
        "author_id",
        "target_type",
        "target_ref",
        "target_label",
        "version_id",
        "name",
        "value",
        "codes",
    ),
}

#: Export extensions, keyed by target-model label (``"app_label.model"``, lowercase). Each entry is
#: a list of ``(columns, resolver)`` pairs in registration order. See
#: :func:`register_export_extension`.
_EXPORT_EXTENSIONS: dict[str, list[tuple[tuple[str, ...], object]]] = {}


def register_export_extension(target_model_label: str, *, columns: tuple[str, ...], resolver) -> None:
    """Register a project extension that complements exported rows with target-derived fields.

    Rows whose annotation target is an instance of *target_model_label* (``"app_label.model"``,
    lowercase — the same string the export emits as ``target_type``) gain the extension's
    *columns*. ``resolver(caller=..., objects=...)`` is called once per export with the distinct,
    already access-filtered target instances and returns ``{str(pk): {column: value}}``; every row
    on that target inherits the values. Missing keys or columns fall back to ``None``, and rows
    with other target types simply lack the columns in JSON while CSV keeps them as empty cells —
    the CSV header carries every registered column so its shape is deployment-static, not
    content-dependent.

    The resolver receives the caller so it can apply field-level gates of its own (a project
    extension can gate ``Recording.original_name`` this way). Call from ``AppConfig.ready()``; a
    column that collides with a base column or an earlier registration raises ``ValueError``
    at registration rather than shadowing silently. Additive by design — registered columns do
    not bump :data:`FORMAT_VERSION`.
    """
    taken = {column for type_columns in _COLUMNS.values() for column in type_columns}
    for registered in _EXPORT_EXTENSIONS.values():
        for existing_columns, _ in registered:
            taken.update(existing_columns)
    collisions = [column for column in columns if column in taken]
    if collisions:
        raise ValueError(f"Export extension column(s) already in use: {', '.join(collisions)}")
    _EXPORT_EXTENSIONS.setdefault(target_model_label, []).append((tuple(columns), resolver))


def _extension_columns() -> tuple[str, ...]:
    """Return every registered extension column, in registration order."""
    return tuple(
        column for registered in _EXPORT_EXTENSIONS.values() for columns, _ in registered for column in columns
    )


@dataclass
class TargetInfo:
    """How one annotation target is named in the export.

    ``ref`` is the recording ``content_hash`` when the target is a Recording, and the most opaque
    public identifier the target offers otherwise (see :func:`_opaque_ref`) — the primary key is
    the last resort, not the rule. ``label`` is the grantee-visible display name, empty for
    non-recording targets.
    """

    type_name: str
    ref: str
    label: str


@dataclass
class ExportFilters:
    """Parsed, validated query parameters for one export request."""

    types: tuple[str, ...]
    export_format: str
    recordings: tuple[str, ...] = ()
    dataset_id: int | None = None
    annotator_ids: tuple[int, ...] = ()
    since: datetime | None = None
    until: datetime | None = None
    version_id: str | None = None

    def as_metadata(self) -> dict:
        """Return the applied filters as they appear in the export's metadata header.

        The same dict enters ``Activity.metadata`` unchanged: annotators are filtered by numeric
        user id, so nothing here is personal data. The audit trail is permanent and the export's
        Activity row targets no user, so ``erase_subject`` can never select it to scrub — an id is
        the only annotator reference that may be written there, opaque while the account exists and
        meaningless once it is erased.
        """
        return {
            "types": list(self.types),
            "format": self.export_format,
            "recordings": list(self.recordings),
            "dataset_id": self.dataset_id,
            "annotator_ids": list(self.annotator_ids),
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "version_id": self.version_id,
        }


@dataclass
class ExportResult:
    """Everything an export renderer needs: the rows, and who and what produced them."""

    filters: ExportFilters
    rows: dict[str, list[dict]] = field(default_factory=dict)
    annotators: list[dict] = field(default_factory=list)
    #: User ids of the annotators whose rows are in the export — the flat form of ``annotators``
    #: (id plus per-type counts), for the audit trail and the annotator-count metadata.
    annotator_ids: list[int] = field(default_factory=list)
    restricted_to_self: bool = False


def parse_filters(
    *,
    types: str | None,
    export_format: str | None,
    recordings: list[str] | None,
    dataset_id: int | None,
    annotator_ids: list[int] | None,
    since: str | None,
    until: str | None,
    version_id: str | None,
) -> ExportFilters:
    """Validate raw query parameters into an :class:`ExportFilters`, raising 422 on bad input.

    ``since`` and ``until`` accept a plain date or a full datetime. A bare date is widened to cover
    the whole day — ``until=2026-08-11`` includes everything annotated on the 11th, which is what
    someone typing a date means, and the alternative silently truncates a day of rows.
    """
    requested = tuple(part.strip() for part in (types or "").split(",") if part.strip()) or EXPORTABLE_TYPES
    unknown = [name for name in requested if name not in _MODELS]
    if unknown:
        raise HttpError(422, f"Unknown annotation type(s): {', '.join(unknown)}. Valid types: events, labels.")
    # Preserve the canonical order rather than the order they were typed, so the JSON key order and
    # the generated filename are stable for the same selection.
    ordered = tuple(name for name in EXPORTABLE_TYPES if name in requested)

    fmt = (export_format or "json").strip().lower()
    if fmt not in ("json", "csv"):
        raise HttpError(422, "Unknown format. Valid formats: json, csv.")
    if fmt == "csv" and len(ordered) != 1:
        raise HttpError(
            422,
            "CSV exports carry one annotation type per file, because events and labels do not "
            "share a column set. Request types=events or types=labels, or use format=json.",
        )

    return ExportFilters(
        types=ordered,
        export_format=fmt,
        recordings=tuple(dict.fromkeys(h.strip() for h in (recordings or []) if h.strip())),
        dataset_id=dataset_id,
        annotator_ids=tuple(dict.fromkeys(annotator_ids or [])),
        since=_parse_boundary(since, "since", end_of_day=False),
        until=_parse_boundary(until, "until", end_of_day=True),
        version_id=(version_id or "").strip() or None,
    )


def _parse_boundary(raw: str | None, name: str, *, end_of_day: bool) -> datetime | None:
    """Parse a date or datetime query parameter into an aware datetime.

    The date-only shape is matched explicitly rather than inferred from a failed datetime parse:
    ``parse_datetime`` accepts ``2026-08-11`` and returns midnight, which would silently truncate
    the last day of an ``until=`` range instead of widening it.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if _DATE_ONLY_RE.fullmatch(text):
        as_date = parse_date(text)
        if as_date is None:
            raise HttpError(422, f"Could not parse '{name}' as a date: {text!r}")
        parsed = datetime.combine(as_date, time.max if end_of_day else time.min)
    else:
        parsed = parse_datetime(text)
        if parsed is None:
            raise HttpError(422, f"Could not parse '{name}' as a date or datetime: {text!r}")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def build_export(*, caller, filters: ExportFilters) -> ExportResult:
    """Collect and serialise every row the caller may export under *filters*."""
    is_staff = bool(getattr(caller, "is_staff", False) or getattr(caller, "is_superuser", False))
    restricted = not is_staff
    if restricted and any(annotator_id != caller.pk for annotator_id in filters.annotator_ids):
        raise HttpError(403, "Exporting another user's annotations requires staff access.")

    target_scope = _resolve_target_scope(filters)
    result = ExportResult(filters=filters, restricted_to_self=restricted)

    collected: dict[str, list] = {}
    for type_name in filters.types:
        queryset = _build_queryset(
            _MODELS[type_name],
            caller=caller,
            filters=filters,
            restricted=restricted,
            target_scope=target_scope,
        )
        collected[type_name] = list(queryset)

    every_row = [row for rows in collected.values() for row in rows]
    targets, target_objects = _resolve_targets(every_row, caller=caller, is_staff=is_staff)
    extras = _resolve_extension_values(caller=caller, targets=targets, objects=target_objects)

    for type_name, rows in collected.items():
        visible = [row for row in rows if (row.target_content_type_id, row.target_object_id) in targets]
        result.rows[type_name] = [_serialise_row(row, type_name, targets=targets, extras=extras) for row in visible]

    result.annotators, result.annotator_ids = _build_roster(result.rows)
    return result


def _resolve_target_scope(filters: ExportFilters) -> set[tuple[int, str]] | None:
    """Return the (content_type_id, object_id) pairs the recording / dataset filters select.

    ``None`` means "no target filter applied" — distinct from an empty set, which means the filters
    matched nothing and the export is legitimately empty.
    """
    if not filters.recordings and filters.dataset_id is None:
        return None

    scope: set[tuple[int, str]] = set()
    if filters.recordings:
        from recordings.models import Recording

        recording_ct = ContentType.objects.get_for_model(Recording)
        matched = Recording.objects.filter(content_hash__in=filters.recordings).values_list("pk", flat=True)
        scope |= {(recording_ct.pk, str(pk)) for pk in matched}

    if filters.dataset_id is not None:
        from library.models import DatasetItem

        items = DatasetItem.objects.filter(dataset_id=filters.dataset_id).values_list("content_type_id", "object_id")
        dataset_scope = {(ct_id, str(object_id)) for ct_id, object_id in items}
        # Both filters given: intersect, so `recording=X&dataset=Y` means "X, and only if it is in
        # Y" rather than the union a naive `|=` would produce.
        scope = (scope & dataset_scope) if filters.recordings else dataset_scope

    return scope


def _build_queryset(model, *, caller, filters: ExportFilters, restricted: bool, target_scope):
    """Return the ordered, filtered queryset of one annotation type for this export."""
    queryset = model.objects.prefetch_related("codes")

    if restricted:
        queryset = queryset.filter(author=caller)
    elif filters.annotator_ids:
        queryset = queryset.filter(author_id__in=filters.annotator_ids)

    if filters.since is not None:
        queryset = queryset.filter(created_at__gte=filters.since)
    if filters.until is not None:
        queryset = queryset.filter(created_at__lte=filters.until)
    if filters.version_id is not None:
        queryset = queryset.filter(version_id=filters.version_id)

    if target_scope is not None:
        if not target_scope:
            return queryset.none()
        # A single IN over the pairs is not portable across backends, so group object ids by
        # content type and OR the per-type clauses together.
        by_type: dict[int, set[str]] = {}
        for ct_id, object_id in target_scope:
            by_type.setdefault(ct_id, set()).add(object_id)
        clause = None
        for ct_id, object_ids in by_type.items():
            part = Q(target_content_type_id=ct_id, target_object_id__in=sorted(object_ids))
            clause = part if clause is None else (clause | part)
        queryset = queryset.filter(clause)

    return queryset.order_by("created_at", "pk")


def _resolve_targets(rows, *, caller, is_staff):
    """Resolve every distinct target in *rows*, omitting the ones this caller must not see.

    Returns ``(targets, objects)``: the ``{key: TargetInfo}`` naming map and the matching
    ``{key: instance}`` map handed to export extensions. A missing key means "drop these rows".
    Targets disappear for three reasons: the object is gone (a hard-deleted or never-existing
    generic FK target), it is a recording the FAILED-hidden or soft-delete rule keeps from this
    caller, or — for a non-staff caller — read access to it has since been revoked. Access is
    checked once per distinct target, not once per row.
    """
    from recordings.api.v1.ninja import _failed_hidden_for_caller, _resolve_display_name
    from recordings.models import Recording

    recording_ct_id = ContentType.objects.get_for_model(Recording).pk

    wanted: dict[int, set[str]] = {}
    for row in rows:
        wanted.setdefault(row.target_content_type_id, set()).add(row.target_object_id)

    resolved: dict[tuple[int, str], TargetInfo] = {}
    fetched: dict[tuple[int, str], object] = {}
    for ct_id, object_ids in wanted.items():
        content_type = ContentType.objects.filter(pk=ct_id).first()
        model_class = content_type.model_class() if content_type else None
        if model_class is None:
            continue
        type_name = f"{content_type.app_label}.{content_type.model}"
        for obj in _fetch_by_ids(model_class, object_ids):
            key = (ct_id, str(obj.pk))
            if ct_id == recording_ct_id:
                if obj.deleted_at is not None:
                    continue
                if _failed_hidden_for_caller(obj, caller, None):
                    continue
                resolved[key] = TargetInfo(type_name, obj.content_hash or "", _resolve_display_name(obj))
                fetched[key] = obj
                continue
            # Non-recording targets (a project plugin's RecordingEpoch, say) usually carry their own
            # opaque public identifier. Prefer it over the primary key, which leaks creation order
            # and volume the same way a sequential recording id would.
            resolved[key] = TargetInfo(type_name, _opaque_ref(obj), "")
            fetched[key] = obj

    if not is_staff:
        resolved = _drop_unreadable(resolved, rows=rows, caller=caller)
    # Extension resolvers must only ever see access-filtered targets, so the instances handed back
    # track the resolved key set exactly.
    return resolved, {key: obj for key, obj in fetched.items() if key in resolved}


def _drop_unreadable(resolved, *, rows, caller):
    """Drop targets the caller can no longer read.

    Only reached for non-staff callers, who by then hold nothing but their own annotations. The
    annotation content is theirs either way; what this keeps back is the target's label and
    identity after a grant was revoked.
    """
    from epicurrents.permissions import can_read_object

    keep = {}
    objects = {(row.target_content_type_id, row.target_object_id): row.target_object for row in rows}
    for key, info in resolved.items():
        target = objects.get(key)
        if target is not None and can_read_object(user=caller, obj=target):
            keep[key] = info
    return keep


def _opaque_ref(obj) -> str:
    """Return the most opaque public identifier *obj* offers, falling back to its primary key.

    Checked in the order the platform prefers them: ``object_hash`` is the annotation-side public
    identifier, ``content_hash`` the recording-side one. The primary-key fallback is a last resort
    for targets that publish neither.
    """
    for attribute in ("object_hash", "content_hash"):
        value = getattr(obj, attribute, "") or ""
        if value:
            return str(value)
    return str(obj.pk)


def _fetch_by_ids(model_class, object_ids):
    """Fetch model instances for a set of string primary keys, tolerating unusable values.

    ``target_object_id`` is a CharField, so it can legitimately hold a key that does not fit this
    model's pk column (a stale row left by a hard-deleted target whose pk space differs). Django
    raises on those rather than returning nothing, so the fetch falls back to one-by-one when the
    bulk form is rejected.
    """
    ids = sorted(object_ids)
    try:
        return list(model_class.objects.filter(pk__in=ids))
    except (ValueError, TypeError):
        found = []
        for object_id in ids:
            try:
                obj = model_class.objects.filter(pk=object_id).first()
            except (ValueError, TypeError):
                continue
            if obj is not None:
                found.append(obj)
        return found


def _resolve_extension_values(*, caller, targets, objects) -> dict[tuple[int, str], dict]:
    """Return ``{target key: {column: value}}`` from the registered export extensions.

    Each resolver is called once with the distinct target instances of its model, not once per
    row, and its values fan out to every row on that target. A resolver that omits a target or a
    column yields ``None`` for it, so a partial resolver cannot make rows ragged.
    """
    extras: dict[tuple[int, str], dict] = {}
    by_label: dict[str, list[tuple[tuple[int, str], object]]] = {}
    for key, info in targets.items():
        if info.type_name in _EXPORT_EXTENSIONS and key in objects:
            by_label.setdefault(info.type_name, []).append((key, objects[key]))
    for label, entries in by_label.items():
        for columns, resolver in _EXPORT_EXTENSIONS[label]:
            values_by_pk = resolver(caller=caller, objects=[obj for _, obj in entries])
            for key, obj in entries:
                values = values_by_pk.get(str(obj.pk), {})
                row_extras = extras.setdefault(key, {})
                for column in columns:
                    row_extras[column] = values.get(column)
    return extras


def _serialise_row(row, type_name: str, *, targets, extras) -> dict:
    """Flatten one annotation row into the export's column set.

    ``created_at`` / ``modified_at`` are not emitted; see the note on :data:`_COLUMNS`.
    Extension columns for the row's target, if any, are appended after the base fields.
    """
    target = targets[(row.target_content_type_id, row.target_object_id)]
    common = {
        "object_hash": row.object_hash,
        "content_hash": row.content_hash,
        "author_id": row.author_id,
        "target_type": target.type_name,
        "target_ref": target.ref,
        "target_label": target.label,
        "version_id": row.version_id,
        "name": row.name,
        "value": row.value,
        "codes": [{"standard": code.standard, "value": code.value, "meta": code.meta} for code in row.codes.all()],
    }
    if type_name == "events":
        common.update(
            {
                "event_class": row.event_class,
                "timestamp": row.timestamp,
                "duration": row.duration,
            }
        )
    common.update(extras.get((row.target_content_type_id, row.target_object_id), {}))
    return common


def _build_roster(rows_by_type: dict[str, list[dict]]) -> tuple[list[dict], list[int]]:
    """Return the per-annotator roster and the matching author ids, both sorted by id.

    The roster heads every export with a per-type row count for each ``author_id`` present. It
    deliberately carries nothing but the id — identity resolves through :func:`list_annotators`
    inside the platform. Authors whose rows were all filtered out do not appear.
    """
    counts: dict[int, dict] = {}
    for type_name, rows in rows_by_type.items():
        for row in rows:
            author_id = row["author_id"]
            entry = counts.setdefault(author_id, {"id": author_id, **{t: 0 for t in EXPORTABLE_TYPES}})
            entry[type_name] += 1
    ordered = sorted(counts)
    return [counts[author_id] for author_id in ordered], ordered


def list_annotators() -> list[dict]:
    """Return every user who has authored an Event or Label, with per-type row counts.

    Backs the staff-only roster endpoint: the counterpart the export file's ``author_id`` values
    are resolved against without the mapping ever entering the file. Sorted by username. An
    erased account leaves the roster together with its annotations (the author FK cascades), so
    ids inside previously exported files stop resolving.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Count

    counts: dict[int, dict[str, int]] = {}
    for type_name, model in _MODELS.items():
        rows = model.objects.values("author_id").annotate(count=Count("pk"))
        for entry in rows:
            counts.setdefault(entry["author_id"], {t: 0 for t in EXPORTABLE_TYPES})[type_name] = entry["count"]

    users = get_user_model().objects.filter(pk__in=counts).only("pk", "username", "first_name", "last_name")
    roster = []
    for user in users:
        full_name = (user.get_full_name() or "").strip()
        roster.append(
            {
                "id": user.pk,
                "username": user.get_username(),
                "name": full_name or user.get_username(),
                **counts[user.pk],
            }
        )
    return sorted(roster, key=lambda entry: entry["username"])


def build_metadata(result: ExportResult, *, exported_by, exported_at: datetime) -> dict:
    """Return the metadata header describing one export.

    ``exported_by`` is an id for the same reason the roster is: the file outlives the platform's
    erasure reach, so no name or username enters it. The audit trail records the same actor with
    full attribution for as long as the account exists.
    """
    return {
        "format_version": FORMAT_VERSION,
        "exported_at": exported_at.isoformat(),
        "exported_by": {"id": exported_by.pk},
        "restricted_to_own_annotations": result.restricted_to_self,
        "filters": result.filters.as_metadata(),
        "counts": {type_name: len(rows) for type_name, rows in result.rows.items()},
        "annotators": result.annotators,
        # Which extension columns the active deployment adds to the base column set, so a parser
        # can tell a project column from a base one without hardcoding the deployment.
        "extension_columns": list(_extension_columns()),
    }


def render_json(result: ExportResult, metadata: dict) -> str:
    """Render the export as a single JSON document with the metadata header first."""
    payload = {"metadata": metadata, **{type_name: result.rows[type_name] for type_name in result.filters.types}}
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def render_csv(result: ExportResult, metadata: dict) -> str:
    """Render the export as one CSV, with the metadata header as leading ``#`` comment lines.

    The comment block is not part of the CSV grammar, so a reader has to be told to skip it
    (``pandas.read_csv(path, comment='#')``). It is still worth carrying: an export whose annotator
    roster lives in a separate file arrives detached from it soon enough.

    Registered extension columns are always appended to the header — empty for rows whose target
    the extension does not cover — so the CSV shape depends on the deployment, not on which
    targets happened to match the filters.
    """
    type_name = result.filters.types[0]
    columns = _COLUMNS[type_name] + _extension_columns()
    rows = result.rows[type_name]

    buffer = io.StringIO()
    for line in _metadata_comment_lines(metadata, type_name):
        buffer.write(f"# {line}\n" if line else "#\n")

    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_cell(row.get(column)) for column in columns})
    return buffer.getvalue()


def _metadata_comment_lines(metadata: dict, type_name: str) -> list[str]:
    """Return the human-readable metadata header for a CSV export, one line per entry."""
    filters = metadata["filters"]
    lines = [
        f"epicurrents annotation export (format_version {metadata['format_version']})",
        f"type: {type_name}",
        f"exported_at: {metadata['exported_at']}",
        f"exported_by: user id {metadata['exported_by']['id']}",
        f"rows: {metadata['counts'][type_name]}",
        "",
    ]
    applied = [f"{key}={value}" for key, value in filters.items() if value not in (None, [], "")]
    lines.append(f"filters: {', '.join(applied) if applied else 'none'}")
    if metadata["restricted_to_own_annotations"]:
        lines.append("scope: own annotations only (caller is not staff)")
    lines.append("")
    if metadata["annotators"]:
        lines.append(f"annotators: {len(metadata['annotators'])} (ids resolve via the platform's annotator roster)")
        for entry in metadata["annotators"]:
            lines.append(f"  id {entry['id']} - {entry[type_name]} {type_name}")
    else:
        lines.append("annotators: none (no rows matched)")
    return lines


def _csv_cell(value):
    """Render one Python value as a CSV cell.

    JSON-valued fields (``value``, ``codes``) are serialised compactly rather than flattened into
    more columns: their shape is per-annotation and no fixed column set covers it.
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return value


def export_filename(result: ExportResult, exported_at: datetime) -> str:
    """Return the Content-Disposition filename for an export."""
    stamp = exported_at.strftime("%Y%m%dT%H%M%SZ")
    if result.filters.export_format == "csv":
        return f"annotations-{result.filters.types[0]}-{stamp}.csv"
    scope = "-".join(result.filters.types)
    return f"annotations-{scope}-{stamp}.json"
