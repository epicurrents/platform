"""Django system checks: what a release-gated deployment must and must not keep.

A dataset built for release gating holds recordings that arrive de-identified from their
contributors and belong to no platform user; the design is that the platform keeps no copy the
contributing centre does not also hold, and the withdrawal path (``purge_dataset_recordings``)
removes the one it has. The originals preservation volume would keep a second copy that no purge
touches, so a deployment that declares itself release-gated (``LIBRARY_RELEASE_GATED_DEPLOYMENT``)
refuses to boot with ``RECORDINGS_ORIGINALS_PATH`` set.

It must also set ``RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS``. A pool is fed by two parallel paths, the validating
submission and an ordinary upload joining the dataset through a release run, and the pooled ingest never keeps the
file's text; an upload written under the default would carry its placeholders and raw record into the same pool.
Registered here rather than in the project because every project running such a dataset needs the same refusals.
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


@register(Tags.compatibility)
def check_release_gated_deployment_discards_embedded_text(app_configs, **kwargs):
    """Error when a release-gated deployment keeps the text of the files it ingests."""
    if not getattr(settings, "LIBRARY_RELEASE_GATED_DEPLOYMENT", False):
        return []
    if getattr(settings, "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS", False):
        return []
    return [
        Error(
            "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS is off on a release-gated deployment.",
            hint=(
                "An upload can join a release-gated pool through a release run, and the pooled ingest keeps none "
                "of a file's text; both paths must arrive in the same shape. Turn "
                "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS on or LIBRARY_RELEASE_GATED_DEPLOYMENT off."
            ),
            id="library.E002",
        )
    ]
