"""Fixture builders for tests that need real recording bytes.

A shipped module rather than a helper inside a test file, because project
plugins live in their own repositories and their suites need these too. Reaching
into ``recordings.tests.*`` for a fixture couples a project to the platform's
test *organisation*, which carries no stability promise at all and is the one
part of the tree free to be reorganised at will — and importing a test module
runs it, which invites double collection.

Deliberately dependency-free: stdlib only, no pytest, no Django. That keeps it
importable from a plain script, from a project's conftest, and from the runtime
image, where the test toolchain is not installed.

The wider consolidation is not done. Roughly a dozen near-identical EDF builders
are still scattered across the platform's own test files (``_make_edf_header``,
``_make_edf_data``, ``_make_edfplus_file``, three separate ``_make_edf_file``
methods in ``test_api.py`` alone). Moving them here as they are needed is the
intended direction; moving them all at once is a refactor of its own.
"""

from __future__ import annotations


def make_edf_bytes(n_channels: int = 1, n_records: int = 1) -> bytes:
    """Return a minimal valid plain-EDF file (not EDF+) as bytes.

    *n_channels* signals of *n_records* one-second records, 256 samples per
    record per channel. Sample bytes are all zeros, so the result is valid to
    parse and meaningless to analyse — which is what a test asserting on
    structure wants and what a test asserting on signal content must not use.

    The identification fields carry the already-anonymised values
    (``X X X X``), so this builds a file that has been through
    de-identification rather than one that needs it. A test exercising PHI
    removal has to construct its own header with real-looking values.
    """
    ns = n_channels
    samples_per_record = 256
    record_bytes = ns * samples_per_record * 2  # 16-bit samples
    header_bytes = 256 + 256 * ns

    def _field(value, width):
        return str(value).ljust(width)[:width].encode("ascii")

    # Main header (256 bytes)
    header = (
        _field("0", 8)  # version
        + _field("X X X X", 80)  # patient
        + _field("Startdate X X X X", 80)  # recording
        + _field("01.01.85", 8)  # start date
        + _field("00.00.00", 8)  # start time
        + _field(header_bytes, 8)  # bytes in header
        + _field("", 44)  # reserved
        + _field(n_records, 8)  # number of data records
        + _field(1, 8)  # duration of data record (seconds)
        + _field(ns, 4)  # number of signals
    )

    # Signal header fields (ns values each)
    def _sig_field(value, width):
        return _field(value, width) * ns

    sig_header = (
        _sig_field("EEG", 16)
        + _sig_field("", 80)  # transducer
        + _sig_field("uV", 8)  # physical dimension
        + _sig_field(-1000, 8)  # physical min
        + _sig_field(1000, 8)  # physical max
        + _sig_field(-32768, 8)  # digital min
        + _sig_field(32767, 8)  # digital max
        + _sig_field("", 80)  # prefiltering
        + _sig_field(samples_per_record, 8)
        + _sig_field("", 32)  # reserved
    )

    # Data records: all zeros
    data = bytes(record_bytes * n_records)

    return header + sig_header + data
