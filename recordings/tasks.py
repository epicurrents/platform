"""Celery tasks for recording lifecycle management.

``process_recording``
    Moves an uploaded file from staging to permanent storage, runs format
    conversion (e.g. Nicolet .e → EDF) and EDF/BDF header processing, then
    notifies the author via push notification.

``purge_deleted_recordings``
    Scheduled task that hard-deletes soft-deleted recordings whose retention
    window has expired, and cleans up orphaned PENDING/PROCESSING rows.
"""

import hashlib
import json
import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

logger = logging.getLogger(__name__)

# File extensions handled by the EDF/BDF processor.
_EDF_EXTENSIONS = {".edf", ".bdf"}


def _annotation_hash(recording_pk: int, suffix: str) -> str:
    """Return a 32-char uppercase hex string suitable for use as ``object_hash``.

    Uses the recording PK (not the file hash) so that uploading the same file
    a second time yields a different set of annotation hashes.  The *suffix*
    distinguishes sibling annotations on the same recording (e.g. each
    interruption has a unique timestamp string as its suffix).
    """
    key = f"{recording_pk}:{suffix}"
    return hashlib.sha256(key.encode()).hexdigest()[:32].upper()


def _write_final_recording_transition(*, recording, update_fields: dict) -> None:
    """Apply ``update_fields`` to ``recording`` and emit the final audit row.

    Shared by ``process_recording`` (Celery) and ``import_recordings``
    (management command). Both compute new field values from in-memory
    state then need to (a) write them atomically via bulk update, (b)
    capture the *DB* state before that write as ``before_state`` so the
    audit diff reflects the actual transition, (c) refresh the local
    instance so downstream code sees the persisted values, and (d) emit
    one ``record_modify_change`` row carrying the ``SignalInfo`` digest
    in ``extra_payload``.

    Reads the pre-update state from a fresh ``Recording.objects.get``
    rather than ``serialize_instance(recording)`` — by the time this
    helper is called both callers have already mutated the in-memory
    ``recording`` to compute the new content_hash, so serialising the
    instance would produce a "before" that already matches the "after"
    and the diff would silently omit the transition. The extra SELECT
    is the price of correctness.
    """
    from activity.audit import record_modify_change, serialize_instance
    from recordings.audit_digests import (
        SIGNAL_INFO_DIGEST_KEY,
        compute_signal_info_digest,
    )
    from recordings.models import Recording

    before_state = serialize_instance(Recording.objects.get(pk=recording.pk))
    Recording.objects.filter(pk=recording.pk).update(**update_fields)
    recording.refresh_from_db()
    record_modify_change(
        actor=None,
        obj=recording,
        before_state=before_state,
        extra_payload={
            SIGNAL_INFO_DIGEST_KEY: compute_signal_info_digest(recording),
        },
    )


def _determine_modality(signal_infos) -> str:
    """Return the dominant signal modality from a list of signal_info objects.

    Counts non-annotation signal types and returns the most frequent one
    (e.g. 'eeg', 'emg', 'eog', 'ekg'). Returns an empty string when no
    typed signals are present.

    Auxiliary types (``trig``/``misc`` — trigger lines, DC inputs, oximetry) are
    excluded from the vote: they are real channels but never a recording's modality,
    and a file whose electrode labels fall outside 10-10 (so its EEG channels demote
    to ``misc``) would otherwise report itself as a ``misc`` recording. They are
    counted only as a last resort, when nothing else is typed at all.
    """
    from collections import Counter

    from recordings.processors.channel_labels import is_auxiliary_type

    typed = [s.signal_type for s in signal_infos if s.signal_type and not s.is_annotation_channel]
    counts = Counter(t for t in typed if not is_auxiliary_type(t)) or Counter(typed)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def _save_edf_results(recording, result) -> None:
    """Persist EDF/BDF processing results to the database.

    Creates:
    - One :class:`RecordingMeta` row with format-level metadata.
    - One :class:`SignalInfo` row per channel.
    - One :class:`~annotations.models.Interruption` row per detected gap.
    - One :class:`~annotations.models.Annotation` row (name "Original
      annotations") when embedded text events or gaps are present.
    """
    from annotations.models import Annotation, Interruption
    from epicurrents.system_user import get_system_user
    from recordings.models import RecordingMeta, SignalInfo
    from recordings.processors.edf import wall_clock_to_data_position

    header = result.header
    signal_infos = result.signal_infos
    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    system_user = get_system_user()

    # ── RecordingMeta ─────────────────────────────────────────────────────
    from recordings.processors.channel_labels import CHANNEL_ORDER_VERSION, assess_channel_layout

    channel_layout, unresolved_count = assess_channel_layout(signal_infos)
    duration = header.data_record_count * header.data_record_duration
    meta = RecordingMeta.objects.create(
        content_type=recording_ct,
        object_id=str(recording.pk),
        format=header.data_format,
        duration=duration,
        data_record_count=header.data_record_count,
        data_record_duration=header.data_record_duration,
        signal_count=header.signal_count,
        discontinuous=header.discontinuous,
        recording_date=None,  # always null: date stripped during de-identification
        channel_layout=channel_layout,
        unresolved_channel_count=unresolved_count,
        channel_order_version=CHANNEL_ORDER_VERSION,
    )

    # ── SignalInfo rows ───────────────────────────────────────────────────
    # The cleaned label / transducer / prefiltering are always written: they are
    # what the stored file holds and what every reader needs. Only the captured
    # originals are optional, and a deployment that holds nothing identifying the
    # acquiring laboratory drops them. Evaluated once rather than per row.
    _drop_source = getattr(settings, "RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA", False)
    SignalInfo.objects.bulk_create(
        [
            SignalInfo(
                meta=meta,
                index=idx,
                label=s.label,
                signal_type=s.signal_type,
                canonical_label=s.canonical_label,
                physical_unit=s.physical_unit,
                transducer_type=s.transducer_type,
                prefiltering=s.prefiltering,
                source_label=("" if _drop_source else s.source_label),
                source_transducer_type=("" if _drop_source else s.source_transducer_type),
                source_prefiltering=("" if _drop_source else s.source_prefiltering),
                source_index=(None if _drop_source else (s.source_index if s.source_index >= 0 else None)),
                physical_min=s.physical_min,
                physical_max=s.physical_max,
                digital_min=s.digital_min,
                digital_max=s.digital_max,
                units_per_bit=s.units_per_bit,
                digital_offset=s.digital_offset,
                sample_count=s.sample_count,
                sampling_rate=s.sampling_rate,
                highpass=s.highpass,
                lowpass=s.lowpass,
                notch=s.notch,
                is_annotation_channel=s.is_annotation_channel,
            )
            for idx, s in enumerate(signal_infos)
        ]
    )

    # ── Interruption rows (one per gap) ───────────────────────────────────
    # Each gap uses its data-position as the suffix, giving every interruption
    # a distinct object_hash even when the same file is uploaded more than once.
    for data_pos, gap_duration in result.gaps.items():
        Interruption.objects.create(
            author=system_user,
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
            object_hash=_annotation_hash(recording.pk, f"interruption:{data_pos}"),
            timestamp=data_pos,
            duration=gap_duration,
        )

    # ── "Original annotations" Annotation (only when there is content) ────
    # A deployment may declare that nothing annotating a recording came out of
    # the uploaded file. The Interruption rows above are unaffected on purpose:
    # a gap is geometry rather than annotation, it carries no text, and the
    # viewer and compute layer read data positions derived from it.
    if getattr(settings, "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS", False):
        return

    has_events = bool(result.annotations)
    has_gaps = bool(result.gaps)
    if has_events or has_gaps:
        content: dict = {}
        if has_events:
            # ``onset`` is a **data position**, matching the Interruption rows above and
            # every signal window the compute layer reads. The TAL field itself is wall
            # clock, so on a discontinuous recording the two disagree by the accumulated
            # gap time from the first splice onward: storing the raw onset here beside a
            # data-position interruption put the same file's annotations and its gaps on
            # two different timelines, and every event after the first gap landed on
            # signal it did not describe.
            #
            # The untranslated value is kept under ``wall_clock_onset`` when it differs,
            # because it is what the file says and what a re-export has to write back.
            content["events"] = [
                {
                    "onset": onset,
                    "duration": anno.duration,
                    "label": anno.label,
                    **({} if onset == anno.onset else {"wall_clock_onset": anno.onset}),
                }
                for anno, onset in ((a, wall_clock_to_data_position(a.onset, result.gaps)) for a in result.annotations)
            ]
        if has_gaps:
            content["interruptions"] = [
                {"onset": float(pos), "duration": float(dur)} for pos, dur in result.gaps.items()
            ]
        Annotation.objects.create(
            author=system_user,
            name="Original annotations",
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
            object_hash=_annotation_hash(recording.pk, "original-annotations"),
            content=content,
        )


def _push_display_name(recording) -> str:
    """Grantee-safe label for push-notification bodies.

    Push bodies surface on lock screens and transit the Celery broker in
    plaintext, so they must not carry ``original_name`` (routinely a patient
    identifier). Mirrors the API fallback: ``display_name`` when set,
    otherwise the stored-hash prefix.
    """
    if recording.display_name:
        return recording.display_name
    return recording.stored_name[:8].upper()


@shared_task
def process_recording(recording_id: int, preserve_annotations: bool = False):
    """Move a staged recording to permanent storage and process its format.

    Called immediately after upload. The file sits in RECORDINGS_STAGING_PATH
    until this task runs, then is moved to RECORDINGS_UPLOAD_PATH.

    If a converter is registered for the uploaded file's extension (see
    :func:`recordings.pipelines.get_converter`), the file is converted to EDF
    before the rest of processing. Converter-produced sidecar events are saved
    as a "Source events" annotation. The stored file and all DB metadata
    (``stored_name``, ``file_extension``, ``file_hash``, ``file_size``) are
    updated to reflect the converted EDF.

    For recognised formats (EDF / BDF) the header is parsed, de-identified,
    and rewritten; signal metadata, gaps, and embedded annotations are stored.
    If format processing fails the recording is kept but marked as FAILED so
    the user is not misled into thinking the file is ready to open in the
    viewer. The original file is always preserved so users can still download
    what they uploaded.

    On unrecoverable infrastructure errors (file missing, DB failure) the
    staged file and database row are both cleaned up, matching the previous
    behaviour.

    Audit attribution: the body runs inside
    ``with_system_activity("recordings.process", interface=CELERY,
    target=recording)`` so state transitions (PENDING → PROCESSING →
    READY/FAILED) auto-attach to one parent ``Activity`` row. The final
    READY/FAILED transition carries a digest of the recording's
    ``SignalInfo`` rows in ``extra_payload`` — those bulk-created rows
    don't fire ``post_save``, so the digest is how their integrity rides
    on the chain.
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from recordings.models import Recording

    try:
        recording = Recording.objects.get(pk=recording_id, status=Recording.Status.PENDING)
    except Recording.DoesNotExist:
        logger.warning(
            "process_recording: recording %d not found or not pending — skipping.",
            recording_id,
        )
        return

    staging_path = Path(recording.file_path)

    with with_system_activity(
        "recordings.process",
        interface=Activity.Interface.CELERY,
        target=recording,
        metadata={
            "recording_id": recording_id,
            "preserve_annotations": preserve_annotations,
        },
    ):
        return _process_recording_body(
            recording=recording,
            recording_id=recording_id,
            staging_path=staging_path,
            preserve_annotations=preserve_annotations,
        )


def _process_recording_body(*, recording, recording_id, staging_path, preserve_annotations):
    """Execute the recording-processing body inside an open audited scope.

    Extracted so the outer task can manage the ``with_system_activity``
    contextmanager without nesting the entire body twice in a try/finally.
    """
    from activity.audit import serialize_instance
    from recordings.models import Recording

    try:
        # ── Validate ──────────────────────────────────────────────────────────
        recording.status = Recording.Status.PROCESSING
        recording.save(update_fields=["status"])

        if not staging_path.exists():
            raise FileNotFoundError(f"Staged file not found: {staging_path}")
        if staging_path.stat().st_size == 0:
            raise ValueError("Uploaded file is empty")

        # ── Preserve original (mode "all") ────────────────────────────────────
        # Done before any move / conversion so the originals volume always
        # holds the bytes the user uploaded.  For format-converter cases
        # (e.g. ``.e``) this is the only chance to preserve the source file;
        # by the time conversion has run the permanent file is the converted
        # EDF.  See recordings/preservation.py for the layout and manifest.
        from recordings.preservation import (
            REASON_ALL,
            should_preserve_original,
            write_original,
        )

        if should_preserve_original():
            write_original(recording, staging_path, reason=REASON_ALL)

        # ── Move to permanent storage ─────────────────────────────────────────
        storage_root = Path(settings.RECORDINGS_UPLOAD_PATH)
        if not storage_root.is_absolute():
            storage_root = Path(settings.BASE_DIR) / storage_root
        storage_root.mkdir(parents=True, exist_ok=True)

        permanent_path = storage_root / recording.stored_name
        shutil.move(str(staging_path), str(permanent_path))
        # Normalise file timestamps to a fixed epoch so the filesystem does not
        # leak when or by whom the file was uploaded (UNIX timestamp 0 = 1970-01-01).
        os.utime(str(permanent_path), (0, 0))

        # ── Pre-conversion (non-EDF formats) ──────────────────────────────────
        ext = recording.file_extension.lower()
        sidecar_data: dict | None = None

        from recordings.pipelines import (
            dispatch_convert_failed,
            dispatch_post_convert,
            dispatch_pre_convert,
            get_converter,
        )

        converter = get_converter(ext)
        if converter is not None:
            import tempfile

            # pre_convert fires while permanent_path still holds the source
            # bytes (before the converter overwrites them). Hard-mode handler
            # exceptions abort ingest; soft-mode exceptions are logged and
            # ingest continues. See recordings/pipelines.py for the contract.
            dispatch_pre_convert(recording, permanent_path, ext)

            convert_tmp = Path(tempfile.mkdtemp(prefix="epicurrents_convert_"))
            try:
                convert_result = converter(permanent_path, convert_tmp)
            except Exception as exc:
                # convert_failed fires inside the converter except so the
                # source path is still valid for preservation / logging
                # handlers. The original exception always re-raises.
                dispatch_convert_failed(recording, permanent_path, exc)
                shutil.rmtree(convert_tmp, ignore_errors=True)
                raise

            if isinstance(convert_result, tuple):
                converted_edf, sidecar_data = convert_result
            else:
                converted_edf, sidecar_data = convert_result, None

            new_ext = converted_edf.suffix.lower()
            new_stored_name = Path(recording.stored_name).stem + new_ext
            new_permanent_path = storage_root / new_stored_name

            shutil.move(str(converted_edf), str(new_permanent_path))
            shutil.rmtree(convert_tmp, ignore_errors=True)

            # post_convert fires after the converted file is in place but
            # before the source file is unlinked, so handlers see both paths
            # as valid.
            dispatch_post_convert(recording, permanent_path, new_permanent_path, sidecar_data)

            try:
                permanent_path.unlink(missing_ok=True)
            except OSError:
                pass

            # Recompute file metrics from the converted EDF.
            os.utime(str(new_permanent_path), (0, 0))
            edf_hasher = hashlib.sha256()
            with new_permanent_path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    edf_hasher.update(chunk)

            # Rewrite original_name to use the EDF extension so filenames
            # shown in the UI (file lists, viewer, notifications) are consistent
            # with the stored format and do not mislead the viewer into looking
            # for a non-EDF reader.
            original_stem = Path(recording.original_name).stem
            new_original_name = original_stem + new_ext

            # Update local references for downstream steps and error cleanup.
            original_ext = recording.file_extension
            recording.stored_name = new_stored_name
            recording.file_extension = new_ext
            recording.file_hash = edf_hasher.hexdigest()
            recording.file_size = new_permanent_path.stat().st_size
            recording.original_name = new_original_name
            recording.save(
                update_fields=[
                    "stored_name",
                    "file_extension",
                    "file_hash",
                    "file_size",
                    "original_name",
                ]
            )
            permanent_path = new_permanent_path
            ext = new_ext

            logger.info(
                "process_recording: converted recording %d from %s to %s.",
                recording_id,
                original_ext,
                new_ext,
            )

        # ── Format-specific processing ────────────────────────────────────────
        format_error: str | None = None

        # The upload API is EEG-only; set modality unconditionally so that
        # channel-label heuristics (e.g. an ECG channel present alongside EEG
        # channels) cannot override the recording type.
        modality = "eeg"
        if ext in _EDF_EXTENSIONS:
            try:
                from recordings.pipelines import get_pipeline
                from recordings.processors.edf import EdfParseError, process_edf_file

                pipeline = get_pipeline("web")
                strip_annotation_text = pipeline.header.strip_annotation_text
                # The load-bearing half of the prohibition. The endpoint refuses
                # the request so a caller learns it was refused; this makes the
                # refusal true for every route into processing, including the
                # import command and any project calling the task directly.
                if preserve_annotations and getattr(settings, "RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS", True):
                    # A local, not `pipeline.header.strip_annotation_text = False`.
                    # get_pipeline copies the built-ins so a per-run change cannot
                    # outlive the run, but an operator pipeline reached through a
                    # dotted path in RECORDING_PIPELINES is a module-level instance
                    # returned as given — mutating it here would leak this upload's
                    # permission into every later recording the worker handled, which
                    # is the regression the copy was added to close, on the one route
                    # the copy does not cover. Nothing downstream reads the object.
                    strip_annotation_text = False
                result = process_edf_file(permanent_path, strip_annotation_text=strip_annotation_text)
                _save_edf_results(recording, result)

                logger.info(
                    "process_recording: EDF/BDF processing succeeded for recording %d "
                    "(%d signals, %d annotations, %d gaps).",
                    recording_id,
                    result.header.signal_count,
                    len(result.annotations),
                    len(result.gaps),
                )
            except EdfParseError as exc:
                format_error = str(exc)
                logger.warning(
                    "process_recording: EDF/BDF header parse failed for recording %d: %s",
                    recording_id,
                    exc,
                )
            except Exception as exc:
                format_error = str(exc)
                logger.warning(
                    "process_recording: EDF/BDF processing failed for recording %d: %s",
                    recording_id,
                    exc,
                    exc_info=True,
                )

        # Sidecar events (converter-generated) are handled by the
        # ``recordings.converters.sidecar.handle_post_convert``
        # post_convert hook, registered in RecordingsConfig.ready and
        # already fired by dispatch_post_convert above.

        # ── Compute content_hash ──────────────────────────────────────────────
        final_status = Recording.Status.FAILED if format_error else Recording.Status.READY
        recording.file_path = str(permanent_path)
        recording.status = final_status
        payload = serialize_instance(recording)
        combined = hashlib.sha256()
        combined.update(recording.file_hash.encode("utf-8"))
        combined.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
        content_hash = combined.hexdigest()

        # ── Preserve source bytes (mode "failed") + populate processing_error ─
        # Idempotent against the mode-"all" write at task start: if the file
        # is already on the originals volume, write_original (called inside
        # finalize_failed_preservation) returns False and the manifest's
        # REASON_ALL stays put. When mode is "failed" only, the source
        # bytes are stashed during pre_convert (see
        # ``recordings.preservation._on_pre_convert``) so that converter-
        # bound formats (e.g. ``.e`` → EDF) preserve the source bytes — not
        # the converted EDF — when processing fails. Native EDF/BDF
        # uploads have no stash; finalize falls back to ``permanent_path``,
        # which still holds the source bytes since no converter ran.
        from recordings.preservation import (
            cleanup_pending_preservation,
            finalize_failed_preservation,
        )

        if format_error:
            finalize_failed_preservation(recording, permanent_path)
        else:
            cleanup_pending_preservation(recording.pk)

        # ── Persist ───────────────────────────────────────────────────────────
        # _write_final_recording_transition handles bulk-update + before_state
        # capture from DB (not in-memory, which has already been mutated for
        # the content_hash computation above) + record_modify_change with
        # the SignalInfo digest in extra_payload. See activity/derived_state.py.
        _write_final_recording_transition(
            recording=recording,
            update_fields={
                "file_path": str(permanent_path),
                "status": final_status,
                "content_hash": content_hash,
                "modality": modality,
                "processing_error": (format_error or "")[:4096],
            },
        )

        logger.info(
            "process_recording: recording %d → %s at %s",
            recording_id,
            final_status,
            permanent_path,
        )

        from notifications.tasks import send_push_to_user

        if format_error:
            send_push_to_user.delay(
                user_id=recording.author_id,
                title="Recording could not be processed",
                body=(
                    f'"{_push_display_name(recording)}" was saved but could not be fully '
                    "processed (unsupported or damaged format). "
                    "You can still download the original file."
                ),
                data={"type": "recording_failed", "recording_id": recording_id},
            )
        else:
            send_push_to_user.delay(
                user_id=recording.author_id,
                title="Recording ready",
                body=f'"{_push_display_name(recording)}" has been processed and is ready.',
                data={"type": "recording_ready", "recording_id": recording_id},
            )

        return {"recording_id": recording_id, "status": final_status}

    except Exception as exc:
        logger.exception(
            "process_recording: recording %d failed — marking FAILED.",
            recording_id,
        )
        # Drop any pending preservation stash so a crash anywhere in the
        # task does not leak the temp source-bytes copy. Idempotent —
        # convert_failed has typically already cleared it for that path.
        from recordings.preservation import cleanup_pending_preservation

        cleanup_pending_preservation(recording_id)
        # Remove staged file if it still exists (permanent path may not exist yet).
        cleanup_root = Path(settings.RECORDINGS_UPLOAD_PATH)
        if not cleanup_root.is_absolute():
            cleanup_root = Path(settings.BASE_DIR) / cleanup_root
        for path in (staging_path, cleanup_root / recording.stored_name):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

        # Preserve the row as FAILED with the reason rather than dropping it,
        # so an unexpected (non-format) failure always leaves something to
        # inspect. Mirrors the handled-format-error path: ``processing_error``
        # is author/superuser-only and FAILED recordings are hidden from
        # grantees, so no PHI is exposed. Refetch + save so the transition
        # fires post_save (audited) rather than a stale in-memory write; guard
        # the save so a secondary failure here cannot mask the original
        # exception.
        try:
            failed = Recording.objects.filter(pk=recording_id).first()
            if failed is not None:
                failed.status = Recording.Status.FAILED
                failed.processing_error = (f"Unexpected processing error: {exc}")[:4096]
                failed.save(update_fields=["status", "processing_error"])
        except Exception:
            logger.exception(
                "process_recording: could not mark recording %d FAILED",
                recording_id,
            )

        from notifications.tasks import send_push_to_user

        send_push_to_user.delay(
            user_id=recording.author_id,
            title="Recording failed",
            body=f'Processing of "{_push_display_name(recording)}" failed. Please try uploading again.',
            data={"type": "recording_failed"},
        )

        raise


@shared_task
def purge_deleted_recordings():
    """Hard-delete recordings that have been in the trash beyond the retention window.

    ⚠️ LOAD-BEARING — GDPR Art. 17 erasure pipeline.
    This task is the erasure half of the soft-delete + purge contract:
    a user (or operator) trashes a recording → ``deleted_at`` is set →
    after ``RECORDINGS_TRASH_RETENTION_DAYS`` the row + file are
    permanently removed. Silent narrowing of either filter
    (``deleted_at__isnull=False, deleted_at__lt=cutoff,
    status__in=[READY, FAILED]`` or the orphan reaper's
    ``status__in=[PENDING, PROCESSING], created_at__lt=cutoff``) leaves
    PHI past the retention window with no externally visible signal.
    Equally damaging is the inverse: a regression that widens either
    filter takes still-live data with it. See AGENTS.md →
    *Load-bearing files* and the contract-test class
    ``TestPurgeDeletedRecordingsContract`` in
    ``recordings/tests/test_tasks.py`` before modifying.

    Reads RECORDINGS_TRASH_RETENTION_DAYS from settings (default 30). For each
    qualifying recording the file is removed from disk first; if removal fails the
    row is left in place so the next run can retry. Successfully purged rows fire
    the standard ``pre_delete`` audit signal inside the ``with_system_activity``
    scope, so each deletion is attributed to the same parent ``Activity`` row
    (``verb="recordings.purge"``, ``interface=celery``).

    Recordings stuck in PENDING or PROCESSING status past the retention window are
    also purged — these represent orphaned rows from failed processing runs where
    the file was already cleaned up (or never moved to permanent storage).

    The host-controlled originals volume
    (``RECORDINGS_ORIGINALS_PATH``) is deliberately **never** read or
    written by this task — see AGENTS.md → *Originals preservation
    volume is strictly write-only*. Removal of preserved originals is
    an out-of-band operator action.
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity

    retention_days = getattr(settings, "RECORDINGS_TRASH_RETENTION_DAYS", 30)
    cutoff = timezone.now() - timedelta(days=retention_days)

    with with_system_activity(
        "recordings.purge",
        interface=Activity.Interface.CELERY,
        metadata={"retention_days": retention_days},
    ):
        return _purge_deleted_recordings_body(cutoff=cutoff, retention_days=retention_days)


def _purge_deleted_recordings_body(*, cutoff, retention_days):
    """Walk both purge querysets inside an open audited scope.

    Iterates the soft-deleted READY queryset first, then the orphaned
    PENDING / PROCESSING reaper. Per-row ``recording.delete()`` fires
    ``pre_delete`` so each removal lands in ``ObjectChangeLog`` under
    the surrounding ``recordings.purge`` ``Activity`` row.
    """
    from recordings.models import Recording

    # Normal trash: soft-deleted READY and FAILED recordings past the
    # retention window. FAILED belongs here because delete_recording trashes
    # recordings of any status and a FAILED recording keeps its file on disk
    # — excluding it left trashed FAILED uploads (file + PHI-bearing
    # original_name) outside every purge branch indefinitely.
    queryset = Recording.objects.filter(
        deleted_at__isnull=False,
        deleted_at__lt=cutoff,
        status__in=[Recording.Status.READY, Recording.Status.FAILED],
    )

    purged = 0
    errors = 0

    for recording in queryset.iterator():
        file_path = Path(recording.file_path)
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError as exc:
            logger.error(
                "Could not delete file %s for recording %d: %s — skipping row.",
                recording.file_path,
                recording.pk,
                exc,
            )
            errors += 1
            continue

        recording.delete()
        purged += 1

    # Orphaned rows: PENDING or PROCESSING recordings created before the cutoff
    # that were never advanced to READY. The worker that handled them either
    # crashed or already deleted the staged file; the DB row is the only remnant.
    orphaned_qs = Recording.objects.filter(
        status__in=[Recording.Status.PENDING, Recording.Status.PROCESSING],
        created_at__lt=cutoff,
    )
    orphaned_purged = 0
    for recording in orphaned_qs.iterator():
        file_path = Path(recording.file_path)
        file_existed_at_check = file_path.exists()
        if file_existed_at_check:
            try:
                file_path.unlink()
            except OSError as exc:
                # File present but unlink failed (permission, I/O error).
                # Preserve the DB row so a future run can retry; deleting
                # the row now would leave the file orphaned on disk with
                # nothing in the DB to remember it.
                logger.error(
                    "Could not delete file %s for orphan recording %d: %s — skipping row.",
                    recording.file_path,
                    recording.pk,
                    exc,
                )
                errors += 1
                continue
        recording.delete()
        orphaned_purged += 1

    logger.info(
        "purge_deleted_recordings: purged=%d orphaned=%d errors=%d cutoff=%s retention_days=%d",
        purged,
        orphaned_purged,
        errors,
        cutoff.isoformat(),
        retention_days,
    )
    return {"purged": purged, "orphaned": orphaned_purged, "errors": errors}
