"""Re-dithering of a pooled submission, so the stored file never equals the bytes a contributor sent.

A submission already in the platform's canonical form passes every ingest pass unchanged, and the
contributor's receipt is the SHA-256 of what they sent. Without this step, whoever holds a receipt
and can read the pool finds its recording by hashing the downloads. The pooled ingest therefore adds
a fresh step of noise to every sample before the recording is created: each sample moves by -1, 0 or
+1 digital units, drawn from the system's cryptographic source, clipped to the channel's digital
range, and the draw is kept nowhere. There is no key, so nothing can reverse it, rotate or leak.

It closes the receipt join and only that one. The signal is untouched above the quantisation step,
so someone holding the submitted file still finds its member by correlation, and the output remains
pseudonymised. The viewer's own dither is the complement: it covers the submitted bytes from export
to ingest, which this step cannot reach. Contract tests in ``recordings/tests/test_redither.py``.
"""

from pathlib import Path

#: The version of this pass, recorded on every pooled ingest run. Bump it when the noise changes.
REDITHER_VERSION = 1


def redither_edf(path: Path) -> None:
    """Add a fresh step of noise to every sample of the EDF or BDF file at *path*, in place.

    The header and the file length are unchanged. The file must be what the submission gate
    accepts: well-formed, its length matching its header, and without an annotation channel,
    since every sample is treated as signal. Raises ``EdfParseError`` or ``ValueError`` otherwise.
    """
    import numpy as np

    from recordings.processors.edf import EdfParseError, parse_edf_header, parse_signal_infos

    data = path.read_bytes()
    header = parse_edf_header(data)
    signals = parse_signal_infos(data, header)
    if header.signal_count and not signals:
        raise EdfParseError("The signal header is truncated or corrupt")
    if any(s.is_annotation_channel for s in signals):
        raise ValueError("A file with an annotation channel cannot be re-dithered sample by sample")
    width = 3 if header.data_format.startswith("bdf") else 2
    counts = [s.sample_count for s in signals]
    per_record = sum(counts)
    total = per_record * header.data_record_count
    if len(data) != header.header_record_bytes + total * width:
        raise ValueError("The file length does not match its header")

    body = np.frombuffer(data, dtype=np.uint8, offset=header.header_record_bytes)
    if width == 2:
        samples = body.view("<i2").astype(np.int32)
    else:
        triples = body.reshape(-1, 3).astype(np.int32)
        samples = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        samples = np.where(samples & 0x800000, samples - 0x1000000, samples)
    samples = samples.reshape(header.data_record_count, per_record)

    # A system source rather than numpy's generator: the draw is what stands between a receipt and
    # its recording, so it must not be reproducible from a seed.
    samples = samples + _draw_noise(samples.size).reshape(samples.shape)
    # The sample width bounds the declared range too, so a header claiming more than the format holds
    # cannot make a sample wrap round on the way back to bytes.
    bound = 1 << (8 * width - 1)
    low = np.maximum(np.repeat([s.digital_min for s in signals], counts), -bound)
    high = np.minimum(np.repeat([s.digital_max for s in signals], counts), bound - 1)
    samples = np.clip(samples, low, high).reshape(-1)

    if width == 2:
        out = samples.astype("<i2").tobytes()
    else:
        unsigned = samples & 0xFFFFFF
        out = np.stack([unsigned & 0xFF, (unsigned >> 8) & 0xFF, (unsigned >> 16) & 0xFF], axis=1)
        out = out.astype(np.uint8).tobytes()
    with path.open("r+b") as fh:
        fh.seek(header.header_record_bytes)
        fh.write(out)


def _draw_noise(size: int):
    """*size* values drawn uniformly from -1, 0 and +1 by the system's cryptographic source.

    Bytes of 255 are discarded rather than folded in, since 256 is not a multiple of three and
    the fold would lean the noise, and so the signal, by a fraction of a step.
    """
    import secrets

    import numpy as np

    parts = []
    remaining = size
    while remaining > 0:
        raw = np.frombuffer(secrets.token_bytes(remaining + remaining // 64 + 16), dtype=np.uint8)
        kept = raw[raw < 255][:remaining]
        parts.append(kept)
        remaining -= kept.size
    drawn = np.concatenate(parts) if parts else np.empty(0, dtype=np.uint8)
    return drawn.astype(np.int32) % 3 - 1
