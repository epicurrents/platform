"""The dataset browser's rows: one per recording member, with the ingest metadata a reader chooses recordings by.

The membership comes from the caller, already gated (``_visible_members`` in the library API), so the
rules for which members a reader sees live in one place. This module adds the columns and the order.

The order is an allowlist. None of its keys is an arrival order: the date key sorts by the month a
reader is shown, a released member's release month or the first of its upload month in UTC, and every key
breaks ties by name and then by the random stored name, never by a sequential id. Each subject field the
caller is shown adds a key, ``subject.<key>``, which sorts by the field's declared choice order, so age
bands sort as declared rather than as strings.

Filters narrow the rows by modality, by the subject fields the caller is shown and by tag, and facets count the
members under each value (:func:`facet_counts`). A filter on a field the caller is not shown is refused, since its
counts would describe the hidden values. On a release-gated dataset, to a reader who does not manage it, a count
between one and k - 1 is suppressed (:func:`suppression_k`): the facet value is served without its count, and a
filtered page that matches so few members is served without its rows. Displayed fields alone cannot isolate a member
once every class holds k, but combined with a tag they can. Suppression is a guard on what one answer says, not part
of the anonymity argument, which rests on the class sizes the release was decided on: two answers can still be
differenced.
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

SORT_KEYS = ("name", "modality", "duration", "channels", "sampling_rate", "date")
DEFAULT_SORT = "name"
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
    sort last in either direction. A ``subject.<key>`` sort needs the *dataset* and its shown *schema*.
    """
    from library.release import release_month_subquery

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
    if sort.startswith(SUBJECT_SORT_PREFIX):
        field = next(f for f in schema if f["key"] == sort[len(SUBJECT_SORT_PREFIX) :])
        qs = qs.annotate(subject_value=_subject_value(dataset, recording_ct, field["key"]))
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

#: The suppression threshold on a release-gated dataset whose releases recorded no k and whose pool sets none.
FALLBACK_SUPPRESSION_K = 5

#: The most tags a tag facet lists, by count; the selected tags are listed besides.
FACET_TAGS = 30


@dataclasses.dataclass
class Filters:
    """The values a browser page is narrowed to: any of the modalities, any of the choices of each subject field, and
    every one of the tags."""

    modality: frozenset[str] = frozenset()
    subject: dict[str, frozenset[str]] = dataclasses.field(default_factory=dict)
    tags: frozenset[int] = frozenset()

    @property
    def active(self) -> bool:
        """Whether any filter is set."""
        return bool(self.modality or self.subject or self.tags)

    @property
    def fields(self) -> list[str]:
        """The filtered dimensions, by name: ``modality``, ``subject.<key>`` and ``tag``."""
        names = ["modality"] if self.modality else []
        names += [f"{SUBJECT_SORT_PREFIX}{key}" for key in sorted(self.subject)]
        return [*names, "tag"] if self.tags else names


def parse_filters(params, schema, *, tags_allowed: bool) -> Filters:
    """Read the filters from the query *params*; ``ValueError`` naming the parameter for one not allowed.

    ``modality`` and ``tag`` may repeat; ``subject.<key>`` repeats per choice, for each field of *schema*, the schema
    the caller is shown, and only with one of the field's choices. *tags_allowed* is false for a caller without an
    account, who is served no tags.
    """
    choices = {item["key"]: set(item["choices"]) for item in schema}
    subject: dict[str, frozenset[str]] = {}
    for name in params:
        if not name.startswith(SUBJECT_SORT_PREFIX):
            continue
        key = name[len(SUBJECT_SORT_PREFIX) :]
        values = {value for value in params.getlist(name) if value}
        if key not in choices or not values <= choices[key]:
            raise ValueError(name)
        subject[key] = frozenset(values)
    raw_tags = [value for value in params.getlist("tag") if value]
    if raw_tags and not tags_allowed:
        raise ValueError("tag")
    try:
        tags = frozenset(int(value) for value in raw_tags)
    except ValueError as exc:
        raise ValueError("tag") from exc
    modality = frozenset(value.strip().lower() for value in params.getlist("modality") if value.strip())
    return Filters(modality=modality, subject=subject, tags=tags)


@dataclasses.dataclass
class MemberFacts:
    """What the filters and facets read of one recording member."""

    modality: str
    subject: dict[str, str]
    tags: frozenset[int]


def member_facts(member_ids, *, dataset, schema, user) -> tuple[dict[int, MemberFacts], dict[int, Any]]:
    """The modality, shown subject values and tag ids of each recording among *member_ids* the browser lists, and the
    ``Tag`` rows those ids name.

    The tags are those :func:`library.tag_scope.tags_on_objects` serves *user*, so a member listed to a dataset author
    who can no longer read it contributes no tag, as its row carries none. A caller without an account, *user*
    ``None``, is served no tags.
    """
    from library.subjects import values_by_object_id
    from library.tag_scope import tags_on_objects

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    rows = list(
        Recording.objects.filter(pk__in=list(member_ids), deleted_at__isnull=True)
        .exclude(status=Recording.Status.FAILED)
        .values_list("pk", "modality")
    )
    pks = [pk for pk, _ in rows]
    values = values_by_object_id(dataset, recording_ct, pks, schema)
    tags = tags_on_objects(user, recording_ct, pks) if user is not None else {}
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
    return skip == "tag" or filters.tags <= facts.tags


def matching_ids(facts: dict[int, MemberFacts], filters: Filters) -> list[int]:
    """The members among *facts* that pass every filter."""
    return [pk for pk, member in facts.items() if _matches(member, filters)]


def suppression_k(dataset, manager: bool) -> int | None:
    """The count below which a browser count is suppressed for this caller, or ``None`` when none is.

    Only on a release-gated dataset, and never to its managers. The threshold is the largest k any of the dataset's
    releases recorded, a class's own k included, since a count is suppressed for whichever class it describes; then
    the k of the pool's profile; then :data:`FALLBACK_SUPPRESSION_K`, so a dataset released without the class-size
    condition still serves no count of one.
    """
    from library.models import DatasetRelease
    from recordings.submissions import get_ingest_profile

    if not dataset.release_gated or manager:
        return None
    recorded = [
        value
        for k, class_k in DatasetRelease.objects.filter(dataset=dataset).values_list("k", "class_k")
        for value in [k, *(class_k or {}).values()]
        if isinstance(value, int) and value > 0
    ]
    if recorded:
        return max(recorded)
    profile = get_ingest_profile(getattr(dataset, "submission_profile", "") or "")
    if profile is not None and profile.k:
        return profile.k
    return FALLBACK_SUPPRESSION_K


def suppressed(count: int, k: int | None) -> int | None:
    """*count*, or ``None`` when it lies between one and k - 1."""
    return None if k is not None and 0 < count < k else count


def _partition(counts: list[tuple[str, int]], k: int | None) -> list[dict]:
    """Facet values of one dimension whose values partition the members, with their counts suppressed below *k*.

    A single suppressed count in a partition is the total less the others, so when exactly one is suppressed the
    smallest count still shown is suppressed with it.
    """
    shown = [suppressed(count, k) for _, count in counts]
    if k is not None and sum(1 for value, (_, count) in zip(shown, counts) if value is None and count) == 1:
        rest = [(count, index) for index, (value, (_, count)) in enumerate(zip(shown, counts)) if value]
        if rest:
            shown[min(rest)[1]] = None
    return [{"value": value, "count": count} for (value, _), count in zip(counts, shown)]


def facet_counts(facts: dict[int, MemberFacts], filters: Filters, schema, *, k: int | None, tag_rows) -> dict:
    """The facets of the browser: per value, how many members it would match, suppressed below *k*.

    A modality or subject value is counted under every filter but its own dimension's, so the other values of a
    filtered dimension stay countable; a tag is counted under every filter, since tags combine with *and*. Modality
    values are those present; a subject field lists each declared choice; tags are the :data:`FACET_TAGS` most
    frequent among the matching members, then by name, and the selected ones the caller can see on a member.
    *tag_rows* maps a tag id to its ``Tag``, as :func:`member_facts` gives them.
    """
    modality = {}
    for member in facts.values():
        if member.modality and _matches(member, filters, skip="modality"):
            modality[member.modality] = modality.get(member.modality, 0) + 1
    for value in filters.modality:
        modality.setdefault(value, 0)
    subject = {}
    for item in schema:
        skip = f"{SUBJECT_SORT_PREFIX}{item['key']}"
        counts = dict.fromkeys(item["choices"], 0)
        for member in facts.values():
            value = member.subject.get(item["key"])
            if value in counts and _matches(member, filters, skip=skip):
                counts[value] += 1
        subject[item["key"]] = _partition(list(counts.items()), k)
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
        "modality": _partition(sorted(modality.items()), k),
        "subject": subject,
        "tags": [
            {
                "id": tag_id,
                "name": tag_rows[tag_id].name,
                "curated": tag_rows[tag_id].curated,
                "count": suppressed(tag_counts.get(tag_id, 0), k),
            }
            for tag_id in listed
        ],
    }
