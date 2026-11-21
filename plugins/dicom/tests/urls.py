"""Test URL configuration for the dicom plugin.

Mounts the plugin API at the real ``/plugin/dicom/api/v1/`` prefix and the
viewer at ``/plugin/dicom/`` alongside the base routes, so the audit-trail
middleware's path recognition is exercised against the true mount shape.
"""

from django.urls import include, path

import plugins.dicom.public_urls as dicom_public_urls
import plugins.dicom.urls as dicom_urls
from epicurrents.urls import urlpatterns as base_urlpatterns

urlpatterns = [
    path("plugin/dicom/api/v1/", include(dicom_urls)),
    path("plugin/dicom/", include(dicom_public_urls)),
] + list(base_urlpatterns)
