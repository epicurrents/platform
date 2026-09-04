"""Tests for the canonical channel-label normaliser
(recordings/processors/channel_labels.py).

Pure-function tests — no Django, no fixtures — so they run in isolation and
document the normalisation contract as an exhaustive input→output table.
"""

import pytest

from recordings.processors.channel_labels import (
    _TEN_TEN_ELECTRODES,
    AUXILIARY_TYPES,
    PRIMARY_TYPES,
    canonicalise_label,
    classify_channel,
    is_auxiliary_type,
    is_non_eeg_type,
)

# (label, signal_type, expected)
_CASES: list[tuple[str, str, str]] = [
    # --- old→new 10-10 rename (identical position) ---
    ("T3", "eeg", "T7"),
    ("T4", "eeg", "T8"),
    ("T5", "eeg", "P7"),
    ("T6", "eeg", "P8"),
    ("EEG T3", "eeg", "T7"),
    ("T3-Ref", "eeg", "T7"),
    # --- modality-prefix strip ---
    ("EEG Fp1-Cz", "eeg", "Fp1"),
    ("EEG Fp1", "eeg", "Fp1"),
    ("eeg fp1", "eeg", "Fp1"),
    ("EEG:C3", "eeg", "C3"),
    ("EEG-C4", "eeg", "C4"),
    # --- referential montage: reference suffix stripped ---
    ("C3-Cz", "eeg", "C3"),
    ("C4-A1", "eeg", "C4"),
    ("F3-M2", "eeg", "F3"),
    ("O1-AVG", "eeg", "O1"),
    ("Fp1-REF", "eeg", "Fp1"),
    ("Cz-Ref", "eeg", "Cz"),
    ("C3-M1", "eeg", "C3"),
    # --- genuine bipolar derivation: both sides canonicalised ---
    ("Fp1-F7", "eeg", "Fp1-F7"),
    ("F7-T3", "eeg", "F7-T7"),
    ("T3-T5", "eeg", "T7-P7"),
    ("Fp2-F8", "eeg", "Fp2-F8"),
    # --- bare electrodes (signal_type inferred as '') ---
    ("Fp1", "", "Fp1"),
    ("C3", "", "C3"),
    ("Oz", "", "Oz"),
    ("Pz", "", "Pz"),
    # --- A1/A2/M1/M2 kept verbatim as standalone channels ---
    ("A1", "", "A1"),
    ("A2", "", "A2"),
    ("M1", "", "M1"),
    ("M2", "eeg", "M2"),
    ("EEG A1", "eeg", "A1"),
    # --- casing normalisation ---
    ("FP1", "eeg", "Fp1"),
    ("fz", "", "Fz"),
    ("OZ", "", "Oz"),
    ("cz", "", "Cz"),
    ("poz", "", "POz"),
    ("fpz", "", "Fpz"),
    # --- non-EEG signal types excluded ---
    ("EKG", "ekg", ""),
    ("ECG", "ecg", ""),
    ("EMG Chin", "emg", ""),
    ("EOG L", "eog", ""),
    ("Fp1", "ekg", ""),  # type wins even over a resolvable-looking label
    # --- annotation / unresolved / empty ---
    ("EDF Annotations", "", ""),
    ("BDF Annotations", "", ""),
    ("Foo", "", ""),
    ("Sp1", "eeg", ""),  # sphenoidal — not a simple rename, left unresolved
    ("T1", "eeg", ""),  # anterior-temporal old label — ambiguous, unresolved
    ("X", "eeg", ""),
    ("", "", ""),
    ("EEG", "eeg", ""),
    ("Fp1-F7-EXTRA", "eeg", ""),  # >2 parts: not a montage we model
]


@pytest.mark.parametrize("label, signal_type, expected", _CASES)
def test_canonicalise_label(label, signal_type, expected):
    assert canonicalise_label(label, signal_type) == expected


@pytest.mark.parametrize("label, signal_type, expected", _CASES)
def test_idempotent(label, signal_type, expected):
    once = canonicalise_label(label, signal_type)
    # Re-feeding the output (a bare canonical name) must be a fixed point.
    assert canonicalise_label(once, "") == once


def test_all_vocabulary_is_a_fixed_point():
    """Every canonical electrode resolves to itself (case-preserving)."""
    for e in _TEN_TEN_ELECTRODES:
        assert canonicalise_label(e, "eeg") == e


def test_raw_label_never_needs_hyphen_special_casing():
    """Whitespace around tokens and separators is tolerated."""
    assert canonicalise_label("  EEG   Fp1 - Cz ", "eeg") == "Fp1"
    assert canonicalise_label("F7 - T3", "eeg") == "F7-T7"


@pytest.mark.parametrize(
    "signal_type, expected",
    [
        ("emg", True),
        ("EOG", True),
        ("ekg", True),
        ("ECG", True),
        # Auxiliary types are non-EEG too: a trigger line's empty canonical_label is
        # expected, not an unresolved electrode the backfill should surface.
        ("misc", True),
        ("trig", True),
        ("TRIG", True),
        ("eeg", False),
        ("", False),
    ],
)
def test_is_non_eeg_type(signal_type, expected):
    assert is_non_eeg_type(signal_type) is expected


@pytest.mark.parametrize(
    "signal_type, expected",
    [
        ("misc", True),
        ("trig", True),
        ("MISC", True),
        ("eeg", False),
        ("eog", False),
        ("emg", False),
        ("ekg", False),
        ("", False),
    ],
)
def test_is_auxiliary_type(signal_type, expected):
    assert is_auxiliary_type(signal_type) is expected


def test_type_sets_are_disjoint():
    """A type is electrophysiological or auxiliary, never both — the whole point of
    the split is that one set is safe to feed a detector and the other is not."""
    assert not (PRIMARY_TYPES & AUXILIARY_TYPES)


# (label, prior_type, expected (signal_type, canonical_label))
_CLASSIFY_CASES: list[tuple[str, str, tuple[str, str]]] = [
    # bare electrode with no inferred type → upgraded to eeg
    ("Fp1", "", ("eeg", "Fp1")),
    ("C3", "", ("eeg", "C3")),
    ("T3", "", ("eeg", "T7")),
    ("A1", "", ("eeg", "A1")),
    # label already carried EEG → type preserved, canonicalised
    ("EEG T3-Ref", "eeg", ("eeg", "T7")),
    ("EEG Fp1-Cz", "eeg", ("eeg", "Fp1")),
    # (non-EEG modalities are covered in _MULTIMODAL_CASES below)
    # unresolved / annotation / empty → unchanged, no canonical
    ("Foo", "", ("", "")),
    ("EDF Annotations", "", ("", "")),
    ("", "", ("", "")),
]


@pytest.mark.parametrize("label, prior_type, expected", _CLASSIFY_CASES)
def test_classify_channel(label, prior_type, expected):
    assert classify_channel(label, prior_type) == expected


def test_classify_never_downgrades_a_detected_type():
    """A label the text-heuristic typed non-EEG is never re-typed as EEG."""
    # extract_signal_type emits emg/eog/ekg (ecg is normalised to ekg).
    for prior in ("emg", "eog", "ekg"):
        signal_type, _ = classify_channel("C3", prior)
        assert signal_type == prior


def test_classify_normalises_ecg_prior_to_ekg():
    assert classify_channel("ECG", "ecg")[0] == "ekg"


# (label, prior_type, expected (signal_type, canonical_label)) — non-EEG modalities
_MULTIMODAL_CASES: list[tuple[str, str, tuple[str, str]]] = [
    # EOG → LOC / ROC / EOG (no E1/E2)
    ("LOC", "", ("eog", "LOC")),
    ("ROC", "", ("eog", "ROC")),
    ("EOG-L", "eog", ("eog", "LOC")),
    ("EOG-R", "eog", ("eog", "ROC")),
    ("LEOG", "eog", ("eog", "LOC")),
    ("EOG Left", "eog", ("eog", "LOC")),
    ("EOG", "eog", ("eog", "EOG")),
    ("EOG1-EOG2", "eog", ("eog", "EOG")),  # bipolar collapses to plain EOG
    ("E1", "", ("eog", "LOC")),  # AASM: E1 = left eye
    ("E2", "", ("eog", "ROC")),  # AASM: E2 = right eye
    ("E1-M2", "eog", ("eog", "LOC")),  # derivation → primary side
    # EMG → EMG/<site> or unclassified
    ("EMG Chin", "emg", ("emg", "EMG/Chin")),
    ("Chin", "", ("emg", "EMG/Chin")),  # distinctive bare token
    ("Chin1", "", ("emg", "EMG/Chin")),
    ("Chin1-Chin2", "emg", ("emg", "EMG/Chin")),  # derivation collapses to site
    ("LLEG", "", ("emg", "EMG/LegL")),
    ("Leg/R", "", ("emg", "EMG/LegR")),
    ("ArmL", "", ("emg", "EMG/ArmL")),
    ("EMG RArm", "emg", ("emg", "EMG/ArmR")),
    ("EMG LAT-REF", "emg", ("emg", "EMG/LegL")),  # ref part ignored
    ("EMG LAT", "emg", ("emg", "EMG/LegL")),  # LAT resolves only with an EMG hint
    ("LAT", "", ("", "")),  # ambiguous bare → unclassified
    ("EMG Masseter", "emg", ("emg", "")),  # muscle name → unclassified (raw shown)
    ("EMG1", "emg", ("emg", "")),  # generic/numbered EMG → unclassified
    # ECG → lead / kept-numbered / generic
    ("ECG", "ekg", ("ekg", "ECG")),
    ("EKG", "ekg", ("ekg", "ECG")),
    ("ECG II", "ekg", ("ekg", "II")),
    ("ECG aVR", "ekg", ("ekg", "aVR")),
    ("ECG1", "ekg", ("ekg", "ECG1")),
    ("ECG2", "ekg", ("ekg", "ECG2")),
    ("ECG1-ECG2", "ekg", ("ekg", "ECG1-ECG2")),
    ("V1", "ekg", ("ekg", "V1")),
    # bare ambiguous non-EEG stays unclassified (no aggressive lead guessing)
    ("II", "", ("", "")),
    ("aVR", "", ("", "")),
    # EEG still resolves alongside
    ("Fp1", "", ("eeg", "Fp1")),
    # A label that says EEG and nothing else names no electrode, so the gate demotes
    # it rather than certifying it (see _DEMOTION_CASES).
    ("EEG", "eeg", ("misc", "")),
]


@pytest.mark.parametrize("label, prior_type, expected", _MULTIMODAL_CASES)
def test_classify_channel_multimodal(label, prior_type, expected):
    assert classify_channel(label, prior_type) == expected


# (label, prior_type, expected (signal_type, canonical_label))
#
# The auxiliary vocabulary: channels a clinical recording carries alongside the
# montage. Two properties are being pinned. First, the role is matched against the
# *whole* remaining label core, never as a substring — that is what keeps ``Fp1`` from
# matching a role and ``TEMP`` from matching inside ``T3-TEMPORAL``. Second, the EEG
# modality prefix and a reference suffix are stripped first, because that is how these
# channels actually appear in the wild: an amplifier that stamps ``EEG `` on every
# label stamps it on the photic line too.
_AUXILIARY_CASES: list[tuple[str, str, tuple[str, str]]] = [
    # trigger / stimulus lines — the reason 'trig' exists as its own type
    ("EEG Photic-Ref", "eeg", ("trig", "Photic")),
    ("Photic", "", ("trig", "Photic")),
    ("PhoticStim", "", ("trig", "Photic")),
    ("Flash", "", ("trig", "Photic")),
    ("EEG Event-Ref", "eeg", ("trig", "Event")),
    ("Marker", "", ("trig", "Event")),
    ("TRIG", "", ("trig", "Trigger")),
    ("TTL", "", ("trig", "Trigger")),
    ("Status", "", ("trig", "Status")),  # BioSemi's trigger channel
    ("Sync", "", ("trig", "Sync")),
    # physiological-but-not-electrophysiological traces
    ("Pulse", "", ("misc", "Pulse")),
    ("SpO2", "", ("misc", "SpO2")),
    ("SaO2", "", ("misc", "SpO2")),
    ("OSAT", "", ("misc", "SpO2")),
    ("Pleth", "", ("misc", "PPG")),
    ("Resp", "", ("misc", "Resp")),
    ("Airflow", "", ("misc", "Resp")),
    ("Thorax", "", ("misc", "Thorax")),
    ("ABD", "", ("misc", "Abdomen")),
    ("Snore", "", ("misc", "Snore")),
    ("Position", "", ("misc", "Position")),
    ("Temp", "", ("misc", "Temp")),
    ("Light", "", ("misc", "Light")),
    ("Imp", "", ("misc", "Impedance")),
    # numbered DC / AUX inputs keep their number, normalised
    ("EEG DC1-Ref", "eeg", ("misc", "DC1")),
    ("DC01", "", ("misc", "DC1")),
    ("DC12", "", ("misc", "DC12")),
    ("AUX3", "", ("misc", "AUX3")),
    # a named biological modality wins over the auxiliary vocabulary: an EOG channel
    # that happens to be called 'Temp' is still an EOG channel
    ("Temp", "eog", ("eog", "EOG")),
]


@pytest.mark.parametrize("label, prior_type, expected", _AUXILIARY_CASES)
def test_classify_channel_auxiliary(label, prior_type, expected):
    assert classify_channel(label, prior_type) == expected


# The gate is symmetric. Promoting a bare electrode to 'eeg' is only defensible if
# the converse also holds: a label that *claims* EEG but resolves to no 10-10
# electrode and no auxiliary role is demoted to 'misc' with no canonical label. The
# alternative — trusting the substring 'EEG' — is what let 'EEG Photic-Ref' through as
# a brain channel and got it multiplied by 1e6.
_DEMOTION_CASES: list[tuple[str, str, tuple[str, str]]] = [
    ("EEG X9-Ref", "eeg", ("misc", "")),
    ("EEG Foo", "eeg", ("misc", "")),
    ("EEG", "eeg", ("misc", "")),
    # Accepted collateral: intracranial and high-density montages are not 10-10, so a
    # depth electrode or an EGI number demotes too when its label carries the marker.
    # Recognising these needs a per-recording montage declaration or a label alias —
    # not a looser match, which is the failure mode being fixed here.
    ("EEG RAH1", "eeg", ("misc", "")),
    ("EEG E17", "eeg", ("misc", "")),
    # A label with no modality marker at all stays unclassified rather than being
    # demoted: there is nothing to demote it *from*.
    ("Foo", "", ("", "")),
    ("X9", "", ("", "")),
]


@pytest.mark.parametrize("label, prior_type, expected", _DEMOTION_CASES)
def test_classify_channel_demotes_unresolved_eeg(label, prior_type, expected):
    assert classify_channel(label, prior_type) == expected


def test_demotion_leaves_real_electrodes_alone():
    """The gate must not cost a single genuine electrode: every 10-10 name still
    classifies as EEG when presented with the marker a real header carries."""
    for e in _TEN_TEN_ELECTRODES:
        assert classify_channel(f"EEG {e}-Ref", "eeg") == ("eeg", e)


def test_classify_canonical_is_stable_under_reclassification():
    """Re-feeding a canonical name with its own type is a fixed point."""
    cases = _MULTIMODAL_CASES + _CLASSIFY_CASES + _AUXILIARY_CASES + _DEMOTION_CASES
    for label, prior, (typ, canon) in cases:
        if canon:
            again_type, again = classify_channel(canon, typ)
            assert again == canon
            assert again_type == typ
