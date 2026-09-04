"""Tests for the non-commercial feature gate (compute/licensing.py).

``require_noncommercial``'s decision is driven entirely through
``noncommercial_enabled``, so most of these monkeypatch that one function to
exercise both states without touching settings.

The gate's own reading of the setting is asserted with ``override_settings``
rather than by observing the ambient value. An earlier version of the first test
called ``noncommercial_enabled()`` with nothing overridden and asserted False,
on the premise that a bare test run has no Django configured. It does —
pytest-django configures ``epicurrents.settings.test_platform``, which reads the
developer's own .env — so the test was really asserting that the machine running
it had the flag off. It passed only while .env happened to omit the key, and
at the time .env.example shipped it as ``true``, so a fresh deployment's .env
made it fail, which is how it was found. The template now ships the line commented out, so
a fresh .env makes no declaration either way, but the test still overrides
rather than trusting the ambient value.
"""

import pytest
from django.test import override_settings

from compute import licensing


@override_settings(EPICURRENTS_NONCOMMERCIAL_USE=False)
def test_disabled_when_the_flag_is_off():
    assert licensing.noncommercial_enabled() is False


@override_settings(EPICURRENTS_NONCOMMERCIAL_USE=True)
def test_enabled_when_the_flag_is_on():
    """The other direction, so the gate is not merely always-False."""
    assert licensing.noncommercial_enabled() is True


def test_absent_setting_reads_as_disabled(settings):
    """The default that matters: a deployment that never mentions the flag gets
    features off, rather than on by accident."""
    del settings.EPICURRENTS_NONCOMMERCIAL_USE
    assert licensing.noncommercial_enabled() is False


def test_require_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(licensing, "noncommercial_enabled", lambda: False)
    with pytest.raises(licensing.NonCommercialFeatureDisabled) as exc:
        licensing.require_noncommercial("some_nc_feature")
    # The exception carries the feature key and a pointer to the env flag.
    assert exc.value.feature == "some_nc_feature"
    assert "EPICURRENTS_NONCOMMERCIAL_USE" in str(exc.value)


def test_require_passes_when_enabled(monkeypatch):
    monkeypatch.setattr(licensing, "noncommercial_enabled", lambda: True)
    licensing.require_noncommercial("some_nc_feature")  # must not raise


def test_unknown_feature_still_gates(monkeypatch):
    monkeypatch.setattr(licensing, "noncommercial_enabled", lambda: False)
    with pytest.raises(licensing.NonCommercialFeatureDisabled):
        licensing.require_noncommercial("some_future_nc_tool")


def test_registry_is_a_key_to_reason_mapping():
    """The registry is empty today; this pins its shape so an entry added later
    carries the human-readable reason the gate reports."""
    assert isinstance(licensing.NONCOMMERCIAL_FEATURES, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in licensing.NONCOMMERCIAL_FEATURES.items())
