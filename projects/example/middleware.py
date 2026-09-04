"""EDF/BDF middleware for the *example* project.

This module demonstrates how to implement a custom
:class:`~federation.middleware.EDFHeaderMiddleware` that stamps a configurable
institution name into the EDF *local recording identification* field before a
file is served to a federated peer or an API consumer.

EDF header layout (relevant slice)
-----------------------------------
The fixed EDF/BDF header is ``256 * (1 + signal_count)`` bytes.  The first 256
bytes contain these ASCII fields:

    Offset   Length  Field
    -------  ------  -----
    0        8       version
    8        80      local patient identification
    88       80      local recording identification
    168      8       startdate (``dd.mm.yy``)
    ...

All fields are padded to their declared length with ASCII spaces.

Implementing EDFHeaderMiddleware
---------------------------------
1.  Subclass :class:`~federation.middleware.EDFHeaderMiddleware`.
2.  Override :attr:`targets` if the middleware should apply only to the FUSE
    filesystem (``frozenset({"fuse"})``) or only to the HTTP download API
    (``frozenset({"api"})``).  The default is both.
3.  Implement :meth:`transform_header`.  The output **must** be exactly the
    same length as the input (isometric constraint).
4.  Wrap the implementation in a try/except and return ``raw_header`` on any
    parse error — this keeps the filesystem and API accessible even for
    non-standard or vendor-extended EDF variants.
5.  Register the class in ``apps.py`` once the extension registry is available
    (see the comment in ``apps.py``).

Implementing EDFSignalMiddleware
---------------------------------
If you need to transform data records (e.g. drop channels, downsample) rather
than just the header:

1.  Subclass :class:`~federation.middleware.EDFSignalMiddleware`.
2.  Implement ``transform_header``, ``output_record_size``, and
    ``transform_record``.
3.  If your transform changes the channel layout, also override
    ``transform_signal_infos`` so the pipeline can compute output sizes without
    reading raw bytes (used by the federation catalogue and FUSE ``st_size``).
4.  Set ``size_invariant = True`` if the output byte length per record equals
    the input byte length (allows the pipeline to skip size-change bookkeeping).

See :mod:`federation.middleware` for the full ABC documentation and built-in
examples (``DropChannelsMiddleware``, ``DownsampleMiddleware``).
"""

from __future__ import annotations

import logging

from federation.middleware import EDFHeaderMiddleware, Scope

logger = logging.getLogger(__name__)

# EDF header field offsets and lengths (bytes).
_LOCAL_RECORDING_OFFSET = 88
_LOCAL_RECORDING_LENGTH = 80


class InstitutionWatermarkMiddleware(EDFHeaderMiddleware):
    """Stamp a configurable institution name into the EDF recording-ID field.

    Reads ``EXAMPLE_INSTITUTION_NAME`` from Django settings (set in
    ``projects/example/settings.py``) and writes it into the *local recording
    identification* header field before the file is served.  The stored file on
    disk is never modified.

    The output is always exactly the same length as the input (isometric).
    If the header is too short to contain the field (malformed file), the raw
    header is returned unchanged and a warning is logged.

    Example pipeline (construct in ``apps.py`` once the registry exists)::

        from projects.example.middleware import InstitutionWatermarkMiddleware
        from federation.middleware import AnonymizeEDFHeader, MiddlewarePipeline

        pipeline = MiddlewarePipeline([
            AnonymizeEDFHeader(),           # anonymise patient / recording IDs first
            InstitutionWatermarkMiddleware(), # then stamp the institution name
        ])
    """

    # Active in both the FUSE filesystem and the HTTP download API.
    # Change to frozenset({"fuse"}) or frozenset({"api"}) to limit the scope.
    targets: frozenset[Scope] = frozenset({"fuse", "api"})

    def transform_header(self, raw_header: bytes) -> bytes:
        required_length = _LOCAL_RECORDING_OFFSET + _LOCAL_RECORDING_LENGTH
        if len(raw_header) < required_length:
            logger.warning(
                "InstitutionWatermarkMiddleware: header too short (%d bytes); "
                "expected at least %d — returning raw header unchanged.",
                len(raw_header),
                required_length,
            )
            return raw_header

        try:
            from django.conf import settings

            institution = getattr(settings, "EXAMPLE_INSTITUTION_NAME", "Unknown Institution")
            # Build the new field value: institution name truncated and space-padded
            # to exactly _LOCAL_RECORDING_LENGTH bytes (EDF requires ASCII spaces).
            label = f"Recorded at {institution}"
            field = label[:_LOCAL_RECORDING_LENGTH].ljust(_LOCAL_RECORDING_LENGTH).encode("ascii", errors="replace")

            return (
                raw_header[:_LOCAL_RECORDING_OFFSET]
                + field
                + raw_header[_LOCAL_RECORDING_OFFSET + _LOCAL_RECORDING_LENGTH :]
            )
        except Exception:
            logger.exception("InstitutionWatermarkMiddleware: transform failed; returning raw header.")
            return raw_header
