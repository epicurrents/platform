"""Run the audit-trail integrity check from a shell, or as a maintenance job.

The same walk the ``verify_audit_integrity`` beat task performs every day:
every chain shard is verified and the derived-state digests of the recent
window recomputed. Anomalies are emitted as security events as they are found;
the command prints the summary and exits non-zero when there was any, so a
maintenance job running it reads as failed rather than as a clean pass with a
number to inspect.
"""

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verify the audit-trail chains and derived-state digests; non-zero exit on any anomaly."

    def add_arguments(self, parser):
        parser.add_argument(
            "--derived-window-days",
            type=int,
            default=None,
            help="Days of derived-state rows to recompute (0 skips the phase). Default: ACTIVITY_DERIVED_CHECK_WINDOW_DAYS.",
        )

    def handle(self, *args, **options):
        from activity.integrity_check import run_integrity_check

        summary = run_integrity_check(derived_window_days=options["derived_window_days"])
        self.stdout.write(json.dumps(summary, indent=2, sort_keys=True, default=str))
        anomalies = (
            summary["chain_breaks"]
            + len(summary["chain_gaps"])
            + summary["genesis_invalid"]
            + summary["key_missing"]
            + summary["derived_mismatches"]
        )
        if anomalies:
            raise CommandError(f"{anomalies} anomalies found in the audit trail")
        self.stdout.write(self.style.SUCCESS("Audit trail verified: no anomalies."))
