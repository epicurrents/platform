"""Test URL configuration for the dicom plugin.

Mounts the plugin API at the real ``/plugin/dicom/api/v1/`` prefix alongside
the base routes, so the audit-trail middleware's path recognition is exercised
against the true mount shape.
"""

from django.urls import include, path

import plugins.dicom.urls as dicom_urls
from epicurrents.urls import urlpatterns as base_urlpatterns

urlpatterns = [
    path("plugin/dicom/api/v1/", include(dicom_urls)),
] + list(base_urlpatterns)
