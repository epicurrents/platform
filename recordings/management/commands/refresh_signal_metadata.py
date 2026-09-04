"""Re-derive stored signal metadata from the recordings on disk.

Repairs recordings whose ``RecordingMeta`` / ``SignalInfo`` rows describe a file that has since been
rewritten — a project reprocessing stage that changed the channel set is the case this exists for.
The drift is invisible until something sizes an EDF header from the stored channel count and reads a
truncated one; see :mod:`recordings.metadata` for what that costs.

Idempotent: a recording whose metadata already matches its file is left alone, so a sweep over an
unaffected deployment writes nothing and reports nothing.

Usage::

    python manage.py refresh_signal_metadata --dry-run     # report drift, write nothing
    python manage.py refresh_signal_metadata               # repair every drifted recording
    python manage.py refresh_signal_metadata --recording <content_hash>
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from recordings.metadata import MetadataRefreshError, refresh_signal_metadata
from recordings.models import Recording


class Command(BaseCommand):
    help = "Re-derive RecordingMeta and SignalInfo from the recording files on disk."

    def add_arguments(self, parser):
        parser.add_argument(
            "--recording",
            metavar="CONTENT_HASH",
            help="Repair a single recording instead of sweeping every eligible one.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report which recordings have drifted without writing anything.",
        )

    def handle(self, *args, **options):
        from activity.models import Activity
        from activity.system_activity import with_system_activity

        dry_run = options["dry_run"]
        content_hash = options["recording"]

        # Not filtered by status. A FAILED recording has no metadata to refresh and is reported as
        # skipped on that basis rather than excluded up front, so an operator sweeping a deployment
        # sees what was passed over instead of silently getting a smaller denominator.
        recordings = Recording.objects.filter(deleted_at__isnull=True).order_by("pk")
        if content_hash:
            recordings = recordings.filter(content_hash=content_hash)
            if not recordings.exists():
                raise CommandError(f"No active recording with content_hash {content_hash!r}.")

        checked = 0
        repaired = 0
        failed = 0
        # The scope's own knobs go in the metadata: a sweep writes no ObjectChangeLog row for the
        # recordings it left alone, so without them the trail cannot say whether a run covered one
        # recording or the whole table, nor whether it wrote at all.
        with with_system_activity(
            "recordings.metadata.refresh",
            interface=Activity.Interface.COMMAND,
            metadata={"content_hash": content_hash, "dry_run": dry_run},
        ):
            for recording in recordings:
                if (recording.file_extension or "").lower() not in (".edf", ".bdf"):
                    continue
                checked += 1
                try:
                    result = refresh_signal_metadata(recording, dry_run=dry_run)
                except MetadataRefreshError as exc:
                    failed += 1
                    self.stderr.write(self.style.WARNING(f"  SKIP  {recording.stored_name}: {exc}"))
                    continue
                if not result.changed:
                    continue
                repaired += 1
                verb = "would repair" if dry_run else "repaired"
                self.stdout.write(f"  {verb}  {recording.stored_name}: {result.summary}")

        self.stdout.write("")
        if dry_run:
            self.stdout.write(f"Checked {checked} recording(s); {repaired} would be repaired, {failed} unreadable.")
            return
        self.stdout.write(
            self.style.SUCCESS(f"Checked {checked} recording(s); repaired {repaired}, {failed} unreadable.")
        )
