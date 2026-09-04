"""Re-derive a recording's stored signal metadata from the file on disk.

``RecordingMeta`` and its ``SignalInfo`` rows are written once at ingest from the header of the
uploaded file. Anything that rewrites the stored file afterwards — a project's reprocessing stage,
a converter re-run — leaves those rows describing a file that no longer exists.

The drift is not cosmetic. Every byte-serving path sizes the EDF header as
``256 * (1 + meta.signal_count)``, so a stale count makes it read a truncated header; the per-signal
fields past the truncation point then parse as zero rather than raising, and the serving pipeline
computes its record geometry from a record size of nothing. Nothing logs, and the caller receives
bytes cut at the wrong offsets.

:func:`refresh_signal_metadata` is the repair, and the step a rewriting task owes the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from activity.request_context import (
    reset_change_logging_suppressed,
    set_change_logging_suppressed,
)
from recordings.processors.channel_labels import assess_channel_layout

EDF_EXTENSIONS = (".edf", ".bdf")


class MetadataRefreshError(Exception):
    """Nothing could be re-derived: the file is unreadable, malformed, or was never processed."""


@dataclass(frozen=True)
class MetadataRefresh:
    """What a refresh found, and what it changed.

    ``changed`` is the only field a caller normally branches on; the counts are there so a command
    can report the drift it corrected and a task can log it.
    """

    changed: bool
    stored_signal_count: int
    file_signal_count: int
    stored_record_count: int
    file_record_count: int

    @property
    def summary(self) -> str:
        """One line naming the drift, for command output and task logs."""
        return (
            f"signals {self.stored_signal_count} → {self.file_signal_count}, "
            f"records {self.stored_record_count} → {self.file_record_count}"
        )


def _header_length(header, file_size: int, file_path: Path) -> int:
    """Return how many bytes of *file_path* make up the EDF header.

    An EDF header states its own length in bytes 184-191, and that length is also implied by the
    channel count in bytes 252-255 as ``256 * (1 + ns)``. Both come from the file, so they have to
    agree; a file where they do not is malformed and gets no metadata written from it.

    The stated length is not trusted unchecked. ``parse_edf_header`` swallows a malformed length
    field and leaves it ``0``, and nothing downstream range-checks it — a negative value reaches
    ``read()`` as "read to EOF", which on a multi-gigabyte recording is an unbounded allocation
    driven by four bytes of file content.
    """
    implied = 256 * (1 + header.signal_count)
    stated = header.header_record_bytes
    if implied < 512 or implied > file_size:
        raise MetadataRefreshError(
            f"Header of {file_path} declares {header.signal_count} signals, which does not fit the "
            f"{file_size}-byte file."
        )
    # A zero means the field was unreadable, which `parse_edf_header` reports by leaving the
    # default in place; the channel count then stands on its own.
    if stated and stated != implied:
        raise MetadataRefreshError(
            f"Header of {file_path} is self-inconsistent: it states {stated} header bytes but its "
            f"{header.signal_count} signals imply {implied}."
        )
    return implied


def _parsed_descriptors(signal_infos) -> list[tuple]:
    """Return the header-derived part of each parsed channel, in file order.

    Comparing only channel counts would call a recording in sync whenever a rewrite kept the same
    number of channels but changed what they say — a different filter setting or physical range
    leaves the byte layout intact and the descriptors wrong.
    """
    return [
        (
            s.label,
            s.physical_unit,
            s.prefiltering,
            s.physical_min,
            s.physical_max,
            s.digital_min,
            s.digital_max,
            s.sample_count,
            s.is_annotation_channel,
        )
        for s in signal_infos
    ]


def _stored_descriptors(meta) -> list[tuple]:
    """Return the same descriptor tuples as :func:`_parsed_descriptors`, read from the database."""
    from recordings.models import SignalInfo

    return _parsed_descriptors(SignalInfo.objects.filter(meta=meta).order_by("index"))


def _parse_file(recording):
    """Return the ``(header, signal_infos)`` the stored file actually describes."""
    from recordings.processors.edf import parse_edf_header, parse_signal_infos

    file_path = Path(recording.file_path)
    if not file_path.exists() or not file_path.is_file():
        raise MetadataRefreshError(f"Recording file not found on disk: {file_path}")
    file_size = file_path.stat().st_size
    if file_size < 512:
        raise MetadataRefreshError(f"{file_path} is too short to hold an EDF header ({file_size} bytes).")
    try:
        with file_path.open("rb") as fh:
            header = parse_edf_header(fh.read(256))
            fh.seek(0)
            raw_header = fh.read(_header_length(header, file_size, file_path))
        header = parse_edf_header(raw_header)
        signal_infos = parse_signal_infos(raw_header, header)
    except MetadataRefreshError:
        raise
    except Exception as exc:
        raise MetadataRefreshError(f"Could not parse the EDF header of {file_path}: {exc}") from exc
    if len(signal_infos) != header.signal_count:
        raise MetadataRefreshError(
            f"Header of {file_path} declares {header.signal_count} signals but only "
            f"{len(signal_infos)} could be parsed; refusing to write partial metadata."
        )
    return header, signal_infos


def refresh_signal_metadata(recording, *, dry_run: bool = False) -> MetadataRefresh:
    """Rewrite ``RecordingMeta`` and ``SignalInfo`` to match the stored file.

    Re-parses the file's header, and when it disagrees with the database replaces the metadata rows
    and re-baselines the ``SignalInfo`` audit digest on the recording. Returns without writing when
    the two already agree, so a re-run is idempotent and a sweep over an unaffected deployment
    writes nothing.

    Interruptions and the "Original annotations" row are deliberately untouched. They are derived
    from signal *content*, which a header refresh has no view of, and recreating them would
    duplicate rows a user may since have edited.

    Call this inside an audited scope. The digest re-baseline is written either way, but outside a
    scope it lands with no ``Activity`` behind it, so the trail records that the metadata changed
    without recording what changed it.

    Under ``dry_run`` the drift is reported and nothing is written, so a sweep can be inspected
    before it is applied.
    """
    from activity.audit import record_modify_change, serialize_instance
    from recordings.audit_digests import (
        SIGNAL_INFO_DIGEST_KEY,
        compute_signal_info_digest,
    )
    from recordings.models import Recording, RecordingMeta, SignalInfo

    extension = (recording.file_extension or "").lower()
    if extension not in EDF_EXTENSIONS:
        raise MetadataRefreshError(f"Only EDF and BDF recordings carry parseable signal metadata, not {extension!r}.")

    header, signal_infos = _parse_file(recording)
    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    meta = RecordingMeta.objects.filter(content_type=recording_ct, object_id=str(recording.pk)).first()
    if meta is None:
        # Refusing rather than creating one. A recording with no metadata never completed ingest —
        # FAILED recordings are the normal case — and writing rows here would make a file that was
        # never processed indistinguishable from one that was. Producing metadata for the first
        # time is what reprocessing is for.
        raise MetadataRefreshError("Recording has no RecordingMeta to refresh; it has not been processed.")

    in_sync = (
        meta.signal_count == header.signal_count
        and meta.data_record_count == header.data_record_count
        and meta.data_record_duration == header.data_record_duration
        and _stored_descriptors(meta) == _parsed_descriptors(signal_infos)
    )
    result = MetadataRefresh(
        changed=not in_sync,
        stored_signal_count=meta.signal_count,
        file_signal_count=header.signal_count,
        stored_record_count=meta.data_record_count,
        file_record_count=header.data_record_count,
    )
    if in_sync or dry_run:
        return result

    with transaction.atomic():
        meta.format = header.data_format
        meta.duration = header.data_record_count * header.data_record_duration
        meta.data_record_count = header.data_record_count
        meta.data_record_duration = header.data_record_duration
        meta.signal_count = header.signal_count
        meta.discontinuous = header.discontinuous
        # recording_date stays as ingest left it: null. Re-reading it from the file would undo the
        # de-identification that stripped it.
        # The montage-shape assessment is fully re-derivable from the cleaned file (see
        # assess_channel_layout), so it refreshes with the rest of the derived state.
        meta.channel_layout, meta.unresolved_channel_count = assess_channel_layout(signal_infos)
        meta.save()

        # SignalInfo rows are high-fanout derived state: their integrity rides on the digest
        # re-baselined below, not on per-row audit entries. `bulk_create` skips the signals by
        # itself, but `QuerySet.delete()` does not — it collects the rows and fires `pre_delete`
        # for each, which on a 24-channel recording writes 24 delete rows saying nothing the
        # digest does not already cover. Suppressing signal logging across the replacement keeps
        # the trail to the one parent row that carries the digest.
        suppression = set_change_logging_suppressed(True)
        try:
            # The source_* fields are the one part of a SignalInfo row a re-derive cannot
            # reproduce: they hold the pre-de-identification originals captured at ingest, and
            # the file on disk only has the cleaned values. Carry them over by channel index,
            # guarded by label identity: a rewrite stage that preserves a channel preserves its
            # cleaned label, while a rewrite that inserts or reorders channels shifts indices —
            # there the guard drops the provenance for the shifted slots rather than attaching
            # it to the wrong physical channel.
            source_fields = {
                row.index: (
                    row.label,
                    (row.source_label, row.source_transducer_type, row.source_prefiltering, row.source_index),
                )
                for row in SignalInfo.objects.filter(meta=meta)
            }
            SignalInfo.objects.filter(meta=meta).delete()
            _create_signal_rows(meta, signal_infos, source_fields=source_fields)
        finally:
            reset_change_logging_suppressed(suppression)

        # The recording's own fields do not change here, so this row's diff is empty; it exists to
        # carry the new digest. Without it the recording keeps a baseline describing the replaced
        # rows, and every later integrity check reports tampering that never happened.
        fresh = Recording.objects.get(pk=recording.pk)
        record_modify_change(
            actor=None,
            obj=fresh,
            before_state=serialize_instance(fresh),
            extra_payload={SIGNAL_INFO_DIGEST_KEY: compute_signal_info_digest(fresh)},
        )

    return result


def _carried_source_fields(source_fields, idx, new_label) -> tuple[str, str, str, int | None]:
    """Return the prior row's ``source_*`` values for *idx*, or the empty defaults.

    Carried over only when the prior row's label equals *new_label* — index alone is
    not channel identity across a rewrite that inserts or reorders channels.
    """
    prior_label, values = source_fields.get(idx, (None, ("", "", "", None)))
    if prior_label != new_label:
        return ("", "", "", None)
    return values


def _create_signal_rows(meta, signal_infos, source_fields=None) -> None:
    """Write one ``SignalInfo`` row per parsed channel, in header order.

    *source_fields* maps a prior row's channel index to its ``(label, (source_label,
    source_transducer_type, source_prefiltering))`` pair; the triple is carried over
    when the label still matches, and channels with no matching prior row (the file
    gained or shifted channels) get empty strings.
    """
    from recordings.models import SignalInfo

    source_fields = source_fields or {}
    # Same rule as the ingest write site: a deployment that keeps no original
    # header fields must not reacquire them when metadata is refreshed, which
    # rebuilds these rows from whatever the previous ones carried.
    if getattr(settings, "RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA", False):
        source_fields = {}
    rows = []
    for idx, signal in enumerate(signal_infos):
        source_label, source_transducer_type, source_prefiltering, source_index = _carried_source_fields(
            source_fields, idx, signal.label
        )
        rows.append(
            SignalInfo(
                meta=meta,
                index=idx,
                source_label=source_label,
                source_transducer_type=source_transducer_type,
                source_prefiltering=source_prefiltering,
                source_index=source_index,
                label=signal.label,
                signal_type=signal.signal_type,
                canonical_label=signal.canonical_label,
                physical_unit=signal.physical_unit,
                transducer_type=signal.transducer_type,
                prefiltering=signal.prefiltering,
                physical_min=signal.physical_min,
                physical_max=signal.physical_max,
                digital_min=signal.digital_min,
                digital_max=signal.digital_max,
                units_per_bit=signal.units_per_bit,
                digital_offset=signal.digital_offset,
                sample_count=signal.sample_count,
                sampling_rate=signal.sampling_rate,
                highpass=signal.highpass,
                lowpass=signal.lowpass,
                notch=signal.notch,
                is_annotation_channel=signal.is_annotation_channel,
            )
        )
    SignalInfo.objects.bulk_create(rows)
