"""Test settings for the dicom plugin.

Extends the platform test settings and adds plugins.dicom to INSTALLED_APPS.
Run the suite with:

    DJANGO_SETTINGS_MODULE=plugins.dicom.settings_test pytest plugins/dicom/tests/

The DICOM storage paths are placeholders — the autouse ``dicom_dirs`` fixture
in ``plugins/dicom/tests/conftest.py`` points them at a per-test tmp_path.
"""

import tempfile

from epicurrents.settings.test_platform import *

INSTALLED_APPS = INSTALLED_APPS + ["plugins.dicom"]

DICOM_UPLOAD_PATH = tempfile.mkdtemp(prefix="dicom-test-upload-")
DICOM_STAGING_PATH = tempfile.mkdtemp(prefix="dicom-test-staging-")
DICOM_MAX_UPLOAD_FILES = 500
DICOM_MAX_UPLOAD_FILE_SIZE = 2 * 1024**3
DICOM_TRASH_RETENTION_DAYS = 30
