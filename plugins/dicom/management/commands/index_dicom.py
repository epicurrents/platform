"""Management command: index_dicom

Scans a filesystem directory for DICOM files and imports them into the
database. Designed for bulk onboarding of existing DICOM archives —
production uploads go through the API endpoint. Both paths share the
parse/persist logic in ``plugins/dicom/ingest.py``, so the resulting rows are
identical.

Usage
-----
    python manage.py index_dicom /path/to/dicom/archive/ --user admin

Options
-------
--user      Username of the Epicurrents user who will own the imported studies.
            Required.
--dry-run   Print discovered files without creating any DB rows or copying
            anything.
--resume    Skip files whose content hash (SHA-256) already appears on one of
            the owning user's instances, so a crashed run can be continued
            without duplicating imports.

The command recursively walks the given directory, treating every file with
the ``.dcm`` extension *or* no extension *or* a ``DICM`` magic byte sequence
as a candidate DICOM file. Non-DICOM candidates are reported and skipped.
"""

import os
import shutil
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = "Bulk-index DICOM files from a directory into the platform."

    def add_arguments(self, parser):
        parser.add_argument(
            "directory",
            help="Path to the directory (searched recursively) containing DICOM files.",
        )
        parser.add_argument(
            "--user",
            required=True,
            help="Username of the Epicurrents user who will own the imported studies.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Discover files and print counts without importing anything.",
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Skip files whose SHA-256 already appears on the user's instances.",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        from activity.models import Activity
        from activity.system_activity import with_system_activity
        from plugins.dicom.ingest import (
            MissingUidsError,
            parse_dicom_header,
            persist_instance,
            refresh_study_aggregates,
            required_uids,
            sha256_file,
        )
        from plugins.dicom.models import DicomInstance

        src_dir = os.path.realpath(options["directory"])
        if not os.path.isdir(src_dir):
            raise CommandError(f"Directory not found: {src_dir}")

        try:
            user = User.objects.get(username=options["user"])
        except User.DoesNotExist:
            raise CommandError(f"User '{options['user']}' not found.")

        dry_run = options["dry_run"]
        resume = options["resume"]

        upload_path = getattr(settings, "DICOM_UPLOAD_PATH", "/data/dicom")
        os.makedirs(upload_path, exist_ok=True)

        # Collect candidate files.
        candidates = []
        for dirpath, _, filenames in os.walk(src_dir):
            for fname in sorted(filenames):
                ext = os.path.splitext(fname)[1].lower()
                if ext in (".dcm", "") or _has_dicom_magic(os.path.join(dirpath, fname)):
                    candidates.append(os.path.join(dirpath, fname))

        self.stdout.write(f"Found {len(candidates)} candidate file(s) in {src_dir}.")
        if dry_run:
            self.stdout.write(self.style.SUCCESS("Dry run — no files imported."))
            return

        # Content hashes already imported for this user, for --resume support.
        existing_hashes: set[str] = set()
        if resume:
            existing_hashes = set(
                DicomInstance.objects.filter(series__study__author=user).values_list("file_hash", flat=True)
            )
            existing_hashes.discard("")

        imported = 0
        skipped = 0
        failed = 0
        duplicates = 0
        touched_studies: dict = {}

        # The source directory is not recorded. Directories holding clinical
        # exports are routinely named after what was exported, and `Activity`
        # metadata is permanent — `erase_subject` reaches it only on rows
        # targeting the user model, which this is not. `file_count` says how
        # large the run was without saying where it came from. See AGENTS.md →
        # *Activity metadata carries identifiers, never names*.
        with with_system_activity(
            "dicom.import",
            interface=Activity.Interface.COMMAND,
            metadata={"file_count": len(candidates), "user_id": user.pk},
        ):
            for src_path in candidates:
                file_hash = sha256_file(src_path)
                if resume and file_hash in existing_hashes:
                    skipped += 1
                    continue

                try:
                    ds = parse_dicom_header(src_path)
                    required_uids(ds)
                except MissingUidsError as exc:
                    self.stderr.write(f"  SKIP (invalid): {src_path} — {exc}")
                    failed += 1
                    continue
                except Exception as exc:
                    self.stderr.write(f"  SKIP (not DICOM): {src_path} — {exc}")
                    failed += 1
                    continue

                stored_name = f"{uuid.uuid4().hex}.dcm"
                dst_path = os.path.join(upload_path, stored_name)
                try:
                    shutil.copy2(src_path, dst_path)
                except OSError as exc:
                    self.stderr.write(f"  SKIP (copy failed): {src_path} — {exc}")
                    failed += 1
                    continue

                try:
                    with transaction.atomic():
                        result = persist_instance(
                            author=user,
                            ds=ds,
                            stored_name=stored_name,
                            file_size=os.path.getsize(dst_path),
                            file_hash=file_hash,
                            status=DicomInstance.Status.READY,
                        )
                except Exception as exc:
                    self.stderr.write(f"  SKIP (db error): {src_path} — {exc}")
                    _unlink_quietly(dst_path)
                    failed += 1
                    continue

                if result.outcome == "duplicate":
                    _unlink_quietly(dst_path)
                    duplicates += 1
                    continue
                if result.previous_stored_name:
                    _unlink_quietly(os.path.join(upload_path, result.previous_stored_name))
                touched_studies[result.study.pk] = result.study
                existing_hashes.add(file_hash)
                imported += 1

            for study in touched_studies.values():
                refresh_study_aggregates(study)

        self.stdout.write(
            f"Imported {imported} file(s) into {len(touched_studies)} study/-ies; "
            f"skipped {skipped} (resume), {duplicates} duplicate(s), {failed} failed."
        )
        self.stdout.write(self.style.SUCCESS("Done."))


def _unlink_quietly(path: str) -> None:
    """Remove *path*, ignoring a missing file."""
    try:
        os.remove(path)
    except OSError:
        pass


def _has_dicom_magic(path: str) -> bool:
    """Return True if the file starts with the DICOM preamble magic bytes."""
    try:
        with open(path, "rb") as f:
            preamble = f.read(132)
        return preamble[128:132] == b"DICM"
    except OSError:
        return False
