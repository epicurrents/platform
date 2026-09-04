"""Canonical physical-unit vocabulary for signal channels.

An EDF/BDF header carries a free-text *physical dimension* per signal — eight
bytes the recording system fills in however it likes. Real files contain ``uV``,
``µV``, ``mV``, ``V``, ``nV``, ``%``, ``mmHg``, ``bpm``, ``none``, ``n/a`` and
blank. Downstream, every consumer needs one question answered: *what unit is this
row in, and how do I get it to microvolts if it is a voltage at all?*

This module answers exactly that and nothing else. It is the counterpart to
:mod:`recordings.processors.channel_labels`: that module maps a free-text label
onto a controlled name vocabulary, this one maps a free-text dimension onto a
controlled unit vocabulary. Both are pure, dependency-free, and non-destructive —
the raw ``SignalInfo.physical_unit`` is never mutated, so the header's own
spelling stays available for forensics.

Design
------
* **Every channel gets a unit — there is no "unknown".** A dimension we do not
  recognise (and a blank one) maps to :data:`GENERIC_UNIT`, ``a.u.``
  (*arbitrary units*), the conventional token for "a real measurement whose
  physical dimension is not established". This is the point of the module: an
  unrecognised dimension must never silently inherit ``uV``, because a value in
  unknown units multiplied by a microvolt scale factor is not a wrong number, it
  is a meaningless one.
* **Recognition is by vocabulary, not by pattern.** ``canonical_unit`` folds case,
  whitespace, bracketing and the µ/μ/Ω/° codepoint variants, then looks the result
  up in an explicit alias table. Anything absent from the table is ``a.u.`` — never
  a guess. Deliberately conservative: adding a unit is a one-line table entry, and
  a missing entry degrades to "unscaled, honestly labelled" rather than to a wrong
  scale.
* **Only voltages convert.** :func:`to_microvolts` returns a factor for the
  voltage family and ``None`` for everything else, including ``a.u.``. A caller
  that needs microvolts and gets ``None`` has learned that this channel is not a
  voltage — which is a fact about the recording, not an error to paper over.
* **No reader knowledge here.** How a particular EDF reader mis-scales a
  particular dimension is that reader's business (see
  ``compute.signal_loader._mne_edf_gain``); this module describes the units
  themselves.
"""

from __future__ import annotations

import re

#: The unit assigned to a dimension we do not recognise, including a blank one.
#: "Arbitrary units" — the value is real and internally consistent, but its
#: physical dimension is not established, so it must not be scaled or compared
#: across channels. Deliberately *not* the empty string: empty reads as "we have
#: no opinion", and having no opinion is what let a blanket ``* 1e6`` through.
GENERIC_UNIT = "a.u."

#: The unit the analysis contract prefers for electrophysiology amplitudes.
MICROVOLT = "uV"

#: Voltage family → factor converting a value in that unit to microvolts.
#: The only conversion this module performs: everything else is reported as-is.
_TO_MICROVOLTS: dict[str, float] = {
    "kV": 1e9,
    "V": 1e6,
    "mV": 1e3,
    "uV": 1.0,
    "nV": 1e-3,
    "pV": 1e-6,
}

#: Non-voltage dimensions common enough in clinical EDF auxiliary channels to be
#: worth naming. Recognising them buys nothing numerically — no conversion is
#: offered — but it distinguishes "an oximetry channel in percent" from "we have
#: no idea", which is the difference between a usable aux channel and a suspect one.
_KNOWN_NON_VOLTAGE: tuple[str, ...] = (
    "A",
    "mA",
    "uA",  # stimulator current
    "Ohm",
    "kOhm",  # electrode impedance
    "mmHg",
    "cmH2O",
    "hPa",  # pressure
    "degC",
    "degF",  # temperature
    "%",  # saturation, fraction
    "bpm",  # heart / respiration rate
    "L/min",
    "mL",  # flow, volume
    "deg",  # position, angle
    "s",
    "ms",  # time
    "Hz",  # frequency
    GENERIC_UNIT,
)

#: Canonical unit → the extra spellings that mean it. Keys are pre-normalised by
#: :func:`_normalise` (lower-cased, whitespace and bracketing removed, µ→u, Ω→ohm,
#: °→deg), so only *spelling* variants are listed here, never case variants.
_ALIAS_SPELLINGS: dict[str, tuple[str, ...]] = {
    "kV": ("kilovolt", "kilovolts"),
    "V": ("volt", "volts"),
    "mV": ("millivolt", "millivolts"),
    "uV": ("microvolt", "microvolts", "mcv"),
    "nV": ("nanovolt", "nanovolts"),
    "pV": ("picovolt", "picovolts"),
    "A": ("amp", "amps", "ampere", "amperes"),
    "mA": ("milliamp", "milliamps", "milliampere"),
    "uA": ("microamp", "microamps", "microampere"),
    "Ohm": ("ohms",),
    "kOhm": ("kiloohm", "kiloohms"),
    "mmHg": ("torr",),
    "cmH2O": ("cmh20",),
    "hPa": ("mbar",),
    "degC": ("degreec", "degreescelsius", "celsius", "centigrade"),
    "degF": ("degreef", "degreesfahrenheit", "fahrenheit"),
    "%": ("percent", "pct", "percentage"),
    "bpm": ("beats/min", "beatsperminute", "1/min", "min-1"),
    "L/min": ("lpm", "litres/min", "liters/min"),
    "mL": ("millilitre", "milliliter"),
    "deg": ("degree", "degrees"),
    "s": ("sec", "secs", "second", "seconds"),
    "ms": ("msec", "msecs", "millisecond", "milliseconds"),
    "Hz": ("hertz", "1/s", "s-1"),
    # Spellings that positively assert "no dimension". Kept explicit rather than
    # relying on the unknown→a.u. fallback so a header that *says* it is unitless
    # is distinguishable, in code review, from one we simply failed to parse.
    GENERIC_UNIT: (
        "",
        "au",
        "a.u",
        "arb",
        "arbitrary",
        "arbitraryunits",
        "none",
        "n/a",
        "na",
        "nil",
        "-",
        "--",
        ".",
        "?",
        "unknown",
        "unspecified",
        "unitless",
        "uncal",
        "uncalibrated",
        "1",
        "0",
    ),
}


def _build_lookup() -> dict[str, str]:
    """Normalised spelling → canonical unit, from the canonical names plus aliases."""
    table: dict[str, str] = {}
    for unit in (*_TO_MICROVOLTS, *_KNOWN_NON_VOLTAGE):
        table[_normalise(unit)] = unit
    for unit, spellings in _ALIAS_SPELLINGS.items():
        for spelling in spellings:
            table[_normalise(spelling)] = unit
    return table


#: Codepoint variants collapsed before lookup. The micro sign, Greek mu and the
#: sjis mu byte pair all reach us as distinct strings because the EDF header is
#: decoded latin-1 (see ``edf._read_field``); they all mean "micro".
_CODEPOINT_ALIASES: tuple[tuple[str, str], ...] = (
    ("\x83\xca", "u"),  # sjis mu, as latin-1 decodes the byte pair
    ("\u00b5", "u"),  # micro sign
    ("\u03bc", "u"),  # Greek small letter mu
    ("\u2126", "ohm"),  # ohm sign
    ("\u03a9", "ohm"),  # Greek capital letter omega
    ("\u00b0", "deg"),  # degree sign
)

#: Bracketing and quoting some systems wrap the dimension in (``[uV]``, ``(uV)``).
_EDGE_JUNK = re.compile(r"""^[\s\[\](){}<>"']+|[\s\[\](){}<>"']+$""")


def _normalise(dimension: str) -> str:
    """Fold a raw physical dimension to its lookup key.

    Case, internal whitespace, edge bracketing and the codepoint variants above
    are all noise; anything else is preserved so the alias table stays the only
    place recognition is decided.
    """
    text = dimension
    for source, target in _CODEPOINT_ALIASES:
        text = text.replace(source, target)
    text = _EDGE_JUNK.sub("", text)
    text = re.sub(r"\s+", "", text)
    return text.lower()


_LOOKUP: dict[str, str] = _build_lookup()

#: Every canonical token this module can return.
KNOWN_UNITS: frozenset[str] = frozenset((*_TO_MICROVOLTS, *_KNOWN_NON_VOLTAGE))

#: The voltage family, for callers that only want to know whether conversion is
#: on the table at all.
VOLTAGE_UNITS: frozenset[str] = frozenset(_TO_MICROVOLTS)


def canonical_unit(dimension: str) -> str:
    """Return the canonical unit token for a raw EDF physical *dimension*.

    Pure and idempotent (``canonical_unit(canonical_unit(x)) == canonical_unit(x)``).
    An unrecognised or blank dimension returns :data:`GENERIC_UNIT` — never ``uV``,
    and never the raw string, so a caller cannot accidentally treat an unvetted
    header field as a unit it understands.
    """
    return _LOOKUP.get(_normalise(dimension), GENERIC_UNIT)


def is_voltage(unit: str) -> bool:
    """True when *unit* (canonical or raw) is a member of the voltage family."""
    return canonical_unit(unit) in VOLTAGE_UNITS


def to_microvolts(unit: str) -> float | None:
    """Factor converting a value in *unit* to microvolts, or ``None``.

    ``None`` means "not a voltage" — including :data:`GENERIC_UNIT`. Callers that
    need microvolts must treat ``None`` as a fact to report, not a default to fill
    in: a non-voltage channel keeps its own unit and its own numbers.
    """
    return _TO_MICROVOLTS.get(canonical_unit(unit))
