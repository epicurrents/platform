"""Test settings for the hedscore plugin.

Extends the platform test settings and adds plugins.hedscore to INSTALLED_APPS. Run the suite with:

    DJANGO_SETTINGS_MODULE=plugins.hedscore.settings_test pytest plugins/hedscore/tests/
"""

from epicurrents.settings.test_platform import *
from plugins.hedscore.settings import *

INSTALLED_APPS = INSTALLED_APPS + ["plugins.hedscore"]
