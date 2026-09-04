"""Settings for the *dicom* plugin.

Environment-level overrides live in ``.env``; these are the defaults consumed
by plugin code when the env var is not set.
"""

import os

# ---------------------------------------------------------------------------
# File storage
# ---------------------------------------------------------------------------

# Absolute path to the directory where indexed DICOM files are stored.
# Must be writable by the application user. Defaults to a ``dicom/``
# subdirectory alongside the EDF recordings directory (if set).
_recordings_base = os.getenv("RECORDINGS_UPLOAD_PATH", "/data/recordings")
DICOM_UPLOAD_PATH = os.getenv(
    "DICOM_UPLOAD_PATH",
    os.path.join(os.path.dirname(_recordings_base), "dicom"),
)

# Absolute path used as a temporary landing zone while files are being
# uploaded and parsed before being moved to DICOM_UPLOAD_PATH.
DICOM_STAGING_PATH = os.getenv(
    "DICOM_STAGING_PATH",
    os.path.join(os.path.dirname(_recordings_base), "dicom-staging"),
)

# ---------------------------------------------------------------------------
# OHIF viewer
# ---------------------------------------------------------------------------

# Filesystem path to the built OHIF viewer dist. When set, the viewer is
# served at /plugin/dicom/viewer/ by plugins.dicom.views.ohif_viewer.
# Build the dist with ``scripts/build_ohif.sh`` (see plugins/dicom/README.md).
# Defaults to the ``ohif-dist/`` directory inside the plugin directory.
DICOM_OHIF_DIST_PATH = os.getenv(
    "DICOM_OHIF_DIST_PATH",
    os.path.join(os.path.dirname(__file__), "ohif-dist"),
)

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
