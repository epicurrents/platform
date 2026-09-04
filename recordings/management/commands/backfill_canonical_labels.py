"""Backfill ``SignalInfo.canonical_label`` for recordings ingested before the
canonical-label normaliser existed.

Re-derives ``canonical_label`` from the raw ``label`` for every ``SignalInfo``
row using the same pure normaliser the ingest path uses
(``processors.channel_labels.classify_channel``), so a re-run is idempotent and
converges to what a fresh reprocess would produce for the canonical name.

Writes with ``bulk_update`` — deliberately bypassing the per-row audit signal, as
``canonical_label`` is derived, non-identifying channel metadata (mirrors the
``bulk_create`` note in ``import_recordings``).

**Only ``canonical_label`` is written, never ``signal_type``.** ``signal_type`` is
covered by the SignalInfo audit digest, so changing it retroactively would
invalidate a recording's baselined digest; the improved type inference
(``classify_channel``) therefore applies at ingest/reprocess, not here. As a
result an existing bare ``Fp1`` may gain ``canonical_label='Fp1'`` while its stored
``signal_type`` stays ``''`` until the recording is reprocessed.

Reports the count of **unclassified** channels (not annotation, no canonical name)
— the UI shows these by their raw label; the count flags non-standard montages
worth an operator's eye.

Usage::

    python manage.py backfill_canonical_labels              # apply
    python manage.py backfill_canonical_labels --dry-run    # report only, no writes
    python manage.py backfill_canonical_labels --show-unclassified
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from recordings.models import SignalInfo
from recordings.processors.channel_labels import classify_channel


class Command(BaseCommand):
    help = (
        "Re-derive SignalInfo.canonical_label from raw labels (idempotent; "
        "signal_type is never changed). Reports unclassified channels."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute and report, but write nothing.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=2000,
            help="Rows per bulk_update flush (default 2000).",
        )
        parser.add_argument(
            "--show-unclassified",
            action="store_true",
            help="List each unclassified channel (recording meta id + label).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        show_unclassified = options["show_unclassified"]

        total = 0
        changed = 0
        unclassified = 0
        pending: list[SignalInfo] = []
        unclassified_rows: list[tuple[int, str]] = []

        def flush() -> None:
            if pending and not dry_run:
                SignalInfo.objects.bulk_update(pending, ["canonical_label"])
            pending.clear()

        queryset = SignalInfo.objects.all().order_by("pk")
        for signal in queryset.iterator(chunk_size=batch_size):
            total += 1
            # Take only the canonical name; signal_type is intentionally left as
            # stored (digest-covered — see module docstring).
            _, new_value = classify_channel(signal.label, signal.signal_type)

            if not signal.is_annotation_channel and not new_value:
                unclassified += 1
                unclassified_rows.append((signal.meta_id, signal.label))

            if new_value != signal.canonical_label:
                signal.canonical_label = new_value
                pending.append(signal)
                changed += 1
                if len(pending) >= batch_size:
                    flush()
        flush()

        verb = "would update" if dry_run else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{total} SignalInfo rows scanned; {verb} {changed}; {unclassified} unclassified channels."
            )
        )
        if unclassified and show_unclassified:
            self.stdout.write("Unclassified channels (recording_meta_id: label):")
            for meta_id, label in unclassified_rows:
                self.stdout.write(f"  {meta_id}: {label!r}")
        elif unclassified:
            self.stdout.write("Re-run with --show-unclassified to list the unclassified channels.")
