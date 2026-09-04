"""Test URL configuration — mounts the example API at its production slot alongside the base routes."""

from django.urls import include, path

import projects.example.urls as example_urls
from epicurrents.urls import urlpatterns as base_urlpatterns

urlpatterns = [
    path("project/api/v1/", include(example_urls)),
] + list(base_urlpatterns)
