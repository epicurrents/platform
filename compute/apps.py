"""Compute app — server-side scientific Python services.

Houses numerical computations that require native compiled extensions
(MNE-C, SciPy LAPACK/BLAS, etc.) and therefore cannot run in the
browser via Pyodide.

Current modules
---------------
``compute.eeg``
    EEG-specific computations.  The first resident is forward-model
    generation: given a standard electrode montage and a spherical head
    model, ``compute_eeg_lead_field`` produces the lead field matrix that
    the browser-side sLORETA / eLORETA / dipole Pyodide script needs for
    the inverse step.

Adding new modules
------------------
Add a sub-package (e.g. ``compute/emg/``) and expose its entry points
through the ``compute/api/v1/ninja.py`` router.  No models are required
for pure computation; add a ``*Cache`` model only when pre-computed
results should be persisted and served to clients.
"""

from django.apps import AppConfig


class ComputeConfig(AppConfig):
    """Django app configuration for server-side scientific computation."""

    default_auto_field = "django.db.models.BigAutoField"
    label = "compute"
    name = "compute"

    def ready(self) -> None:
        # Wire the concrete signal loader into the execution layer's injectable seam
        # (compute.tasks._load_window). Registered here, not at import time, so the
        # loader module — and MNE, which it imports lazily — is not pulled in until
        # the app registry is ready. Tests can override it via set_signal_loader.
        from .signal_loader import load_signal_window
        from .tasks import set_signal_loader

        set_signal_loader(load_signal_window)
