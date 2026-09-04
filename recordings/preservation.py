"""Tiered preservation of the as-uploaded recording file.

The platform supports three preservation modes (``RECORDINGS_PRESERVE_MODE``):

``"none"``
    Default.  The platform never copies the upload to the originals volume.
    Failed uploads are deleted (current behaviour).

``"failed"``
    The platform copies the upload to the originals volume **when ingest
    fails**.  Successful uploads are not preserved beyond the platform's own
    processed-file storage.  Used by deployments that want a regulatory
    backstop for re-ingest of failures.

``"all"``
    The platform copies the upload to the originals volume **before
    processing runs** — the only correct time, since processing rewrites the
    file in place for header anonymisation.

The originals volume is **strictly write-only from the platform's
perspective**.  No code path in this module or anywhere else reads from
``RECORDINGS_ORIGINALS_PATH``; the manifest written alongside each copy is
the platform's only way of describing what should be on disk.  Operator
tooling reads the volume directly using whatever workflow they prefer.

On-disk layout::

    <RECORDINGS_ORIGINALS_PATH>/
        <stored_name_prefix>/
            <sanitized-original-filename>
            manifest.json

where ``stored_name_prefix`` is the 32-character random hex prefix of
``Recording.stored_name`` — unique per upload and stable across the
recording's lifetime (independent of ``content_hash``, which the platform
rewrites during anonymisation).
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


MODE_NONE = "none"
MODE_FAILED = "failed"
MODE_ALL = "all"

VALID_PRESERVE_MODES = frozenset({MODE_NONE, MODE_FAILED, MODE_ALL})

# Reason tokens written into manifest.json.
REASON_ALL = "all"
REASON_FAILED = "failed"

# Filenames the preservation directory reserves for its own bookkeeping.
# A user-supplied ``original_name`` that resolves to one of these would
# otherwise be silently overwritten by the manifest write.
_RESERVED_FILENAMES = frozenset({"manifest.json"})


def _current_mode() -> str:
    """Return the configured preservation mode, defaulting to ``"none"``."""
    return getattr(settings, "RECORDINGS_PRESERVE_MODE", MODE_NONE)


def _originals_root() -> Path | None:
    """Return the configured originals volume root, or None when unset."""
    path = getattr(settings, "RECORDINGS_ORIGINALS_PATH", None)
    if not path:
        return None
    return Path(path)


def validate_settings() -> None:
    """Raise ``ImproperlyConfigured`` when the preservation settings are incoherent.

    Called from ``RecordingsConfig.ready`` so the deployment fails to start
    if the operator set a non-default mode without configuring the originals
    volume mount.  Exposed as a standalone function for unit testing.
    """
    mode = _current_mode()
    if mode not in VALID_PRESERVE_MODES:
        raise ImproperlyConfigured(f"RECORDINGS_PRESERVE_MODE={mode!r} is not one of {sorted(VALID_PRESERVE_MODES)}.")
    if mode != MODE_NONE and _originals_root() is None:
        raise ImproperlyConfigured(
            f"RECORDINGS_PRESERVE_MODE={mode!r} requires "
            "RECORDINGS_ORIGINALS_PATH to point at the host-controlled "
            "originals volume mount."
        )


def should_preserve_failed() -> bool:
    """Return True when failed ingests should write to the originals volume."""
    return _current_mode() in (MODE_FAILED, MODE_ALL)


def should_preserve_original() -> bool:
    """Return True when every upload should be copied before processing."""
    return _current_mode() == MODE_ALL


def _safe_original_filename(original_name: str) -> str:
    """Strip directory components from *original_name* and reject empties.

    ``original_name`` is user-supplied at upload time; using it directly as a
    path component would let a hostile filename ('..%2Fetc%2Fpasswd', etc.)
    escape the per-recording directory.  Only the final path segment is
    retained.  Falls back to ``"upload"`` when the result is empty, and
    rewrites names that collide with the directory's reserved bookkeeping
    files (``manifest.json``) so the manifest writer cannot clobber the
    preserved data file.
    """
    name = Path(original_name or "").name.strip()
    if not name or name in (".", ".."):
        return "upload"
    if name.lower() in _RESERVED_FILENAMES:
        # Prefix to disambiguate from the manifest the platform itself writes.
        return f"original_{name}"
    return name


def _target_dir(recording) -> Path | None:
    """Return the per-recording directory under the originals volume.

    Returns None when the originals volume is not configured — callers should
    treat this as "feature disabled" and skip silently.
    """
    root = _originals_root()
    if root is None:
        return None
    prefix = (recording.stored_name or "").split(".", 1)[0]
    if not prefix:
        return None
    return root / prefix


def write_original(
    recording,
    source_path: str | Path,
    *,
    reason: str,
    original_name_override: str | None = None,
) -> bool:
    """Copy the file at *source_path* to the originals volume.

    Idempotent — if the directory already carries a ``manifest.json`` (e.g.
    mode ``"all"`` wrote on task start and ingest then failed, triggering
    the failure path), the call is a no-op and returns False.  Returns True
    when a new file is written.

    *reason* must be ``REASON_ALL`` or ``REASON_FAILED``; the value is
    recorded in ``manifest.json`` so operators can tell which tier
    produced the copy.

    *original_name_override* lets a caller force the as-uploaded filename
    used for the data file and the manifest, independent of the current
    value of ``recording.original_name``.  This matters when a format
    converter has already rewritten ``original_name`` to the converted
    extension (e.g. ``.e`` → ``.edf``) but the bytes at ``source_path``
    are still the as-uploaded source.  Without the override the on-disk
    file lands under a misleading name and the manifest claims a
    post-conversion identity.

    Failures (missing volume, unwritable path, source missing) are logged
    and swallowed — preservation is best-effort and must not block ingest.
    """
    if reason not in (REASON_ALL, REASON_FAILED):
        raise ValueError(f"write_original reason must be {REASON_ALL!r} or {REASON_FAILED!r}, got {reason!r}")

    target_dir = _target_dir(recording)
    if target_dir is None:
        logger.debug(
            "write_original: originals volume not configured; skipping for recording %s",
            recording.pk,
        )
        return False

    source = Path(source_path)
    if not source.exists() or not source.is_file():
        logger.warning(
            "write_original: source %s missing for recording %s — preservation skipped",
            source,
            recording.pk,
        )
        return False

    # Idempotency is anchored on ``manifest.json`` rather than the data file's
    # name.  The data filename is derived from ``recording.original_name``,
    # which the converter pipeline can rewrite (e.g. ``.e`` → ``.edf``)
    # between a mode-"all" write at task start and a mode-"failed" write
    # after format processing fails.  Checking the data file would let the
    # second write proceed under a different name and overwrite the
    # manifest's preservation_reason, losing the operator's ability to tell
    # which tier produced the copy.
    manifest_path = target_dir / "manifest.json"
    if manifest_path.exists():
        logger.debug(
            "write_original: %s already preserved (manifest present at %s); skipping",
            recording.pk,
            manifest_path,
        )
        return False

    effective_original_name = original_name_override if original_name_override is not None else recording.original_name
    filename = _safe_original_filename(effective_original_name)
    target_file = target_dir / filename

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_file)
        _write_manifest(
            target_dir,
            recording,
            reason=reason,
            original_name=effective_original_name,
        )
    except OSError as exc:
        logger.error(
            "write_original: failed to preserve recording %s to %s — %s",
            recording.pk,
            target_file,
            exc,
        )
        return False

    logger.info(
        "write_original: preserved recording %s (reason=%s) to %s",
        recording.pk,
        reason,
        target_file,
    )
    return True


def _write_manifest(
    target_dir: Path,
    recording,
    *,
    reason: str,
    original_name: str | None = None,
) -> None:
    """Write ``manifest.json`` next to the preserved file.

    Format (stable; consumed by operator tooling):

    ``recording_pk``          Internal PK of the Recording row.
    ``stored_name``           Public stored name (``<32-hex>.<ext>``).
    ``original_name``         Filename as uploaded (use the caller's
                              *original_name* override if supplied; otherwise
                              ``recording.original_name`` which may have been
                              rewritten by a converter).
    ``file_hash``             SHA-256 of the original bytes.
    ``file_size``             Size in bytes.
    ``author_id``             User PK of the uploader.
    ``uploaded_at``           ISO 8601 datetime (``created_at``).
    ``preservation_reason``   ``"all"`` or ``"failed"``.
    """
    payload = {
        "recording_pk": recording.pk,
        "stored_name": recording.stored_name,
        "original_name": (original_name if original_name is not None else recording.original_name),
        "file_hash": recording.file_hash,
        "file_size": recording.file_size,
        "author_id": recording.author_id,
        "uploaded_at": (recording.created_at.isoformat() if recording.created_at else None),
        "preservation_reason": reason,
    }
    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Source-bytes stash for converter-bound formats in mode "failed"
# ---------------------------------------------------------------------------
#
# Mode "all" preserves the staging file before any conversion (see line 299 of
# recordings/tasks.py). Mode "failed" cannot preserve up-front — the bytes
# would land on the originals volume for every upload, defeating the mode's
# purpose. But by the time we know an upload has failed, the converter
# (e.g. ``.e`` → EDF) has already replaced the source with the converted EDF.
#
# This stash holds a COPY of the source bytes from before conversion ran, so
# the end-of-task preservation can write the source — not the derivative —
# when format processing fails. Cleaned up on every code path:
#
# - conversion succeeds + format processing succeeds → cleanup on finalize
# - conversion succeeds + format processing fails → promote to originals
# - converter raises → write_original from source_path argument, drop stash
# - task crashes anywhere → outer except calls cleanup_pending_preservation

# recording.pk → {"path": Path, "original_name": str}
_PENDING_STASHES: dict[int, dict] = {}


def _stash_dir() -> Path:
    """Per-process stash directory under the OS temp dir.

    Distinct from ``RECORDINGS_ORIGINALS_PATH`` (the operator-controlled
    write-only volume) — this is platform-internal scratch space that must
    be writable AND deletable.
    """
    return Path(tempfile.gettempdir()) / "epicurrents_preserve_stash"


def _stash_path_for(recording_id: int) -> Path:
    return _stash_dir() / str(recording_id)


def _on_pre_convert(recording, source_path: Path, ext: str) -> None:
    """pre_convert handler — stash source bytes when mode is ``"failed"``.

    Skipped for mode ``"all"`` (the staging-file preservation at task start
    already covers it) and mode ``"none"`` (nothing to preserve at all).
    """
    if _current_mode() != MODE_FAILED:
        return
    stash_dir = _stash_dir()
    try:
        stash_dir.mkdir(parents=True, exist_ok=True)
        stash_path = _stash_path_for(recording.pk)
        shutil.copy2(source_path, stash_path)
    except OSError as exc:
        # Stash failure is non-fatal — we just lose the ability to preserve
        # the source on a later failure for THIS upload. Log and continue.
        logger.warning(
            "preservation: failed to stash source for recording %s — "
            "Phase 3 failed-preservation will fall back to the converted "
            "bytes if processing later fails. Cause: %s",
            recording.pk,
            exc,
        )
        return
    _PENDING_STASHES[recording.pk] = {
        "path": stash_path,
        "original_name": recording.original_name,
    }


def _on_convert_failed(recording, source_path: Path, exception) -> None:
    """convert_failed handler — preserve source bytes inside the except block.

    Runs while ``source_path`` still holds the source bytes (the outer task
    cleanup hasn't unlinked them yet). Writes directly from ``source_path``
    rather than from the stash because the bytes are identical and the
    source is the canonical reference; the stash is then dropped since it's
    no longer needed.
    """
    if should_preserve_failed():
        write_original(recording, source_path, reason=REASON_FAILED)
    _drop_stash(recording.pk)


def _drop_stash(recording_id: int) -> None:
    """Remove the stash entry + on-disk temp file for ``recording_id``.

    Idempotent — no-op when no stash exists for the given id.
    """
    entry = _PENDING_STASHES.pop(recording_id, None)
    if entry is None:
        return
    try:
        Path(entry["path"]).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "preservation: failed to unlink stash file %s for recording %s: %s",
            entry["path"],
            recording_id,
            exc,
        )


def finalize_failed_preservation(recording, fallback_path: Path) -> None:
    """End-of-task hook — write source bytes to originals on FAILED ingest.

    Called by ``process_recording`` when ``format_error`` is set (the
    post-conversion format processor failed, or the file was native EDF/BDF
    and failed there). Resolution:

    - If a stash exists for this recording (pre_convert ran with mode
      ``"failed"``), promote the stashed source bytes to the originals
      volume with REASON_FAILED. The stash's recorded ``original_name``
      is used since ``recording.original_name`` may have been rewritten by
      the converter.
    - Else (no converter ran, or mode wasn't ``"failed"`` at pre_convert
      time), call ``write_original(recording, fallback_path,
      reason=REASON_FAILED)`` — same behaviour as the pre-hook code path.

    Always idempotent on existing manifests (mode ``"all"`` already wrote
    a manifest at task start; this call is a no-op in that case). Always
    drops the stash on the way out.
    """
    if not should_preserve_failed():
        _drop_stash(recording.pk)
        return

    entry = _PENDING_STASHES.get(recording.pk)
    if entry is not None:
        write_original(
            recording,
            entry["path"],
            reason=REASON_FAILED,
            original_name_override=entry["original_name"],
        )
    else:
        write_original(recording, fallback_path, reason=REASON_FAILED)

    _drop_stash(recording.pk)


def cleanup_pending_preservation(recording_id: int) -> None:
    """End-of-task hook on the success path — drop any pending stash.

    Also used by the outer-except cleanup in ``process_recording`` so a
    crash anywhere in the task doesn't leak stash files. Idempotent.
    """
    _drop_stash(recording_id)
