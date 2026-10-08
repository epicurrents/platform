"""Recordings registered in place: files the platform reads and never writes.

A linked recording's ``file_path`` points into one of ``RECORDINGS_LINKED_ROOTS``, a read-only mount of a tree the
operator holds. ``import_recordings --link`` registers such a file by reading it: the rows are written as for any
import, but the file is not copied, normalised, de-identified, reordered or re-dated, because a dataset too large to
duplicate cannot be given a platform-owned copy to rewrite. Three rules follow, and this module is where each lives.

- **Nothing writes or unlinks the file.** Every path that removes a recording's file goes through
  :func:`unlink_recording_file`, which removes a managed file and leaves a linked one alone. Deleting a linked
  recording deletes its rows.
- **The file is served only while it is the file that was linked.** The rows describe the bytes as they were read:
  the record geometry the serving paths cut by, the digest a pin checks, the channel order. A file that has changed
  since is refused by :func:`ensure_linked_file_unchanged` rather than served against rows that no longer describe it.
- **De-identification is the operator's assertion, not the platform's pass.** The import refuses to link without
  ``--source-deidentified``; the file keeps whatever header and annotation text it has, and the de-identification
  record says so (``recordings.deidentification_record``). Readers on a de-identifying grant still receive bytes
  through the serve pipeline, which cleans the header and strips annotation text on the way out.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings


class LinkedSourceError(Exception):
    """A file cannot be linked: outside every linked root, unreadable, or of a kind that would need rewriting."""


class LinkedFileChanged(Exception):
    """A linked file is no longer the file that was linked, or is no longer reachable under a linked root."""


def linked_roots() -> list[Path]:
    """Return the configured linked roots that exist, resolved."""
    roots = []
    for entry in getattr(settings, "RECORDINGS_LINKED_ROOTS", None) or []:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            roots.append(Path(entry).resolve(strict=True))
        except OSError:
            continue
    return roots


def _under_a_root(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in linked_roots())


def resolve_linked_source(path: Path) -> Path:
    """Return *path* resolved, or raise :class:`LinkedSourceError` when it is not a file under a linked root.

    Resolved before the check so that a symlink inside a root cannot name a file outside it.
    """
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise LinkedSourceError(f"Cannot resolve {path}: {exc}") from exc
    if not resolved.is_file():
        raise LinkedSourceError(f"{resolved} is not a regular file.")
    if not _under_a_root(resolved):
        raise LinkedSourceError(
            f"{resolved} is not under any of RECORDINGS_LINKED_ROOTS; only files there can be linked in place."
        )
    return resolved


def read_linked_edf(path: Path, data: bytes):
    """Parse the bytes *data* read from the linked EDF/BDF file at *path*, returning an ``EdfProcessingResult``.

    The caller reads the file once and passes the bytes, so a large archive is read once for both its digest
    and its rows; *path* is read only for the container marker and named in errors.

    The result has the shape the ingest pass returns, so the rows are written by the same ``_save_edf_results``.
    The channel block is cleaned in memory only, which gives the database the canonical labels every reader works
    with and keeps the file's own labels in the ``source_*`` fields; the channels stay in the file's order, since
    that is the order the bytes are in. The annotation text stays in the file, and the result says so.
    """
    from recordings.container import is_container
    from recordings.processors.edf import (
        EdfProcessingResult,
        deidentify_signal_infos,
        parse_annotations,
        parse_edf_header,
        parse_signal_infos,
    )

    if is_container(path):
        # The footer of the viewer's container is detached by truncating the file.
        raise LinkedSourceError(f"{path} is a viewer export container, which cannot be stored without rewriting it.")
    header = parse_edf_header(data)
    signal_infos = parse_signal_infos(data, header)
    # The serving paths cut the bytes by the header's record geometry and nothing will ever repair the file, so one
    # that does not match its header is refused now rather than failing for every reader later.
    bytes_per_sample = 3 if header.data_format.rstrip("+") == "bdf" else 2
    record_size = sum(info.sample_count for info in signal_infos) * bytes_per_sample
    header_size = 256 * (1 + header.signal_count)
    if header.data_record_count <= 0 or len(data) != header_size + header.data_record_count * record_size:
        raise LinkedSourceError(
            f"{path} is {len(data)} bytes, which its header's {header.data_record_count} records of {record_size} "
            "bytes do not account for."
        )
    annotations, gaps = parse_annotations(data, header, signal_infos)
    deidentify_signal_infos(signal_infos)
    for position, info in enumerate(signal_infos):
        info.source_index = position
    return EdfProcessingResult(
        header=header,
        signal_infos=signal_infos,
        annotations=annotations,
        gaps=gaps,
        annotation_text_preserved=True,
    )


def linked_file_change(recording) -> str | None:
    """Return why a linked recording's file may not be served, or ``None`` when it may.

    The file must still resolve under a linked root, be ``file_size`` bytes long and not have been modified after
    ``linked_at``. Always ``None`` for a managed recording.
    """
    from recordings.models import Recording

    if recording.storage != Recording.Storage.LINKED:
        return None
    try:
        resolved = Path(recording.file_path).resolve(strict=True)
        stat = resolved.stat()
    except OSError:
        return "the linked file is not reachable"
    if not _under_a_root(resolved):
        return "the linked file is no longer under a linked root"
    if stat.st_size != recording.file_size:
        return "the linked file has changed size since it was linked"
    if recording.linked_at is None or datetime.fromtimestamp(stat.st_mtime, tz=UTC) > recording.linked_at:
        return "the linked file has been modified since it was linked"
    return None


def ensure_linked_file_unchanged(recording) -> None:
    """Raise :class:`LinkedFileChanged` when :func:`linked_file_change` names a reason."""
    reason = linked_file_change(recording)
    if reason is not None:
        raise LinkedFileChanged(reason)


def unlink_recording_file(recording, *, missing_ok: bool = False) -> bool:
    """Remove a recording's file from disk, unless the recording is linked. Returns whether a file was removed.

    The one sanctioned way to delete a recording's file. A managed file is unlinked as the caller asks, with
    ``OSError`` propagating so a caller that keeps the row on failure still can; a linked file belongs to the
    operator and is never touched, so its recording's removal is a removal of rows.
    """
    from recordings.models import Recording

    if recording.storage == Recording.Storage.LINKED:
        return False
    path = Path(recording.file_path)
    if missing_ok and not path.exists():
        return False
    path.unlink()
    return True
