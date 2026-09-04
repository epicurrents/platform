"""Tests for the recording-aware mains-notch resolver
(compute/mains.py::resolve_recording_notch_hz).

Precedence: explicit request value → Recording.power_line_frequency override →
deployment EEG_MAINS_HZ default. Reuses resolve_notch_hz's tri-state semantics,
so these need only override_settings and a duck-typed recording (no DB).
"""

from types import SimpleNamespace

from django.test import override_settings

from compute.mains import resolve_notch_hz, resolve_recording_notch_hz


def _rec(power_line_frequency):
    return SimpleNamespace(power_line_frequency=power_line_frequency)


@override_settings(EEG_MAINS_HZ=50.0)
def test_explicit_request_value_wins():
    assert resolve_recording_notch_hz(_rec(60.0), explicit=40.0) == 40.0


@override_settings(EEG_MAINS_HZ=50.0)
def test_recording_override_beats_deployment_default():
    assert resolve_recording_notch_hz(_rec(60.0)) == 60.0


@override_settings(EEG_MAINS_HZ=50.0)
def test_falls_back_to_deployment_default_without_override():
    assert resolve_recording_notch_hz(_rec(None)) == 50.0


@override_settings(EEG_MAINS_HZ=None)
def test_unset_everywhere_means_no_notch():
    assert resolve_recording_notch_hz(_rec(None)) is None


@override_settings(EEG_MAINS_HZ=50.0)
def test_explicit_zero_disables_notch():
    # 0 is the request-level "explicitly off" escape hatch (see resolve_notch_hz).
    assert resolve_recording_notch_hz(_rec(60.0), explicit=0.0) is None


@override_settings(EEG_MAINS_HZ=50.0)
def test_matches_plain_resolver_without_override():
    assert resolve_recording_notch_hz(_rec(None)) == resolve_notch_hz(None)
