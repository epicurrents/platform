"""Release gating for datasets: hidden until released, refused to share-token callers, dated by release month.

⚠️ LOAD-BEARING — the release gate.
Three decisions here are what the anonymity argument of a release-gated dataset rests on, and
each fails silently. :func:`member_hidden_from_reader` is what keeps an unreleased member from
surfacing one by one as it arrives, and a member from resolving for a request carrying a share
token; a narrowed condition serves a pooled recording to a forwardable link with nothing in any
log to notice. :func:`release_month_for` and :func:`release_month_subquery` replace the upload time
with the release month, and :func:`member_name_subquery` keeps a listing from being an arrival
order; either regressing reinstates the arrival timing the pool exists to hide.
:func:`stored_digest_withheld_ids` keeps a member's digest, and the content pin that tests it, from
readers who could hold a contributor's receipt; a widened answer lets a receipt find its member. And
:func:`eligibility_cutoff` with :func:`eligible_items` is the cadence: a member uploaded in M
eligible from the run at the start of M+2, one regime for backlog and new submissions alike, so
the release month says nothing about which kind a recording is. Contract
tests are in ``library/tests/test_release.py`` (``TestMemberGate``, ``TestRecordingSurfaces``,
``TestDatasetSurfaces``, ``TestCadence``, ``TestClassSize``, ``TestStoredDigest``, ``TestReleaseCommand``); the assessment that depends on
them is ``docs/anonymisation-compliance.md``.

A release-gated dataset (``Dataset.release_gated``) is a pool whose members must not surface one
by one as they arrive. Four rules follow, all enforced here and registered from
``library.apps.LibraryConfig.ready()``:

- **The gate acts only on what the gating party controls.** A membership counts when the
  dataset's author authored the member, or when the dataset is a submission pool and the member
  is a pooled (system-authored) recording (:func:`controls_membership`). Gating a dataset, and
  adding to a gated one, is refused for anything else (:func:`gate_refusal`,
  :func:`membership_refusal`), and a membership that got in regardless hides nothing.
- **Unreleased members are hidden.** A member with no ``DatasetItem.release`` resolves for the
  dataset's managers only: the dataset author, a holder of a ``can_write`` grant on the dataset,
  the member's own author and superusers (who never reach a gate). Everyone else, a direct
  grantee included, is denied before any grant is read, through the read-visibility gate
  :func:`member_hidden_from_reader` registered for ``recordings.recording`` and
  ``media.mediafile``.
- **No member resolves for a request carrying a share token**, released or not, whoever holds
  the token. A forwardable link fails the onward-transfer test the gate exists for, and the
  accountability argument needs an individual account. The dataset itself is hidden from
  share-token callers by :func:`dataset_hidden_from_reader`, so a join link lists nothing.
- **A member's stored digest is served to managers only.** ``stored_hash`` and the content pin
  on the byte-serving endpoints are withheld from every other reader through
  :func:`stored_digest_withheld_ids`, because the digest could equal the hash on a contributor's
  receipt. A pooled recording is withheld whatever has become of its pool.
- **A released member is dated by its release month.** ``DatasetRelease.release_month`` replaces
  the upload time on every surface that serves a time to a reader who is not a manager, and
  listings of members order by name rather than by any time. A pooled recording keeps its
  latest release month after its pool is trashed or ungated, and one never released is dated
  :data:`WITHHELD_DATE`.

Releases run on a monthly cadence: a member uploaded in month M is eligible from the run at the
start of M+2, so every member waits between one and two months and a month's submissions from
every contributor surface together. The cadence is the platform's; which eligible members a run
publishes is the project's, through :func:`register_release_selector`. Without a selector a run
publishes everything eligible. :func:`select_by_class_size` is the class-size condition a selector
builds on: an eligible member is published once its equivalence class holds k recordings.
:func:`approved_items` is the curator review: a member is published once enough dataset managers
approved it, and :func:`veto_member` removes one a manager found identifying. The equivalence classes the anonymity report counts are the
project's too, through :func:`register_equivalence_class`; the reports themselves are in
:mod:`library.reports`.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db.models import BigIntegerField, Case, CharField, DateTimeField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, Lower, NullIf

from library.models import Dataset, DatasetItem, DatasetRelease, month_start

# ---------------------------------------------------------------------------
# Naming datasets and members on the command line
# ---------------------------------------------------------------------------


def resolve_dataset(identifier: str) -> Dataset | None:
    """Return the live dataset named by a 32-character hash or an integer primary key, or ``None``."""
    identifier = identifier.strip()
    qs = Dataset.objects.filter(deleted_at__isnull=True)
    if len(identifier) == 32 and identifier.isalnum():
        return qs.filter(object_hash=identifier.upper()).first()
    if identifier.isdigit():
        return qs.filter(pk=int(identifier)).first()
    return None


def member_handle(item: DatasetItem) -> str:
    """Name a member by its content type and public handle, never by a label."""
    obj = item.content_object
    if obj is None:
        return f"{item.content_type.model}:{item.object_id}"
    stored_name = getattr(obj, "stored_name", None)
    if stored_name:
        return f"{item.content_type.model}:{stored_name.split('.', 1)[0]}"
    object_hash = getattr(obj, "object_hash", None) or getattr(obj, "content_hash", None)
    return f"{item.content_type.model}:{object_hash or item.object_id}"


# ---------------------------------------------------------------------------
# Who may see an unreleased member
# ---------------------------------------------------------------------------


def is_dataset_manager(user: Any, dataset: Dataset) -> bool:
    """True for the dataset author, a superuser and a holder of a ``can_write`` grant on the dataset.

    Managers are the readers a gated dataset's unreleased members exist for: they curate what
    the next run publishes. Write access rather than share access, because managing the pool
    is a write and sharing it is the author's decision.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or dataset.author_id == user.pk:
        return True
    from epicurrents.models import AccessRight

    dataset_ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
    target_filter = Q(access_target_id=user.pk)
    group_ids = list(user.groups.values_list("id", flat=True))
    if group_ids:
        target_filter |= Q(access_target_group_id__in=group_ids)
    return (
        AccessRight.objects.active()
        .filter(content_type=dataset_ct, object_id=str(dataset.pk), can_write=True)
        .filter(target_filter)
        .exists()
    )


#: The date served in place of a pooled recording's upload time when no release has ever published it.
#: A pooled recording that is not a live released member is dated by its latest release; one never
#: released has no date a reader may see, and the ingest month would be the arrival time the pool hides.
WITHHELD_DATE = datetime(1970, 1, 1, tzinfo=UTC)


def system_user_id() -> int | None:
    """Primary key of the system user, or None when it does not exist yet; never creates it.

    Only the pooled ingest (``recordings.tasks``) creates recordings the system user owns, so a
    system-authored recording is a pooled one, whatever has happened to its pool since.
    """
    from django.contrib.auth import get_user_model

    from epicurrents.system_user import _SYSTEM_USERNAME

    return get_user_model().objects.filter(username=_SYSTEM_USERNAME).values_list("pk", flat=True).first()


def is_pooled_recording(obj: Any) -> bool:
    """True for a recording the pooled ingest created: a system-authored recording."""
    from recordings.models import Recording

    if not isinstance(obj, Recording):
        return False
    system_id = system_user_id()
    return system_id is not None and obj.author_id == system_id


def controls_membership(dataset: Any, member_author_id: int | None, system_id: int | None = None) -> bool:
    """True when *dataset*'s gate may act on a member authored by *member_author_id*.

    A gate hides, dates and withholds on behalf of whoever put the member there, so it acts only
    on memberships the gating party legitimately controls: members its author authored, and in a
    submission pool the system-authored recordings the pooled ingest put there. A third party who
    gates a dataset holding someone else's recording hides nothing of it.
    """
    from library.pools import is_pool

    if member_author_id is None:
        return False
    if dataset.author_id == member_author_id:
        return True
    return system_id is not None and member_author_id == system_id and is_pool(dataset)


def _author_ids(content_type: ContentType, object_ids) -> dict[str, int | None]:
    """``author_id`` per object of *content_type* among *object_ids*, keyed by the string pk."""
    model = content_type.model_class()
    pks = [int(pk) for pk in object_ids if str(pk).isdigit()]
    if model is None or not pks or not any(f.attname == "author_id" for f in model._meta.concrete_fields):
        return {}
    rows = model._default_manager.filter(pk__in=pks).values_list("pk", "author_id")
    return {str(pk): author_id for pk, author_id in rows}


def _controlled_rows(content_type: ContentType, object_ids=None, **filters) -> list[tuple[str, int, int | None]]:
    """``(object_id, dataset_id, release_id)`` of the live gated memberships the gate controls.

    *object_ids* limits the lookup; ``None`` reads every gated membership of the content type.
    """
    rows = DatasetItem.objects.filter(
        content_type=content_type,
        dataset__release_gated=True,
        dataset__deleted_at__isnull=True,
        **filters,
    )
    if object_ids is not None:
        rows = rows.filter(object_id__in=[str(pk) for pk in object_ids])
    rows = list(
        rows.values_list("object_id", "dataset_id", "release_id", "dataset__author_id", "dataset__submission_profile")
    )
    if not rows:
        return []
    authors = _author_ids(content_type, {object_id for object_id, *_rest in rows})
    system_id = system_user_id()
    controlled = []
    for object_id, dataset_id, release_id, dataset_author_id, profile in rows:
        author_id = authors.get(str(object_id))
        if author_id is None:
            continue
        if author_id == dataset_author_id or (system_id is not None and author_id == system_id and profile):
            controlled.append((str(object_id), dataset_id, release_id))
    return controlled


def gated_memberships(obj: Any) -> list[DatasetItem]:
    """The rows placing *obj* in a live release-gated dataset whose gate controls it (:func:`controls_membership`)."""
    object_pk = getattr(obj, "pk", None)
    if object_pk is None:
        return []
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    items = DatasetItem.objects.filter(
        content_type=ct,
        object_id=str(object_pk),
        dataset__release_gated=True,
        dataset__deleted_at__isnull=True,
    ).select_related("dataset", "release")
    author_id = getattr(obj, "author_id", None)
    system_id = system_user_id()
    return [item for item in items if controls_membership(item.dataset, author_id, system_id)]


def member_hidden_from_reader(user: Any, obj: Any, share_token: str | None = None) -> bool:
    """Read-visibility gate: True when a gated-dataset member must not resolve for this caller.

    Hidden when the request carries a share token, whatever else the caller holds; when the
    caller is anonymous; and when the member is unreleased in any gated dataset the caller does
    not manage. ``user=None`` is the federated shape and sees released members only. The
    member's own author keeps seeing it, as with FAILED uploads: what a user uploaded is theirs
    to see regardless of where it was filed. Only memberships the dataset's gate controls count
    (:func:`controls_membership`), so a third party's gated dataset hides nothing it does not own.
    """
    memberships = gated_memberships(obj)
    if not memberships:
        return False
    if (share_token or "").strip():
        return True
    if user is None or not getattr(user, "is_authenticated", False):
        # Anonymous without a token holds nothing anyway; a peer reads released members.
        return any(item.release_id is None for item in memberships)
    if getattr(user, "is_superuser", False) or getattr(obj, "author_id", None) == user.pk:
        return False
    unreleased = [item for item in memberships if item.release_id is None]
    return any(not is_dataset_manager(user, item.dataset) for item in unreleased)


def dataset_hidden_from_reader(user: Any, obj: Any, share_token: str | None = None) -> bool:
    """Read-visibility gate for datasets: a release-gated dataset resolves for no share-token request."""
    return bool(getattr(obj, "release_gated", False)) and bool((share_token or "").strip())


def gate_refusal(dataset: Dataset) -> str | None:
    """Why the gate may not be turned on for *dataset*, or None when it may.

    Every recording and media member must be the dataset author's own, or a pooled recording of
    a pool: a gate on anyone else's member would hide it from its own readers.
    """
    from recordings.models import Recording

    system_id = system_user_id()
    member_types = [Recording]
    from django.apps import apps

    if apps.is_installed("media"):
        member_types.append(apps.get_model("media", "MediaFile"))
    for model in member_types:
        ct = ContentType.objects.get_for_model(model, for_concrete_model=False)
        ids = DatasetItem.objects.filter(dataset=dataset, content_type=ct).values_list("object_id", flat=True)
        for author_id in _author_ids(ct, ids).values():
            if not controls_membership(dataset, author_id, system_id):
                return "Only a dataset whose members are all its author's own can be release-gated."
    return None


def membership_refusal(dataset: Dataset, obj: Any) -> str | None:
    """Why *obj* may not be added to the gated *dataset*, or None when it may (always None for an ungated one).

    A gated dataset takes only its author's own objects, whoever adds them, superusers included:
    what the gate hides must be the gating party's to hide.
    """
    if not dataset.release_gated:
        return None
    author_id = getattr(obj, "author_id", None)
    if author_id is None or author_id != dataset.author_id:
        return "A release-gated dataset takes only its author's own objects."
    return None


# ---------------------------------------------------------------------------
# Batch helpers for listings
# ---------------------------------------------------------------------------


def unreleased_member_ids(content_type: ContentType) -> set[str]:
    """``object_id`` values of *content_type* that are unreleased, gate-controlled members of a live gated dataset."""
    return {object_id for object_id, _dataset_id, release_id in _controlled_rows(content_type) if release_id is None}


def hidden_member_ids(user: Any, content_type: ContentType, object_ids, share_token: str | None = None) -> set[str]:
    """The batch form of :func:`member_hidden_from_reader`: ids among *object_ids* the gate hides from this caller."""
    rows = _controlled_rows(content_type, object_ids)
    if not rows:
        return set()
    if (share_token or "").strip():
        return {object_id for object_id, _dataset_id, _release_id in rows}
    unreleased = [(object_id, dataset_id) for object_id, dataset_id, release_id in rows if release_id is None]
    if not unreleased:
        return set()
    if user is None or not getattr(user, "is_authenticated", False):
        return {object_id for object_id, _dataset_id in unreleased}
    if getattr(user, "is_superuser", False):
        return set()
    authors = _author_ids(content_type, {object_id for object_id, _dataset_id in unreleased})
    datasets = Dataset.objects.in_bulk({dataset_id for _object_id, dataset_id in unreleased})
    managed = {pk for pk, dataset in datasets.items() if is_dataset_manager(user, dataset)}
    return {
        object_id
        for object_id, dataset_id in unreleased
        if dataset_id not in managed and authors.get(object_id) != user.pk
    }


def pooled_ids(content_type: ContentType, object_ids) -> set[str]:
    """The system-authored recordings among *object_ids*; empty for any other content type."""
    from recordings.models import Recording

    if content_type.model_class() is not Recording:
        return set()
    system_id = system_user_id()
    if system_id is None:
        return set()
    return {
        object_id for object_id, author_id in _author_ids(content_type, object_ids).items() if author_id == system_id
    }


def release_months_by_id(content_type: ContentType, object_ids) -> dict[str, datetime]:
    """Release month per member among *object_ids* that a reader who is not its author sees in place of its upload time.

    A gate-controlled released member of a live gated dataset takes the earliest release month of
    its memberships. A pooled recording is dated by its latest release whatever has become of its
    pool since, trashed or no longer gated, and one never released by :data:`WITHHELD_DATE`: its
    ingest month is the arrival time the pool hides.
    """
    object_ids = [str(pk) for pk in object_ids]
    rows = _controlled_rows(content_type, object_ids, release__isnull=False)
    release_months = dict(
        DatasetRelease.objects.filter(pk__in={release_id for _o, _d, release_id in rows}).values_list(
            "pk", "release_month"
        )
    )
    months: dict[str, datetime] = {}
    for object_id, _dataset_id, release_id in rows:
        month = release_months.get(release_id)
        if month is not None and (object_id not in months or month < months[object_id]):
            months[object_id] = month
    pooled = pooled_ids(content_type, object_ids)
    if pooled:
        latest: dict[str, datetime] = {}
        for object_id, month in DatasetItem.objects.filter(
            content_type=content_type, object_id__in=pooled, release__isnull=False
        ).values_list("object_id", "release__release_month"):
            key = str(object_id)
            if key not in latest or month > latest[key]:
                latest[key] = month
        for object_id in pooled:
            months[object_id] = latest.get(object_id, WITHHELD_DATE)
    return months


def release_month_for(obj: Any) -> datetime | None:
    """The release month *obj* is dated by for a reader, or None when it is neither a released member nor pooled."""
    object_pk = getattr(obj, "pk", None)
    if object_pk is None:
        return None
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return release_months_by_id(ct, [object_pk]).get(str(object_pk))


def stored_digest_withheld_ids(user: Any, content_type: ContentType, object_ids) -> set[str]:
    """``object_id`` values among *object_ids* whose stored digest this caller must not receive.

    A gate-controlled member of a live release-gated dataset serves its ``stored_hash`` only to a
    manager of one of its gated datasets and to superusers; the recording's author is exempted by
    the caller, which already knows it. A pooled recording is withheld the same way whether or not
    its pool is still live and gated, since a trashed or ungated pool does not make its digest safe
    to serve. A pooled contributor holds a receipt naming the submitted bytes by their SHA-256, and
    nothing guarantees the ingest pass changes every file, so a served digest could equal a receipt
    and let whoever holds one find the member in a listing. Withholding it also closes the content
    pin to the same readers, since a pin answers whether a guessed digest matches. ``user=None`` is
    the federated shape and receives none.
    """
    object_ids = [str(pk) for pk in object_ids]
    member_of: dict[str, set[int]] = {}
    for object_id, dataset_id, _release_id in _controlled_rows(content_type, object_ids):
        member_of.setdefault(object_id, set()).add(dataset_id)
    for object_id in pooled_ids(content_type, object_ids):
        member_of.setdefault(object_id, set())
    if not member_of:
        return set()
    if user is not None and getattr(user, "is_superuser", False):
        return set()
    managed: set[int] = set()
    if user is not None and getattr(user, "is_authenticated", False):
        datasets = Dataset.objects.filter(pk__in=set().union(*member_of.values()))
        managed = {dataset.pk for dataset in datasets if is_dataset_manager(user, dataset)}
    return {object_id for object_id, dataset_ids in member_of.items() if not dataset_ids & managed}


def stored_digest_withheld(user: Any, obj: Any) -> bool:
    """True when *obj*'s stored digest is withheld from this caller; see :func:`stored_digest_withheld_ids`."""
    object_pk = getattr(obj, "pk", None)
    if object_pk is None:
        return False
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return str(object_pk) in stored_digest_withheld_ids(user, ct, [object_pk])


def release_month_subquery(model):
    """An annotation giving each row of *model* the release month it is dated by, null for non-members.

    For ordering a listing so that a released member sorts by its release month in place of its
    upload time; pair it with a name key so members released in the same month do not fall back
    to arrival order. The same rule as :func:`release_months_by_id`: a gate-controlled membership
    of a live gated dataset whose author authored the member, and for a pooled recording its
    latest release anywhere, else :data:`WITHHELD_DATE`.
    """
    ct = ContentType.objects.get_for_model(model, for_concrete_model=False)
    object_ref = Cast(OuterRef("pk"), CharField())
    controlled = Subquery(
        DatasetItem.objects.filter(
            content_type=ct,
            object_id=object_ref,
            dataset__release_gated=True,
            dataset__deleted_at__isnull=True,
            dataset__author_id=OuterRef("author_id"),
            release__isnull=False,
        )
        .order_by("release__release_month")
        .values("release__release_month")[:1],
        output_field=DateTimeField(),
    )
    system_id = system_user_id()
    if system_id is None:
        return controlled
    pooled = Coalesce(
        Subquery(
            DatasetItem.objects.filter(content_type=ct, object_id=object_ref, release__isnull=False)
            .order_by("-release__release_month")
            .values("release__release_month")[:1],
            output_field=DateTimeField(),
        ),
        Value(WITHHELD_DATE, output_field=DateTimeField()),
    )
    return Case(When(author_id=system_id, then=pooled), default=controlled, output_field=DateTimeField())


def _named_subquery(model, name_expression) -> Subquery:
    """A subquery yielding *name_expression* for the row of *model* whose pk is the outer item's ``object_id``."""
    return Subquery(
        model.objects.filter(pk=Cast(OuterRef("object_id"), BigIntegerField()))
        .annotate(sort_name=name_expression)
        .values("sort_name")[:1],
        output_field=CharField(),
    )


def _lower_or_null(field_name: str):
    return NullIf(Lower(field_name), Value(""))


def member_name_subquery() -> Case:
    """An annotation naming a dataset item by its member's name, whatever the member's type.

    A recording is named by its display name, else its stored name; a media file the same way;
    a nested dataset by its name; all case-folded, so an unnamed member's hex handle sorts among
    the names rather than before them. Each branch is keyed on the item's content type, since primary
    keys collide across models. A member of any other type annotates null and sorts last by
    object id. For ordering the items of a gated dataset, where ``added_at`` would reveal the
    arrival order the release month is there to hide.
    """
    from django.apps import apps

    from recordings.models import Recording

    branches = [
        When(
            content_type=ContentType.objects.get_for_model(Recording, for_concrete_model=False),
            then=_named_subquery(Recording, Coalesce(_lower_or_null("display_name"), Lower("stored_name"))),
        ),
        When(
            content_type=ContentType.objects.get_for_model(Dataset, for_concrete_model=False),
            then=_named_subquery(Dataset, Lower("name")),
        ),
    ]
    if apps.is_installed("media"):
        MediaFile = apps.get_model("media", "MediaFile")
        branches.append(
            When(
                content_type=ContentType.objects.get_for_model(MediaFile, for_concrete_model=False),
                then=_named_subquery(MediaFile, Coalesce(_lower_or_null("display_name"), Lower("stored_name"))),
            )
        )
    return Case(*branches, default=Value(None), output_field=CharField())


# ---------------------------------------------------------------------------
# Release runs
# ---------------------------------------------------------------------------


@dataclass
class ReleaseDecision:
    """What a release selector returns: the eligible members to publish and the record fields for the run.

    ``items`` is a subset of the eligible set the selector was handed. The remaining fields are
    the ¶ 41 record: the preparation profile version, the equivalence-class conditions in
    force, the primary keys of the users who signed the run off and a reference to the written
    assessment. Any left blank is filled from the command line, or left blank.
    """

    items: list[DatasetItem]
    profile_version: str = ""
    k: int | None = None
    m: int | None = None
    sign_off_user_ids: list[int] = field(default_factory=list)
    assessment_reference: str = ""


ReleaseSelector = Callable[..., ReleaseDecision]

_RELEASE_SELECTOR: ReleaseSelector | None = None


def register_release_selector(selector: ReleaseSelector | None) -> None:
    """Register the project's release selector, or clear it with ``None``.

    The selector is called as ``selector(dataset, eligible, as_of=date)`` with the
    cadence-eligible unreleased members and returns a :class:`ReleaseDecision`. This is where
    a project applies its own conditions: equivalence-class size (:func:`select_by_class_size`), embargo
    attestation, curator veto. One selector per deployment; registering a second replaces the
    first, since a dataset pool has one release policy.
    """
    global _RELEASE_SELECTOR
    _RELEASE_SELECTOR = selector


EquivalenceClass = Callable[[Any], Hashable | None]

_EQUIVALENCE_CLASS: EquivalenceClass | None = None


def register_equivalence_class(fn: EquivalenceClass | None) -> None:
    """Register the project's equivalence-class function, or clear it with ``None``.

    The function takes a recording and returns the key of the class it belongs to, or ``None``
    for a recording the project leaves unclassified. What a class is (an age band, a montage
    template, a condition marker, a combination) is the pool's design, so the platform never
    defines one; it only counts. The anonymity report recomputes the classes live over the
    released pool, which is what makes it re-runnable after a profile change, and a project's
    release selector is expected to apply the same function so the k it records and the k the
    report finds agree. One function per deployment, like the selector.
    """
    global _EQUIVALENCE_CLASS
    _EQUIVALENCE_CLASS = fn


def equivalence_class_function() -> EquivalenceClass | None:
    """The registered equivalence-class function, or ``None``."""
    return _EQUIVALENCE_CLASS


def eligibility_cutoff(as_of: date) -> datetime:
    """The instant before which a member must have been uploaded to be eligible at a run on *as_of*.

    Uploaded in month M means eligible from the run at the start of M+2, so the cutoff is the
    first instant of the month before the run's month: everything uploaded before it has waited
    at least one full month, and at most two.
    """
    year, month = as_of.year, as_of.month - 1
    if month == 0:
        year, month = year - 1, 12
    return month_start(date(year, month, 1))


def eligible_items(dataset: Dataset, *, as_of: date) -> list[DatasetItem]:
    """Unreleased members of *dataset* whose upload time is before the cadence cutoff for *as_of*.

    A recording member is dated by its own ``created_at``; any other member type by the time
    it was added to the dataset. Trashed and FAILED recordings are never eligible: a release
    must not publish what no reader could then read.
    """
    from recordings.models import Recording

    cutoff = eligibility_cutoff(as_of)
    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    items = list(
        DatasetItem.objects.filter(dataset=dataset, release__isnull=True).select_related("content_type").order_by("pk")
    )
    recording_pks = [item.object_id for item in items if item.content_type_id == recording_ct.pk]
    uploaded_at = {
        str(pk): created_at
        for pk, created_at in Recording.objects.filter(
            pk__in=recording_pks, deleted_at__isnull=True, status=Recording.Status.READY
        ).values_list("pk", "created_at")
    }
    eligible: list[DatasetItem] = []
    for item in items:
        if item.content_type_id == recording_ct.pk:
            uploaded = uploaded_at.get(item.object_id)
            if uploaded is None:
                continue
        else:
            uploaded = item.added_at
        if uploaded < cutoff:
            eligible.append(item)
    return eligible


def deidentification_version_by_id(items: list[DatasetItem]) -> dict[str, int]:
    """The de-identification pass version stamped on each recording member among *items*, keyed by object id."""
    from recordings.models import Recording, RecordingMeta

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    pks = [item.object_id for item in items if item.content_type_id == recording_ct.pk]
    if not pks:
        return {}
    rows = RecordingMeta.objects.filter(content_type=recording_ct, object_id__in=pks).values_list(
        "object_id", "deidentification_version"
    )
    return {str(object_id): int(version) for object_id, version in rows if version is not None}


def deidentification_versions_of(items: list[DatasetItem]) -> list[int]:
    """Distinct de-identification pass versions of the recording members among *items*, sorted."""
    return sorted(set(deidentification_version_by_id(items).values()))


def select_by_class_size(dataset: Dataset, eligible: list[DatasetItem], *, k: int) -> list[DatasetItem]:
    """The eligible members whose equivalence class holds at least *k* recordings once this run publishes.

    A class is counted over the pool as released and still readable (released, not trashed,
    READY) plus the eligible members in it, so a class that reached k earlier takes new members one
    at a time and one that has not waits until enough are eligible together. The class of a member
    is the registered function's key in its string form, the same key the anonymity report sizes,
    so the k a run records and the k the report finds agree. Unclassified members, and members that
    are not recordings, are never selected; without a registered function nothing is. A building
    block for a project's selector, which still decides what else a release needs.
    """
    from recordings.models import Recording

    fn = equivalence_class_function()
    if fn is None:
        return []
    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    released_ids = list(
        DatasetItem.objects.filter(dataset=dataset, content_type=recording_ct, release__isnull=False).values_list(
            "object_id", flat=True
        )
    )
    eligible_ids = [item.object_id for item in eligible if item.content_type_id == recording_ct.pk]
    readable = Recording.objects.filter(deleted_at__isnull=True, status=Recording.Status.READY)
    by_id = {
        str(recording.pk): recording
        for recording in readable.filter(pk__in=[int(pk) for pk in [*released_ids, *eligible_ids] if str(pk).isdigit()])
    }

    def class_of(object_id: str) -> str | None:
        recording = by_id.get(str(object_id))
        key = fn(recording) if recording is not None else None
        return None if key is None else str(key)

    sizes: dict[str, int] = {}
    for object_id in [*released_ids, *eligible_ids]:
        key = class_of(object_id)
        if key is not None:
            sizes[key] = sizes.get(key, 0) + 1
    selected = []
    for item in eligible:
        if item.content_type_id != recording_ct.pk:
            continue
        key = class_of(item.object_id)
        if key is not None and sizes[key] >= k:
            selected.append(item)
    return selected


def _standing_approvals(items: list[DatasetItem]) -> list[tuple[int, int | None]]:
    """``(item_id, reviewer_id)`` of the approvals of *items* that still count.

    An approval counts while its curator is still a manager of the member's dataset: a curator
    whose write grant was revoked or expired no longer releases anything, nor signs a run off.
    An approval whose curator's account has since been deleted (null reviewer) still counts: it
    was given by a distinct curator when it was made, which the per-reviewer constraint
    guaranteed.
    """
    from django.contrib.auth import get_user_model

    from library.models import MemberApproval

    by_pk = {item.pk: item for item in items}
    rows = list(MemberApproval.objects.filter(item__in=list(by_pk)).values_list("item_id", "reviewer_id"))
    reviewers = get_user_model().objects.in_bulk({reviewer_id for _item, reviewer_id in rows if reviewer_id})
    datasets = Dataset.objects.in_bulk({item.dataset_id for item in items})
    managing: dict[tuple[int, int], bool] = {}
    standing = []
    for item_id, reviewer_id in rows:
        if reviewer_id is None:
            standing.append((item_id, None))
            continue
        dataset_id = by_pk[item_id].dataset_id
        key = (reviewer_id, dataset_id)
        if key not in managing:
            reviewer = reviewers.get(reviewer_id)
            managing[key] = reviewer is not None and is_dataset_manager(reviewer, datasets[dataset_id])
        if managing[key]:
            standing.append((item_id, reviewer_id))
    return standing


def approval_counts(items: list[DatasetItem]) -> dict[int, int]:
    """The number of standing curator approvals each of *items* holds, keyed by item primary key."""
    counts = {item.pk: 0 for item in items}
    for item_id, _reviewer_id in _standing_approvals(items):
        counts[item_id] += 1
    return counts


def approved_items(items: list[DatasetItem], *, required: int) -> list[DatasetItem]:
    """The members of *items* approved by at least *required* distinct curators still managing their dataset."""
    counts = approval_counts(items)
    return [item for item in items if counts[item.pk] >= required]


def approvers_of(items: list[DatasetItem]) -> list[int]:
    """Primary keys of the curators with a standing approval of any of *items*, sorted: the sign-off of a run."""
    return sorted({reviewer_id for _item, reviewer_id in _standing_approvals(items) if reviewer_id is not None})


def veto_member(item: DatasetItem) -> str:
    """Remove a vetoed recording member, and return what went: ``"recording"`` or ``"membership"``.

    A pooled recording vetoed in its pool, a system-authored recording in a dataset configured as
    a submission pool, is removed whole: its file is unlinked, then the recording is deleted and
    with it the membership. The file goes first, as in the purge: a row outliving its bytes is a
    dead pointer, while bytes outliving their row are what a veto exists to prevent, and an unlink
    failure raises and deletes nothing. Anywhere else only the membership goes: a curator is a
    dataset manager, and managing a dataset confers no right to delete another user's data, nor
    a pooled recording someone placed in a dataset of their own.

    Must be called inside an audited scope and a transaction, so each deletion is recorded. Only
    recording members are vetoed; another member type raises ``ValueError``.
    """
    from pathlib import Path

    from library.pools import is_pool
    from recordings.models import Recording

    if item.content_type_id != ContentType.objects.get_for_model(Recording, for_concrete_model=False).pk:
        raise ValueError("Only a recording member can be vetoed")
    recording = Recording.objects.filter(pk=int(item.object_id)).first()
    if recording is None or not is_pool(item.dataset) or not is_pooled_recording(recording):
        item.delete()
        return "membership"
    if recording.file_path:
        Path(recording.file_path).unlink(missing_ok=True)
    recording.delete()
    return "recording"


def decide_release(dataset: Dataset, *, as_of: date) -> tuple[list[DatasetItem], ReleaseDecision]:
    """Compute the eligible set and hand it to the registered selector, or publish it whole."""
    eligible = eligible_items(dataset, as_of=as_of)
    if _RELEASE_SELECTOR is None:
        return eligible, ReleaseDecision(items=list(eligible))
    decision = _RELEASE_SELECTOR(dataset, list(eligible), as_of=as_of)
    eligible_pks = {item.pk for item in eligible}
    decision.items = [item for item in decision.items if item.pk in eligible_pks]
    return eligible, decision


def run_release(
    dataset: Dataset,
    *,
    as_of: date,
    actor: Any = None,
    profile_version: str = "",
    k: int | None = None,
    m: int | None = None,
    sign_off_user_ids: list[int] | None = None,
    assessment_reference: str = "",
) -> tuple[DatasetRelease, list[DatasetItem], list[DatasetItem]]:
    """Perform a release run on *dataset* and return the run, the eligible set and the released members.

    Must be called inside an audited scope and a transaction: the dataset row and its unreleased
    items are locked first, so two runs cannot select the same members; the run row is created,
    each released item is saved with its release pointer and the pass version it carried, so the
    audit signal records the change, and nothing else moves. Command-line values fill whichever
    record fields the selector left blank, except the sign-off, which is the union of the
    selector's and the command line's: the curators who approved the members and anyone who
    signed the run off as a whole, one ``DatasetReleaseSignOff`` row per existing account. A run
    that releases nothing still leaves a row, since a run with an empty eligible set or a
    selector that withheld everything is a fact worth dating.

    *as_of* may not lie after today (UTC), which would move the cadence forward and stamp a
    release month that has not happened, nor before the dataset's latest release, which would
    date members earlier than members already published. Both raise ``ValueError``.
    """
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from library.models import DatasetReleaseSignOff

    Dataset.objects.select_for_update().filter(pk=dataset.pk).first()
    dataset.refresh_from_db()
    if not dataset.release_gated:
        raise ValueError("Dataset is not release-gated")
    today = timezone.now().astimezone(UTC).date()
    if as_of > today:
        raise ValueError(f"A release cannot be dated after today ({today.isoformat()})")
    latest = (
        DatasetRelease.objects.filter(dataset=dataset).order_by("-released_on").values_list("released_on", flat=True)
    )
    latest = latest.first()
    if latest is not None and as_of < latest:
        raise ValueError(f"A release cannot be dated before the dataset's latest release ({latest.isoformat()})")
    list(DatasetItem.objects.select_for_update().filter(dataset=dataset, release__isnull=True).values_list("pk"))
    eligible, decision = decide_release(dataset, as_of=as_of)
    versions = deidentification_version_by_id(decision.items)
    release = DatasetRelease.objects.create(
        dataset=dataset,
        author=actor if getattr(actor, "pk", None) is not None else None,
        released_on=as_of,
        profile_version=decision.profile_version or profile_version,
        deidentification_versions=sorted(set(versions.values())),
        k=decision.k if decision.k is not None else k,
        m=decision.m if decision.m is not None else m,
        assessment_reference=decision.assessment_reference or assessment_reference,
        member_count=len(decision.items),
    )
    signers = set(decision.sign_off_user_ids or []) | set(sign_off_user_ids or [])
    for user_pk in sorted(get_user_model().objects.filter(pk__in=signers).values_list("pk", flat=True)):
        DatasetReleaseSignOff.objects.create(release=release, user_id=user_pk)
    for item in decision.items:
        item.release = release
        item.released_deidentification_version = versions.get(str(item.object_id))
        item.save(update_fields=["release", "released_deidentification_version"])
    return release, eligible, decision.items


def sign_off_user_ids(release: DatasetRelease) -> list[int]:
    """Primary keys of the accounts that signed *release* off and still exist, sorted."""
    return sorted(release.sign_offs.filter(user__isnull=False).values_list("user_id", flat=True))


def clear_erased_assessment_references(sender, instance, **kwargs) -> None:
    """``pre_delete`` receiver on the user model: clear the assessment reference of the runs the user authored.

    The reference is free text the runner typed, registered as their personal data; it is scrubbed
    from the audit trail by ``erase_subject`` and must not outlive the account on the live row
    either. Saved per row, so the change is audited inside the erasure's scope and that audit row,
    which names the runner as author, is scrubbed with the rest.
    """
    for release in DatasetRelease.objects.filter(author_id=instance.pk).exclude(assessment_reference=""):
        release.assessment_reference = ""
        release.save(update_fields=["assessment_reference"])
