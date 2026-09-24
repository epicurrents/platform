"""Report, per recording, which de-identification pass wrote the stored file and under what settings.

The artefact an operator hands to a supervisory authority or attaches to a data-sharing agreement:
EDPB Guidelines 02/2026 paragraph 41 ask the controller to document the anonymisation process
per recording, and this is that documentation as the platform holds it. For every READY
recording the report gives the pass versions stamped on its ``RecordingMeta``, whether the stored
file keeps its annotation text, and the ingest privacy overrides sealed into its READY audit row,
beside the versions and overrides the running deployment has now. A recording whose stamped
version is behind the current pass is flagged: the platform never reprocesses on its own, so an
operator who wants the current pass on such a file re-ingests it deliberately.

Three kinds of row need reading with care. A recording processed before the record existed
carries version ``0`` and no audit record, and its annotation-text flag was never stamped, so the
report says so rather than reading the column's default as a fact. A recording the platform stores
without processing — a format with no parser — has no meta row and no pass to be behind. And a
recording whose audit record disagrees with its meta row has been edited after ingest; the report
prints both and leaves the conclusion to the reader.

Each row also repeats ``Recording.public_source``, the DOI or URL of the published dataset the
author says the data was taken from, so the reader can tell a recording acquired here from one
whose original is already public (paragraph 26); the platform asserts nothing about the locator.

Reads only. The run is recorded as an ``Activity`` row so the trail shows when a report was
produced, which is part of what paragraph 41 asks to be kept.

Usage::

    python manage.py deidentification_report                     # every READY recording
    python manage.py deidentification_report --recording <hash>  # one, by its URL hash
    python manage.py deidentification_report --format json       # machine-readable
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from recordings.deidentification_record import (
    INGEST_OVERRIDE_SETTINGS,
    current_ingest_overrides,
    current_pass_versions,
    stored_deidentification_records,
)
from recordings.models import Recording, RecordingMeta

_TEXT_STATES = {None: "unknown", True: "preserved", False: "stripped"}


def _hash_of(recording: Recording) -> str:
    return recording.stored_name.split(".", 1)[0]


def _overrides_abbrev(overrides: dict | None) -> str:
    """Print the four override flags as ``name=T/F`` pairs, or a dash when nothing was recorded."""
    if not overrides:
        return "-"
    return " ".join(f"{name}={'T' if overrides.get(name) else 'F'}" for name in INGEST_OVERRIDE_SETTINGS)


def report_row(
    recording: Recording, meta: RecordingMeta | None, audit_record: dict | None, current: dict[str, int]
) -> dict:
    """Assemble one recording's entry of the report."""
    row: dict = {
        "hash": _hash_of(recording),
        "public_source": recording.public_source or None,
        "deidentification_version": None,
        "channel_order_version": None,
        "annotation_text_preserved": None,
        "audit_record": audit_record,
        "behind": False,
    }
    if meta is None:
        return row
    stamped = meta.deidentification_version
    row["deidentification_version"] = stamped
    row["channel_order_version"] = meta.channel_order_version
    # The flag was stamped together with the version, so on a pre-record row
    # the column holds its default rather than a decision.
    row["annotation_text_preserved"] = meta.annotation_text_preserved if stamped else None
    row["behind"] = (
        stamped < current["deidentification_version"] or meta.channel_order_version < current["channel_order_version"]
    )
    return row


class Command(BaseCommand):
    help = "Report which de-identification pass wrote each stored recording and the ingest settings recorded."

    def add_arguments(self, parser):
        parser.add_argument(
            "--recording",
            metavar="HASH",
            help="Report one recording, by the 32-character hash in its URL.",
        )
        parser.add_argument(
            "--format",
            choices=("text", "json"),
            default="text",
            help="Output format (default: text).",
        )

    def handle(self, *args, **options):
        from django.contrib.contenttypes.models import ContentType

        from activity.models import Activity
        from activity.system_activity import with_system_activity

        recordings = Recording.objects.filter(status=Recording.Status.READY, deleted_at__isnull=True).order_by("pk")
        wanted = (options["recording"] or "").strip().upper()
        if wanted:
            if len(wanted) != 32 or not wanted.isalnum():
                raise CommandError("--recording takes the 32-character hash from the recording's URL.")
            recordings = recordings.filter(stored_name__startswith=f"{wanted}.")
            if not recordings.exists():
                raise CommandError(f"No READY recording with hash {wanted}.")

        current = current_pass_versions()
        overrides_now = current_ingest_overrides()
        recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        # Two batch reads rather than two queries per recording: every meta row
        # for the recording type, and the newest sealed record per recording.
        metas = {m.object_id: m for m in RecordingMeta.objects.filter(content_type=recording_ct)}
        audit_records = stored_deidentification_records()

        rows: list[dict] = []
        single = recordings.first() if wanted else None
        with with_system_activity(
            "recordings.deidentification_report",
            interface=Activity.Interface.COMMAND,
            target=single,
            metadata={"format": options["format"], "recording_count": recordings.count()},
        ):
            for recording in recordings.iterator(chunk_size=200):
                key = str(recording.pk)
                rows.append(report_row(recording, metas.get(key), audit_records.get(key), current))

        report = {
            "generated_at": timezone.now().isoformat(),
            "current": {**current, "ingest_overrides": overrides_now},
            "recordings": rows,
            "behind_count": sum(1 for r in rows if r["behind"]),
            "public_source_count": sum(1 for r in rows if r["public_source"]),
        }
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2))
            return
        self._write_text(report)

    def _write_text(self, report: dict) -> None:
        current = report["current"]
        self.stdout.write(f"De-identification report, {report['generated_at']}")
        self.stdout.write(
            f"Current pass: de-identification v{current['deidentification_version']}, "
            f"channel order v{current['channel_order_version']}"
        )
        self.stdout.write(f"Ingest overrides now: {_overrides_abbrev(current['ingest_overrides'])}")
        self.stdout.write("")
        self.stdout.write(
            f"{'HASH':32}  {'PASS':>4}  {'ORDER':>5}  {'ANNOTATION TEXT':15}  {'RECORDED OVERRIDES':60}  PUBLIC SOURCE"
        )
        unstamped = 0
        unprocessed = 0
        for row in report["recordings"]:
            source = row["public_source"] or "-"
            if row["deidentification_version"] is None:
                unprocessed += 1
                self.stdout.write(f"{row['hash']:32}  {'-':>4}  {'-':>5}  {'no pass applies':15}  {'-':60}  {source}")
                continue
            if row["deidentification_version"] == 0:
                unstamped += 1
            record = row["audit_record"] or {}
            flag = "behind " if row["behind"] else ""
            overrides = f"{flag}{_overrides_abbrev(record.get('ingest_overrides'))}"
            self.stdout.write(
                f"{row['hash']:32}  {row['deidentification_version']:>4}  {row['channel_order_version']:>5}  "
                f"{_TEXT_STATES[row['annotation_text_preserved']]:15}  {overrides:60}  {source}"
            )
        self.stdout.write("")
        summary = (
            f"{len(report['recordings'])} recording(s); {report['behind_count']} behind the current pass; "
            f"{unstamped} processed before the record existed; {unprocessed} stored without a pass; "
            f"{report['public_source_count']} from a published source."
        )
        if report["behind_count"]:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
