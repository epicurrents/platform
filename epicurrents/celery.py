"""Celery application entry — bootstraps the worker against the DJANGO_MODE-selected settings."""

import os

from celery import Celery

from epicurrents.settings_mode import get_settings_module

os.environ.setdefault("DJANGO_SETTINGS_MODULE", get_settings_module())

app = Celery("epicurrents")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
