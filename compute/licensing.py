"""Non-commercial feature gating for the compute app.

Some compute features embed material licensed for **non-commercial use only**:

* **Operator-provisioned model weights** whose licence says so — a braindecode
  foundation model published under CC BY-NC, for instance (gated per model
  through ``spec.noncommercial``; see ``compute/braindecode/README.md``).

These are **disabled by default** and unlock only when a deployment explicitly
declares non-commercial use via ``settings.EPICURRENTS_NONCOMMERCIAL_USE``
(env: ``EPICURRENTS_NONCOMMERCIAL_USE``). The default-off, opt-in-by-assertion
design means a downstream (possibly commercial) fork gets nothing until someone
deliberately declares the deployment non-commercial — the affirmative act sits
with the person enabling the feature, not with whoever forgot to disable it.

**This is a compliance safeguard and an intent declaration, not a licence.**
Enabling the flag does not grant any commercial right; it only unlocks features
whose licences already permit the non-commercial use being asserted. "Commercial"
is judged by the Creative Commons NonCommercial standard — charging a
cost-recovery fee does not by itself make a deployment commercial.

Enforcement pattern
-------------------
The authoritative gate lives at each feature's point of use — the model /
artefact loaders — because that is where the licensed material is actually
loaded and the hardest point to bypass. Management commands, Celery tasks, and
API routes add earlier, friendlier checks so the failure is a clean 4xx /
CommandError rather than a deep stack trace, but the loader is the backstop.
"""

from __future__ import annotations

# Registry of features gated behind the non-commercial declaration. Add a line
# here when introducing another non-commercially-licensed capability, and call
# ``require_noncommercial(<key>)`` at its point of use.
NONCOMMERCIAL_FEATURES: dict[str, str] = {}
"""Empty today. Braindecode weights gate per model through ``spec.noncommercial``
rather than through this registry, so nothing is registered here yet; the gate
itself works for any key, registered or not."""


class NonCommercialFeatureDisabled(RuntimeError):
    """Raised when a non-commercial-only feature is used without the opt-in.

    Carries the feature key in ``feature`` so callers (API layer, commands) can
    translate it into an appropriate response.
    """

    def __init__(self, feature: str, message: str):
        super().__init__(message)
        self.feature = feature


def noncommercial_enabled() -> bool:
    """True when this deployment has declared non-commercial use.

    Reads ``settings.EPICURRENTS_NONCOMMERCIAL_USE``. Defaults to ``False`` when
    Django is not configured (e.g. a bare unit test) — i.e. gated features are
    off unless explicitly enabled, never on by accident.
    """
    try:
        from django.conf import settings

        return bool(getattr(settings, "EPICURRENTS_NONCOMMERCIAL_USE", False))
    except Exception:
        return False


def require_noncommercial(feature: str) -> None:
    """Raise :class:`NonCommercialFeatureDisabled` unless non-commercial use is declared.

    ``feature`` should be a key in :data:`NONCOMMERCIAL_FEATURES` (used for the
    message; an unknown key still gates — it just prints the raw name).
    """
    if noncommercial_enabled():
        return
    desc = NONCOMMERCIAL_FEATURES.get(feature, feature)
    raise NonCommercialFeatureDisabled(
        feature,
        f"'{feature}' is a non-commercial-only feature ({desc}) and is disabled. "
        "It is available only when this deployment declares non-commercial use: "
        "set EPICURRENTS_NONCOMMERCIAL_USE=true in the environment. This flag is a "
        "compliance safeguard, not a licence for commercial use — see "
        "compute/README.md.",
    )
