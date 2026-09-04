"""Annotations URL config — mounts the Ninja API under ``/annotations/api/v1/``."""

from django.urls import path

from annotations.api.v1.ninja import api as annotations_api

urlpatterns = [
    path("api/v1/", annotations_api.urls),
]
