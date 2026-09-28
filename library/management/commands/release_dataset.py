"""Run a release on a release-gated dataset: publish the eligible members and record the run.

A release-gated dataset (``Dataset.release_gated``) hides its members from every reader but its
managers until a run publishes them. Runs happen on a monthly cadence: a member uploaded in month M
is eligible from the run at the start of M+2, so each waits between one and two months and a
month's submissions from every contributor surface together. The cadence is fixed here; which of
the eligible members a run publishes is the project's decision, through the selector registered
with ``library.release.register_release_selector``. With no selector the run publishes everything
eligible.

The run leaves a ``DatasetRelease`` row, the process record EDPB Guidelines 02/2026 ¶ 41 asks for:
the preparation profile version, the de-identification pass versions of what was released, the k
and m conditions in force, who signed off (user primary keys) and a reference to the written
assessment. The selector fills what it knows; the options below fill the rest. Released members
are dated by the run's month on every reader-facing surface from then on.

Audited as ``library.dataset.release`` against the dataset, with the run date in the metadata; each
published member's change is recorded under it.

Usage::

    python manage.py release_dataset <dataset-hash>
    python manage.py release_dataset <dataset-hash> --as-of 2026-11-01 --dry-run
    python manage.py release_dataset <dataset-hash> --profile-version 3 --k 5 --m 2 \\
        --sign-off 12 --sign-off 41 --assessment-reference "Assessment: teaching pool v3"
"""

from __future__ import annotations

import json
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from activity.models import Activity
from activity.system_activity import with_system_activity
from library.models import Dataset
from library.release import decide_release, member_handle, resolve_dataset, run_release


def _resolve_dataset(identifier: str) -> Dataset:
    """Resolve a live dataset by its 32-character hash or integer primary key, or raise."""
    dataset = resolve_dataset(identifier)
    if dataset is None:
        raise CommandError(f"No active dataset matches {identifier!r}")
    return dataset


class Command(BaseCommand):
    """Publish the eligible members of a release-gated dataset and record the run."""

    help = "Run a release on a release-gated dataset and record it"

    def add_arguments(self, parser):
        parser.add_argument("dataset", help="Dataset hash or primary key")
        parser.add_argument("--as-of", help="Run date, YYYY-MM-DD (default today); sets the release month")
        parser.add_argument("--dry-run", action="store_true", help="Report the eligible and selected members only")
        parser.add_argument("--actor", help="Username recorded as the run's author")
        parser.add_argument(
            "--actor-id", type=int, help="Primary key of the user recorded as the run's author (the maintenance tier)"
        )
        parser.add_argument(
            "--profile-version", default="", help="Preparation profile version the members were checked against"
        )
        parser.add_argument("--k", type=int, help="Equivalence-class size in force")
        parser.add_argument("--m", type=int, help="Contributors the pool was held to before ingest (the profile's m)")
        parser.add_argument(
            "--sign-off",
            action="append",
            type=int,
            default=[],
            help="Primary key of a user who signed the run off (repeatable)",
        )
        parser.add_argument("--assessment-reference", default="", help="Reference to the written assessment")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        dataset = _resolve_dataset(options["dataset"])
        if not dataset.release_gated:
            raise CommandError(f"Dataset {dataset.object_hash} is not release-gated")
        as_of = self._parse_date(options.get("as_of"))
        actor = None
        if options.get("actor") and options.get("actor_id") is not None:
            raise CommandError("Give --actor or --actor-id, not both")
        if options.get("actor"):
            actor = get_user_model().objects.filter(username=options["actor"]).first()
            if actor is None:
                raise CommandError(f"No user named {options['actor']!r}")
        if options.get("actor_id") is not None:
            actor = get_user_model().objects.filter(pk=options["actor_id"]).first()
            if actor is None:
                raise CommandError(f"No user with id {options['actor_id']}")

        if options["dry_run"]:
            eligible, decision = decide_release(dataset, as_of=as_of)
            report = self._report(dataset, as_of, eligible, decision.items, release=None)
            self._emit(report, options["format"], dry_run=True)
            return

        try:
            with (
                with_system_activity(
                    "library.dataset.release",
                    interface=Activity.Interface.COMMAND,
                    target=dataset,
                    actor=actor,
                    metadata={"as_of": as_of.isoformat()},
                ),
                transaction.atomic(),
            ):
                release, eligible, released = run_release(
                    dataset,
                    as_of=as_of,
                    actor=actor,
                    profile_version=options["profile_version"],
                    k=options.get("k"),
                    m=options.get("m"),
                    sign_off_user_ids=options["sign_off"],
                    assessment_reference=options["assessment_reference"],
                )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        report = self._report(dataset, as_of, eligible, released, release=release)
        self._emit(report, options["format"], dry_run=False)

    @staticmethod
    def _parse_date(value: str | None) -> date:
        if not value:
            return timezone.now().date()
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise CommandError(f"--as-of must be YYYY-MM-DD, got {value!r}") from exc

    @staticmethod
    def _report(dataset, as_of, eligible, released, *, release) -> dict:
        released_pks = {item.pk for item in released}
        return {
            "dataset": dataset.object_hash,
            "as_of": as_of.isoformat(),
            "release_id": release.pk if release is not None else None,
            "release_month": release.release_month.date().isoformat() if release is not None else None,
            "eligible_count": len(eligible),
            "released_count": len(released),
            "withheld_count": len([item for item in eligible if item.pk not in released_pks]),
            "deidentification_versions": release.deidentification_versions if release is not None else [],
            "released": [member_handle(item) for item in released],
            "withheld": [member_handle(item) for item in eligible if item.pk not in released_pks],
        }

    def _emit(self, report: dict, fmt: str, *, dry_run: bool) -> None:
        if fmt == "json":
            self.stdout.write(json.dumps({**report, "dry_run": dry_run}, indent=2))
            return
        verb = "Would release" if dry_run else "Released"
        self.stdout.write(
            f"{verb} {report['released_count']} of {report['eligible_count']} eligible members of dataset "
            f"{report['dataset']} as of {report['as_of']}"
            + (f" (release {report['release_id']}, month {report['release_month']})" if report["release_id"] else "")
        )
        for handle in report["released"]:
            self.stdout.write(f"  released  {handle}")
        for handle in report["withheld"]:
            self.stdout.write(f"  withheld  {handle}")
