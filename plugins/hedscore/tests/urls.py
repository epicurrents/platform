"""Test URL configuration: the plugin API at its real ``/plugin/hedscore/api/v1/`` prefix beside the base routes."""

from django.urls import include, path

import plugins.hedscore.urls as hedscore_urls
from epicurrents.urls import urlpatterns as base_urlpatterns

urlpatterns = [
    path("plugin/hedscore/api/v1/", include(hedscore_urls)),
] + list(base_urlpatterns)
