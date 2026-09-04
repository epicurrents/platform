"""Federated FUSE filesystem for Epicurrents.

Mounts a virtual directory tree of recordings shared by federated peers as
ordinary read-only local files.  Non-EDF files are proxied byte-for-byte.
EDF/BDF files may be transformed in two independent, layered stages described
below.

Directory layout
----------------
::

    <mountpoint>/
        <peer-slug>/           one directory per trusted peer
            <filename>         one file per accessible recording

The ``peer-slug`` is derived from the peer's URL by replacing non-alphanumeric
characters with underscores, so ``https://neuro.example.com`` becomes
``neuro.example.com``.

Two-layer EDF/BDF transform model
----------------------------------
Data shared via federation may need to be transformed for two distinct reasons
that are deliberately kept separate:

**Layer 1 — Server-side privacy control (authoritative)**
    The *serving* instance decides whether the bytes it sends over the wire are
    anonymised.  This is configured by setting ``apply_middleware=True`` on the
    ``AccessRight`` federation grant for the peer.  When set, the server runs
    its own middleware pipeline (default: :class:`~federation.middleware.AnonymizeEDFHeader`)
    before transmitting and the mounting instance receives already-anonymised bytes.
    This is the correct place to enforce privacy: the data owner controls it and
    it cannot be bypassed by the mounting instance.

**Layer 2 — Local post-processing (optional)**
    The *mounting* instance may supply an additional
    :class:`~federation.middleware.MiddlewarePipeline` to ``mount_federation_fs``
    for analysis-specific transforms — for example, dropping irrelevant channels
    or downsampling before handing bytes to a Python analysis library.  This
    pipeline is applied *after* the server bytes arrive and is purely a local
    convenience; it carries no privacy guarantees.

    The default local pipeline is empty (no-op).  Pass an explicit pipeline only
    when local post-processing is needed.

How reads work
--------------
Each ``read(path, size, offset)`` call:

1. Generates a short-lived ``FederatedBearer`` JWT for the owning peer.
2. Issues an HTTP ``Range: bytes=<offset>-<end>`` request to the remote
   ``/recordings/api/v1/<hash>/file`` endpoint. Bytes returned by the server are
   already at Layer 1 anonymisation level (raw if the grant has
   ``apply_middleware=False``, anonymised if it has ``apply_middleware=True``).
3. **For EDF/BDF files:** if a local post-processing pipeline was supplied at
   mount time, it is applied to the received bytes as Layer 2.
4. **For all other file types:** bytes are returned verbatim.

Local pipeline strategies
--------------------------
When a local pipeline is active, one of three strategies is used depending on
the pipeline composition:

*Isometric* (header-only :class:`~federation.middleware.EDFHeaderMiddleware`):
    The transformed header is fetched once, cached in ``_TransformCache``, and
    substituted for the corresponding bytes in each read.  Signal bytes are
    streamed directly from the remote, keeping memory usage bounded.  File sizes
    are unchanged.

*Signal middleware* (:class:`~federation.middleware.EDFSignalMiddleware`):
    The header is rewritten and each data record is transformed independently.
    At mount time a :class:`~federation.middleware.SignalPipelineContext` is
    pre-computed for each accessible EDF recording so that ``stat()`` sizes are
    accurate immediately.  The context is built from the per-channel ``signals``
    data included in the catalogue response via
    :meth:`~federation.middleware.MiddlewarePipeline.build_signal_context_from_infos`
    — no extra network I/O.  Each ``read()`` maps the output byte range to the
    overlapping input records, fetches them in a single HTTP range request,
    transforms each record, and slices to the exact bytes requested — peak
    memory proportional to the overlapping records, not the full file.

*Full-file* (:class:`~federation.middleware.EDFFullFileMiddleware`):
    The entire transformed file is cached on first access.  ``stat()`` sizes are
    pre-computed at catalogue load time.

    .. warning::

        Full-file pipelines hold complete transformed files in memory for the
        lifetime of the mount.  For large catalogues or large recordings, size
        the mount host accordingly.  A future revision may add LRU eviction.

EDF/BDF header size
-------------------
Per the EDF specification the header is always ``256 * (1 + ns)`` bytes where
``ns`` is the number of signal channels, deterministic from the
``RecordingMeta.signal_count`` stored locally.  The ``st_size`` reported by
``getattr`` reflects the post-local-pipeline output size (equal to the remote
size when no local pipeline is active).

Requirements
------------
- ``pip install fusepy`` (PyPI package ``fusepy``).
- Linux: ``sudo apt install libfuse2``.
- macOS: install macFUSE from https://osxfuse.github.io/

Mount via the management command::

    python manage.py mount_federation_fs /mnt/fed --user-id 1
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from federation.middleware import MiddlewarePipeline, SignalPipelineContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


class _PeerDir(NamedTuple):
    slug: str
    peer_url: str


class _RecordingFile(NamedTuple):
    slug: str
    peer_url: str
    recording_hash: str  # 32-char hex upper-case token from stored_name
    filename: str  # how the file appears in the virtual filesystem
    file_size: int  # bytes reported by getattr (post-pipeline output size)
    header_size: int  # EDF/BDF header bytes (256*(1+ns)); 0 for non-EDF
    is_edf: bool  # True for EDF or BDF (including +/+D variants)
    remote_file_size: int = 0  # original bytes on the peer (used for raw fetches)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _make_jwt(peer_url: str, local_user_id: str) -> str:
    """Sign a short-lived outbound JWT for *peer_url* on behalf of *local_user_id*."""
    from federation.auth import (
        create_jwt,
        get_local_instance_url,
        get_local_private_key,
    )

    return create_jwt(
        get_local_private_key(),
        issuer=get_local_instance_url(),
        audience=peer_url,
        subject=str(local_user_id),
        ttl=60,
    )


def _http_range(url: str, jwt: str, start: int, end: int, timeout: int = 30) -> bytes:
    """Fetch bytes *start*-*end* (inclusive) from *url* with a FederatedBearer JWT.

    Accepts both 206 Partial Content and 200 OK (some HTTP servers return 200
    when the range covers the entire file).

    Uses the strict TLS context from :func:`federation.auth._build_tls_context`
    so the security posture is the same as ``fetch_peer_public_key``: a single
    explicit helper rather than relying on Python's default.

    Raises urllib.error.URLError / urllib.error.HTTPError on network or
    authorization failures.
    """
    from federation.auth import _build_tls_context

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"FederatedBearer {jwt}",
            "Range": f"bytes={start}-{end}",
            "User-Agent": "epicurrents-fuse/1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_build_tls_context()) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Transform cache
# ---------------------------------------------------------------------------


class _TransformCache:
    """Thread-safe cache for middleware-transformed EDF/BDF file content.

    For *isometric* pipelines (header-only middleware): caches only the
    transformed header bytes.  Signal data is fetched raw from the remote on
    each read, keeping memory usage bounded.

    For *full-file* pipelines (any
    :class:`~federation.middleware.EDFFullFileMiddleware` present): caches the
    entire transformed file (header + signals concatenated).  The remote file
    is fetched in full on the first read, the pipeline is applied, and the
    result is held for the lifetime of the mount.

    Keyed by ``(peer_url, recording_hash)``.  Entries are never evicted.

    .. warning::

        Full-file pipelines hold complete transformed files in memory.  For
        large catalogues or large recordings this may be significant.  A future
        revision may add LRU eviction with a configurable size limit.
    """

    def __init__(self, pipeline: MiddlewarePipeline) -> None:
        self._pipeline = pipeline
        self._lock = threading.Lock()
        self._store: dict[tuple[str, str], bytes] = {}

    def get_header(
        self,
        peer_url: str,
        recording_hash: str,
        header_size: int,
        jwt: str,
    ) -> bytes:
        """Return the transformed header for an *isometric* pipeline.

        Fetches the raw header bytes on first access, applies all
        :class:`~federation.middleware.EDFHeaderMiddleware` entries, and caches
        the result.  Subsequent calls for the same recording return from cache.
        """
        key = (peer_url, recording_hash)
        with self._lock:
            if key in self._store:
                return self._store[key]

        raw = self._fetch_range(peer_url, recording_hash, 0, header_size - 1, jwt)
        transformed = self._pipeline.apply_header(raw)

        with self._lock:
            self._store.setdefault(key, transformed)
            return self._store[key]

    def get_file(
        self,
        peer_url: str,
        recording_hash: str,
        header_size: int,
        remote_file_size: int,
        jwt: str,
    ) -> bytes:
        """Return the complete transformed file for a *full-file* pipeline.

        Fetches the entire remote file on first access, applies the full
        pipeline (header middlewares then full-file middlewares), and caches the
        concatenated result.  Subsequent calls return from cache.
        """
        key = (peer_url, recording_hash)
        with self._lock:
            if key in self._store:
                return self._store[key]

        raw_header = self._fetch_range(peer_url, recording_hash, 0, header_size - 1, jwt)
        signal_size = remote_file_size - header_size
        raw_signals = (
            self._fetch_range(peer_url, recording_hash, header_size, remote_file_size - 1, jwt)
            if signal_size > 0
            else b""
        )
        new_header, new_signals = self._pipeline.apply_full(raw_header, raw_signals)
        transformed = new_header + new_signals

        with self._lock:
            self._store.setdefault(key, transformed)
            return self._store[key]

    @staticmethod
    def _fetch_range(
        peer_url: str,
        recording_hash: str,
        start: int,
        end: int,
        jwt: str,
    ) -> bytes:
        url = f"{peer_url.rstrip('/')}/recordings/api/v1/{recording_hash}/file"
        try:
            return _http_range(url, jwt, start, end)
        except urllib.error.URLError as exc:
            logger.warning(
                "Could not fetch bytes %d-%d for %s from %s: %s",
                start,
                end,
                recording_hash,
                peer_url,
                exc,
            )
            raise OSError(errno.EIO, str(exc)) from exc


# ---------------------------------------------------------------------------
# Catalogue loader
# ---------------------------------------------------------------------------


def _peer_slug(url: str) -> str:
    """Derive a safe filesystem directory name from a peer URL.

    https://neuro.example.com/  ->  neuro.example.com
    http://10.0.0.1:8000        ->  10.0.0.1_8000
    """
    import re

    slug = url.removeprefix("https://").removeprefix("http://").rstrip("/")
    return re.sub(r"[^A-Za-z0-9._-]", "_", slug)


def _edf_header_size(signal_count: int) -> int:
    """Return the EDF/BDF header size in bytes for *signal_count* channels.

    Per the EDF specification: 256 * (1 + ns) where ns is the number of
    signals.  This is always exact; there are no variable-length fields.
    """
    return 256 * (1 + signal_count)


def _reconstruct_edf_header_from_catalogue(meta: dict, signals: list[dict]) -> bytes:
    """Build valid EDF/BDF header bytes from catalogue metadata without a file fetch.

    Uses the per-channel ``signals`` array now included in catalogue responses
    (``RecordingMetaOut.signals``, populated via ``SignalInfoOut``) together
    with general recording metadata to assemble a syntactically valid EDF/BDF
    header.  Patient and recording-ID fields are intentionally left blank because
    the server-side pipeline always applies ``AnonymizeEDFHeader`` for federated
    reads.

    Parameters
    ----------
    meta:
        ``meta`` dict from a catalogue entry (``RecordingMetaOut`` fields).
    signals:
        Per-channel signal dicts from ``meta["signals"]`` (``SignalInfoOut``
        fields), in any order — sorted by ``index`` internally.
    """
    from recordings.processors.edf import EdfHeader, EdfSignalInfo, build_header

    fmt = (meta.get("format") or "edf").lower()
    base_fmt = fmt.rstrip("+")
    is_plus = fmt.endswith("+")
    discontinuous = bool(meta.get("discontinuous", False))

    if is_plus:
        reserved = f"{base_fmt.upper()}+{'D' if discontinuous else 'C'}"
    else:
        reserved = ""

    n_records = int(meta.get("data_record_count") or 0)
    record_duration = float(meta.get("data_record_duration") or 1.0)
    sorted_signals = sorted(signals, key=lambda s: s.get("index", 0))
    signal_count = len(sorted_signals)

    edf_header = EdfHeader(
        data_format=fmt,
        patient_id="",
        local_recording_id="",
        recording_date=None,
        header_record_bytes=256 * (1 + signal_count),
        reserved=reserved,
        data_record_count=n_records,
        data_record_duration=record_duration,
        signal_count=signal_count,
        is_plus=is_plus,
        discontinuous=discontinuous,
    )
    signal_infos = [
        EdfSignalInfo(
            label=s.get("label", ""),
            transducer_type=s.get("transducer_type", ""),
            physical_unit=s.get("physical_unit", ""),
            physical_min=float(s.get("physical_min", 0.0)),
            physical_max=float(s.get("physical_max", 1.0)),
            digital_min=int(s.get("digital_min", -32768)),
            digital_max=int(s.get("digital_max", 32767)),
            prefiltering=s.get("prefiltering", ""),
            sample_count=int(s.get("sample_count", 0)),
            reserved="",
        )
        for s in sorted_signals
    ]
    return build_header(edf_header, signal_infos)


def load_catalogue(
    local_user_id: str,
    pipeline: MiddlewarePipeline | None = None,
) -> tuple[dict[str, _PeerDir], dict[str, _RecordingFile], dict[tuple[str, str], object]]:
    """Query trusted peers and build the virtual directory/file catalogue.

    For each trusted FederatedPeer, makes an authenticated HTTP GET to
    ``/recordings/api/v1/?status=ready`` on the remote instance and maps each
    accessible recording to a :class:`_RecordingFile` entry.

    *pipeline* is the **local post-processing pipeline** (Layer 2).  It is used
    here solely to pre-compute the post-transform ``st_size`` stored in
    ``_RecordingFile.file_size``.  Pass ``None`` (or an empty pipeline) when no
    local post-processing is needed.

    The baseline for ``st_size`` is taken from the ``download_size`` field in
    the peer's catalogue response when present, otherwise from ``file_size``
    (raw on-disk bytes).  ``download_size`` is populated by the serving instance
    for federated requests where the ``AccessRight`` grant has
    ``apply_middleware=True``, so it reflects the server's Layer 1 output size
    without requiring the mounting instance to independently recompute it.  For
    isometric pipelines (the current default) ``download_size`` equals
    ``file_size``; for future signal pipelines it will differ.

    For pipelines that include :class:`~federation.middleware.EDFSignalMiddleware`
    entries, a :class:`~federation.middleware.SignalPipelineContext` is
    pre-computed for each accessible EDF recording so that ``getattr`` reports
    the correct post-transform size from the moment the filesystem is mounted.
    The context is built via
    :meth:`~federation.middleware.MiddlewarePipeline.build_signal_context_from_infos`
    with a header reconstructed from the per-channel ``signals`` data in the
    catalogue response — **zero extra network requests**.

    Returns ``(dirs, files, signal_contexts)`` where:

    - *dirs* maps slug → :class:`_PeerDir`
    - *files* maps absolute virtual path → :class:`_RecordingFile`
    - *signal_contexts* maps ``(peer_url, recording_hash)`` →
      :class:`~federation.middleware.SignalPipelineContext` (empty when the
      pipeline has no signal middleware)

    Peers that are unreachable at mount time are skipped with a warning; their
    directories are still created so a remount or refresh can populate them.
    """
    import json

    from federation.auth import _build_tls_context
    from federation.models import FederatedPeer

    tls_context = _build_tls_context()

    dirs: dict[str, _PeerDir] = {}
    files: dict[str, _RecordingFile] = {}
    signal_contexts: dict[tuple[str, str], object] = {}

    # Resolve the acting local user once, for the consumer-side access trail
    # emitted per peer below.  (See _resolve_local_user for why attribution comes
    # from local_user_id and why a miss must not block the mount.)
    acting_user = _resolve_local_user(local_user_id)

    for peer in FederatedPeer.objects.filter(is_trusted=True):
        slug = _peer_slug(peer.url)
        dirs[slug] = _PeerDir(slug=slug, peer_url=peer.url)

        try:
            jwt = _make_jwt(peer.url, local_user_id)
        except ValueError as exc:
            logger.warning("Cannot create JWT for peer %s: %s", peer.url, exc)
            continue

        remote_url = f"{peer.url.rstrip('/')}/recordings/api/v1/?status=ready&limit=200"
        req = urllib.request.Request(
            remote_url,
            headers={
                "Authorization": f"FederatedBearer {jwt}",
                "Accept": "application/json",
                "User-Agent": "epicurrents-fuse/1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=tls_context) as resp:
                recordings: list[dict] = json.loads(resp.read())
        except Exception as exc:
            logger.warning("Could not fetch recording list from %s: %s", peer.url, exc)
            continue

        count = 0
        for rec in recordings:
            rec_hash = rec.get("hash", "")
            if not rec_hash:
                continue

            ext = rec.get("file_extension", "")
            filename = rec.get("original_name") or f"{rec_hash}{ext}"
            # Prefer the server-reported post-pipeline size when present.
            # The recording list endpoint populates download_size for federated
            # requests where apply_middleware=True on the AccessRight grant,
            # reflecting what the server will actually transmit after its own
            # Layer 1 pipeline.  Falling back to file_size (raw on-disk bytes)
            # is correct when apply_middleware=False or for non-EDF files.
            remote_file_size = rec.get("download_size") or rec.get("file_size", 0)

            meta = rec.get("meta") or {}
            signal_count = meta.get("signal_count", 0)
            n_records = meta.get("data_record_count", 0)
            fmt = (meta.get("format") or "").lower().rstrip("+")
            is_edf = fmt in ("edf", "bdf")
            header_size = _edf_header_size(signal_count) if (is_edf and signal_count) else 0

            # Pre-compute the post-transform file size for getattr.
            if pipeline is not None and is_edf and header_size:
                if pipeline.has_signal_middleware and n_records:
                    catalogue_signals = meta.get("signals")
                    try:
                        from federation.middleware import _SignalInfoLike

                        sorted_sigs = sorted(catalogue_signals or [], key=lambda s: s.get("index", 0))
                        signal_info_objs = [
                            _SignalInfoLike(
                                label=s.get("label", ""),
                                sample_count=int(s.get("sample_count", 0)),
                                is_annotation_channel=bool(s.get("is_annotation_channel", False)),
                            )
                            for s in sorted_sigs
                        ]
                        full_fmt = (meta.get("format") or "").lower()
                        bps = 3 if full_fmt.startswith("bdf") else 2
                        raw_header = _reconstruct_edf_header_from_catalogue(meta, catalogue_signals or [])
                        ctx = pipeline.build_signal_context_from_infos(
                            signal_info_objs,
                            bps=bps,
                            n_records=n_records,
                            header_size=header_size,
                            raw_header=raw_header,
                        )
                        file_size = ctx.output_file_size
                        signal_contexts[(peer.url, rec_hash)] = ctx
                    except Exception as exc:
                        logger.warning(
                            "Could not build signal context from catalogue for %s from %s: %s",
                            rec_hash,
                            peer.url,
                            exc,
                        )
                        file_size = remote_file_size
                else:
                    file_size = pipeline.compute_output_size(remote_file_size, header_size)
            else:
                file_size = remote_file_size

            virtual_path = f"/{slug}/{filename}"
            files[virtual_path] = _RecordingFile(
                slug=slug,
                peer_url=peer.url,
                recording_hash=rec_hash,
                filename=filename,
                file_size=file_size,
                header_size=header_size,
                is_edf=is_edf,
                remote_file_size=remote_file_size,
            )
            count += 1

        logger.info("FederationFS: peer %s — %d accessible recording(s)", slug, count)
        _record_federation_access(acting_user, peer, count)

    return dirs, files, signal_contexts


def _resolve_local_user(local_user_id):
    """Resolve the acting local user for the federation audit trail; ``None`` on
    any miss.

    Federated access happens out of request context (the mount command / FUSE
    worker), so attribution comes from the mount's ``local_user_id`` rather than a
    ``request.user``. A miss degrades to ``None`` — the access is still recorded,
    just unattributed — and must never block a mount, so every failure mode
    collapses to ``None``.
    """
    try:
        from django.contrib.auth import get_user_model

        return get_user_model().objects.filter(pk=local_user_id).first()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not resolve local user %s for federation audit: %s",
            local_user_id,
            exc,
        )
        return None


def _record_federation_read(actor, peer, peer_url, recording_hash) -> None:
    """Consumer-side per-recording read record — the counterpart of the serving
    peer's ``download_recording`` / ``slice_recording`` ``FederationAuditLog``
    rows.

    Where :func:`_record_federation_access` mirrors the server's *enumeration*
    (``list_recordings``) event, this mirrors the server's *data-read* events: it
    names the specific recording actually pulled, closing the granularity half of
    the audit asymmetry that the per-peer enumeration record only half-covered.
    Emitted once per recording per mount (see
    :meth:`FederationOperations._audit_first_read`), never per range request.

    ``target`` is the peer: a federated recording has no local model row to point
    at, so the recording is identified in metadata by its ``recording_hash`` — the
    stable, non-PII, cross-instance key. Metadata carries *only* that hash: the
    filename is deliberately excluded (it is the recording's ``original_name`` and
    can carry PII, and ``with_system_activity`` stores metadata as clear JSON), and
    the peer is not duplicated in metadata because it is already the ``target``.
    Best-effort; a failure here never breaks a read.
    """
    try:
        from activity.models import Activity
        from activity.system_activity import with_system_activity

        with with_system_activity(
            "federation.remote.read",
            interface=Activity.Interface.COMMAND,
            actor=actor,
            target=peer,
            metadata={"recording_hash": recording_hash},
        ):
            pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not record federation read of %s from %s: %s",
            recording_hash,
            peer_url,
            exc,
        )


def _record_federation_access(actor, peer, recording_count: int) -> None:
    """Leave a consumer-side activity trail for a federated catalogue access.

    The serving peer logs the *inbound* check into its ``FederationAuditLog``;
    this is the symmetric **outbound** record on the consumer end, answering
    "which of our users reached which peer, and how much did they see." Federated
    access is a load-bearing feature, so it must be as auditable on the pulling
    side as it already is on the serving side — the absence of this record was
    the audit asymmetry we are closing.

    Emitted once per peer at catalogue-load (mount / refresh) time, carrying the
    number of recordings the grant made visible. This is deliberately
    *enumeration* granularity, not per-byte-read granularity: the FUSE ``read()``
    hot path has no local-user context to attribute a read to, and one row per
    range request would swamp the timeline. The peer + count pair is the useful
    unit — "user U saw N recordings on peer P at time T".

    Runs outside request context (mount command / FUSE worker), so it uses
    :func:`~activity.system_activity.with_system_activity` with
    ``interface=COMMAND`` — the same primitive the federation management commands
    use. Auditing must never break a mount, so any failure here is swallowed with
    a warning rather than propagated.
    """
    try:
        from activity.models import Activity
        from activity.system_activity import with_system_activity

        with with_system_activity(
            "federation.remote.access",
            interface=Activity.Interface.COMMAND,
            actor=actor,
            target=peer,
            metadata={"peer_url": peer.url, "recording_count": recording_count},
        ):
            pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not record federation access to peer %s: %s", peer.url, exc)


# ---------------------------------------------------------------------------
# Signal pipeline helpers
# ---------------------------------------------------------------------------


def _fetch_transformed_signal_range(
    entry: _RecordingFile,
    ctx: SignalPipelineContext,
    offset: int,
    end: int,
    jwt: str,
) -> bytes:
    """Fetch, transform, and slice a signal-region byte range.

    *offset* and *end* are **output-space** byte positions, both greater than
    or equal to ``ctx.new_header_size``.

    Maps the requested output range to the minimal set of input data records,
    fetches those records from the remote peer in a **single** HTTP range
    request, transforms each record, assembles the output, and slices it to the
    exact bytes requested.
    """
    out_sig_start = offset - ctx.new_header_size
    out_sig_end = end - ctx.new_header_size
    first_rec = out_sig_start // ctx.output_record_size
    last_rec = out_sig_end // ctx.output_record_size

    # One HTTP request covers all overlapping input records.
    fetch_start = entry.header_size + first_rec * ctx.input_record_size
    fetch_end = entry.header_size + (last_rec + 1) * ctx.input_record_size - 1
    url = f"{entry.peer_url.rstrip('/')}/recordings/api/v1/{entry.recording_hash}/file"
    try:
        raw_data = _http_range(url, jwt, fetch_start, fetch_end)
    except urllib.error.URLError as exc:
        raise OSError(errno.EIO, str(exc)) from exc

    # Transform each record and concatenate.
    n_recs = last_rec - first_rec + 1
    transformed = bytearray()
    for i in range(n_recs):
        rec = raw_data[i * ctx.input_record_size : (i + 1) * ctx.input_record_size]
        transformed += ctx.transform_record(rec)

    # Slice to the exact output bytes requested.
    slice_start = out_sig_start - first_rec * ctx.output_record_size
    slice_end = slice_start + (out_sig_end - out_sig_start + 1)
    return bytes(transformed[slice_start:slice_end])


def _read_signal_range(
    entry: _RecordingFile,
    ctx: SignalPipelineContext,
    offset: int,
    end: int,
    jwt: str,
) -> bytes:
    """Serve a FUSE byte range from a signal-middleware-transformed EDF/BDF file.

    Handles requests that fall entirely within the (transformed) header, entirely
    within the signal region, or spanning the header/signal boundary.

    *offset* and *end* are **output-space** positions (post-transform coordinates).
    """
    # Header region only.
    if offset < ctx.new_header_size:
        header_end = min(end, ctx.new_header_size - 1)
        header_chunk = ctx.new_header[offset : header_end + 1]
        if end < ctx.new_header_size:
            return header_chunk
        # Spans the header/signal boundary.
        signal_chunk = _fetch_transformed_signal_range(entry, ctx, ctx.new_header_size, end, jwt)
        return header_chunk + signal_chunk

    # Signal region only.
    return _fetch_transformed_signal_range(entry, ctx, offset, end, jwt)


# ---------------------------------------------------------------------------
# FUSE Operations
# ---------------------------------------------------------------------------

try:
    from fuse import FuseOSError, Operations

    _FUSE_AVAILABLE = True
except (ImportError, OSError):
    # Two distinct failures, both meaning "no usable FUSE here":
    #
    # * ``ImportError`` — fusepy is not installed.
    # * ``OSError`` — fusepy *is* installed (it is a hard entry in
    #   ``requirements.txt``) but the shared library it wraps is not, which is
    #   the normal state of any machine without libfuse2 or macFUSE.  fusepy
    #   resolves the library at *import* time and raises ``OSError("Unable to
    #   find libfuse")`` there rather than on first use.
    #
    # Catching only ``ImportError`` therefore defeated the very purpose of this
    # stub: on such a machine the whole module became unimportable, and with it
    # every test that only wanted its pure-Python helpers.  The condition to
    # test is whether the library resolved, not whether the wrapper is present.
    _FUSE_AVAILABLE = False

    class Operations:  # type: ignore[no-redef]
        """Stub so the module can be imported without a usable libfuse."""

    class FuseOSError(OSError):  # type: ignore[no-redef]
        pass


class FederationOperations(Operations):
    """fusepy Operations implementation for the federated filesystem.

    Thread-safe: ``read()`` may be called concurrently from multiple FUSE
    worker threads.  The transform cache uses its own internal lock; the
    catalogue dicts are read-only after construction.

    Parameters
    ----------
    local_user_id:
        ID of the local user on whose behalf outbound JWTs are signed.
    pipeline:
        **Optional local post-processing pipeline (Layer 2).**  When supplied,
        it is applied to EDF/BDF bytes *after* they have been received from the
        serving peer.  Suitable for analysis-specific transforms such as channel
        dropping or downsampling.

        Leave as ``None`` (the default) when no local transforms are needed.
        The pipeline is filtered to the ``"fuse"`` scope before use.

        .. note::

            This pipeline is *not* the privacy/anonymisation control.  Whether
            the serving peer sends anonymised or raw bytes is determined by the
            ``apply_middleware`` flag on the federation ``AccessRight`` grant
            (Layer 1, configured on the serving instance).  See module docstring.
    """

    def __init__(
        self,
        local_user_id: str,
        pipeline: MiddlewarePipeline | None = None,
    ) -> None:
        if not _FUSE_AVAILABLE:
            raise RuntimeError(
                "No usable FUSE: importing 'fuse' did not yield a working "
                "binding.  Two things are needed, and the wrapper alone is not "
                "enough — it resolves libfuse at import time, so a missing "
                "library is indistinguishable from a missing module:\n"
                "  pip install fusepy\n"
                "  Linux: sudo apt install libfuse2\n"
                "  macOS: install macFUSE from https://osxfuse.github.io/"
            )
        from federation.middleware import MiddlewarePipeline

        self.local_user_id = str(local_user_id)
        self._mount_time = int(time.time())
        # Default to an empty (no-op) local pipeline.  Anonymisation of bytes
        # received from peers is Layer 1: controlled by apply_middleware on the
        # federation AccessRight grant on the *serving* instance.
        if pipeline is None:
            pipeline = MiddlewarePipeline([])
        self._pipeline = pipeline.for_scope("fuse")
        self._transform_cache = _TransformCache(self._pipeline)
        self._dirs, self._files, self._signal_contexts = load_catalogue(self.local_user_id, self._pipeline)
        # Consumer-side per-recording read audit, deduplicated for this mount.
        # The acting user is resolved once (attribution is the mount's user); the
        # seen-set collapses a recording's many range reads to a single "first
        # read" record; the peer map avoids a DB hit on every first read.
        self._acting_user = _resolve_local_user(self.local_user_id)
        self._read_audited: set[tuple[str, str]] = set()
        self._peer_by_url: dict[str, object] = {}

    # ── Metadata ──────────────────────────────────────────────────────────────

    def getattr(self, path: str, fh=None) -> dict:
        ts = self._mount_time
        base = {
            "st_atime": ts,
            "st_ctime": ts,
            "st_mtime": ts,
            "st_uid": os.getuid(),
            "st_gid": os.getgid(),
        }

        if path == "/":
            return {**base, "st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2}

        parts = path.strip("/").split("/", 1)
        if len(parts) == 1 and parts[0] in self._dirs:
            return {**base, "st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2}

        entry = self._files.get(path)
        if entry is not None:
            return {
                **base,
                "st_mode": stat.S_IFREG | 0o444,
                "st_nlink": 1,
                "st_size": entry.file_size,
            }

        raise FuseOSError(errno.ENOENT)

    def readdir(self, path: str, fh) -> list[str]:
        names = [".", ".."]
        if path == "/":
            names.extend(self._dirs.keys())
        else:
            slug = path.strip("/")
            prefix = f"/{slug}/"
            names.extend(f.filename for p, f in self._files.items() if p.startswith(prefix))
        return names

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _peer_for_url(self, peer_url: str):
        """Resolve (and cache for this mount) the FederatedPeer for *peer_url*.

        Cached so the per-recording read audit costs at most one query per peer
        per mount, not one per first-read.  ``None`` if the peer cannot be
        resolved — the read record is still emitted with the URL in metadata.
        """
        if peer_url not in self._peer_by_url:
            peer = None
            try:
                from federation.models import FederatedPeer

                peer = FederatedPeer.objects.filter(url=peer_url).first()
            except Exception:  # pragma: no cover - defensive
                peer = None
            self._peer_by_url[peer_url] = peer
        return self._peer_by_url[peer_url]

    def _audit_first_read(self, entry) -> None:
        """Record the first genuine byte-read of *entry*'s recording this mount.

        Deduplicated to once per ``(peer, recording)``: the serving peer logs
        each download/slice request, but on the pulling side one logical read
        fans out into many range requests, so recording every one would be pure
        noise.  The first read is the access; the rest are that same access
        continuing.  The seen-set is updated *before* the write so a failing (or
        slow) audit is never retried on subsequent reads.  Best-effort and never
        fatal to the read.
        """
        key = (entry.peer_url, entry.recording_hash)
        if key in self._read_audited:
            return
        self._read_audited.add(key)
        _record_federation_read(
            self._acting_user,
            self._peer_for_url(entry.peer_url),
            entry.peer_url,
            entry.recording_hash,
        )

    def open(self, path: str, flags: int) -> int:
        if path not in self._files:
            raise FuseOSError(errno.ENOENT)
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise FuseOSError(errno.EACCES)
        return 0

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        """Return up to *size* bytes from *path* starting at *offset*.

        **Layer 1 (server-side, authoritative):** bytes received from the
        serving peer are already at the privacy level set by the data owner —
        raw if the federation ``AccessRight`` grant has ``apply_middleware=False``,
        anonymised if it has ``apply_middleware=True``.  This layer is invisible
        here; it happens transparently inside the HTTP range request.

        **Layer 2 (local, optional):** if a local post-processing pipeline was
        supplied at mount time, it is applied to the received bytes before they
        are returned to the caller.  For EDF/BDF files one of three strategies
        is used (tried in order):

        - *Isometric* (header-only
          :class:`~federation.middleware.EDFHeaderMiddleware`): the locally
          transformed header is served from cache; signal bytes are proxied
          directly from the server on each call.  No full-file buffering.
        - *Signal middleware* (any
          :class:`~federation.middleware.EDFSignalMiddleware` present): the
          output range is mapped back to the overlapping input records, fetched
          in a single HTTP range request, transformed per-record, and sliced.
          Peak memory is proportional to the overlapping records, not the full
          file.
        - *Full-file* (any
          :class:`~federation.middleware.EDFFullFileMiddleware` present): the
          entire locally-transformed file is cached on first access and sliced
          for all subsequent reads.

        When no local pipeline is active (the default), bytes are returned to
        the caller exactly as received from the server.

        Non-EDF files are always proxied byte-for-byte regardless of the local
        pipeline.
        """
        entry = self._files.get(path)
        if entry is None:
            raise FuseOSError(errno.ENOENT)

        if offset >= entry.file_size:
            return b""

        # First genuine byte-read of this recording in the mount session → one
        # audit record.  Placed after the past-EOF guard (a read past the end is
        # not an access) and deduplicated inside, so it fires once regardless of
        # how many range reads follow.
        self._audit_first_read(entry)

        end = min(offset + size - 1, entry.file_size - 1)
        remote_url = f"{entry.peer_url.rstrip('/')}/recordings/api/v1/{entry.recording_hash}/file"

        try:
            jwt = _make_jwt(entry.peer_url, self.local_user_id)
        except ValueError as exc:
            logger.error("Cannot sign JWT for %s: %s", entry.peer_url, exc)
            raise FuseOSError(errno.EIO) from exc

        try:
            # Layer 1 is implicit: the HTTP range request below returns bytes
            # already at the serving peer's privacy level (raw or anonymised
            # depending on apply_middleware on the federation AccessRight grant).
            # Layer 2 (local post-processing) begins here.
            if entry.is_edf and entry.header_size:
                if self._pipeline.is_isometric:
                    # ── Isometric: transformed header cached, raw signals proxied ──
                    if offset < entry.header_size:
                        anon_header = self._transform_cache.get_header(
                            entry.peer_url,
                            entry.recording_hash,
                            entry.header_size,
                            jwt,
                        )
                        header_end = min(end, entry.header_size - 1)
                        header_chunk = anon_header[offset : header_end + 1]

                        if end < entry.header_size:
                            # Entire request within header; no network I/O needed.
                            return header_chunk

                        # Request spans the header/data boundary.
                        data_chunk = _http_range(remote_url, jwt, entry.header_size, end)
                        return header_chunk + data_chunk

                    # Signal region only — transparent byte-range proxy.
                    return _http_range(remote_url, jwt, offset, end)

                elif self._pipeline.has_signal_middleware:
                    # ── Signal pipeline: per-record transform, single range fetch ──
                    key = (entry.peer_url, entry.recording_hash)
                    ctx = self._signal_contexts.get(key)
                    if ctx is None:
                        # Signal context unavailable (header fetch failed at mount time).
                        logger.warning("No signal context for %s; falling back to raw proxy", path)
                        return _http_range(remote_url, jwt, offset, end)
                    return _read_signal_range(entry, ctx, offset, end, jwt)

                else:
                    # ── Full-file: serve slices from complete transformed cache ──
                    content = self._transform_cache.get_file(
                        entry.peer_url,
                        entry.recording_hash,
                        entry.header_size,
                        entry.remote_file_size,
                        jwt,
                    )
                    return content[offset : end + 1]

            # ── Non-EDF or EDF without header info: transparent byte-range proxy ──
            return _http_range(remote_url, jwt, offset, end)

        except urllib.error.HTTPError as exc:
            # Must be caught before URLError and OSError (both are base classes).
            if exc.code in (401, 403):
                raise FuseOSError(errno.EACCES) from exc
            if exc.code == 404:
                raise FuseOSError(errno.ENOENT) from exc
            logger.warning("HTTP %s fetching %s: %s", exc.code, path, exc)
            raise FuseOSError(errno.EIO) from exc
        except urllib.error.URLError as exc:
            # Must be caught before OSError (URLError is a subclass).
            logger.warning("Network error fetching %s: %s", path, exc)
            raise FuseOSError(errno.EIO) from exc
        except OSError:
            # Let FuseOSError and OSError(EIO) from _TransformCache._fetch_range propagate.
            raise

    # ── Unsupported write operations ──────────────────────────────────────────

    def write(self, path, data, offset, fh):
        raise FuseOSError(errno.EROFS)

    def create(self, path, mode, fi=None):
        raise FuseOSError(errno.EROFS)

    def unlink(self, path):
        raise FuseOSError(errno.EROFS)

    def mkdir(self, path, mode):
        raise FuseOSError(errno.EROFS)

    def rmdir(self, path):
        raise FuseOSError(errno.EROFS)
