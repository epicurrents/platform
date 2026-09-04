"""Settings overrides for the *example* project.

This file is optional.  When present, its public names are merged into the
active Django settings module according to these rules:

List settings (INSTALLED_APPS, MIDDLEWARE, AUTH_PASSWORD_VALIDATORS,
AUTHENTICATION_BACKENDS, PASSWORD_HASHERS):
    The project's list is *appended* to the base list.  Duplicates are
    skipped.  The project app itself (``projects.example``) is always added
    to INSTALLED_APPS by the loader before this file is processed, so you
    only need to list *extra* third-party apps here.

Dict settings (CELERY_BEAT_SCHEDULE):
    The project's dict is *merged* into the base dict.  Project keys win on
    conflict.

Everything else:
    The project value *replaces* the base value.

Caution: overriding security-sensitive scalars (e.g. DEBUG, SECRET_KEY,
SECURE_SSL_REDIRECT) from a project settings file is rarely intentional.
Prefer environment variables for deployment-level concerns; reserve this file
for feature flags and project-specific application settings.
"""

# ── Example: project-specific application settings ────────────────────────────
# These are arbitrary settings consumed by project code (models, middleware,
# views).  Use a distinctive prefix to avoid collision with core settings.

# Name of the clinical institution, stamped into EDF recording-identification
# fields by InstitutionWatermarkMiddleware.
EXAMPLE_INSTITUTION_NAME = "Example Clinic"

# Maximum number of characters allowed in a RecordingNote.notes field
# (enforced at the API level, not in the DB schema).
EXAMPLE_NOTE_MAX_LENGTH = 2000


# ── Example: override a core scalar setting ────────────────────────────────────
# Keep the recycle bin shorter in this deployment — 7 days instead of the
# default 30.
RECORDINGS_TRASH_RETENTION_DAYS = 7


# ── Example: add a Django web middleware ───────────────────────────────────────
# List entries are *appended* to the base MIDDLEWARE list, so the ordering
# below is relative to the end of the stack, not the beginning.  If you need
# precise placement (e.g. before SessionMiddleware), set the full MIDDLEWARE
# list here instead — it will replace the base list entirely.
MIDDLEWARE = [
    # A hypothetical project-specific request logger.
    # "projects.example.http_middleware.ExampleRequestLogger",
]


# ── Example: add an extra third-party Django app ──────────────────────────────
# The project app itself is already added by the loader; only list additional
# apps here.
INSTALLED_APPS = [
    # "django_extensions",  # example extra app
]
