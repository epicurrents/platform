"""Test settings — fast, self-contained, no external services required."""

from .common import *

# Deterministic HMAC key for the v2+ audit-trail algorithm. 32 bytes of
# repeating "a" — not a secret, predictable on purpose so test fixtures can
# exercise the keyed-hash path without env setup.
ACTIVITY_HASH_KEYS = {1: b"a" * 32}
ACTIVITY_HASH_KEY_CURRENT = 1

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Fast password hashing for tests — never use in production.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Run Celery tasks synchronously so we can assert on side-effects.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Capture outgoing emails in django.core.mail.outbox instead of sending.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# In-process cache — cleared between tests by conftest fixture.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Use temp-agnostic placeholders; individual tests override via tmp_path.
RECORDINGS_STAGING_PATH = "/tmp/epicurrents_test_staging"
RECORDINGS_UPLOAD_PATH = "/tmp/epicurrents_test_uploads"

# No VAPID keys in tests — push tasks short-circuit silently.
WEBPUSH_VAPID_PRIVATE_KEY = ""
WEBPUSH_VAPID_PUBLIC_KEY = ""

FRONTEND_URL = "http://testserver"

# Tests use synthetic peer URLs (peer.example.com, etc.) that don't resolve in
# DNS, so the SSRF guard's hostname lookup would fail.  Disable it by default;
# tests that exercise the SSRF check itself re-enable it via `settings`.
FEDERATION_ALLOW_PRIVATE_PEER_URLS = True

# Silence log noise during test runs.
LOGGING = {}
