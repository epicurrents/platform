"""Management command: import EDF/BDF files from a local directory.

Usage
-----
::

    python manage.py import_recordings <source_path> --username <owner>
        [--pipeline import]
        [--structure recursive|recursive-flat|flat]
        [--reprocess]
        [--resume | --discard]

``source_path``
    Directory containing EDF/BDF files (and optional ``.json`` sidecars).

``--username``
    Username of the user who will own all imported recordings.  The user must
    already exist.

``--pipeline``
    Named pipeline label defined in ``RECORDING_PIPELINES`` or one of the
    built-ins (``"web"``, ``"import"``).  Defaults to ``"import"``.

``--structure``
    How subdirectories are handled:

    ``recursive`` *(default)*
        Scan recursively; mirror the directory tree as a ``Collection``
        hierarchy owned by the import user.
    ``recursive-flat``
        Scan recursively but do not create any ``Collection`` objects.
    ``flat``
        Only process files in the top-level directory.

``--reprocess``
    Re-process files already marked ``DONE`` in the current (resumed) job.
    Skipped by default.

``--resume`` / ``--discard``
    Required when an ``IN_PROGRESS`` job already exists.  ``--resume``
    continues from where the last run stopped; ``--discard`` marks the old
    job ``ABORTED`` and starts a fresh one (the already-copied files are
    *not* deleted).

Progress
--------
Each file's outcome is persisted in :class:`ImportJobFile` so that interrupted
imports can be resumed.  Only one job may be ``IN_PROGRESS`` at a time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

_EDF_EXTENSIONS = {".edf", ".bdf"}
# Extensions that require conversion to EDF before processing.
_CONVERTIBLE_EXTENSIONS = {".e"}
_HANDLED_EXTENSIONS = _EDF_EXTENSIONS | _CONVERTIBLE_EXTENSIONS


class Command(BaseCommand):
    help = "Import EDF/BDF (and convertible, e.g. Nicolet .e) files from a directory into Epicurrents."

    def add_arguments(self, parser):
        parser.add_argument(
            "source_path",
            help="Path to the directory containing EDF/BDF files.",
        )
        parser.add_argument(
            "--username",
            required=True,
            help="Username of the user who will own all imported recordings.",
        )
        parser.add_argument(
            "--pipeline",
            default="import",
            help='Pipeline label to use for processing (default: "import").',
        )
        parser.add_argument(
            "--structure",
            choices=["recursive", "recursive-flat", "flat"],
            default="recursive",
            help=(
                "Directory scanning mode: 'recursive' (default) mirrors the "
                "directory tree as Collections, 'recursive-flat' scans all "
                "subdirectories without creating Collections, 'flat' only "
                "processes the top-level directory."
            ),
        )
        parser.add_argument(
            "--reprocess",
            action="store_true",
            help="Re-process files already marked as done in the resumed job.",
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Resume the existing unfinished import job.",
        )
        parser.add_argument(
            "--discard",
            action="store_true",
            help=("Discard the existing unfinished import job and start fresh. Already-copied files are kept."),
        )
        parser.add_argument(
            "--preserve-annotations",
            action="store_true",
            help=(
                "Keep original EDF+/BDF+ annotation text in the stored file. "
                "By default annotations are stripped from the file at ingest "
                "(they are always saved to the database regardless)."
            ),
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model

        from recordings.models import ImportJob
        from recordings.pipelines import get_pipeline

        source_path = Path(options["source_path"]).resolve()
        if not source_path.is_dir():
            raise CommandError(f"Source path does not exist or is not a directory: {source_path}")

        User = get_user_model()
        try:
            owner = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError(f"User not found: {options['username']!r}")

        try:
            pipeline = get_pipeline(options["pipeline"])
        except ValueError as exc:
            raise CommandError(str(exc))
        if options["preserve_annotations"]:
            # Refused rather than ignored: a bulk import that silently stripped
            # what the operator asked to keep would be discovered, if at all,
            # only by opening a stored file much later.
            if not getattr(settings, "RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS", True):
                raise CommandError(
                    "--preserve-annotations is not permitted on this deployment "
                    "(RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS is off)."
                )
            pipeline.header.strip_annotation_text = False

        structure_map = {
            "recursive": ImportJob.Structure.RECURSIVE,
            "recursive-flat": ImportJob.Structure.RECURSIVE_FLAT,
            "flat": ImportJob.Structure.FLAT,
        }
        structure = structure_map[options["structure"]]

        if options["resume"] and options["discard"]:
            raise CommandError("--resume and --discard are mutually exclusive.")

        # ── Check for an existing unfinished job ──────────────────────────────
        existing = ImportJob.objects.filter(status=ImportJob.Status.IN_PROGRESS).first()
        if existing:
            if not options["resume"] and not options["discard"]:
                raise CommandError(
                    f"There is an unfinished import job (id={existing.pk}, "
                    f"started={existing.created_at.isoformat()}, "
                    f"source={existing.source_path!r}). "
                    "Use --resume to continue it or --discard to start a new one."
                )
            if options["discard"]:
                existing.status = ImportJob.Status.ABORTED
                existing.save(update_fields=["status"])
                self.stdout.write(f"Discarded job {existing.pk}.")
                existing = None

        if existing and options["resume"]:
            job = existing
            self.stdout.write(f"Resuming job {job.pk} (source={job.source_path!r}, pipeline={job.pipeline_label!r}).")
        else:
            job = ImportJob.objects.create(
                owner=owner,
                source_path=str(source_path),
                pipeline_label=options["pipeline"],
                structure=structure,
            )
            self.stdout.write(f"Created import job {job.pk}.")

        self._run_job(job, source_path, pipeline, owner, options["reprocess"])

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    def _run_job(self, job, source_path, pipeline, owner, reprocess: bool) -> None:
        from activity.models import Activity
        from activity.system_activity import with_system_activity

        # Identifiers, not names, and no source path. `erase_subject` reaches
        # `Activity` rows only where `target_content_type` is the user model, so
        # anything personal in the metadata of a row targeting something else —
        # this one targets the job — survives an Art. 17 erasure permanently.
        # The pk stays meaningful only while the account exists, which is the
        # property that makes it safe here; the path is recoverable from
        # `job.source_path` on the live row, where deleting the job removes it.
        with with_system_activity(
            "recordings.import",
            interface=Activity.Interface.COMMAND,
            target=job,
            metadata={
                "structure": job.structure,
                "owner_id": owner.pk,
                "reprocess": bool(reprocess),
            },
        ):
            self._run_job_body(job, source_path, pipeline, owner, reprocess)

    def _run_job_body(self, job, source_path, pipeline, owner, reprocess: bool) -> None:
        from recordings.models import ImportJob, ImportJobFile

        edf_files = self._collect_files(source_path, job.structure)
        self.stdout.write(f"Found {len(edf_files)} file(s) to import.")

        # Ensure an ImportJobFile row exists for every discovered file.
        # `bulk_create` deliberately bypasses the audit signal — ImportJobFile
        # is auxiliary progress tracking, not user data, and per-row chain
        # entries here would inflate ObjectChangeLog without analytical
        # benefit. The parent ImportJob row carries the file count via
        # `job.files.count()`, and per-file outcome lands as a signal-driven
        # MODIFY on the ImportJobFile row when its `status` is updated below.
        existing_paths = set(job.files.values_list("relative_path", flat=True))
        new_rows = []
        for p in edf_files:
            rel = str(p.relative_to(source_path))
            if rel not in existing_paths:
                new_rows.append(ImportJobFile(job=job, relative_path=rel))
        if new_rows:
            ImportJobFile.objects.bulk_create(new_rows)

        # Build collection tree once (only for recursive mode).
        collection_map: dict = {}
        if job.structure == ImportJob.Structure.RECURSIVE:
            collection_map = self._build_collection_tree(source_path, edf_files, owner)

        done = skipped = failed = 0

        for job_file in job.files.order_by("pk"):
            if job_file.status == ImportJobFile.Status.DONE and not reprocess:
                skipped += 1
                continue

            abs_path = source_path / job_file.relative_path
            if not abs_path.exists():
                job_file.status = ImportJobFile.Status.FAILED
                job_file.error = "File not found at import time."
                job_file.processed_at = timezone.now()
                job_file.save(update_fields=["status", "error", "processed_at"])
                failed += 1
                self.stdout.write(self.style.WARNING(f"  MISSING  {job_file.relative_path}"))
                continue

            try:
                recording = self._process_file(abs_path, job, source_path, owner, pipeline, collection_map)
                job_file.status = ImportJobFile.Status.DONE
                job_file.recording = recording
                job_file.error = ""
                job_file.processed_at = timezone.now()
                job_file.save(update_fields=["status", "recording", "error", "processed_at"])
                done += 1
                self.stdout.write(f"  OK       {job_file.relative_path}")
            except Exception as exc:
                job_file.status = ImportJobFile.Status.FAILED
                job_file.error = str(exc)
                job_file.processed_at = timezone.now()
                job_file.save(update_fields=["status", "error", "processed_at"])
                failed += 1
                self.stdout.write(self.style.ERROR(f"  FAILED   {job_file.relative_path}: {exc}"))
                logger.exception("import_recordings: failed to process %s", abs_path)

        job.status = ImportJob.Status.COMPLETED
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "completed_at"])

        self.stdout.write(
            self.style.SUCCESS(f"\nJob {job.pk} complete: {done} imported, {skipped} skipped, {failed} failed.")
        )

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def _collect_files(self, source_path: Path, structure: str) -> list[Path]:
        from recordings.models import ImportJob

        if structure == ImportJob.Structure.FLAT:
            files = [p for p in source_path.iterdir() if p.is_file() and p.suffix.lower() in _HANDLED_EXTENSIONS]
        else:
            files = [p for p in source_path.rglob("*") if p.is_file() and p.suffix.lower() in _HANDLED_EXTENSIONS]
        return sorted(files)

    # ------------------------------------------------------------------
    # Collection tree
    # ------------------------------------------------------------------

    def _build_collection_tree(self, source_path: Path, edf_files: list[Path], owner) -> dict[Path, object]:
        """Return a mapping of absolute directory path → Collection.

        Collections are created (or reused if they already exist) for every
        directory between ``source_path`` and each EDF file's parent.
        """
        from library.models import Collection

        collection_map: dict[Path, object] = {}

        for abs_path in edf_files:
            rel = abs_path.parent.relative_to(source_path)
            if not rel.parts:
                continue  # file is directly in source_path — no collection needed

            current_abs = source_path
            current_parent = None
            for part in rel.parts:
                current_abs = current_abs / part
                if current_abs in collection_map:
                    current_parent = collection_map[current_abs]
                    continue
                # get_or_create keyed on (author, name, parent, not-deleted).
                # If multiple non-deleted collections with the same name exist
                # under the same parent we reuse the first one found.
                coll = Collection.objects.filter(
                    author=owner,
                    name=part,
                    parent=current_parent,
                    deleted_at__isnull=True,
                ).first()
                if coll is None:
                    coll = Collection.objects.create(
                        author=owner,
                        name=part,
                        parent=current_parent,
                    )
                collection_map[current_abs] = coll
                current_parent = coll

        return collection_map

    # ------------------------------------------------------------------
    # Single-file processing
    # ------------------------------------------------------------------

    def _process_file(
        self,
        abs_path: Path,
        job,
        source_path: Path,
        owner,
        pipeline,
        collection_map: dict,
    ):
        from django.conf import settings
        from django.contrib.contenttypes.models import ContentType

        from activity.audit import serialize_instance
        from annotations.models import Annotation
        from epicurrents.models import AccessRight
        from epicurrents.system_user import get_system_user
        from library.models import CollectionItem
        from recordings.converters.sidecar import save_sidecar_events
        from recordings.models import ImportJob, Recording, stored_original_name
        from recordings.processors.edf import process_edf_file
        from recordings.tasks import (
            _annotation_hash,
            _determine_modality,
            _save_edf_results,
            _write_final_recording_transition,
        )

        # ── Compute source file hash ──────────────────────────────────────────
        hasher = hashlib.sha256()
        with abs_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                hasher.update(chunk)
        file_hash = hasher.hexdigest()

        # ── Pre-conversion (non-EDF formats) ──────────────────────────────────
        import tempfile as _tempfile

        from recordings.pipelines import get_converter

        suffix = abs_path.suffix.lower()
        # source_for_edf is what gets copied to permanent storage; may be
        # replaced by the converted EDF when a converter is registered.
        source_for_edf = abs_path
        original_name_for_db = abs_path.name
        sidecar_data_from_converter: dict | None = None

        if suffix not in _EDF_EXTENSIONS:
            converter = get_converter(suffix)
            if converter is None:
                raise ValueError(f"No converter registered for {suffix!r} files and it is not a native EDF/BDF file.")
            convert_tmp = Path(_tempfile.mkdtemp(prefix="epicurrents_import_convert_"))
            try:
                convert_result = converter(abs_path, convert_tmp)
            except Exception:
                shutil.rmtree(convert_tmp, ignore_errors=True)
                raise

            if isinstance(convert_result, tuple):
                converted_edf, sidecar_data_from_converter = convert_result
            else:
                converted_edf, sidecar_data_from_converter = convert_result, None

            source_for_edf = converted_edf
            suffix = converted_edf.suffix.lower()
            # Rewrite the original name with the EDF extension so filenames
            # shown in the UI are consistent with the stored format.
            original_name_for_db = abs_path.stem + suffix

            # Recompute hash from the converted EDF (this is what gets stored).
            edf_hasher = hashlib.sha256()
            with converted_edf.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    edf_hasher.update(chunk)
            file_hash = edf_hasher.hexdigest()

        # ── Copy to permanent storage ─────────────────────────────────────────
        stored_name = f"{uuid.uuid4().hex}{suffix}"
        storage_root = Path(settings.RECORDINGS_UPLOAD_PATH)
        if not storage_root.is_absolute():
            storage_root = Path(settings.BASE_DIR) / storage_root
        storage_root.mkdir(parents=True, exist_ok=True)
        permanent_path = storage_root / stored_name

        shutil.copy2(str(source_for_edf), str(permanent_path))
        # Normalise file timestamps to epoch 0 for privacy (same as web upload).
        os.utime(str(permanent_path), (0, 0))

        # Clean up conversion temp dir after the copy.
        if source_for_edf != abs_path:
            shutil.rmtree(source_for_edf.parent, ignore_errors=True)

        # ── EDF processing ────────────────────────────────────────────────────
        try:
            result = process_edf_file(
                permanent_path,
                strip_annotation_text=pipeline.header.strip_annotation_text,
            )
        except Exception:
            permanent_path.unlink(missing_ok=True)
            raise

        # ── Persist to DB (atomic) ────────────────────────────────────────────
        with transaction.atomic():
            recording = Recording.objects.create(
                author=owner,
                original_name=stored_original_name(original_name_for_db, suffix),
                stored_name=stored_name,
                file_extension=suffix,
                file_size=permanent_path.stat().st_size,
                file_path=str(permanent_path),
                file_hash=file_hash,
                status=Recording.Status.PROCESSING,
            )

            _save_edf_results(recording, result)

            if sidecar_data_from_converter is not None:
                try:
                    save_sidecar_events(recording, sidecar_data_from_converter)
                except Exception as exc:
                    logger.warning(
                        "import_recordings: failed to save Nicolet sidecar events for %s: %s",
                        abs_path,
                        exc,
                    )

            # Compute content_hash (mirrors process_recording task logic).
            modality = _determine_modality(result.signal_infos)
            recording.file_path = str(permanent_path)
            recording.status = Recording.Status.READY
            recording.modality = modality
            payload = serialize_instance(recording)
            combined = hashlib.sha256()
            combined.update(file_hash.encode("utf-8"))
            combined.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
            content_hash = combined.hexdigest()

            # Shared helper handles bulk-update + DB-state before_state
            # capture + record_modify_change with SignalInfo digest.
            _write_final_recording_transition(
                recording=recording,
                update_fields={
                    "status": Recording.Status.READY,
                    "content_hash": content_hash,
                    "modality": modality,
                },
            )

            # AccessRight — owner gets full access.
            recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
            AccessRight.objects.create(
                content_type=recording_ct,
                object_id=str(recording.pk),
                access_giver=owner,
                access_target=owner,
                can_read=True,
                can_write=True,
                can_share=True,
            )

            # ── Preserve original (mode "all") ────────────────────────────
            # Imports never reach the FAILED-status path — EDF parse errors
            # re-raise above and the row is never persisted — so only mode
            # ``"all"`` needs to write here.  Source is the as-uploaded
            # file at ``abs_path``.  When a converter has run, the
            # recording's ``original_name`` was rewritten to the converted
            # extension; pass the actual source filename via
            # ``original_name_override`` so the preserved file is stored
            # under its true name and the manifest records the as-uploaded
            # identity.
            from recordings.preservation import (
                REASON_ALL,
                should_preserve_original,
                write_original,
            )

            if should_preserve_original():
                write_original(
                    recording,
                    abs_path,
                    reason=REASON_ALL,
                    original_name_override=abs_path.name,
                )

            # Sidecar JSON — stored as "Import annotations" Annotation.
            sidecar_path = abs_path.parent / (abs_path.name + ".json")
            if sidecar_path.exists():
                try:
                    sidecar_content = json.loads(sidecar_path.read_text(encoding="utf-8"))
                    Annotation.objects.create(
                        author=get_system_user(),
                        name="Import annotations",
                        target_content_type=recording_ct,
                        target_object_id=str(recording.pk),
                        object_hash=_annotation_hash(recording.pk, "import-annotations"),
                        content=sidecar_content,
                    )
                except Exception as exc:
                    logger.warning(
                        "import_recordings: ignoring bad sidecar for %s: %s",
                        abs_path,
                        exc,
                    )

            # Collection membership.
            if job.structure == ImportJob.Structure.RECURSIVE:
                collection = collection_map.get(abs_path.parent)
                if collection is not None:
                    CollectionItem.objects.get_or_create(
                        collection=collection,
                        content_type=recording_ct,
                        object_id=str(recording.pk),
                    )

        return recording
