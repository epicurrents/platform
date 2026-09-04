"""Development settings — SQLite by default, Postgres via ``DB_DEV_ENGINE=postgres``, ``DEBUG=True``."""

from decouple import config

from .common import *

DEBUG = config("DEBUG", default=True, cast=bool)

db_dev_engine = config("DB_DEV_ENGINE", default="sqlite").strip().lower()

if db_dev_engine == "postgres":
    # Postgres dev mode (typically docker dev with the shared docker
    # Postgres instance) reads the canonical DB_* env vars rather than a
    # separate DB_DEV_* set — the docker stack ships a single database
    # and there's no value in maintaining two parallel credential
    # blocks. Operators who want local-host dev pointed at a different
    # Postgres instance override DB_NAME / DB_HOSTNAME etc. in their
    # shell or a local .env.
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
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / config("DB_DEV_NAME", default="db.sqlite3"),
        }
    }

# Cookie Secure-flag defaults for development. The HTTP serving path
# (runserver on the host, or the docker dev stack bound to plain HTTP
# localhost) needs both cookies sent over HTTP — otherwise admin form
# submits 403 on CSRF. Both stay env-overrideable so a dev box behind
# local HTTPS (mkcert + reverse proxy) can opt back up to True.
CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", default=False, cast=bool)
SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", default=False, cast=bool)

# The enforce_session_csrf chokepoint is off by default in development: the
# Vite dev server serves the SPA from a different origin than the API, so it
# cannot read the csrftoken cookie to echo it back, and host tooling (httpie,
# curl) would otherwise need a token on every write. Opt back in by setting
# SESSION_CSRF_ENFORCED=true to exercise the production path locally.
SESSION_CSRF_ENFORCED = config("SESSION_CSRF_ENFORCED", default=False, cast=bool)

# The API request-rate throttle is off by default in development: hot-reload
# loops, test scripts, and a single dev poking every endpoint would otherwise
# trip it. Set API_THROTTLE_ENABLED=true to exercise it locally.
API_THROTTLE_ENABLED = config("API_THROTTLE_ENABLED", default=False, cast=bool)

# Diagnostic logging for development. Without an explicit LOGGING dict the
# stack falls back to Django's defaults, which keep app loggers silent
# below WARNING — so the INFO/DEBUG diagnostics that production emits as
# JSON would be invisible locally. Mirror production's handler/level shape
# with a human-readable formatter and the same LOG_LEVEL / DJANGO_LOG_LEVEL
# knobs, so raising verbosity works identically in both modes (see
# docs/debugging.md).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {
            "format": "{asctime} {levelname:<7} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": config("LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": config("DJANGO_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
}
