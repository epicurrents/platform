"""List every active grant with the state of its sharer's contextual assessment.

The reassessment side of EDPB Guidelines 02/2026: a contextual finding that data is anonymous for a
recipient (paragraph 96) has to be re-checked on defined triggers, when capabilities change
(paragraphs 35 and 88) and after a security incident touching what the finding relied on
(paragraph 42). The platform never makes the finding; this command makes the triggers visible.
For every active grant that is not the author's own row it reports the recorded assessment, if
any, and one of four states:

- ``none`` — no assessment recorded. The ordinary case: the grant serves pseudonymised personal
  data and claims nothing more, so there is nothing to re-run.
- ``current`` — assessed within the cut-off and after the pass that last wrote every recording
  the grant covers.
- ``stale`` — assessed longer ago than ``--older-than`` days (default half a year, the sweep's
  cadence).
- ``reprocessed`` — a recording the grant covers, directly or as a dataset member, was written by
  the de-identification pass after the assessment was made, so the assessment looked at different
  output than the recipient now receives.

``--due`` keeps only the last two, which is the list a sharer is handed to re-run. Targets are
named by kind and primary key, never by username; the reference is the sharer's own text and is
printed, since the operator running this is the one audience for it besides the sharer.

Reads only. The run is recorded as an ``Activity`` row with counts, so the trail shows when the
sweep was made, which is part of what paragraph 41 asks to be kept.

Usage::

    python manage.py grant_assessments                       # every active grant
    python manage.py grant_assessments --due                 # only stale and reprocessed
    python manage.py grant_assessments --older-than 90       # a tighter cut-off
    python manage.py grant_assessments --format json         # machine-readable
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db.models import F
from django.utils import timezone

from epicurrents.models import AccessRight

STATUS_NONE = "none"
STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_REPROCESSED = "reprocessed"
DUE_STATUSES = (STATUS_STALE, STATUS_REPROCESSED)

#: Half a year: the cadence of the GDPR sweep in docs/gdpr-compliance.md.
DEFAULT_OLDER_THAN_DAYS = 183


def _target_of(right: AccessRight) -> str:
    """Name the target leg by kind and primary key, never by name."""
    if right.access_target_id is not None:
        return f"user:{right.access_target_id}"
    if right.access_target_group_id is not None:
        return f"group:{right.access_target_group_id}"
    if right.federated_peer_id is not None:
        return f"peer:{right.federated_peer_id}" + (f"/{right.remote_user_id}" if right.remote_user_id else "")
    return "token"


def _handle_of(obj) -> str:
    """The object's public handle: a recording's URL hash, a dataset's object hash, else the pk."""
    stored_name = getattr(obj, "stored_name", None)
    if stored_name:
        return stored_name.split(".", 1)[0]
    return getattr(obj, "object_hash", None) or str(obj.pk)


def _objects_by_row(rights: list[AccessRight]) -> dict[int, object]:
    """Resolve every granted object in one query per content type."""
    by_ct: dict = defaultdict(set)
    for right in rights:
        by_ct[right.content_type].add(right.object_id)
    found: dict[tuple[int, str], object] = {}
    for content_type, object_ids in by_ct.items():
        model = content_type.model_class()
        if model is None:
            continue
        pks = []
        for object_id in object_ids:
            try:
                pks.append(model._meta.pk.to_python(object_id))
            except (ValidationError, ValueError, TypeError):
                # A row pointing at nothing this model could have: reported
                # as an unresolved object rather than aborting the sweep.
                continue
        for obj in model._default_manager.filter(pk__in=pks):
            found[(content_type.pk, str(obj.pk))] = obj
    return {right.pk: found.get((right.content_type_id, right.object_id)) for right in rights}


def _covered_recording_pks(obj) -> list[str]:
    """Recording pks a grant on *obj* covers: the recording itself, or a dataset's recording members."""
    from django.contrib.contenttypes.models import ContentType

    from library.models import Dataset, DatasetItem
    from recordings.models import Recording

    if isinstance(obj, Recording):
        return [str(obj.pk)]
    if isinstance(obj, Dataset):
        recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        return list(
            DatasetItem.objects.filter(dataset=obj, content_type=recording_ct).values_list("object_id", flat=True)
        )
    return []


def assessment_status(
    right: AccessRight, *, cutoff: date, written_on: dict[str, date], covered: list[str]
) -> tuple[str, int]:
    """Classify one grant; returns the status and how many covered recordings were reprocessed after it."""
    assessed = right.assessment_date
    if assessed is None:
        return STATUS_NONE, 0
    reprocessed = sum(1 for pk in covered if pk in written_on and written_on[pk] > assessed)
    if reprocessed:
        return STATUS_REPROCESSED, reprocessed
    if assessed < cutoff:
        return STATUS_STALE, 0
    return STATUS_CURRENT, 0


def sweep(*, older_than_days: int) -> list[dict]:
    """Every active non-author grant with its assessment state, oldest grant first."""
    from recordings.deidentification_record import deidentification_record_dates

    cutoff = timezone.localdate() - timedelta(days=older_than_days)
    rights = list(
        AccessRight.objects.active()
        .exclude(access_target_id=F("access_giver_id"))
        .select_related("content_type")
        .order_by("pk")
    )
    objects = _objects_by_row(rights)
    written_on = deidentification_record_dates()
    rows = []
    for right in rights:
        obj = objects.get(right.pk)
        covered = _covered_recording_pks(obj) if obj is not None else []
        status, reprocessed = assessment_status(right, cutoff=cutoff, written_on=written_on, covered=covered)
        rows.append(
            {
                "grant_id": right.pk,
                "object": {
                    "model": f"{right.content_type.app_label}.{right.content_type.model}",
                    "handle": _handle_of(obj) if obj is not None else None,
                },
                "target": _target_of(right),
                "apply_middleware": right.apply_middleware,
                "expires_at": right.expires_at.isoformat() if right.expires_at else None,
                "assessment_reference": right.assessment_reference or None,
                "assessment_date": right.assessment_date.isoformat() if right.assessment_date else None,
                "status": status,
                "reprocessed_count": reprocessed,
            }
        )
    return rows


class Command(BaseCommand):
    help = "List every active grant with the state of its contextual assessment; --due keeps the ones to re-run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than",
            type=int,
            default=DEFAULT_OLDER_THAN_DAYS,
            metavar="DAYS",
            help=f"An assessment older than this many days is stale (default {DEFAULT_OLDER_THAN_DAYS}).",
        )
        parser.add_argument("--due", action="store_true", help="List only stale and reprocessed grants.")
        parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")

    def handle(self, *args, **options):
        from activity.models import Activity
        from activity.system_activity import with_system_activity

        older_than = options["older_than"]
        with with_system_activity(
            "epicurrents.grant_assessments",
            interface=Activity.Interface.COMMAND,
            metadata={"older_than_days": older_than, "due_only": options["due"], "format": options["format"]},
        ):
            rows = sweep(older_than_days=older_than)
        counts = {
            status: sum(1 for r in rows if r["status"] == status)
            for status in (STATUS_NONE, STATUS_CURRENT, STATUS_STALE, STATUS_REPROCESSED)
        }
        if options["due"]:
            rows = [r for r in rows if r["status"] in DUE_STATUSES]
        report = {
            "generated_at": timezone.now().isoformat(),
            "older_than_days": older_than,
            "counts": counts,
            "grants": rows,
        }
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2))
            return
        self._write_text(report, due_only=options["due"])

    def _write_text(self, report: dict, *, due_only: bool) -> None:
        self.stdout.write(f"Grant assessments, {report['generated_at']}; stale after {report['older_than_days']} days")
        self.stdout.write("")
        self.stdout.write(
            f"{'GRANT':>6}  {'OBJECT':44}  {'TARGET':14}  {'DEID':4}  {'ASSESSED':10}  {'STATUS':11}  REFERENCE"
        )
        for row in report["grants"]:
            obj = f"{row['object']['model']} {row['object']['handle'] or '?'}"
            status = row["status"]
            if status == STATUS_REPROCESSED:
                status = f"{status} ({row['reprocessed_count']})"
            self.stdout.write(
                f"{row['grant_id']:>6}  {obj:44}  {row['target']:14}  {'T' if row['apply_middleware'] else 'F':4}  "
                f"{row['assessment_date'] or '-':10}  {status:11}  {row['assessment_reference'] or ''}"
            )
        self.stdout.write("")
        counts = report["counts"]
        due = counts[STATUS_STALE] + counts[STATUS_REPROCESSED]
        summary = (
            f"{sum(counts.values())} grant(s): {counts[STATUS_NONE]} without an assessment, "
            f"{counts[STATUS_CURRENT]} current, {counts[STATUS_STALE]} stale, "
            f"{counts[STATUS_REPROCESSED]} reprocessed since assessed."
        )
        if due_only:
            summary += f" Listed {len(report['grants'])} due."
        self.stdout.write(self.style.WARNING(summary) if due else self.style.SUCCESS(summary))
