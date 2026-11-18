"""The dataset browser's rows: one per recording member, with the ingest metadata a reader chooses recordings by.

The membership comes from the caller, already gated (``_visible_members`` in the library API), so the
rules for which members a reader sees live in one place. This module adds the columns and the order.

The order is an allowlist. None of its keys is an arrival order: the date key sorts by the month a
reader is shown, a released member's release month or the first of its upload month in UTC, and every key
breaks ties by name and then by the random stored name, never by a sequential id. Each subject field the
caller is shown adds a key, ``subject.<key>``, which sorts by the field's declared choice order, so age
bands sort as declared rather than as strings.
"""

from datetime import UTC

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
