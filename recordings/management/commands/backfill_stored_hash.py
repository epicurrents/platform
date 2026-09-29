"""Compute ``Recording.stored_hash`` for recordings processed before the field existed.

``stored_hash`` is the SHA-256 of the file as stored, taken at the end of ingest after the last
in-place rewrite, and the one digest the API serves. Recordings that reached READY before the
field was added carry an empty value, which the metadata panel shows as no hash at all. This
command derives it from the bytes on disk for every such recording.

Idempotent: only READY recordings with an empty ``stored_hash`` are candidates, so a second run
writes nothing. A FAILED recording is never stamped, since its file still holds the bytes as
uploaded and a digest of those would be ``file_hash`` under another name. A recording whose file
is missing is reported and skipped; ``validate_originals`` and the operator's backup are the tools
for that.

Each write is a model save inside an audited scope, so the trail records the value every
recording gained and the run that gave it.

Usage::

    python manage.py backfill_stored_hash --dry-run    # count the candidates, write nothing
    python manage.py backfill_stored_hash              # stamp every candidate
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from recordings.metadata import stored_hash_of
from recordings.models import Recording


class Command(BaseCommand):
    help = "Derive Recording.stored_hash from the stored file for READY recordings that lack one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the candidates and write nothing.",
        )

    def handle(self, *args, **options):
        from activity.models import Activity
        from activity.system_activity import with_system_activity

        dry_run = options["dry_run"]
        candidates = Recording.objects.filter(
            status=Recording.Status.READY,
            deleted_at__isnull=True,
            stored_hash="",
        ).order_by("pk")

        stamped = 0
        missing = 0
        with with_system_activity(
            "recordings.stored_hash.backfill",
            interface=Activity.Interface.COMMAND,
            metadata={"dry_run": dry_run, "candidate_count": candidates.count()},
        ):
            for recording in candidates.iterator(chunk_size=200):
                path = Path(recording.file_path)
                if not path.is_file():
                    missing += 1
                    self.stderr.write(self.style.WARNING(f"  SKIP  {recording.stored_name}: file not on disk"))
                    continue
                digest = stored_hash_of(path)
                stamped += 1
                if dry_run:
                    continue
                recording.stored_hash = digest
                recording.save(update_fields=["stored_hash", "modified_at"])

        if dry_run:
            self.stdout.write(f"{stamped} recording(s) would be stamped; {missing} skipped for a missing file.")
            return
        self.stdout.write(self.style.SUCCESS(f"Stamped {stamped} recording(s); {missing} skipped for a missing file."))
