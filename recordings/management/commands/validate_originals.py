"""Cross-check the originals preservation volume against the recordings database.

Reports three classes of mismatch between the host-controlled originals volume
(``RECORDINGS_ORIGINALS_PATH``) and the live ``Recording`` table:

* **Orphans** — directories on disk with no matching ``Recording`` row.  Most
  commonly produced when a recording is purged from the platform; the
  operator decides whether to keep, archive, or remove the directory.
* **Missing** — ``Recording`` rows the current preservation tier *should*
  have written but the volume does not contain.  Useful for spotting
  recordings that processed before preservation was switched on, or that
  failed to write for some reason (volume full, permission denied, etc.).
* **Size mismatches** — the preserved file's on-disk size does not match
  the size recorded in ``manifest.json``.  Indicates external mutation
  of the volume (the platform never rewrites preserved files).

The command is **strictly read-only and metadata-only** — it stats files
and reads ``manifest.json`` but never opens preserved file contents.  This
preserves the "the platform never reads the originals volume" invariant.

Usage::

    python manage.py validate_originals                 # human-readable report
    python manage.py validate_originals --json          # machine-readable
    python manage.py validate_originals --no-size-check # skip stat() pass

Pair with ``--expect-tier`` when the current setting differs from the tier
that was active when the recordings were processed (e.g. when auditing a
volume after a mode switch).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from recordings.models import Recording
from recordings.preservation import (
    MODE_ALL,
    MODE_FAILED,
    MODE_NONE,
    VALID_PRESERVE_MODES,
)


class Command(BaseCommand):
    help = "Validate the recordings originals volume against the DB. Read-only and metadata-only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="emit_json",
            help="Emit a JSON report on stdout instead of the human-readable summary.",
        )
        parser.add_argument(
            "--no-size-check",
            action="store_true",
            dest="skip_size",
            help="Skip the on-disk vs. manifest size comparison.",
        )
        parser.add_argument(
            "--expect-tier",
            choices=sorted(VALID_PRESERVE_MODES),
            default=None,
            help=(
                "Override the assumed preservation tier when computing the "
                "'missing' set.  Defaults to the current "
                "RECORDINGS_PRESERVE_MODE."
            ),
        )

    def handle(self, *args, **options):
        originals_path = getattr(settings, "RECORDINGS_ORIGINALS_PATH", None)
        if not originals_path:
            raise CommandError("RECORDINGS_ORIGINALS_PATH is not configured. Nothing to validate.")
        originals_root = Path(originals_path)
        if not originals_root.exists() or not originals_root.is_dir():
            raise CommandError(f"RECORDINGS_ORIGINALS_PATH does not exist or is not a directory: {originals_root}")

        expect_tier = options["expect_tier"] or getattr(settings, "RECORDINGS_PRESERVE_MODE", MODE_NONE)

        report = _build_report(
            originals_root=originals_root,
            expect_tier=expect_tier,
            check_size=not options["skip_size"],
        )

        if options["emit_json"]:
            # Pure JSON to stdout — any trailing human-readable warning text
            # would corrupt the output for consumers piping it into ``jq``
            # or another JSON parser.
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            self._render_text(report)

        # Non-zero exit when any issue is reported so cron / CI pickups can
        # branch.  The human-readable warning goes to stderr so it doesn't
        # interleave with JSON output on stdout.
        has_issues = (
            report["orphans"]
            or report["missing"]
            or report["size_mismatches"]
            or report["malformed"]
            or report.get("soft_deleted_preserved", [])
        )
        if has_issues:
            if not options["emit_json"]:
                self.stderr.write("")
                self.stderr.write(self.style.WARNING("validate_originals: issues found."))
            sys.exit(1)

    def _render_text(self, report: dict) -> None:
        write = self.stdout.write
        style = self.style
        write(f"Originals volume: {report['originals_path']}")
        write(f"Current preservation mode: {report['current_mode']}  (expecting tier: {report['expected_tier']})")
        write(
            f"Directories on disk: {report['directory_count']}  "
            f"Active recordings in DB: {report['active_recording_count']}"
        )
        write("")

        self._render_group(
            "Orphans (on disk, no matching Recording row)",
            report["orphans"],
            keys=("dir", "stored_name_prefix"),
            empty_label="(none — every preserved directory maps to a live row)",
        )
        self._render_group(
            "Soft-deleted but preserved (Recording.deleted_at set; volume retained per policy)",
            report["soft_deleted_preserved"],
            keys=("recording_pk", "stored_name", "dir"),
            empty_label="(none — no preserved directories point at trashed recordings)",
        )
        self._render_group(
            "Missing (DB rows the expected tier should preserve, but no directory)",
            report["missing"],
            keys=("recording_pk", "stored_name", "status"),
            empty_label="(none — every expected row has a directory)",
        )
        self._render_group(
            "Size mismatches (on-disk size != manifest)",
            report["size_mismatches"],
            keys=("dir", "manifest_size", "on_disk_size"),
            empty_label="(none — every preserved file matches its manifest)",
        )
        self._render_group(
            "Malformed directories (missing manifest or file)",
            report["malformed"],
            keys=("dir", "reason"),
            empty_label="(none — every directory carries a manifest and a file)",
        )

        if not any(
            report[k]
            for k in (
                "orphans",
                "soft_deleted_preserved",
                "missing",
                "size_mismatches",
                "malformed",
            )
        ):
            write(style.SUCCESS("validate_originals: clean."))

    def _render_group(self, title: str, rows: list[dict], *, keys: tuple, empty_label: str) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(f"== {title}: {len(rows)}"))
        if not rows:
            self.stdout.write(f"  {empty_label}")
            self.stdout.write("")
            return
        for row in rows:
            parts = [f"{k}={row.get(k)}" for k in keys]
            self.stdout.write("  - " + "  ".join(parts))
        self.stdout.write("")


# ────────────────────────────────────────────────────────────────────────────
# Report building (testable in isolation)
# ────────────────────────────────────────────────────────────────────────────


def _stored_prefix(stored_name: str) -> str:
    """Return the 32-hex prefix used as the per-recording directory name."""
    return (stored_name or "").split(".", 1)[0]


def _build_report(
    *,
    originals_root: Path,
    expect_tier: str,
    check_size: bool,
) -> dict:
    """Assemble the validation report.  No filesystem writes, no content reads."""
    on_disk_dirs: dict[str, Path] = {}
    malformed: list[dict] = []
    size_mismatches: list[dict] = []

    for entry in sorted(originals_root.iterdir()):
        if not entry.is_dir():
            # Stray files at the volume root are reported as malformed for
            # operator attention.
            malformed.append({"dir": str(entry), "reason": "not_a_directory"})
            continue

        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            malformed.append({"dir": str(entry), "reason": "manifest_missing"})
            continue

        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            malformed.append({"dir": str(entry), "reason": f"manifest_unreadable: {exc}"})
            continue

        # The directory name should match the manifest's stored_name prefix —
        # if not, treat as malformed (operator-managed rename or copy mistake).
        manifest_prefix = _stored_prefix(manifest.get("stored_name", ""))
        if entry.name != manifest_prefix:
            malformed.append(
                {
                    "dir": str(entry),
                    "reason": (f"directory_name_mismatch (dir={entry.name}, manifest={manifest_prefix})"),
                }
            )
            continue

        # A directory with a manifest but no data file is malformed even when
        # the size check is disabled.  Locate the data file here so missing-
        # data and size-mismatch checks share one filesystem pass.
        data_file = _locate_data_file(entry)
        if data_file is None:
            malformed.append({"dir": str(entry), "reason": "data_file_missing"})
            continue

        on_disk_dirs[manifest_prefix] = entry

        if check_size:
            mismatch = _check_size(data_file, manifest)
            if mismatch is not None:
                size_mismatches.append({"dir": str(entry), **mismatch})

    # Map DB rows by stored_name prefix for fast intersection.  Include
    # soft-deleted rows so the cross-check can distinguish a recording that
    # was trashed (volume retained per policy) from a true orphan.
    all_recordings = Recording.objects.all().only("id", "stored_name", "status", "deleted_at")
    db_by_prefix = {_stored_prefix(r.stored_name): r for r in all_recordings}
    active_db_by_prefix = {p: r for p, r in db_by_prefix.items() if r.deleted_at is None}

    orphans = []
    soft_deleted_preserved = []
    for prefix, path in on_disk_dirs.items():
        row = db_by_prefix.get(prefix)
        if row is None:
            orphans.append({"dir": str(path), "stored_name_prefix": prefix})
        elif row.deleted_at is not None:
            soft_deleted_preserved.append(
                {
                    "recording_pk": row.pk,
                    "stored_name": row.stored_name,
                    "dir": str(path),
                }
            )

    expected_prefixes = _expected_prefixes(active_db_by_prefix, expect_tier)
    missing = [
        {
            "recording_pk": active_db_by_prefix[p].pk,
            "stored_name": active_db_by_prefix[p].stored_name,
            "status": active_db_by_prefix[p].status,
        }
        for p in expected_prefixes
        if p not in on_disk_dirs
    ]

    return {
        "originals_path": str(originals_root),
        "current_mode": getattr(settings, "RECORDINGS_PRESERVE_MODE", MODE_NONE),
        "expected_tier": expect_tier,
        "directory_count": len(on_disk_dirs),
        "active_recording_count": len(active_db_by_prefix),
        "orphans": orphans,
        "soft_deleted_preserved": soft_deleted_preserved,
        "missing": missing,
        "size_mismatches": size_mismatches,
        "malformed": malformed,
    }


def _locate_data_file(target_dir: Path) -> Path | None:
    """Return the single non-manifest file in *target_dir*, or None.

    Used both as a malformed-directory check (missing data file is treated
    as malformed) and as the input for the size cross-check.
    """
    for entry in target_dir.iterdir():
        if entry.name == "manifest.json" or not entry.is_file():
            continue
        return entry
    return None


def _check_size(data_file: Path, manifest: dict) -> dict | None:
    """Compare *data_file*'s on-disk size against the manifest's recorded size.

    Returns a mismatch dict (without the ``dir`` key — the caller fills that
    in) when ``Path.stat().st_size`` differs from ``manifest["file_size"]``;
    None when sizes match or the manifest carries no ``file_size``.
    """
    expected = manifest.get("file_size")
    if expected is None:
        return None
    actual = data_file.stat().st_size
    if actual != expected:
        return {"manifest_size": expected, "on_disk_size": actual}
    return None


def _expected_prefixes(db_by_prefix: dict[str, Recording], expect_tier: str) -> set[str]:
    """Return the set of stored_name prefixes the *expect_tier* should preserve.

    Mode ``"none"`` expects nothing.  Mode ``"failed"`` expects every FAILED
    row.  Mode ``"all"`` expects every active row except those still in the
    PENDING / PROCESSING transition (which the worker has not yet written).
    """
    if expect_tier == MODE_NONE:
        return set()
    if expect_tier == MODE_FAILED:
        return {p for p, r in db_by_prefix.items() if r.status == Recording.Status.FAILED}
    if expect_tier == MODE_ALL:
        transient = {Recording.Status.PENDING, Recording.Status.PROCESSING}
        return {p for p, r in db_by_prefix.items() if r.status not in transient}
    return set()
