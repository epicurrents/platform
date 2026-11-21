"""Settings for the *dicom* plugin.

Environment-level overrides live in ``.env``; these are the defaults consumed
by plugin code when the env var is not set.
"""

import os

from epicurrents.settings.env import env_bool

# ---------------------------------------------------------------------------
# File storage
# ---------------------------------------------------------------------------

# Absolute path to the directory where indexed DICOM files are stored.
# Must be writable by the application user. Defaults to a ``dicom/``
# directory on the media volume, beside the media uploads: that volume is
# mounted writable in web and celery and read by the borg backup, and the
# plugin brings no volume of its own. A directory beside the recordings volume
# (the old default) is inside the container's root-owned ``/data``, where the
# first upload fails with a permission error.
_media_base = os.path.dirname(os.getenv("MEDIA_UPLOAD_PATH", "/data/media/uploads").rstrip("/"))
DICOM_UPLOAD_PATH = os.getenv("DICOM_UPLOAD_PATH", os.path.join(_media_base, "dicom"))

# Absolute path used as a temporary landing zone while files are being
# uploaded and parsed before being moved to DICOM_UPLOAD_PATH. On the same
# volume as the upload path, so the move is a rename.
DICOM_STAGING_PATH = os.getenv("DICOM_STAGING_PATH", os.path.join(_media_base, "dicom-staging"))

# ---------------------------------------------------------------------------
# OHIF viewer
# ---------------------------------------------------------------------------

# Filesystem path to the built OHIF viewer dist. When set, the viewer is
# served at /plugin/dicom/ohif/ by plugins.dicom.views.ohif_viewer.
# Build the dist with ``scripts/build_ohif.sh`` (see plugins/dicom/README.md).
# Defaults to the ``ohif-dist/`` directory inside the plugin directory.
DICOM_OHIF_DIST_PATH = os.getenv(
    "DICOM_OHIF_DIST_PATH",
    os.path.join(os.path.dirname(__file__), "ohif-dist"),
)

# Development only: append ohif-app-config.demo.js to the viewer's configuration,
# adding OHIF's public demo server as a data source and turning the study list
# on, so the viewer can be tried on every modality without radiology data of
# one's own. Ignored unless DEBUG is on, so a production deployment cannot serve
# it whatever its .env says.
DICOM_OHIF_DEMO_DATA = env_bool("DICOM_OHIF_DEMO_DATA", default=False)

# ---------------------------------------------------------------------------
# Behaviour flags
# ---------------------------------------------------------------------------

# Maximum number of DICOM files accepted in a single upload request.
DICOM_MAX_UPLOAD_FILES = int(os.getenv("DICOM_MAX_UPLOAD_FILES", "500"))

# Maximum size in bytes of a single uploaded DICOM file.
DICOM_MAX_UPLOAD_FILE_SIZE = int(os.getenv("DICOM_MAX_UPLOAD_FILE_SIZE", str(2 * 1024**3)))

# Days a soft-deleted study stays recoverable before the scheduled purge
# hard-deletes its rows and files.
DICOM_TRASH_RETENTION_DAYS = int(os.getenv("DICOM_TRASH_RETENTION_DAYS", "30"))

# Scheduled purge — merged into the platform beat schedule by the plugin
# loader (CELERY_BEAT_SCHEDULE is dict-merged), alongside the recordings and
# media trash purges.
CELERY_BEAT_SCHEDULE = {
    "purge-deleted-dicom-studies": {
        "task": "dicom.purge_deleted_dicom_studies",
        "schedule": 3 * 60 * 60,
    },
}
