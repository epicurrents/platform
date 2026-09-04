"""Compute URL config — mounts the Ninja API under ``/compute/api/v1/``."""

from django.urls import path

from compute.api.v1.ninja import api as compute_api

urlpatterns = [
    path("api/v1/", compute_api.urls),
]
