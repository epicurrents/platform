"""Recordings URL config — mounts the Ninja API under ``/recordings/api/v1/``."""

from django.urls import path

from recordings.api.v1.ninja import api as recordings_api

urlpatterns = [
    path("api/v1/", recordings_api.urls),
]
