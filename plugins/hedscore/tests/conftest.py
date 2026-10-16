"""pytest configuration for the hedscore plugin suite; run under ``plugins.hedscore.settings_test``."""

import pytest


@pytest.fixture(autouse=True)
def use_hedscore_urlconf(settings):
    settings.ROOT_URLCONF = "plugins.hedscore.tests.urls"
