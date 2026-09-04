"""Postgres-backed test settings — test_platform's fast config on a real server.

Identical to :mod:`epicurrents.settings.test_platform` (eager Celery,
deterministic audit-hash keys, fast password hasher, in-process cache) except
the database is PostgreSQL instead of in-memory SQLite. Use it to exercise
behaviour SQLite cannot reproduce — advisory locks (the audit chain's per-shard
lock), JSONB, and Postgres transaction semantics.

Point pytest at it with ``--ds=epicurrents.settings.test_postgres`` against a
running Postgres (the docker ``db`` service); pytest-django creates and drops a
``test_<DB_NAME>`` database, so the connecting role needs CREATEDB (the docker
image's ``POSTGRES_USER`` is a superuser and satisfies this).
"""

from decouple import config

from .test_platform import *

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="epicurrents"),
        "USER": config("DB_USERNAME", default="epicurrents"),
        "PASSWORD": config("DB_PASSWORD", default="epicurrents"),
        "HOST": config("DB_HOSTNAME", default="db"),
        "PORT": config("DB_PORT", default=5432, cast=int),
    }
}
