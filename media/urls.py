"""Media URL config — mounts the Ninja API under ``/media/api/v1/``."""

from django.urls import path

from media.api.v1.ninja import api as media_api

urlpatterns = [
    path("api/v1/", media_api.urls),
]
