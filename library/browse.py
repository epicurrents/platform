"""The dataset browser's rows: one per recording member, with the ingest metadata a reader chooses recordings by.

The membership comes from the caller, already gated (``_visible_members`` in the library API), so the
rules for which members a reader sees live in one place. This module adds the columns and the order.

The order is an allowlist. None of its keys is an arrival order: the default, ``order``, is the dataset's own
(the recordings of the item listing as that listing orders them, folder by folder, the ones a manager arranged
first and the rest by name), the date key sorts by the month a
reader is shown, a released member's release month or the first of its upload month in UTC, and every key
breaks ties by name and then by the random stored name, never by a sequential id. Each subject field the
caller is shown adds a key, ``subject.<key>``, which sorts by the field's declared choice order, so age
bands sort as declared rather than as strings, and a number field by its value.

Filters narrow the rows by modality, by the subject fields the caller is shown and by tag, and facets count the
members under each value (:func:`facet_counts`). A number field filters by a range instead, ``subject.<key>.min``
and ``subject.<key>.max``, both inclusive and either optional, and its facet is the lowest and highest value
under the other filters. A filter on a field the caller is not shown is refused, since its
counts would describe the hidden values. Every count is a sum over rows the caller can page through.

A member the caller cannot read is left out of the rows and the counts (:func:`readable_ids`), which is decided once
for the dataset where the dataset's own grant conveys the read, so the whole membership can be filtered and sorted
without a check per member.
"""

import dataclasses
from datetime import UTC
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db.models import (
    Case,
    CharField,
    Count,
    DateTimeField,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Subquery,
    Value,
    When,
)
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, Lower, NullIf, TruncMonth

from recordings.models import Recording, RecordingMeta, SignalInfo

SORT_KEYS = ("order", "name", "modality", "duration", "channels", "sampling_rate", "date")
#: The dataset's own order, so the browser and a viewer opened from it start as the item listing does.
DEFAULT_SORT = "order"
SUBJECT_SORT_PREFIX = "subject."


def parse_sort(value: str | None, subject_keys=()) -> tuple[str, bool]:
    """Split a ``sort`` parameter into its key and whether it is descending; ``ValueError`` for a key not allowed.

    *subject_keys* are the subject fields the caller is shown, each allowed as ``subject.<key>``.
    """
    value = (value or DEFAULT_SORT).strip()
    descending = value.startswith("-")
    key = value[1:] if descending else value
    allowed = (*SORT_KEYS, *(f"{SUBJECT_SORT_PREFIX}{subject_key}" for subject_key in subject_keys))
    if key not in allowed:
        raise ValueError(key)
    return key, descending


def _subject_value(dataset, recording_ct, key: str):
    """A recording's value for the subject field *key* in *dataset*; null without one."""
    from library.models import DatasetItemSubject

    return Subquery(
        DatasetItemSubject.objects.filter(
            item__dataset=dataset,
            item__content_type=recording_ct,
            item__object_id=Cast(OuterRef("pk"), CharField()),
        ).values(value=KT(f"values__{key}"))[:1],
        output_field=CharField(),
    )


def _choice_index(field: dict):
    """The index of the annotated ``subject_value`` among *field*'s choices; null without a value."""
    return Case(
        *(When(subject_value=choice, then=Value(index)) for index, choice in enumerate(field["choices"])),
        default=None,
        output_field=IntegerField(),
    )


def browse_rows(member_ids, *, sort: str, descending: bool, dataset=None, schema=()):
    """The recordings among *member_ids*, annotated with the browser's columns and ordered by *sort*.

    Trashed and FAILED recordings are left out for every caller, as in the dataset's item listing.
    Columns a recording does not have yet, before its ingest has written the metadata, are null and
    sort last in either direction. A ``subject.<key>`` sort needs the *dataset* and its shown *schema*, and
    ``order`` the *dataset*, whose manual order it is (``library.release.dataset_member_ordering``); without a
    dataset it is the name order.
    """
    from library.release import member_order_annotations, release_month_subquery

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    meta = RecordingMeta.objects.filter(content_type=recording_ct, object_id=Cast(OuterRef("pk"), CharField()))
    channels = SignalInfo.objects.filter(
        meta__content_type=recording_ct,
        meta__object_id=Cast(OuterRef("pk"), CharField()),
        is_annotation_channel=False,
    )
    qs = (
        Recording.objects.filter(pk__in=list(member_ids), deleted_at__isnull=True)
        .exclude(status=Recording.Status.FAILED)
        .annotate(
            duration=Subquery(meta.values("duration")[:1], output_field=FloatField()),
            channel_layout=Subquery(meta.values("channel_layout")[:1], output_field=CharField()),
            deidentification_version=Subquery(meta.values("deidentification_version")[:1], output_field=IntegerField()),
            channel_count=Subquery(
                channels.order_by().values("meta").annotate(n=Count("pk")).values("n")[:1], output_field=IntegerField()
            ),
            sampling_rate_max=Subquery(
                channels.order_by("-sampling_rate").values("sampling_rate")[:1], output_field=FloatField()
            ),
            sampling_rate_min=Subquery(
                channels.order_by("sampling_rate").values("sampling_rate")[:1], output_field=FloatField()
            ),
            sort_name=Lower(Coalesce(NullIf(F("display_name"), Value("")), F("stored_name"))),
            sort_month=Coalesce(
                release_month_subquery(Recording), TruncMonth("created_at", tzinfo=UTC), output_field=DateTimeField()
            ),
        )
    )
    if sort == "order":
        if dataset is None:
            sort = "name"
        else:
            qs = qs.annotate(**member_order_annotations(Recording, dataset))
            rank, position = F("member_folder_rank"), F("member_position")
            if descending:
                # The listing read from its end: the unsorted members, last in it, come first.
                return qs.order_by(rank.desc(), position.desc(nulls_first=True), "-sort_name", "-stored_name")
            return qs.order_by(rank.asc(), position.asc(nulls_last=True), "sort_name", "stored_name")
    if sort.startswith(SUBJECT_SORT_PREFIX):
        from library.subjects import is_number_field

        field = next(f for f in schema if f["key"] == sort[len(SUBJECT_SORT_PREFIX) :])
        qs = qs.annotate(subject_value=_subject_value(dataset, recording_ct, field["key"]))
        if is_number_field(field):
            qs = qs.annotate(subject_order=Cast(F("subject_value"), FloatField()))
        else:
            qs = qs.annotate(subject_order=_choice_index(field))
        column = F("subject_order")
    else:
        column = {
            "name": F("sort_name"),
            "modality": Lower(NullIf(F("modality"), Value(""))),
            "duration": F("duration"),
            "channels": F("channel_count"),
            "sampling_rate": F("sampling_rate_max"),
            "date": F("sort_month"),
        }[sort]
    primary = column.desc(nulls_last=True) if descending else column.asc(nulls_last=True)
    return qs.order_by(primary, "sort_name", "stored_name")


def recording_count(member_ids) -> int:
    """How many rows :func:`browse_rows` returns for *member_ids*, without the columns."""
    return (
        Recording.objects.filter(pk__in=list(member_ids), deleted_at__isnull=True)
        .exclude(status=Recording.Status.FAILED)
        .count()
    )


def member_recording_ids(members) -> list[int]:
    """The recording primary keys among a ``DatasetItem`` queryset *members*."""
    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    return [
        int(object_id)
        for object_id in members.filter(content_type=recording_ct).values_list("object_id", flat=True)
        if str(object_id).isdigit()
    ]


# ---------------------------------------------------------------------------
# Filters and facets
# ---------------------------------------------------------------------------

#: The most tags a tag facet lists, by count; the selected tags are listed besides.
FACET_TAGS = 30
RANGE_BOUNDS = ("min", "max")


@dataclasses.dataclass
class Filters:
    """The values a browser page is narrowed to: any of the modalities, any of the choices of each subject field, a
    range of each number field, and every one of the tags. A range is ``(low, high)``, inclusive, ``None`` at an
    open end."""

    modality: frozenset[str] = frozenset()
    subject: dict[str, frozenset[str]] = dataclasses.field(default_factory=dict)
    ranges: dict[str, tuple[float | None, float | None]] = dataclasses.field(default_factory=dict)
    tags: frozenset[int] = frozenset()

    @property
    def active(self) -> bool:
        """Whether any filter is set."""
        return bool(self.modality or self.subject or self.ranges or self.tags)

    @property
    def fields(self) -> list[str]:
        """The filtered dimensions, by name: ``modality``, ``subject.<key>`` and ``tag``."""
        names = ["modality"] if self.modality else []
        names += [f"{SUBJECT_SORT_PREFIX}{key}" for key in sorted({*self.subject, *self.ranges})]
        return [*names, "tag"] if self.tags else names


def parse_filters(params, schema, *, tags_allowed: bool) -> Filters:
    """Read the filters from the query *params*; ``ValueError`` naming the parameter for one not allowed.

    ``modality`` and ``tag`` may repeat; ``subject.<key>`` repeats per choice, for each field of choices of *schema*,
    the schema the caller is shown, and only with one of the field's choices. A number field of *schema* takes
    ``subject.<key>.min`` and ``subject.<key>.max`` instead, once each, a finite number; an empty one is open.
    *tags_allowed* is false for a caller without an account, who is served no tags.
    """
    from library.subjects import is_number, is_number_field

    choices = {item["key"]: set(item["choices"]) for item in schema if not is_number_field(item)}
    numbers = {item["key"] for item in schema if is_number_field(item)}
    subject: dict[str, frozenset[str]] = {}
    bounds: dict[str, dict[str, float]] = {}
    for name in params:
        if not name.startswith(SUBJECT_SORT_PREFIX):
            continue
        key = name[len(SUBJECT_SORT_PREFIX) :]
        key, _, bound = key.rpartition(".") if "." in key else (key, "", "")
        if bound:
            raw = [value.strip() for value in params.getlist(name) if value.strip()]
            if key not in numbers or bound not in RANGE_BOUNDS or len(raw) > 1:
                raise ValueError(name)
            if raw:
                try:
                    number = float(raw[0])
                except ValueError as exc:
                    raise ValueError(name) from exc
                if not is_number(number):
                    raise ValueError(name)
                bounds.setdefault(key, {})[bound] = number
            continue
        values = {value for value in params.getlist(name) if value}
        if key not in choices or not values <= choices[key]:
            raise ValueError(name)
        subject[key] = frozenset(values)
    ranges = {key: (found.get("min"), found.get("max")) for key, found in bounds.items()}
    raw_tags = [value for value in params.getlist("tag") if value]
    if raw_tags and not tags_allowed:
        raise ValueError("tag")
    try:
        tags = frozenset(int(value) for value in raw_tags)
    except ValueError as exc:
        raise ValueError("tag") from exc
    modality = frozenset(value.strip().lower() for value in params.getlist("modality") if value.strip())
    return Filters(modality=modality, subject=subject, ranges=ranges, tags=tags)


@dataclasses.dataclass
class MemberFacts:
    """What the filters and facets read of one recording member."""

    modality: str
    subject: dict[str, Any]
    tags: frozenset[int]


def readable_ids(rows, *, dataset, user, share_token: str | None = None) -> set[int]:
    """The recordings among *rows*, ``(pk, author_id)`` pairs, that the caller can read.

    *rows* are live, not FAILED, and among the members the release gate lets the caller see, so no read-visibility
    gate is left to hide one. A superuser reads them all, and so does a caller the dataset's own grant conveys read
    to (:func:`library.permissions.dataset_conveys_read`): one query for the dataset. Anyone else, an author browsing
    a dataset they hold no grant on, reads the recordings they authored and those the per-object check grants.
    """
    from epicurrents.permissions import can_read_object
    from library.permissions import dataset_conveys_read

    pks = {pk for pk, _ in rows}
    if user is not None and getattr(user, "is_superuser", False):
        return pks
    if dataset_conveys_read(user, dataset, share_token):
        return pks
    user_pk = getattr(user, "pk", None)
    readable = {pk for pk, author_id in rows if user_pk is not None and author_id == user_pk}
    for recording in Recording.objects.filter(pk__in=pks - readable):
        if can_read_object(user=user, obj=recording, share_token=share_token):
            readable.add(recording.pk)
    return readable


def member_facts(
    member_ids, *, dataset, schema, user, share_token: str | None = None
) -> tuple[dict[int, MemberFacts], dict[int, Any]]:
    """The modality, shown subject values and tag ids of each recording among *member_ids* the browser lists, and the
    ``Tag`` rows those ids name.

    Only the members the caller can read (:func:`readable_ids`) have facts, so a member listed in a dataset whose
    author can no longer read it has no row and is in no count. A caller without an account, *user* ``None``, is
    served no tags.
    """
    from library.subjects import values_by_object_id
    from library.tag_scope import tags_on_objects

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    rows = list(
        Recording.objects.filter(pk__in=list(member_ids), deleted_at__isnull=True)
        .exclude(status=Recording.Status.FAILED)
        .values_list("pk", "author_id", "modality")
    )
    readable = readable_ids(
        [(pk, author_id) for pk, author_id, _ in rows], dataset=dataset, user=user, share_token=share_token
    )
    rows = [(pk, modality) for pk, _, modality in rows if pk in readable]
    pks = [pk for pk, _ in rows]
    values = values_by_object_id(dataset, recording_ct, pks, schema)
    tags = tags_on_objects(user, recording_ct, pks, readable={str(pk) for pk in pks}) if user is not None else {}
    tag_rows = {tag.pk: tag for object_tags in tags.values() for tag in object_tags}
    facts = {
        pk: MemberFacts(
            modality=(modality or "").lower(),
            subject=values.get(str(pk), {}),
            tags=frozenset(tag.pk for tag in tags.get(str(pk), ())),
        )
        for pk, modality in rows
    }
    return facts, tag_rows


def _matches(facts: MemberFacts, filters: Filters, *, skip: str | None = None) -> bool:
    """Whether a member passes *filters*, leaving out the dimension named *skip*."""
    if skip != "modality" and filters.modality and facts.modality not in filters.modality:
        return False
    for key, values in filters.subject.items():
        if skip != f"{SUBJECT_SORT_PREFIX}{key}" and facts.subject.get(key) not in values:
            return False
    for key, (low, high) in filters.ranges.items():
        if skip != f"{SUBJECT_SORT_PREFIX}{key}" and not _in_range(facts.subject.get(key), low, high):
            return False
    return skip == "tag" or filters.tags <= facts.tags


def _in_range(value: Any, low: float | None, high: float | None) -> bool:
    """Whether *value* is a number within ``[low, high]``; a member without a number is outside every range."""
    from library.subjects import is_number

    if not is_number(value):
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def matching_ids(facts: dict[int, MemberFacts], filters: Filters) -> list[int]:
    """The members among *facts* that pass every filter."""
    return [pk for pk, member in facts.items() if _matches(member, filters)]


def facet_counts(facts: dict[int, MemberFacts], filters: Filters, schema, *, tag_rows) -> dict:
    """The facets of the browser: per value, how many members it would match.

    A modality or subject value is counted under every filter but its own dimension's, so the other values of a
    filtered dimension stay countable; a tag is counted under every filter, since tags combine with *and*. Modality
    values are those present; a subject field lists each declared choice; tags are the :data:`FACET_TAGS` most
    frequent among the matching members, then by name, and the selected ones the caller can see on a member. A
    number field's facet is the lowest and highest value, and how many members carry one, under every filter but its
    own range. *tag_rows* maps a tag id to its ``Tag``, as :func:`member_facts` gives them.
    """
    from library.subjects import is_number, is_number_field

    modality = {}
    for member in facts.values():
        if member.modality and _matches(member, filters, skip="modality"):
            modality[member.modality] = modality.get(member.modality, 0) + 1
    for value in filters.modality:
        modality.setdefault(value, 0)
    subject = {}
    ranges = {}
    for item in schema:
        skip = f"{SUBJECT_SORT_PREFIX}{item['key']}"
        if is_number_field(item):
            found = [
                member.subject[item["key"]]
                for member in facts.values()
                if is_number(member.subject.get(item["key"])) and _matches(member, filters, skip=skip)
            ]
            ranges[item["key"]] = {
                "min": min(found) if found else None,
                "max": max(found) if found else None,
                "count": len(found),
            }
            continue
        counts = dict.fromkeys(item["choices"], 0)
        for member in facts.values():
            value = member.subject.get(item["key"])
            if value in counts and _matches(member, filters, skip=skip):
                counts[value] += 1
        subject[item["key"]] = [{"value": value, "count": count} for value, count in counts.items()]
    tag_counts: dict[int, int] = {}
    for member in facts.values():
        if _matches(member, filters):
            for tag_id in member.tags:
                tag_counts[tag_id] = tag_counts.get(tag_id, 0) + 1
    listed = sorted(
        (tag_id for tag_id in tag_counts if tag_id in tag_rows),
        key=lambda tag_id: (-tag_counts[tag_id], tag_rows[tag_id].name.casefold(), tag_id),
    )[:FACET_TAGS]
    listed += sorted(tag_id for tag_id in filters.tags if tag_id in tag_rows and tag_id not in listed)
    return {
        "modality": [{"value": value, "count": count} for value, count in sorted(modality.items())],
        "subject": subject,
        "ranges": ranges,
        "tags": [
            {
                "id": tag_id,
                "name": tag_rows[tag_id].name,
                "curated": tag_rows[tag_id].curated,
                "count": tag_counts.get(tag_id, 0),
            }
            for tag_id in listed
        ],
    }
