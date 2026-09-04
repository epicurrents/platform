"""Mains (power-line) frequency resolution for the compute app.

Numerical cores deliberately carry no regional mains default and no Django
import. This module is the single place the glue layers (Celery tasks, API
routes, management commands) turn a notch choice into the concrete value a
core expects.

Two resolvers, in increasing context:

* :func:`resolve_notch_hz` — request value vs the deployment ``EEG_MAINS_HZ``
  setting (declared in ``epicurrents/settings/common.py``; 50 across Europe, 60 in
  North America, unset ⇒ no notch).
* :func:`resolve_recording_notch_hz` — adds the per-recording override
  ``Recording.power_line_frequency`` between the two, so a dataset imported from
  another mains region is handled without touching the deployment default.
"""

from __future__ import annotations


def resolve_notch_hz(explicit: float | None) -> float | None:
    """Turn a request-level notch choice into a concrete frequency or ``None``.

    Semantics (chosen so "unspecified" and "explicitly off" are distinguishable):

    * ``explicit is None`` — unspecified: fall back to the deployment setting
      ``settings.EEG_MAINS_HZ`` (which may itself be ``None`` → no notch).
    * ``explicit == 0`` — the caller explicitly disabled the notch → ``None``.
    * ``explicit > 0`` — use that frequency verbatim.

    The point: a European deployment sets ``EEG_MAINS_HZ = 50`` once, and no code
    path silently applies a 60 Hz notch to 50 Hz-mains data.
    """
    if explicit is None:
        from django.conf import settings

        return getattr(settings, "EEG_MAINS_HZ", None)
    if explicit == 0:
        return None
    return float(explicit)


def resolve_recording_notch_hz(recording, explicit: float | None = None) -> float | None:
    """Resolve the effective mains notch for a *recording*.

    Precedence: an explicit request-level value wins; else the recording's own
    ``power_line_frequency`` override; else the deployment ``EEG_MAINS_HZ``
    default (which may itself be ``None`` → no notch). Reuses
    :func:`resolve_notch_hz` for the tri-state semantics, so a ``0`` request value
    still means "explicitly off".

    *recording* only needs a ``power_line_frequency`` attribute — no Django import
    — so this stays usable from the numeric glue layer. The resolved value is what
    flows into the detector and into the detection-cache identity key, so a later
    override edit correctly re-keys to a fresh result.

    A BIDS exporter should feed ``PowerLineFrequency`` from this resolver too
    (called with no *explicit*), so the sidecar and the preprocessing agree.
    """
    override = explicit if explicit is not None else getattr(recording, "power_line_frequency", None)
    return resolve_notch_hz(override)
