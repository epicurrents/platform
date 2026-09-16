"""Bulk export of Event and Label annotations as JSON or CSV.

The per-target list endpoints in :mod:`annotations.api.v1.ninja` answer "what is on this object";
this module answers "give me the rows, across targets, attributable per annotator". It exists for
the research / QA workflow where someone needs the rater output off the platform and into pandas or
R, and needs to keep apart whose output is whose.

Access is tiered: a caller who may export across annotators gets every author's rows, anyone else
is restricted to their own. :func:`can_export_all_annotators` is the single derivation of that
tier — a superuser always qualifies, and whether a plain staff account does is the deployment's
choice through ``ANNOTATION_EXPORT_ALL_ANNOTATORS_REQUIRES_SUPERUSER``, which defaults to requiring
superuser. The restriction is applied to the queryset, not checked afterwards, so a caller outside
the tier cannot widen it through any filter combination.

The narrow default is deliberate. This module reads clinical annotation text, which the
annotation-text rule withholds from a grantee reading under a de-identifying grant; exporting
across annotators is the one path that answers past that rule, so it is gated on the tier the
platform reserves for its other content-level bypasses rather than on admin visibility.

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
:func:`list_annotators` backs the roster endpoint the exporter reads it from, on the same tier. See
annotations/README.md for the operator note.

Projects extend this endpoint rather than replacing it, through two registries that differ in what
they contribute. :func:`register_export_extension` complements rows the export already found with
target-derived columns, while the target hiding and de-identification above keep applying to them.
:func:`register_export_row_source` contributes rows of its own, from a model the annotations app
does not know, as an additional exportable type. A project that keeps rater output outside
``Event`` / ``Label`` needs the second: without it the export answers such a deployment with a
well-formed empty file, which reads as "nothing was annotated" rather than as "this table was never
consulted". Neither registry lets a project decide who may export which rows.
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

#: The core annotation types, in the order they appear in a JSON payload. Registered row sources
#: add to this set at runtime — :func:`exportable_types` is what a deployment can export.
EXPORTABLE_TYPES = ("events", "labels")

#: Display names for the core types. Registered row sources carry their own on the registration.
_CORE_TYPE_LABELS = {"events": "Events", "labels": "Labels"}

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
#: file. The roster endpoint (:func:`list_annotators`) maps ids to identities inside the
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

#: Columns both registries withhold unless the registration names them in ``include_withheld``. They
#: are the fields the core types omit (see :data:`_COLUMNS`), and a project table carries them as
#: readily as an annotation does: a sequential primary key leaks creation order and volume, and the
#: absolute timestamps are what the de-identification rule keeps off annotation responses.
_WITHHELD_COLUMNS = frozenset({"id", "created_at", "modified_at"})


def _exported_columns(columns, include_withheld, *, owner: str) -> tuple[str, ...]:
    """Return *columns* without the withheld ones the registration did not opt into.

    An ``include_withheld`` entry that is not a withheld column, or that *columns* does not declare,
    raises ``ValueError``: either makes the opt-in read as doing something it does not.
    """
    include = set(include_withheld)
    not_withheld = sorted(include - _WITHHELD_COLUMNS)
    if not_withheld:
        raise ValueError(f"{owner}: include_withheld names column(s) that are not withheld: {', '.join(not_withheld)}")
    undeclared = sorted(include - set(columns))
    if undeclared:
        raise ValueError(f"{owner}: include_withheld names undeclared column(s): {', '.join(undeclared)}")
    return tuple(column for column in columns if column not in _WITHHELD_COLUMNS or column in include)


@dataclass(frozen=True)
class ExportRowSource:
    """A project-owned model contributing rows to the export as an export type of its own.

    See :func:`register_export_row_source` for what each field declares and which filter the export
    applies through it. ``columns`` holds the exported columns, withheld ones already removed.
    """

    type_name: str
    label: str
    columns: tuple[str, ...]
    get_queryset: object
    serialise: object
    author_field: str = "author"
    created_field: str = "created_at"
    version_field: str | None = None
    include_withheld: tuple[str, ...] = ()


#: Registered row sources, keyed by type name. See :func:`register_export_row_source`.
_ROW_SOURCES: dict[str, ExportRowSource] = {}

#: Shape a registered type name must take, matching the core types' own spelling.
_TYPE_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


def register_export_row_source(
    type_name: str,
    *,
    label: str,
    columns: tuple[str, ...],
    get_queryset,
    serialise,
    author_field: str = "author",
    created_field: str = "created_at",
    version_field: str | None = None,
    include_withheld: tuple[str, ...] = (),
) -> None:
    """Register a project model as an additional exportable annotation type.

    *type_name* joins ``events`` and ``labels`` in ``?types=``, in the JSON payload, and in the
    roster's per-type counts. It is lower-case snake like the core pair, because it travels through
    a comma-separated query parameter and into the download's filename, and it may not be
    ``metadata``, the JSON key the export's header sits under. *label* names the type to a
    person choosing what to export. *columns* is the source's complete column set in CSV order, and
    must contain ``author_id``: the export's frame is per-annotator attribution, and a row it cannot
    attribute has no place in it.

    The registration is declarative and the export keeps the enforcement. ``get_queryset`` returns
    the rows the source offers for export, before any of the export's own filters — a callable, so
    the queryset is built per export rather than once at registration. The export applies the annotator restriction through ``author_field`` (a foreign
    key to the user model), the ``since`` / ``until`` window through ``created_field``, and the
    ordering, so a source cannot widen its own scope; rows with no author are left out. ``serialise``
    maps one instance to a dict, from which the export keeps only the declared *columns*, filling a
    missing one with ``None`` and overwriting ``author_id`` from the row's own foreign key. A
    serialiser can therefore neither misattribute a row nor ship a field the registration does not
    show.

    ``id``, ``created_at`` and ``modified_at`` are withheld even when declared — the fields the core
    types omit, for the reasons on :data:`_COLUMNS`. Name a declared one in *include_withheld* to
    export it anyway. The opt-in sits at the call site, where a reviewer sees it; a deployment that
    exports timestamps records why in the phi-exposure exemptions.

    A source's rows have no annotation target, so the ``recording`` and ``dataset_id`` filters can
    never apply to them, and ``version_id`` applies only through a named ``version_field``. A source
    is **omitted** from an export narrowed by a filter it cannot answer. Omission is the safe
    direction: rows a filter could not be applied to would otherwise arrive looking as though they
    had passed it.

    Call from ``AppConfig.ready()``. A malformed, reserved or already-taken name, a column set without
    ``author_id``, a column an export extension already uses, or an ``include_withheld`` entry that
    is not a declared withheld column raises ``ValueError``. Registered
    types are additive and do not bump :data:`FORMAT_VERSION`; a parser meets them as new keys in
    the metadata header's ``counts``.
    """
    if not _TYPE_NAME_RE.fullmatch(type_name):
        raise ValueError(f"Export type name {type_name!r} must match {_TYPE_NAME_RE.pattern}.")
    if type_name == "metadata":
        # render_json keys each type's rows beside the header, so this name would overwrite it.
        raise ValueError("Export type name 'metadata' is reserved for the export's header.")
    if type_name in _MODELS or type_name in _ROW_SOURCES:
        raise ValueError(f"Export type {type_name!r} is already registered.")
    if "author_id" not in columns:
        raise ValueError(f"Export row source {type_name!r} must emit an 'author_id' column.")
    exported = _exported_columns(columns, include_withheld, owner=f"Export row source {type_name!r}")
    # Checked in both directions — here and in register_export_extension — so the outcome does not
    # depend on which app's ready() ran first.
    collisions = [column for column in exported if column in _extension_columns()]
    if collisions:
        raise ValueError(f"Export row source column(s) already used by an export extension: {', '.join(collisions)}")
    _ROW_SOURCES[type_name] = ExportRowSource(
        type_name=type_name,
        label=label,
        columns=exported,
        get_queryset=get_queryset,
        serialise=serialise,
        author_field=author_field,
        created_field=created_field,
        version_field=version_field,
        include_withheld=tuple(include_withheld),
    )


def exportable_types() -> tuple[str, ...]:
    """Return every type this deployment can export: the core pair, then registered row sources.

    The core pair keeps its canonical order so an existing parser sees no reordering. Registered
    sources follow sorted, so the order is a property of the deployment rather than of app-loading
    sequence.
    """
    return EXPORTABLE_TYPES + tuple(sorted(_ROW_SOURCES))


def exportable_type_choices() -> list[dict]:
    """Return ``{"name", "label"}`` for every exportable type, in :func:`exportable_types` order."""
    return [
        {"name": name, "label": _CORE_TYPE_LABELS.get(name) or _ROW_SOURCES[name].label} for name in exportable_types()
    ]


def _columns_for(type_name: str) -> tuple[str, ...]:
    """Return the base column order for one exportable type, core or registered."""
    source = _ROW_SOURCES.get(type_name)
    return source.columns if source else _COLUMNS[type_name]


def register_export_extension(
    target_model_label: str, *, columns: tuple[str, ...], resolver, include_withheld: tuple[str, ...] = ()
) -> None:
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
    column that collides with a base column, a registered row source's column, or an earlier
    registration raises ``ValueError`` at registration rather than shadowing silently. Additive by
    design — registered columns do not bump :data:`FORMAT_VERSION`.

    ``id``, ``created_at`` and ``modified_at`` are withheld even when declared, as they are for a row
    source; name a declared one in *include_withheld* to export it anyway.
    """
    exported = _exported_columns(columns, include_withheld, owner=f"Export extension on {target_model_label!r}")
    taken = {column for type_columns in _COLUMNS.values() for column in type_columns}
    taken.update(column for source in _ROW_SOURCES.values() for column in source.columns)
    for registered in _EXPORT_EXTENSIONS.values():
        for existing_columns, _ in registered:
            taken.update(existing_columns)
    collisions = [column for column in exported if column in taken]
    if collisions:
        raise ValueError(f"Export extension column(s) already in use: {', '.join(collisions)}")
    _EXPORT_EXTENSIONS.setdefault(target_model_label, []).append((exported, resolver))


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
    available = exportable_types()
    requested = tuple(part.strip() for part in (types or "").split(",") if part.strip()) or available
    unknown = [name for name in requested if name not in available]
    if unknown:
        raise HttpError(
            422,
            f"Unknown annotation type(s): {', '.join(unknown)}. Valid types: {', '.join(available)}.",
        )
    # Preserve the canonical order rather than the order they were typed, so the JSON key order and
    # the generated filename are stable for the same selection.
    ordered = tuple(name for name in available if name in requested)

    fmt = (export_format or "json").strip().lower()
    if fmt not in ("json", "csv"):
        raise HttpError(422, "Unknown format. Valid formats: json, csv.")
    if fmt == "csv" and len(ordered) != 1:
        raise HttpError(
            422,
            "CSV exports carry one annotation type per file, because the types do not share a "
            "column set. Request a single type, or use format=json.",
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


def can_export_all_annotators(caller) -> bool:
    """Return True when *caller* may export across annotators rather than only their own rows.

    A superuser always may. Whether a plain staff account may is the deployment's
    choice, through ``ANNOTATION_EXPORT_ALL_ANNOTATORS_REQUIRES_SUPERUSER``, which
    defaults to requiring the superuser tier.

    The default is the narrow one because this reads clinical annotation text rather
    than administration data, and every other content-level bypass in the platform —
    ``_can_see_original_name``, the read resolver's own fast path — is superuser-only.
    A deployment whose staff tier *is* its research-coordinator tier turns the setting
    off and gets the wider behaviour back.

    This is the single derivation of that tier. The export endpoint's own check calls
    it too: two derivations of one access question drift, and the queryset scoping
    below follows from this answer rather than repeating it.
    """
    from django.conf import settings

    if getattr(caller, "is_superuser", False):
        return True
    if getattr(settings, "ANNOTATION_EXPORT_ALL_ANNOTATORS_REQUIRES_SUPERUSER", True):
        return False
    return bool(getattr(caller, "is_staff", False))


def build_export(*, caller, filters: ExportFilters) -> ExportResult:
    """Collect and serialise every row the caller may export under *filters*."""
    exports_all_annotators = can_export_all_annotators(caller)
    restricted = not exports_all_annotators
    if restricted and any(annotator_id != caller.pk for annotator_id in filters.annotator_ids):
        raise HttpError(403, "Exporting another user's annotations requires the cross-annotator export tier.")

    target_scope = _resolve_target_scope(filters)
    result = ExportResult(filters=filters, restricted_to_self=restricted)

    collected: dict[str, list] = {}
    for type_name in filters.types:
        if type_name in _ROW_SOURCES:
            continue
        queryset = _build_queryset(
            _MODELS[type_name],
            caller=caller,
            filters=filters,
            restricted=restricted,
            target_scope=target_scope,
        )
        collected[type_name] = list(queryset)

    every_row = [row for rows in collected.values() for row in rows]
    targets, target_objects = _resolve_targets(every_row, caller=caller, all_annotators=exports_all_annotators)
    extras = _resolve_extension_values(caller=caller, targets=targets, objects=target_objects)

    for type_name, rows in collected.items():
        visible = [row for row in rows if (row.target_content_type_id, row.target_object_id) in targets]
        result.rows[type_name] = [_serialise_row(row, type_name, targets=targets, extras=extras) for row in visible]

    for type_name in filters.types:
        source = _ROW_SOURCES.get(type_name)
        if source is not None:
            result.rows[type_name] = _source_rows(
                source, caller=caller, filters=filters, restricted=restricted, target_scope=target_scope
            )

    result.annotators, result.annotator_ids = _build_roster(result.rows)
    return result


def _source_rows(
    source: ExportRowSource, *, caller, filters: ExportFilters, restricted: bool, target_scope
) -> list[dict]:
    """Return the serialised rows one registered row source contributes under *filters*.

    Empty when the export is narrowed by a filter the source cannot answer — see
    :func:`register_export_row_source` on why omission is the safe direction. Every filter is
    applied here rather than by the source, and each row is projected onto the declared columns with
    the author id read off the row itself, so what leaves the platform is what the registration
    declares.
    """
    if target_scope is not None:
        return []
    if filters.version_id is not None and source.version_field is None:
        return []

    author_id_field = f"{source.author_field}_id"
    queryset = source.get_queryset().filter(**{f"{author_id_field}__isnull": False})
    if restricted:
        queryset = queryset.filter(**{author_id_field: caller.pk})
    elif filters.annotator_ids:
        queryset = queryset.filter(**{f"{author_id_field}__in": filters.annotator_ids})
    if filters.since is not None:
        queryset = queryset.filter(**{f"{source.created_field}__gte": filters.since})
    if filters.until is not None:
        queryset = queryset.filter(**{f"{source.created_field}__lte": filters.until})
    if filters.version_id is not None:
        queryset = queryset.filter(**{source.version_field: filters.version_id})

    rows = []
    for obj in queryset.order_by(source.created_field, "pk"):
        serialised = source.serialise(obj)
        row = {column: serialised.get(column) for column in source.columns}
        row["author_id"] = getattr(obj, author_id_field)
        rows.append(row)
    return rows


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


def _resolve_targets(rows, *, caller, all_annotators):
    """Resolve every distinct target in *rows*, omitting the ones this caller must not see.

    Returns ``(targets, objects)``: the ``{key: TargetInfo}`` naming map and the matching
    ``{key: instance}`` map handed to export extensions. A missing key means "drop these rows".
    Targets disappear for three reasons: the object is gone (a hard-deleted or never-existing
    generic FK target), it is a recording the FAILED-hidden or soft-delete rule keeps from this
    caller, or — for a caller outside the cross-annotator tier — read access to it has since been
    revoked. Access is checked once per distinct target, not once per row.
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

    if not all_annotators:
        resolved = _drop_unreadable(resolved, rows=rows, caller=caller)
    # Extension resolvers must only ever see access-filtered targets, so the instances handed back
    # track the resolved key set exactly.
    return resolved, {key: obj for key, obj in fetched.items() if key in resolved}


def _drop_unreadable(resolved, *, rows, caller):
    """Drop targets the caller can no longer read.

    Only reached for callers outside the cross-annotator tier, who by then hold nothing but their
    own annotations. The annotation content is theirs either way; what this keeps back is the
    target's label and identity after a grant was revoked.
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
    blank = {t: 0 for t in exportable_types()}
    counts: dict[int, dict] = {}
    for type_name, rows in rows_by_type.items():
        for row in rows:
            author_id = row["author_id"]
            entry = counts.setdefault(author_id, {"id": author_id, **blank})
            entry[type_name] += 1
    ordered = sorted(counts)
    return [counts[author_id] for author_id in ordered], ordered


def list_annotators() -> list[dict]:
    """Return every user who has authored an exportable row, with per-type row counts.

    Backs the roster endpoint, which is gated on the same tier as the export itself: the
    counterpart the export file's ``author_id`` values
    are resolved against without the mapping ever entering the file. Registered row sources are
    counted alongside the core types, so a project's raters appear even when they have authored no
    Event or Label. Sorted by username. An erased account leaves the roster together with its
    annotations (the author FK cascades), so ids inside previously exported files stop resolving.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Count

    blank = {t: 0 for t in exportable_types()}
    counts: dict[int, dict[str, int]] = {}
    for type_name, model in _MODELS.items():
        rows = model.objects.values("author_id").annotate(count=Count("pk"))
        for entry in rows:
            counts.setdefault(entry["author_id"], dict(blank))[type_name] = entry["count"]
    for type_name, source in _ROW_SOURCES.items():
        author_id_field = f"{source.author_field}_id"
        # A source's queryset may arrive ordered, and Django adds explicit ordering fields to the
        # GROUP BY — one annotator would then span several groups, the last overwriting the rest.
        rows = source.get_queryset().order_by().values(author_id_field).annotate(count=Count("pk"))
        for entry in rows:
            counts.setdefault(entry[author_id_field], dict(blank))[type_name] = entry["count"]

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

    Registered extension columns are always appended to a core type's header — empty for rows whose
    target the extension does not cover — so the CSV shape depends on the deployment, not on which
    targets happened to match the filters. A row source's CSV carries its declared columns only:
    extensions key on annotation targets, which a source's rows do not have.
    """
    type_name = result.filters.types[0]
    columns = _columns_for(type_name)
    if type_name not in _ROW_SOURCES:
        columns += _extension_columns()
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
