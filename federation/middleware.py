"""EDF/BDF middleware pipeline for the federated FUSE filesystem and API.

⚠️ LOAD-BEARING — PHI sanitization on the wire.
This file decides what bytes leave the instance when an EDF/BDF file
is served to a federated peer or to any caller whose ``AccessRight``
carries ``apply_middleware=True``.  Three classes of silent failure
are in scope here:

1. **Sanitization regression.** ``AnonymizeEDFHeader`` rewrites patient
   and recording identifiers in the fixed EDF header; if its delegate
   ``recordings.processors.edf._build_clean_header`` drifts (or this
   class stops calling it), un-sanitized headers go out.
2. **Pipeline-ordering or scope-filtering regression.** ``apply_header``,
   ``apply_full``, ``build_signal_context``, ``for_scope``: if any of
   these silently drops middlewares, the configured sanitization stops
   running while the API keeps serving 200s.
3. **Fail-open trade-off.** Every concrete middleware here catches all
   exceptions inside ``transform_header`` / ``transform_record`` and
   falls back to returning the raw bytes.  This is a deliberate design
   choice (filesystem remains accessible for non-standard or
   vendor-extended EDF variants), tested in
   ``test_anonymize_returns_same_length_on_garbage_input``, but it
   means a malformed-but-parseable input could leak PHI.  Errors are
   logged via ``logger.exception`` so forensics has a trail; do not
   "improve" the fall-back to raise without coordinating the
   serving-side behaviour.

See AGENTS.md → *Load-bearing files* before modifying.  Contract tests
are in ``federation/tests/test_middleware.py`` (65 cases covering
pipeline shape, scope filtering, size-preserving properties, the
actual PHI removal — ``"X X X X" in anon_hdr.patient_id`` — and the
fail-open behaviour).  The companion file
``recordings/processors/edf.py`` (still on the LOAD-BEARING candidate
watchlist) implements ``_build_clean_header`` that this module
delegates to.

Overview
--------
Middleware transforms EDF/BDF file content on-the-fly before it is served to
callers.  Three abstract base classes define the available transform types:

Middleware types
~~~~~~~~~~~~~~~~
:class:`EDFHeaderMiddleware`
    Transforms only the EDF/BDF fixed-size header
    (``256 * (1 + signal_count)`` bytes).  The transform **must** be
    *isometric*: the output length must equal the input length.  Suitable for
    field rewrites such as patient / recording identifier removal.

:class:`EDFSignalMiddleware`
    Transforms the EDF/BDF header **and** each data record independently,
    without buffering the full file.  Suitable for structural transforms that
    change the signal layout, such as channel dropping or downsampling.

    The pipeline orchestrator maps output byte ranges algebraically back to
    input record indices, reads only the overlapping records from disk, and
    transforms them.  Peak memory is proportional to the number of records
    that overlap the requested range — not the total file size.

    Requirements for implementors:

    - :meth:`~EDFSignalMiddleware.transform_record` must be **stateless across
      records**: each record is processed independently without context from
      neighbouring records.  For transforms that need cross-record context
      (e.g. FIR/IIR filters) see the design note below.
    - :meth:`~EDFSignalMiddleware.output_record_size` must agree exactly with
      the byte length returned by ``transform_record``.
    - :meth:`~EDFSignalMiddleware.transform_header` must return a structurally
      valid EDF/BDF header consistent with ``output_record_size``.

:class:`EDFFullFileMiddleware`
    Receives the parsed header and the signal data as separate byte strings and
    may return different lengths for either.  Suitable for transforms that
    structurally alter the file when per-record independence cannot be assumed.
    Implementors **must** override :meth:`~EDFFullFileMiddleware.compute_output_size`
    to allow the FUSE catalogue to report the correct ``st_size`` at mount time
    without materialising every recording.

    .. warning::

        Because the FUSE layer serves arbitrary ``(offset, size)`` byte ranges,
        a full-file pipeline must hold the entire transformed file in memory for
        the lifetime of the mount once a file is first accessed.  Plan memory
        budgets accordingly.

        The API download path does **not** buffer full-file pipelines.
        :class:`EDFSignalMiddleware` is the correct choice for transforms
        (downsampling, channel dropping) that should work without buffering on
        HTTP range requests.

Signal middleware and cross-record context (future)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A FIR or IIR filter that needs context around a requested time window can load
a small surrounding window of data records rather than the entire file:

1. Parse ``nr_data_records``, ``dr_duration``, and ``samples_per_record``
   from the EDF header (``recordings.processors.edf.parse_edf_header``).
2. Identify which data records overlap the requested byte range.
3. Extend the fetch window by ``context_records`` on each side
   (configurable per middleware; typically 1–5 seconds' worth of records).
4. Apply the filter kernel to the extended window, then slice out the bytes
   corresponding to the original request.

This keeps peak memory proportional to
``context_seconds × sampling_rate × channel_count × bytes_per_sample``
rather than the full file size.  A future ``context_records`` attribute on
:class:`EDFSignalMiddleware` would expose this to the pipeline orchestrator.

Scope targeting
~~~~~~~~~~~~~~~
Each middleware declares a :attr:`targets` frozenset that controls where it is
active::

    targets = frozenset({"fuse"})         # FUSE virtual filesystem only
    targets = frozenset({"api"})          # download API only
    targets = frozenset({"fuse", "api"})  # both

Non-EDF/BDF files are never processed by EDF middleware, regardless of
``targets``.

Pipeline ordering
~~~~~~~~~~~~~~~~~
:class:`EDFHeaderMiddleware` entries run first (in list order), followed by
:class:`EDFSignalMiddleware` entries (in list order).
:class:`EDFFullFileMiddleware` entries run last (in list order).

Built-in middleware
~~~~~~~~~~~~~~~~~~~
:class:`AnonymizeEDFHeader` — removes patient and recording identifiers from
the fixed header.

:class:`DropChannelsMiddleware` — removes the specified signal channels from
every data record and rewrites the header accordingly.

:class:`DownsampleMiddleware` — reduces the sampling rate of the specified
signal channels by an integer factor via decimation and rewrites the header.

:class:`StripAnnotationTextMiddleware` — removes text labels from EDF+/BDF+
annotation TALs at serve time, keeping only the mandatory timekeeping entries.
Useful when the stored file must remain intact but annotation text should not
be transmitted to federated peers or API consumers.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Literal, NamedTuple

logger = logging.getLogger(__name__)

Scope = Literal["fuse", "api"]


class _SignalInfoLike(NamedTuple):
    """Minimal duck-typed signal info used inside :meth:`MiddlewarePipeline.build_signal_context_from_infos`.

    Middlewares only read ``label``, ``sample_count``, and
    ``is_annotation_channel`` from signal info objects.  This NamedTuple
    satisfies that interface for objects produced by
    :meth:`EDFSignalMiddleware.transform_signal_infos` when the source is a DB
    row (``recordings.models.SignalInfo``) rather than a parsed
    ``recordings.processors.edf.EdfSignalInfo`` dataclass.
    """

    label: str
    sample_count: int
    is_annotation_channel: bool


# ---------------------------------------------------------------------------
# Abstract base classes
# ---------------------------------------------------------------------------


class EDFHeaderMiddleware(ABC):
    """Transform the EDF/BDF fixed-size header.  Output must be isometric.

    Applied before any :class:`EDFSignalMiddleware` or
    :class:`EDFFullFileMiddleware` in the pipeline.
    Non-EDF files are never passed to this middleware.
    """

    #: Scopes in which this middleware is active.  Override in subclasses.
    targets: frozenset[Scope] = frozenset({"fuse", "api"})

    @abstractmethod
    def transform_header(self, raw_header: bytes) -> bytes:
        """Return the transformed header.

        *raw_header* is the complete EDF/BDF header
        (``256 * (1 + signal_count)`` bytes).  The returned bytes **must** be
        exactly the same length as the input.
        """


class EDFSignalMiddleware(ABC):
    """Transform EDF/BDF signals on a per-data-record basis without full-file buffering.

    Applied after all :class:`EDFHeaderMiddleware` entries.  Each data record
    is processed independently; the pipeline orchestrator maps output byte
    ranges back to the minimal set of input records, transforms only those, and
    assembles the slice.

    Non-EDF files are never passed to this middleware.
    """

    #: Scopes in which this middleware is active.  Override in subclasses.
    targets: frozenset[Scope] = frozenset({"fuse", "api"})

    #: True when every data record produced by this middleware has the same byte
    #: length as the corresponding input record.  Subclasses that only rewrite
    #: record *content* without changing the layout (e.g.
    #: :class:`StripAnnotationTextMiddleware`) should set this to ``True`` so
    #: that :meth:`MiddlewarePipeline.is_size_preserving` can report an accurate
    #: file-size guarantee without reading the on-disk EDF header.
    size_invariant: bool = False

    @abstractmethod
    def transform_header(self, raw_header: bytes) -> bytes:
        """Return a new EDF/BDF header reflecting the transformed signal layout.

        May change ``ns``, ``sample_count`` values, or any other header field.
        The returned header must be structurally self-consistent (correct
        ``header_record_bytes``, correct ``ns``, etc.).

        Unlike :class:`EDFHeaderMiddleware`, the output need **not** be the
        same length as the input.
        """

    @abstractmethod
    def output_record_size(self, signal_infos: list, bytes_per_sample: int) -> int:
        """Return the byte length of one transformed data record.

        Must equal the exact byte count that :meth:`transform_record` returns.
        *signal_infos* and *bytes_per_sample* describe the **input** records
        for this middleware step.
        """

    @abstractmethod
    def transform_record(self, record_bytes: bytes, signal_infos: list, bytes_per_sample: int) -> bytes:
        """Transform a single data record and return the result.

        *record_bytes* contains all channel samples for one record, interleaved
        per the EDF specification (all samples of channel 0, then all of
        channel 1, …).  *signal_infos* and *bytes_per_sample* describe the
        **input** record for this step.
        """

    def transform_signal_infos(self, signal_infos: list) -> list:
        """Return the signal info list after this middleware's structural transform.

        Used by :meth:`MiddlewarePipeline.build_signal_context_from_infos` to
        propagate the per-channel layout through the pipeline without parsing
        raw header bytes.  Each element only needs ``label``, ``sample_count``,
        and ``is_annotation_channel`` attributes.

        The default implementation returns *signal_infos* unchanged, which is
        correct for middlewares that do not alter channel count or sample counts
        (e.g. :class:`StripAnnotationTextMiddleware`).  Override in subclasses
        that add, remove, or resample channels.
        """
        return signal_infos


class EDFFullFileMiddleware(ABC):
    """Transform EDF/BDF header and signal data together.

    Applied after all :class:`EDFHeaderMiddleware` and
    :class:`EDFSignalMiddleware` entries.  May return header and/or signal
    bytes of different lengths (e.g. down-sampling reduces signal data volume
    and alters ``samples_per_record`` in the header).

    Implementors **must** override :meth:`compute_output_size` so that the FUSE
    catalogue can report a correct ``st_size`` without materialising every
    recording at mount time.
    """

    #: Scopes in which this middleware is active.  Override in subclasses.
    targets: frozenset[Scope] = frozenset({"fuse", "api"})

    @abstractmethod
    def transform(self, raw_header: bytes, raw_signals: bytes) -> tuple[bytes, bytes]:
        """Return *(new_header, new_signals)*.

        Both return values may differ in length from the inputs.  *raw_header*
        has already been processed by all :class:`EDFHeaderMiddleware` and
        :class:`EDFSignalMiddleware` entries in the pipeline.
        """

    @abstractmethod
    def compute_output_size(self, file_size: int, header_size: int) -> int:
        """Return the expected output file size without processing any data.

        Called at catalogue load time for every EDF/BDF file so that
        ``getattr`` can report a correct ``st_size``.  Must be a pure function
        of the integer arguments — do **not** fetch or read data here.

        *file_size* is the remote file size in bytes.
        *header_size* is ``256 * (1 + signal_count)`` (always exact per the
        EDF specification).
        """


# ---------------------------------------------------------------------------
# Signal pipeline context
# ---------------------------------------------------------------------------


class SignalPipelineContext:
    """Precomputed layout for range-aware serving of a signal-middleware pipeline.

    Returned by :meth:`MiddlewarePipeline.build_signal_context`.  Holds
    everything needed to serve arbitrary byte ranges from the virtual
    (post-transform) file without buffering the full content.

    Attributes
    ----------
    new_header : bytes or None
        The complete transformed EDF/BDF header, or ``None`` when the context
        was built from structured metadata (e.g. DB rows) without a raw header
        available.  ``None`` contexts are valid for size computation and record
        transforms but cannot serve header bytes directly.
    new_header_size : int
        Byte length of the transformed header — may differ from the original
        header size when channel count changes.  Always set, even when
        ``new_header`` is ``None``.
    input_record_size : int
        Byte size of one data record in the **original** (on-disk) file.
    output_record_size : int
        Byte size of one data record in the **transformed** output.
    n_records : int
        Total number of data records (unchanged by signal transforms).
    output_file_size : int
        ``new_header_size + n_records * output_record_size``.
    """

    def __init__(
        self,
        new_header: bytes | None,
        input_record_size: int,
        output_record_size: int,
        n_records: int,
        chain: list,  # [(EDFSignalMiddleware, signal_infos, bytes_per_sample), ...]
        new_header_size: int | None = None,
    ) -> None:
        self.new_header = new_header
        if new_header is not None:
            self.new_header_size = len(new_header)
        elif new_header_size is not None:
            self.new_header_size = new_header_size
        else:
            raise ValueError("new_header_size is required when new_header is None")
        self.input_record_size = input_record_size
        self.output_record_size = output_record_size
        self.n_records = n_records
        self.output_file_size = self.new_header_size + n_records * output_record_size
        self._chain = chain

    def transform_record(self, record_bytes: bytes) -> bytes:
        """Apply all signal middleware steps to a single input record."""
        result = record_bytes
        for m, infos, bps in self._chain:
            result = m.transform_record(result, infos, bps)
        return result


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class MiddlewarePipeline:
    """Ordered sequence of EDF middlewares, filterable by scope.

    Execution order: :class:`EDFHeaderMiddleware` first (in list order), then
    :class:`EDFSignalMiddleware` (in list order), then
    :class:`EDFFullFileMiddleware` (in list order).
    Non-EDF/BDF files are never passed to any middleware.
    """

    def __init__(
        self,
        middlewares: list[EDFHeaderMiddleware | EDFSignalMiddleware | EDFFullFileMiddleware],
    ) -> None:
        self._middlewares: list = list(middlewares)

    @property
    def is_empty(self) -> bool:
        return not self._middlewares

    @property
    def is_isometric(self) -> bool:
        """True when no middleware changes the total byte count.

        An isometric pipeline allows the FUSE layer to stream signal bytes
        directly from the remote without buffering the full file.
        Only pipelines composed entirely of :class:`EDFHeaderMiddleware`
        entries are isometric.
        """
        return not any(isinstance(m, (EDFSignalMiddleware, EDFFullFileMiddleware)) for m in self._middlewares)

    @property
    def is_size_preserving(self) -> bool:
        """True when the total output file size equals the input file size.

        Like :attr:`is_isometric` but also accepts
        :class:`EDFSignalMiddleware` entries that declare
        ``size_invariant = True`` — i.e. middlewares that transform record
        *content* without changing the byte length of each record (for
        example :class:`StripAnnotationTextMiddleware`).

        Use this property instead of :attr:`is_isometric` when computing
        ``download_size``: size-preserving pipelines can skip the on-disk
        EDF header read and return ``file_size`` directly.
        """
        for m in self._middlewares:
            if isinstance(m, EDFFullFileMiddleware):
                return False
            if isinstance(m, EDFSignalMiddleware) and not m.size_invariant:
                return False
        return True

    @property
    def has_signal_middleware(self) -> bool:
        """True when at least one :class:`EDFSignalMiddleware` is present."""
        return any(isinstance(m, EDFSignalMiddleware) for m in self._middlewares)

    def for_scope(self, scope: Scope) -> MiddlewarePipeline:
        """Return a new pipeline containing only middlewares active for *scope*."""
        return MiddlewarePipeline([m for m in self._middlewares if scope in m.targets])

    def compute_output_size(self, file_size: int, header_size: int) -> int:
        """Compute the post-transform file size using declared size functions only.

        For isometric pipelines returns *file_size* unchanged.
        :class:`EDFFullFileMiddleware` entries are chained: each receives the
        cumulative output size of the previous entry.

        .. note::
            :class:`EDFSignalMiddleware` entries are **not** accounted for here
            because computing their output size requires reading the file header.
            Use :meth:`build_signal_context` to obtain the exact output size
            for signal-middleware pipelines on the API download path.
        """
        if self.is_isometric:
            return file_size
        size = file_size
        for m in self._middlewares:
            if isinstance(m, EDFFullFileMiddleware):
                size = m.compute_output_size(size, header_size)
        return size

    def apply_header(self, raw_header: bytes) -> bytes:
        """Apply all :class:`EDFHeaderMiddleware` entries in list order."""
        result = raw_header
        for m in self._middlewares:
            if isinstance(m, EDFHeaderMiddleware):
                result = m.transform_header(result)
        return result

    def apply_full(self, raw_header: bytes, raw_signals: bytes) -> tuple[bytes, bytes]:
        """Apply the complete pipeline: header middlewares then full-file.

        Returns *(transformed_header, transformed_signals)*.
        :class:`EDFSignalMiddleware` entries are skipped by this method;
        use :meth:`build_signal_context` instead for signal transforms.
        """
        header = self.apply_header(raw_header)
        signals = raw_signals
        for m in self._middlewares:
            if isinstance(m, EDFFullFileMiddleware):
                header, signals = m.transform(header, signals)
        return header, signals

    def build_signal_context(self, raw_header: bytes, n_records: int) -> SignalPipelineContext:
        """Precompute the layout for range-aware signal-middleware serving.

        Parses *raw_header* (the on-disk header, before any transforms) and
        threads it through the :class:`EDFHeaderMiddleware` steps first, then
        through each :class:`EDFSignalMiddleware` step in order, recording the
        signal layout at each stage.

        Returns a :class:`SignalPipelineContext` that can transform individual
        records and map output byte ranges to input record indices.
        """
        from recordings.processors.edf import parse_edf_header, parse_signal_infos

        # Phase 1: header-only middlewares (isometric, header size unchanged).
        current_header = self.apply_header(raw_header)

        # Phase 2: signal middlewares — track signal layout at each step.
        #
        # Signal layout is parsed from a separate tracker that starts at
        # raw_header.  EDFHeaderMiddleware transforms are isometric and never
        # alter the signal structure (ns, sample_counts), so raw_header and
        # apply_header(raw_header) carry identical signal layouts.  Using
        # raw_header here makes the method robust when header middlewares
        # produce bytes that are not individually parseable as EDF (e.g. test
        # mocks that replace the header with a sentinel value).
        #
        # current_header accumulates all transforms (Phase 1 + Phase 2 signal
        # transform_header calls) so that SignalPipelineContext.new_header
        # reflects the complete pipeline output.
        current_layout_header = raw_header
        chain: list = []  # [(EDFSignalMiddleware, signal_infos, bytes_per_sample)]
        for m in self._middlewares:
            if not isinstance(m, EDFSignalMiddleware):
                continue
            hdr = parse_edf_header(current_layout_header)
            sig_infos = parse_signal_infos(current_layout_header, hdr)
            base_fmt = hdr.data_format.rstrip("+")
            bps = 3 if base_fmt == "bdf" else 2
            chain.append((m, sig_infos, bps))
            current_layout_header = m.transform_header(current_layout_header)
            current_header = m.transform_header(current_header)

        # Compute input record size (from original file, before any transforms).
        if chain:
            first_infos, first_bps = chain[0][1], chain[0][2]
            input_rec_size = sum(si.sample_count * first_bps for si in first_infos)
        else:
            # No signal middleware — sizes unchanged.
            hdr = parse_edf_header(current_layout_header)
            sig_infos = parse_signal_infos(current_layout_header, hdr)
            base_fmt = hdr.data_format.rstrip("+")
            bps = 3 if base_fmt == "bdf" else 2
            input_rec_size = sum(si.sample_count * bps for si in sig_infos)

        # Compute output record size (from the last signal middleware step).
        if chain:
            last_m, last_infos, last_bps = chain[-1]
            out_rec_size = last_m.output_record_size(last_infos, last_bps)
        else:
            out_rec_size = input_rec_size

        return SignalPipelineContext(
            new_header=current_header,
            input_record_size=input_rec_size,
            output_record_size=out_rec_size,
            n_records=n_records,
            chain=chain,
        )

    def build_signal_context_from_infos(
        self,
        signal_infos: list,
        bps: int,
        n_records: int,
        header_size: int,
        raw_header: bytes | None = None,
    ) -> SignalPipelineContext:
        """Build a :class:`SignalPipelineContext` from pre-parsed signal metadata.

        Unlike :meth:`build_signal_context`, which reads an on-disk EDF header,
        this method works entirely from structured signal info objects —
        suitable for DB-sourced ``recordings.models.SignalInfo`` rows or
        per-channel dicts from a federation catalogue response.

        Each element of *signal_infos* must expose ``label``, ``sample_count``,
        and ``is_annotation_channel`` attributes (duck-typed; both Django model
        instances and :class:`_SignalInfoLike` tuples qualify).

        Parameters
        ----------
        signal_infos:
            Per-channel signal info objects in channel-index order.
        bps:
            Bytes per sample: 2 for EDF/EDF+, 3 for BDF/BDF+.
        n_records:
            Total number of data records.
        header_size:
            Input header byte count (``256 * (1 + signal_count)``).
        raw_header:
            Optional raw EDF/BDF header bytes.  When provided, it is threaded
            through :meth:`apply_header` and each
            :class:`EDFSignalMiddleware`'s ``transform_header`` to populate
            :attr:`~SignalPipelineContext.new_header` in the returned context
            (identical behaviour to :meth:`build_signal_context`).  When
            ``None`` the context has ``new_header = None`` and is suitable for
            size computation only — pass it to a serving path only after
            filling ``new_header`` separately.

        Returns
        -------
        SignalPipelineContext
            Fully populated context.  ``output_file_size`` is always accurate.
            ``new_header`` is ``None`` when *raw_header* was not provided.
        """
        current_layout_infos: list = list(signal_infos)
        chain: list = []  # [(EDFSignalMiddleware, signal_infos, bps), ...]

        current_header: bytes | None = self.apply_header(raw_header) if raw_header is not None else None

        for m in self._middlewares:
            if not isinstance(m, EDFSignalMiddleware):
                continue
            chain.append((m, current_layout_infos, bps))
            current_layout_infos = m.transform_signal_infos(current_layout_infos)
            if current_header is not None:
                current_header = m.transform_header(current_header)

        # Input record size is determined by the infos at the first step.
        if chain:
            first_infos = chain[0][1]
            input_rec_size = sum(si.sample_count * bps for si in first_infos)
        else:
            input_rec_size = sum(si.sample_count * bps for si in signal_infos)

        # Output record size is determined by the last step's output infos.
        if chain:
            last_m, last_infos, last_bps = chain[-1]
            out_rec_size = last_m.output_record_size(last_infos, last_bps)
        else:
            out_rec_size = input_rec_size

        # Output header size derived from final signal count; may differ from
        # header_size when channel count changes (e.g. DropChannelsMiddleware).
        output_header_size = 256 * (1 + len(current_layout_infos))

        return SignalPipelineContext(
            new_header=current_header,
            input_record_size=input_rec_size,
            output_record_size=out_rec_size,
            n_records=n_records,
            chain=chain,
            new_header_size=output_header_size,
        )


# ---------------------------------------------------------------------------
# Built-in implementations
# ---------------------------------------------------------------------------


class AnonymizeEDFHeader(EDFHeaderMiddleware):
    """Remove patient, recording, and channel-block identifiers from EDF/BDF headers.

    Rewrites the *local patient identification* field to ``"X X X X"`` and the
    *local recording identification* date to ``"01.01.85"``, delegating to
    ``recordings.processors.edf._build_clean_header``, and applies the same
    channel-block de-identification as ingest
    (``recordings.processors.edf.deidentify_signal_infos``): canonical or
    ``MISC<n>`` labels, blanked transducer strings, reconstructed prefiltering.
    For a file the platform ingested this is a no-op — the stored bytes already
    carry both transforms — so the layer exists as defense in depth for bytes
    not produced by this platform's ingest (see
    docs/engineering-notes/channel-deidentification-plan.md, Phase 2).

    This is the default middleware applied by the federated FUSE filesystem when
    no custom :class:`MiddlewarePipeline` is supplied.

    The transform is *isometric*: the output is always exactly the same length
    as the input.  Falls back to returning the raw header unchanged on parse
    errors so the filesystem remains accessible for non-standard or
    vendor-extended EDF variants.
    """

    targets: frozenset[Scope] = frozenset({"fuse", "api"})

    def transform_header(self, raw_header: bytes) -> bytes:
        try:
            from recordings.processors.edf import (
                _build_clean_header,
                deidentify_signal_infos,
                parse_edf_header,
                parse_signal_infos,
            )

            header = parse_edf_header(raw_header)
            signal_infos = parse_signal_infos(raw_header, header)
            deidentify_signal_infos(signal_infos)
            return _build_clean_header(header, signal_infos)
        except Exception:
            logger.exception("EDF header anonymization failed; serving raw header instead")
            return raw_header


class DropChannelsMiddleware(EDFSignalMiddleware):
    """Remove the specified signal channels from every data record.

    The header is rebuilt with the remaining channels only (using
    :func:`recordings.processors.edf.build_header`, which preserves patient
    and recording identification fields).  Annotation channels (``edf
    annotations`` / ``bdf annotations``) are never dropped regardless of the
    *drop_labels* list.

    Combine with :class:`AnonymizeEDFHeader` if de-identification is also
    required::

        pipeline = MiddlewarePipeline([
            AnonymizeEDFHeader(),
            DropChannelsMiddleware(["EEG Fp1-Cz", "EMG"]),
        ])

    Parameters
    ----------
    drop_labels:
        Case-insensitive channel labels to remove.  Labels are matched after
        stripping surrounding whitespace.
    """

    targets: frozenset[Scope] = frozenset({"fuse", "api"})

    def __init__(self, drop_labels: list[str]) -> None:
        self._drop = frozenset(lbl.strip().upper() for lbl in drop_labels)

    def _kept_indices(self, signal_infos: list) -> list[int]:
        """Indices of channels that survive the drop, preserving annotation channels."""
        return [
            i
            for i, si in enumerate(signal_infos)
            if si.label.strip().upper() not in self._drop or si.is_annotation_channel
        ]

    def transform_header(self, raw_header: bytes) -> bytes:
        try:
            from recordings.processors.edf import (
                build_header,
                parse_edf_header,
                parse_signal_infos,
            )

            header = parse_edf_header(raw_header)
            sig_infos = parse_signal_infos(raw_header, header)
            kept = [sig_infos[i] for i in self._kept_indices(sig_infos)]
            header.signal_count = len(kept)
            header.data_record_count = header.data_record_count  # unchanged
            return build_header(header, kept)
        except Exception:
            logger.exception("DropChannelsMiddleware.transform_header failed; returning raw header")
            return raw_header

    def transform_signal_infos(self, signal_infos: list) -> list:
        return [signal_infos[i] for i in self._kept_indices(signal_infos)]

    def output_record_size(self, signal_infos: list, bytes_per_sample: int) -> int:
        kept = self._kept_indices(signal_infos)
        return sum(signal_infos[i].sample_count * bytes_per_sample for i in kept)

    def transform_record(self, record_bytes: bytes, signal_infos: list, bytes_per_sample: int) -> bytes:
        kept = self._kept_indices(signal_infos)
        kept_set = set(kept)
        result = bytearray()
        offset = 0
        for i, si in enumerate(signal_infos):
            size = si.sample_count * bytes_per_sample
            if i in kept_set:
                result += record_bytes[offset : offset + size]
            offset += size
        return bytes(result)


class DropAnnotationChannelsMiddleware(EDFSignalMiddleware):
    """Remove EDF+ / BDF+ annotation channels from the output.

    Unlike :class:`DropChannelsMiddleware` (which protects annotation channels
    from being dropped), this middleware specifically targets them.  Its
    intended use is epoch-file serving: the TAL timestamps in an EDF+ slice
    reference the *original recording* timeline, not the slice, so they are
    meaningless and confuse viewers that expect timekeeping onsets to start at
    zero.  Removing the annotation channels converts the slice to a plain EDF
    that any standard reader handles correctly.

    If the recording has no annotation channels the middleware is a no-op.
    """

    targets: frozenset[Scope] = frozenset({"api"})
    size_invariant = False

    def _kept_indices(self, signal_infos: list) -> list[int]:
        return [i for i, si in enumerate(signal_infos) if not si.is_annotation_channel]

    def transform_header(self, raw_header: bytes) -> bytes:
        try:
            from recordings.processors.edf import (
                build_header,
                parse_edf_header,
                parse_signal_infos,
            )

            header = parse_edf_header(raw_header)
            sig_infos = parse_signal_infos(raw_header, header)
            kept_idx = self._kept_indices(sig_infos)
            if len(kept_idx) == len(sig_infos):
                return raw_header  # no annotation channels — nothing to do
            kept = [sig_infos[i] for i in kept_idx]
            header.signal_count = len(kept)
            # Downgrade EDF+/BDF+ → plain EDF/BDF.  The EDF+ spec requires an
            # annotation channel to be present; removing it while leaving the
            # "EDF+C" reserved field produces a malformed file.  Clearing the
            # reserved field and stripping the "+" from data_format makes the
            # slice a valid plain EDF that any standard reader handles correctly.
            # Epoch files never contain data gaps, so EDF+D support is not needed.
            if header.is_plus:
                header.data_format = header.data_format.rstrip("+")  # 'edf+' → 'edf'
                header.reserved = " " * len(header.reserved)
                header.is_plus = False
                header.discontinuous = False
            return build_header(header, kept)
        except Exception:
            logger.exception("DropAnnotationChannelsMiddleware.transform_header failed; returning raw header")
            return raw_header

    def transform_signal_infos(self, signal_infos: list) -> list:
        return [signal_infos[i] for i in self._kept_indices(signal_infos)]

    def output_record_size(self, signal_infos: list, bytes_per_sample: int) -> int:
        return sum(signal_infos[i].sample_count * bytes_per_sample for i in self._kept_indices(signal_infos))

    def transform_record(self, record_bytes: bytes, signal_infos: list, bytes_per_sample: int) -> bytes:
        kept = set(self._kept_indices(signal_infos))
        result = bytearray()
        offset = 0
        for i, si in enumerate(signal_infos):
            size = si.sample_count * bytes_per_sample
            if i in kept:
                result += record_bytes[offset : offset + size]
            offset += size
        return bytes(result)


class DownsampleMiddleware(EDFSignalMiddleware):
    """Reduce the sampling rate of signal channels by an integer factor.

    Every *factor*-th sample is kept (naive decimation — no anti-aliasing
    filter is applied).  Annotation channels are never downsampled.

    The header is rebuilt with updated ``samples_per_record`` values.

    Combine with :class:`AnonymizeEDFHeader` if de-identification is also
    required::

        pipeline = MiddlewarePipeline([
            AnonymizeEDFHeader(),
            DownsampleMiddleware(factor=4),
        ])

    Parameters
    ----------
    factor:
        Decimation factor (must be ≥ 2).  The output sampling rate is
        approximately ``original_rate / factor``.
    channels:
        Case-insensitive channel labels to downsample.  ``None`` (default)
        downsamples all non-annotation channels.
    """

    targets: frozenset[Scope] = frozenset({"fuse", "api"})

    def __init__(self, factor: int, channels: list[str] | None = None) -> None:
        if factor < 2:
            raise ValueError(f"Downsample factor must be >= 2, got {factor}")
        self.factor = factor
        self._channels: frozenset[str] | None = (
            None if channels is None else frozenset(c.strip().upper() for c in channels)
        )

    def _affects(self, si) -> bool:
        """True when this channel should be downsampled."""
        if si.is_annotation_channel:
            return False
        if self._channels is None:
            return True
        return si.label.strip().upper() in self._channels

    def transform_header(self, raw_header: bytes) -> bytes:
        try:
            from dataclasses import replace as _dc_replace

            from recordings.processors.edf import (
                build_header,
                parse_edf_header,
                parse_signal_infos,
            )

            header = parse_edf_header(raw_header)
            sig_infos = parse_signal_infos(raw_header, header)
            new_infos = []
            for si in sig_infos:
                if self._affects(si):
                    new_sc = max(1, si.sample_count // self.factor)
                    new_infos.append(_dc_replace(si, sample_count=new_sc))
                else:
                    new_infos.append(si)
            return build_header(header, new_infos)
        except Exception:
            logger.exception("DownsampleMiddleware.transform_header failed; returning raw header")
            return raw_header

    def transform_signal_infos(self, signal_infos: list) -> list:
        return [
            _SignalInfoLike(
                label=si.label,
                sample_count=max(1, si.sample_count // self.factor) if self._affects(si) else si.sample_count,
                is_annotation_channel=si.is_annotation_channel,
            )
            for si in signal_infos
        ]

    def output_record_size(self, signal_infos: list, bytes_per_sample: int) -> int:
        total = 0
        for si in signal_infos:
            if self._affects(si):
                total += max(1, si.sample_count // self.factor) * bytes_per_sample
            else:
                total += si.sample_count * bytes_per_sample
        return total

    def transform_record(self, record_bytes: bytes, signal_infos: list, bytes_per_sample: int) -> bytes:
        result = bytearray()
        offset = 0
        for si in signal_infos:
            size = si.sample_count * bytes_per_sample
            chunk = record_bytes[offset : offset + size]
            if self._affects(si):
                # Decimate: take every factor-th sample.
                samples = [chunk[i : i + bytes_per_sample] for i in range(0, size, bytes_per_sample)]
                result += b"".join(samples[:: self.factor])
            else:
                result += chunk
            offset += size
        return bytes(result)


class StripAnnotationTextMiddleware(EDFSignalMiddleware):
    """Remove annotation text labels from EDF+/BDF+ TALs at serve time.

    For each annotation channel in every data record, retains only the
    mandatory timekeeping TAL (which carries the record's start time) and
    replaces all text annotation TALs with null-byte padding.  Signal
    channels are never touched; the header and per-record byte lengths are
    unchanged.

    This allows EDF+ recordings to be served over federation (or to any
    user whose ``AccessRight`` has ``apply_middleware=True``) without
    exposing embedded clinical notes, while keeping the timekeeping
    structure valid and the file size identical to the original.

    EDF files without annotation channels are passed through untouched.
    Falls back to preserving raw annotation channel bytes on TAL parse
    errors, so the file remains accessible for non-standard EDF variants.

    Combine with :class:`AnonymizeEDFHeader` for full de-identification::

        pipeline = MiddlewarePipeline([
            AnonymizeEDFHeader(),
            StripAnnotationTextMiddleware(),
        ])
    """

    targets: frozenset[Scope] = frozenset({"fuse", "api"})
    size_invariant: bool = True

    def transform_header(self, raw_header: bytes) -> bytes:
        # Annotation channel structure (sample_count, label) is unchanged;
        # only data-record content is modified.
        return raw_header

    def output_record_size(self, signal_infos: list, bytes_per_sample: int) -> int:
        return sum(si.sample_count * bytes_per_sample for si in signal_infos)

    def transform_record(self, record_bytes: bytes, signal_infos: list, bytes_per_sample: int) -> bytes:
        from recordings.processors.edf import _encode_timekeeping_tal, _parse_tal_record

        result = bytearray(record_bytes)
        offset = 0
        for si in signal_infos:
            size = si.sample_count * bytes_per_sample
            if si.is_annotation_channel:
                channel_bytes = record_bytes[offset : offset + size]
                try:
                    record_onset, _ = _parse_tal_record(channel_bytes)
                    if record_onset is not None:
                        tal = _encode_timekeeping_tal(record_onset)
                        result[offset : offset + size] = tal + bytes(size - len(tal))
                    # If no timekeeping TAL found, leave channel bytes unchanged.
                except Exception:
                    logger.exception(
                        "StripAnnotationTextMiddleware: TAL parse error; serving raw annotation channel bytes"
                    )
            offset += size
        return bytes(result)
