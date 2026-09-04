"""Tests for the canonical unit vocabulary (recordings/processors/units.py).

Pure-function tests — no Django, no fixtures — documenting the recognition contract
as an input→output table, the same shape as ``test_channel_labels.py``.

The property that matters most is negative: an unrecognised dimension must map to
``a.u.`` and offer no conversion. A blanket ``* 1e6`` applied to a dimension the
header never established does not produce an approximately wrong amplitude, it
produces a number that means nothing, and the only way to keep that from recurring is
to make "I don't recognise this" a value the type system carries rather than a case
the code forgets.
"""

import pytest

from recordings.processors.units import (
    GENERIC_UNIT,
    KNOWN_UNITS,
    MICROVOLT,
    VOLTAGE_UNITS,
    canonical_unit,
    is_voltage,
    to_microvolts,
)

# (raw physical dimension, canonical unit)
_CASES: list[tuple[str, str]] = [
    # --- microvolts, in every spelling a real header uses ---
    ("uV", "uV"),
    ("\u00b5V", "uV"),  # micro sign
    ("\u03bcV", "uV"),  # Greek small letter mu
    ("\x83\xcaV", "uV"),  # sjis mu byte pair, as latin-1 decodes it
    ("UV", "uV"),
    ("uv", "uV"),
    ("  uV  ", "uV"),
    ("[uV]", "uV"),  # bracketed
    ("(uV)", "uV"),
    ('"uV"', "uV"),
    ("microvolt", "uV"),
    ("microvolts", "uV"),
    ("mcV", "uV"),
    # --- the rest of the voltage family ---
    ("V", "V"),
    ("v", "V"),
    ("volts", "V"),
    ("mV", "mV"),
    ("mv", "mV"),
    ("millivolt", "mV"),
    ("nV", "nV"),
    ("nanovolts", "nV"),
    ("pV", "pV"),
    ("kV", "kV"),
    # --- non-voltage dimensions worth naming ---
    ("%", "%"),
    ("percent", "%"),
    ("mmHg", "mmHg"),
    ("torr", "mmHg"),
    ("cmH2O", "cmH2O"),
    ("mbar", "hPa"),
    ("degC", "degC"),
    ("\u00b0C", "degC"),  # degree sign folded
    ("Celsius", "degC"),
    ("\u00b0F", "degF"),
    ("Ohm", "Ohm"),
    ("ohms", "Ohm"),
    ("\u2126", "Ohm"),  # ohm sign
    ("\u03a9", "Ohm"),  # Greek capital letter omega
    ("kOhm", "kOhm"),
    ("bpm", "bpm"),
    ("1/min", "bpm"),
    ("beats/min", "bpm"),
    ("Hz", "Hz"),
    ("1/s", "Hz"),
    ("L/min", "L/min"),
    ("mL", "mL"),
    ("ml", "mL"),
    ("deg", "deg"),
    ("\u00b0", "deg"),
    ("s", "s"),
    ("ms", "ms"),
    ("mA", "mA"),
    ("uA", "uA"),
    ("A", "A"),
    # --- headers that positively assert "no dimension" ---
    ("", GENERIC_UNIT),
    ("   ", GENERIC_UNIT),
    ("none", GENERIC_UNIT),
    ("None", GENERIC_UNIT),
    ("n/a", GENERIC_UNIT),
    ("NA", GENERIC_UNIT),
    ("nil", GENERIC_UNIT),
    ("-", GENERIC_UNIT),
    ("--", GENERIC_UNIT),
    (".", GENERIC_UNIT),
    ("?", GENERIC_UNIT),
    ("unknown", GENERIC_UNIT),
    ("unitless", GENERIC_UNIT),
    ("uncal", GENERIC_UNIT),
    ("AU", GENERIC_UNIT),
    ("a.u.", GENERIC_UNIT),
    ("arb", GENERIC_UNIT),
    ("1", GENERIC_UNIT),
    ("0", GENERIC_UNIT),
    # --- and anything we simply do not recognise ---
    ("banana", GENERIC_UNIT),
    ("EEG", GENERIC_UNIT),  # a header that put the label in the unit field
    ("uV/mmHg", GENERIC_UNIT),  # a compound we deliberately do not parse
    ("Fp1", GENERIC_UNIT),
]


@pytest.mark.parametrize("dimension, expected", _CASES)
def test_canonical_unit(dimension, expected):
    assert canonical_unit(dimension) == expected


def test_canonical_unit_is_idempotent():
    """Every output must be a valid input mapping to itself, or a stored unit token
    could not be re-read by the same function that produced it."""
    for dimension, expected in _CASES:
        assert canonical_unit(expected) == expected


def test_canonical_unit_output_is_always_a_known_token():
    """There is no third outcome: a raw dimension yields a member of KNOWN_UNITS or
    the generic unit — never the raw string, which a caller might mistake for vetted."""
    for dimension in [d for d, _ in _CASES] + ["\x00", "42 mV", "??"]:
        assert canonical_unit(dimension) in KNOWN_UNITS


def test_unrecognised_never_becomes_a_voltage():
    """The bug this module exists to prevent."""
    for dimension in ("", "none", "n/a", "?", "banana", "EEG", "1"):
        unit = canonical_unit(dimension)
        assert unit == GENERIC_UNIT
        assert not is_voltage(unit)
        assert to_microvolts(unit) is None


# (unit or raw dimension, factor to microvolts or None)
_CONVERSIONS: list[tuple[str, float | None]] = [
    ("uV", 1.0),
    ("\u00b5V", 1.0),
    ("microvolts", 1.0),
    ("mV", 1e3),
    ("V", 1e6),
    ("kV", 1e9),
    ("nV", 1e-3),
    ("pV", 1e-6),
    # Not voltages — no factor exists, and inventing one is the whole failure mode.
    ("%", None),
    ("mmHg", None),
    ("bpm", None),
    ("Ohm", None),
    (GENERIC_UNIT, None),
    ("", None),
    ("banana", None),
]


@pytest.mark.parametrize("unit, factor", _CONVERSIONS)
def test_to_microvolts(unit, factor):
    got = to_microvolts(unit)
    if factor is None:
        assert got is None
    else:
        assert got == pytest.approx(factor)


@pytest.mark.parametrize("unit, factor", _CONVERSIONS)
def test_is_voltage_agrees_with_to_microvolts(unit, factor):
    """One predicate, one conversion, no way for them to disagree — a caller that
    branches on ``is_voltage`` and then calls ``to_microvolts`` must never get None."""
    assert is_voltage(unit) is (factor is not None)


def test_voltage_conversions_compose():
    """The factors are a consistent ladder, not independently-typed constants."""
    assert to_microvolts("mV") == pytest.approx(to_microvolts("uV") * 1e3)
    assert to_microvolts("V") == pytest.approx(to_microvolts("mV") * 1e3)
    assert to_microvolts("nV") == pytest.approx(to_microvolts("uV") * 1e-3)


def test_microvolt_is_the_conversion_target():
    assert to_microvolts(MICROVOLT) == 1.0
    assert MICROVOLT in VOLTAGE_UNITS
    assert VOLTAGE_UNITS <= KNOWN_UNITS
    assert GENERIC_UNIT in KNOWN_UNITS
    assert GENERIC_UNIT not in VOLTAGE_UNITS


def test_generic_unit_is_not_empty():
    """``a.u.`` is a positive statement ("real measurement, dimension unestablished").
    The empty string reads as "no opinion", and having no opinion is what let a blanket
    microvolt scale factor through in the first place."""
    assert GENERIC_UNIT
    assert GENERIC_UNIT.strip() == GENERIC_UNIT
