"""The two reports a release-gated dataset keeps beside its assessment: who reads it, and how its classes size up.

⚠️ LOAD-BEARING — the ¶ 41 and ¶ 89 evidence.
These reports are what a deployment hands to a supervisory authority, or to itself at the
six-monthly sweep, as the record that its pool is what the assessment says it is. Two failure
directions are silent. A report that names a reader, a contributor or a centre publishes what the
pool was built to hide, so the access report counts and never names, and the anonymity report
repeats m rather than recomputing it. And a report that flatters, by counting a withdrawn member
into the pool, reading a re-written member as unchanged, or computing min k over a pool that is
not the pool as released up to that run, certifies an anonymity the data does not have. Contract
tests are in ``library/tests/test_reports.py`` (``TestAccessReport``, ``TestAnonymityReport``,
and the command classes for the output shape).

Both are read-only computations over what the platform already records, presented by the
``dataset_access_report`` and ``dataset_anonymity_report`` commands and their maintenance
operations.

The **access report** is the evidence for the access-control argument in EDPB Guidelines 02/2026
¶ 89 and the input to the six-monthly sweep: per recording member, how many requests and how
many distinct authenticated readers the ``Activity`` trail shows over a window, with byte-serving
requests counted separately, plus the requests that carried no account and the requests that were
refused (4xx; a server error is not a refusal). It reads the archived rows as well as the live ones, because the activity archiver's
default window is shorter than the sweep's and a report that silently lost its older half would
be worse than none.

The **anonymity report** is the ¶ 41 record per release, re-run on every release run and every
profile change (¶ 35): the record the run stored, the per-recording de-identification record of
what it released with any member since re-written by a newer pass flagged, the members withdrawn
since, and the equivalence-class sizes over the pool as released up to that run, from which the
minimum k, the fraction of members below the recorded k, the prosecutor risk and the entropy
follow. The classes come from the project's registered function; without one the report says so
and computes nothing.

What the platform cannot recompute, it reports as recorded. The distinct-contributor condition m
needs to know which centre each recording came from, and the pooled ingest severs that link by
design, so m is the project's to establish at release time and the platform only repeats the
value the run stored. Journalist risk needs population frequencies the platform does not hold.
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import median
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Max, Q

from library.models import Dataset, DatasetItem, DatasetRelease
from library.release import equivalence_class_function, member_handle

#: The verbs that mean a reader looked at a recording: metadata, annotations or bytes.
READ_VERBS: tuple[str, ...] = (
    "recordings.read",
    "recordings.status",
    "recordings.read.slice",
    "recordings.annotations.list",
    "recordings.download",
    "recordings.download.slice",
)

#: The subset of :data:`READ_VERBS` that serves signal bytes.
BYTE_VERBS: tuple[str, ...] = (
    "recordings.read.slice",
    "recordings.download",
    "recordings.download.slice",
)


def _recording_ct() -> ContentType:
    from recordings.models import Recording

    return ContentType.objects.get_for_model(Recording, for_concrete_model=False)


def _recording_items(dataset: Dataset, **filters) -> list[DatasetItem]:
    return list(
        DatasetItem.objects.filter(dataset=dataset, content_type=_recording_ct(), **filters)
        .select_related("content_type", "release")
        .prefetch_related("content_object")
        .order_by("pk")
    )


# ---------------------------------------------------------------------------
# Access report
# ---------------------------------------------------------------------------


def access_report(dataset: Dataset, *, since: datetime, until: datetime | None = None) -> dict[str, Any]:
    """Count reads of every recording member of *dataset* between *since* and *until* (now when omitted).

    Per member: requests, byte-serving requests, distinct authenticated readers, requests with
    no account, and the month of the last successful read. Per dataset: the same totals, the
    distinct readers across the pool, the refused requests (4xx; a server error is not a
    refusal), and the largest and median request count per reader. A listing request is not
    per member and is not counted. Counts only: no reader is named.

    A request with no account is one the trail holds no actor for. On a gated dataset that is a
    share-token or unauthenticated caller, which the gate refuses, so a served request without
    an account is a finding; but an erased account's requests land there too, since erasure
    nulls the actor, so the column is read against the deployment's erasure history.
    """
    from activity.models import Activity

    items = _recording_items(dataset)
    pks = [item.object_id for item in items]
    rows = Activity.including_archived.filter(
        interface=Activity.Interface.API,
        target_content_type=_recording_ct(),
        target_object_id__in=pks,
        verb__in=READ_VERBS,
        created_at__gte=since,
    )
    if until is not None:
        rows = rows.filter(created_at__lt=until)
    served = rows.filter(status_code__gte=200, status_code__lt=300)
    refused = rows.filter(status_code__gte=400, status_code__lt=500).count()

    per_member = {
        row["target_object_id"]: row
        for row in served.values("target_object_id").annotate(
            requests=Count("id"),
            byte_requests=Count("id", filter=Q(verb__in=BYTE_VERBS)),
            readers=Count("actor_id", distinct=True),
            no_account=Count("id", filter=Q(actor_id__isnull=True)),
            last_read=Max("created_at"),
        )
    }
    per_reader = [row["n"] for row in served.filter(actor_id__isnull=False).values("actor_id").annotate(n=Count("id"))]

    members = []
    for item in items:
        counts = per_member.get(item.object_id)
        members.append(
            {
                "member": member_handle(item),
                "released": item.release.release_month.date().isoformat()[:7] if item.release_id else None,
                "requests": counts["requests"] if counts else 0,
                "byte_requests": counts["byte_requests"] if counts else 0,
                "readers": counts["readers"] if counts else 0,
                "no_account": counts["no_account"] if counts else 0,
                "last_read": counts["last_read"].date().isoformat()[:7] if counts else None,
            }
        )
    return {
        "dataset": dataset.object_hash,
        "since": since.isoformat(),
        "until": until.isoformat() if until is not None else None,
        "member_count": len(members),
        "requests": sum(m["requests"] for m in members),
        "byte_requests": sum(m["byte_requests"] for m in members),
        "readers": len(per_reader),
        "no_account": sum(m["no_account"] for m in members),
        "refused": refused,
        "reader_requests_max": max(per_reader) if per_reader else 0,
        "reader_requests_median": median(per_reader) if per_reader else 0,
        "members": members,
    }


# ---------------------------------------------------------------------------
# Anonymity report
# ---------------------------------------------------------------------------


def class_sizes(items: list[DatasetItem]) -> tuple[dict[str, int] | None, int]:
    """Size every equivalence class among the recording members in *items*.

    Returns the sizes keyed by the class key's string form, and the number of members the
    function left unclassified. The sizes are ``None`` when no function is registered, which
    is a different fact from every class being empty. A recording that no longer exists is
    counted as unclassified rather than handed to the function.
    """
    fn = equivalence_class_function()
    if fn is None:
        return None, len(items)
    from recordings.models import Recording

    by_pk = Recording.objects.in_bulk([int(item.object_id) for item in items])
    sizes: dict[str, int] = {}
    unclassified = 0
    for item in items:
        recording = by_pk.get(int(item.object_id))
        key = fn(recording) if recording is not None else None
        if key is None:
            unclassified += 1
            continue
        name = str(key)
        sizes[name] = sizes.get(name, 0) + 1
    return dict(sorted(sizes.items())), unclassified


def class_summary(sizes: dict[str, int] | None, unclassified: int, k: int | None) -> dict[str, Any]:
    """Derive the anonymity figures from class sizes: minimum k, fraction below the recorded k, risk, entropy.

    Prosecutor risk is the chance of picking the right record for a target known to be in the
    pool, one over the smallest class. The entropy is the base-2 log of that class size, the
    residual anonymity of its members in bits. The fraction below k is over classified members
    and is ``None`` when the run recorded no k. Everything is ``None`` when there is nothing to
    count.
    """
    if sizes is None:
        return {
            "class_function_registered": False,
            "classes": None,
            "unclassified": unclassified,
            "min_k": None,
            "below_k_fraction": None,
            "prosecutor_risk": None,
            "entropy_bits": None,
        }
    classified = sum(sizes.values())
    min_k = min(sizes.values()) if sizes else None
    below = sum(size for size in sizes.values() if k is not None and size < k)
    return {
        "class_function_registered": True,
        "classes": [{"key": key, "size": size} for key, size in sizes.items()],
        "unclassified": unclassified,
        "min_k": min_k,
        "below_k_fraction": (below / classified) if (k is not None and classified) else None,
        "prosecutor_risk": (1 / min_k) if min_k else None,
        "entropy_bits": math.log2(min_k) if min_k else None,
    }


def anonymity_report(dataset: Dataset, release: DatasetRelease) -> dict[str, Any]:
    """The ¶ 41 record of *release* as it stands now, with the pool's class sizes as released up to it.

    The pool is every member released by this run or an earlier one and still present; a
    withdrawn member has left it, which is why the report is worth re-running. A member is
    flagged ``changed_since_release`` when the pass version now stamped on it is not among the
    versions the run recorded, so a re-processed recording is visible at the release it belongs
    to. The recorded m is repeated, never recomputed: the platform holds no contributor per
    recording once the pool has ingested it.
    """
    from recordings.models import RecordingMeta

    released = _recording_items(dataset, release=release)
    pool = _recording_items(dataset, release__released_on__lte=release.released_on, release__isnull=False)
    stamped = {
        object_id: version
        for object_id, version in RecordingMeta.objects.filter(
            content_type=_recording_ct(), object_id__in=[item.object_id for item in released]
        ).values_list("object_id", "deidentification_version")
    }
    recorded = {int(v) for v in release.deidentification_versions}
    members = []
    for item in released:
        version = stamped.get(item.object_id)
        members.append(
            {
                "member": member_handle(item),
                "deidentification_version": version,
                "changed_since_release": bool(recorded) and version is not None and int(version) not in recorded,
            }
        )
    sizes, unclassified = class_sizes(pool)
    return {
        "dataset": dataset.object_hash,
        "release_id": release.pk,
        "released_on": release.released_on.isoformat(),
        "release_month": release.release_month.date().isoformat()[:7],
        "profile_version": release.profile_version,
        "deidentification_versions": list(release.deidentification_versions),
        "k": release.k,
        "m": release.m,
        "sign_off_count": len(release.sign_off_user_ids or []),
        "assessment_reference": release.assessment_reference,
        "member_count": release.member_count,
        "present_count": len(released),
        "withdrawn_count": max(release.member_count - len(released), 0),
        "changed_count": sum(1 for m in members if m["changed_since_release"]),
        "pool_count": len(pool),
        "members": members,
        **class_summary(sizes, unclassified, release.k),
    }
