"""Epicurrents core app — access control, audit middleware, settings, project loader, lifecycle commands."""

from .celery import app as celery_app
from .version import __version__

__all__ = ("__version__", "celery_app")
