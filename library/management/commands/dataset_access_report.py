"""Report who reads a release-gated dataset: requests and distinct readers per member over a window.

The evidence for the access-control argument in EDPB Guidelines 02/2026 ¶ 89, and the input to the
six-monthly sweep: for every recording member of the dataset, how many requests and how many
distinct authenticated readers the ``Activity`` trail shows over the window, with byte-serving
requests counted on their own, the requests that carried no account (a refused share-token caller,
or an account since erased), and the month of the last read. The dataset line adds the distinct readers across the pool, the refused requests and the
largest and median request count per reader. Counts only: no reader is named.

Archived activity rows are read as well as live ones, since the archiver's default window is
shorter than the sweep's. Listing requests are not per member and are not counted. Members that
have since been purged have no row to be listed under.

Reads only. The run is recorded as an ``Activity`` row with the window and the counts, so the trail
shows when the evidence was produced.

Usage::

    python manage.py dataset_access_report <dataset-hash>                 # the last half year
    python manage.py dataset_access_report <dataset-hash> --days 30
    python manage.py dataset_access_report <dataset-hash> --since 2026-01-01 --format json
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from activity.models import Activity
from activity.system_activity import with_system_activity
from library.release import resolve_dataset
from library.reports import access_report

#: Half a year: the cadence of the GDPR sweep in docs/gdpr-compliance.md.
DEFAULT_DAYS = 183


class Command(BaseCommand):
    """Count requests and distinct readers per member of a release-gated dataset."""

    help = "Report requests and distinct readers per member of a release-gated dataset over a window"

    def add_arguments(self, parser):
        parser.add_argument("dataset", help="Dataset hash or primary key")
        parser.add_argument(
            "--days", type=int, default=DEFAULT_DAYS, help=f"Window length in days (default {DEFAULT_DAYS})"
        )
        parser.add_argument("--since", help="Window start, YYYY-MM-DD; overrides --days")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        dataset = resolve_dataset(options["dataset"])
        if dataset is None:
            raise CommandError(f"No active dataset matches {options['dataset']!r}")
        if not dataset.release_gated:
            raise CommandError(f"Dataset {dataset.object_hash} is not release-gated")
        since = self._since(options)
        with with_system_activity(
            "library.dataset.access_report",
            interface=Activity.Interface.COMMAND,
            target=dataset,
            metadata={"since": since.isoformat(), "format": options["format"]},
        ) as activity:
            report = access_report(dataset, since=since)
            if activity is not None:
                activity.metadata.update(
                    {
                        "member_count": report["member_count"],
                        "request_count": report["requests"],
                        "reader_count": report["readers"],
                    }
                )
                activity.save(update_fields=["metadata"])
        self._emit(report, options["format"])

    @staticmethod
    def _since(options) -> datetime:
        if options.get("since"):
            try:
                day = date.fromisoformat(options["since"])
            except ValueError as exc:
                raise CommandError(f"--since must be YYYY-MM-DD, got {options['since']!r}") from exc
            return datetime.combine(day, datetime.min.time(), tzinfo=timezone.get_current_timezone())
        if options["days"] < 1:
            raise CommandError("--days must be at least 1")
        return timezone.now() - timedelta(days=options["days"])

    def _emit(self, report: dict, fmt: str) -> None:
        if fmt == "json":
            self.stdout.write(json.dumps(report, indent=2))
            return
        self.stdout.write(
            f"Dataset {report['dataset']} since {report['since'][:10]}: {report['requests']} requests "
            f"({report['byte_requests']} for bytes) by {report['readers']} readers over {report['member_count']} "
            f"members; {report['no_account']} without an account, {report['refused']} refused; "
            f"per reader max {report['reader_requests_max']}, median {report['reader_requests_median']}"
        )
        self.stdout.write(
            f"{'member':<44} {'released':<9} {'requests':>8} {'bytes':>6} {'readers':>7} {'noacct':>6} last"
        )
        for member in report["members"]:
            self.stdout.write(
                f"{member['member']:<44} {member['released'] or '-':<9} {member['requests']:>8} "
                f"{member['byte_requests']:>6} {member['readers']:>7} {member['no_account']:>6} "
                f"{member['last_read'] or '-'}"
            )
