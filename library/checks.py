"""Django system check: a release-gated deployment must not keep an originals volume.

A dataset built for release gating holds recordings that arrive de-identified from their
contributors and belong to no platform user; the design is that the platform keeps no copy the
contributing centre does not also hold, and the withdrawal path (``purge_dataset_recordings``)
removes the one it has. The originals preservation volume would keep a second copy that no purge
touches, so a deployment that declares itself release-gated (``LIBRARY_RELEASE_GATED_DEPLOYMENT``)
refuses to boot with ``RECORDINGS_ORIGINALS_PATH`` set. Registered here rather than in the
project because every project running such a dataset needs the same refusal.
"""

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.compatibility)
def check_release_gated_deployment_keeps_no_originals(app_configs, **kwargs):
    """Error when a release-gated deployment configures the originals volume."""
    if not getattr(settings, "LIBRARY_RELEASE_GATED_DEPLOYMENT", False):
        return []
    if not getattr(settings, "RECORDINGS_ORIGINALS_PATH", None):
        return []
    return [
        Error(
            "RECORDINGS_ORIGINALS_PATH is set on a release-gated deployment.",
            hint=(
                "A release-gated dataset keeps no copy its contributors do not hold and its withdrawal "
                "path never reaches the originals volume. Unset RECORDINGS_ORIGINALS_PATH (and "
                "RECORDINGS_PRESERVE_MODE) or turn LIBRARY_RELEASE_GATED_DEPLOYMENT off."
            ),
            id="library.E001",
        )
    ]
