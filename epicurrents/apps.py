"""Django app configuration for the epicurrents core app.

This app owns:
- ``AccessRight`` — generic object-level access control model used by all other apps.
- Permissions module (``epicurrents.permissions``) — ``can_read_object``,
  ``can_write_object``, ``can_modify_object``, and the extension registry.
- ``ApiActivityLoggingMiddleware`` — request-level audit logging.
- Project loader (``epicurrents.project_loader``) — per-deployment customisation layer.
- Plugin loader (``epicurrents.plugin_loader``) — composable add-on layer validated at boot.
- Background tasks: ``purge_expired_access_rights``, ``clear_expired_sessions``.
- Management commands: ``activate_project``, ``deactivate_project``,
  ``remove_project_data``, ``init_env``, ``createadmin``, ``generate_vapid_keys``,
  ``sync_prod_to_dev``.
"""

import logging
import os
import re
import sys

from decouple import config
from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


# Commands that legitimately run BEFORE the operator has set real secrets:
# init_env writes them; generate_vapid_keys / check / help don't touch the DB.
# The placeholder-credential guard skips these so bootstrap can proceed.
_SKIP_PLACEHOLDER_CHECK_COMMANDS = frozenset({"init_env", "generate_vapid_keys", "check", "help", "--help", "-h"})

# Spellings of "yes" accepted for the boot-guard acknowledgement flags. Anything
# else, including nonsense, reads as "not acknowledged": these gate whether a
# warning is printed, so the safe failure direction is to keep warning. Never
# raise from here — the callers run inside AppConfig.ready(), where an exception
# is a deployment that will not start.
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


def _is_truthy(value: str) -> bool:
    """Whether an environment value spells an affirmative acknowledgement."""
    return value.strip().lower() in _TRUTHY_VALUES


# Known placeholder values from .env.example. Booting production with any
# of these for DB_PASSWORD would expose the database with a well-known
# credential. The empty string is included because an explicit
# ``DB_PASSWORD=`` line in .env (no value) reaches the settings as "".
_DB_PASSWORD_PLACEHOLDERS = frozenset({"epicurrents", "change-me", ""})

# Same shape for BORG_PASSPHRASE, but the empty string is intentionally
# excluded: an operator who clears the value is explicitly disabling
# repokey-encrypted backups (e.g. when an external backup system handles
# the data instead). The borg container will then fail every backup cycle
# loudly in its own logs, which is the right surface for that signal —
# blocking Django boot would force the operator to remove the borg service
# from compose just to disable backups, which isn't a workflow we want to
# require. ``change-me`` still fires because it's the .env.example default
# that survives ``init_env`` without ``--force``.
_BORG_PASSPHRASE_PLACEHOLDERS = frozenset({"change-me"})

# SECRET_KEY signs sessions and password-reset tokens; a publicly-known
# value means full account takeover by cookie forgery. ``change-me`` is the
# .env.example default; the ``django-insecure-`` prefix marks both this
# repo's settings fallback and any key copied from a ``startproject``
# template. The empty string reaches settings when .env carries a bare
# ``SECRET_KEY=`` line.
_SECRET_KEY_PLACEHOLDERS = frozenset({"change-me", ""})
_SECRET_KEY_PLACEHOLDER_PREFIX = "django-insecure-"

# ``createadmin`` (run on every container start) creates a superuser with
# ADMIN_PASSWORD when none exists. ``admin`` is both the settings fallback
# and the .env.example default; the empty string would create a superuser
# with an empty password.
_ADMIN_PASSWORD_PLACEHOLDERS = frozenset({"admin", "change-me", ""})

# FRONTEND_URL builds the link in every password-reset mail and the default OIDC
# redirect URI. The .env.example value points at Vite's dev server on port 5173,
# which no deployment serves — left in place, password reset silently mails a
# link to a machine that is not there. Only this exact value is refused: a
# production-mode install genuinely reached over localhost is supported (it is
# what docs/getting-started.md verifies), so the test is for the dev-server port,
# not for "localhost".
_FRONTEND_URL_PLACEHOLDER = "http://localhost:5173"


# Byte-size units as Caddy's ``request_body max_size`` parses them, established by
# adapting a config and reading the resulting JSON rather than from documentation:
# a bare letter and the ``B`` forms are DECIMAL (``2GB`` and ``2G`` both give
# 2,000,000,000), and only the ``iB`` forms are binary (``2GiB`` = 2,147,483,648).
# That asymmetry is the whole reason this guard exists — the obvious-looking
# ``PROXY_MAX_BODY_SIZE=2GB`` is *smaller* than a 2 GiB application limit.
_BYTE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "t": 1000**4,
    "tb": 1000**4,
    "ki": 1024,
    "kib": 1024,
    "mi": 1024**2,
    "mib": 1024**2,
    "gi": 1024**3,
    "gib": 1024**3,
    "ti": 1024**4,
    "tib": 1024**4,
}


def parse_byte_size(value: str) -> int | None:
    """Parse a Caddy-style size string to bytes, or ``None`` if it is not one.

    ``None`` means "do not draw a conclusion". The caller must not treat an
    unparseable value as a mismatch: Caddy would refuse the same string at its
    own startup, which is a louder and more accurate signal than Django guessing.
    """
    text = (value or "").strip()
    if not text:
        return None
    # No whitespace between number and unit: Caddy rejects "2 GB" outright, and a
    # parser that is more permissive than the thing it models would quietly
    # approve a pairing the proxy will not even start with.
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([a-zA-Z]*)", text)
    if not match:
        return None
    multiplier = _BYTE_UNITS.get(match.group(2).lower())
    if multiplier is None:
        return None
    return int(float(match.group(1)) * multiplier)


class EpicurrentsConfig(AppConfig):
    """Django app configuration for epicurrents core domain."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "epicurrents"

    def ready(self):
        # Imported for the @register side effect, as activity.checks is. At
        # module scope this would run before the app registry is populated,
        # which is exactly what the check needs to read.
        from . import checks  # noqa: F401

        self._validate_plugins()
        self._guard_placeholder_db_password()
        self._guard_placeholder_borg_passphrase()
        self._guard_placeholder_secret_key()
        self._guard_placeholder_admin_password()
        self._guard_activity_hash_key()
        self._guard_proxy_body_limit()
        self._guard_placeholder_frontend_url()
        self._warn_insecure_cookies()
        self._warn_unconfigured_email()
        self._warn_healthcheck_host()
        self._warn_local_only_backups()
        self._warn_unbounded_workers()
        self._warn_unbounded_worker_pool()
        self._warn_debug_mode()

    def _validate_plugins(self):
        """Validate enabled plugins now that the app registry is populated.

        Resolves each plugin's declared ``requires`` dependencies and checks
        for URL-namespace collisions, raising ``ImproperlyConfigured`` at boot
        rather than letting a misconfiguration surface as a first-request 500.
        A no-op when ``EPICURRENTS_PLUGINS`` is empty. Skips the bootstrap
        command set so ``init_env`` / ``generate_vapid_keys`` run before any
        plugin is configured.
        """
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from epicurrents.plugin_loader import validate_plugins

        validate_plugins()

    def _guard_placeholder_db_password(self):
        """Refuse to start in production with the .env.example placeholder DB_PASSWORD.

        Boot-time hard refusal rather than a soft warning. The placeholder
        is the documented default in .env.example; an operator who copies
        the file without running ``init_env`` (or who forgets to edit the
        DB credentials block) would silently expose the database with a
        well-known password. Better to fail visibly at startup.
        """
        # Only enforce when the operator has explicitly opted into
        # production mode. Development / unset modes still allow the
        # placeholder so bootstrap, tests, and local experimentation
        # work without ceremony.
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return

        # Skip during commands that run BEFORE secrets are generated.
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        databases = getattr(django_settings, "DATABASES", {})
        password = databases.get("default", {}).get("PASSWORD", "")
        if password in _DB_PASSWORD_PLACEHOLDERS:
            raise ImproperlyConfigured(
                "DB_PASSWORD is set to a placeholder value "
                f"({password!r}) while DJANGO_MODE=production. Set a real "
                "DB_PASSWORD in .env before starting the platform. Run "
                "`python manage.py init_env` to generate secrets, then "
                "edit .env to set DB_NAME, DB_USERNAME, and DB_PASSWORD "
                "to your real database credentials."
            )

    def _guard_placeholder_borg_passphrase(self):
        """Refuse to start in production with the .env.example placeholder BORG_PASSPHRASE.

        Same shape and motivation as the DB_PASSWORD guard above. A known
        passphrase silently produces backups that anyone with read access
        to the borg repo file can decrypt — defeating the whole point of
        repokey encryption.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        passphrase = os.environ.get("BORG_PASSPHRASE", "")
        if passphrase in _BORG_PASSPHRASE_PLACEHOLDERS:
            raise ImproperlyConfigured(
                "BORG_PASSPHRASE is set to a placeholder value "
                f"({passphrase!r}) while DJANGO_MODE=production. Set a "
                "real BORG_PASSPHRASE in .env before starting the platform. "
                "Run `python manage.py init_env --force` to regenerate "
                "all secrets (including BORG_PASSPHRASE), or edit .env "
                "directly with a strong unique value and record it where "
                "it survives a server loss — losing it means losing "
                "access to every backup."
            )

    def _guard_placeholder_secret_key(self):
        """Refuse to start in production with a placeholder SECRET_KEY.

        Same shape and motivation as the DB_PASSWORD guard above. SECRET_KEY
        signs session cookies and password-reset tokens, so a publicly-known
        value (the .env.example placeholder or the settings fallback) lets
        anyone forge a session for any account.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        secret_key = getattr(django_settings, "SECRET_KEY", "")
        if secret_key in _SECRET_KEY_PLACEHOLDERS or secret_key.startswith(_SECRET_KEY_PLACEHOLDER_PREFIX):
            raise ImproperlyConfigured(
                "SECRET_KEY is set to a placeholder value while "
                "DJANGO_MODE=production. A publicly-known signing key lets "
                "anyone forge session cookies and password-reset tokens. "
                "Run `python manage.py init_env` to generate a real key, "
                "or set SECRET_KEY in .env to a long random value."
            )

    def _guard_placeholder_admin_password(self):
        """Refuse to start in production with a placeholder ADMIN_PASSWORD.

        ``createadmin`` runs on every container start and creates a
        superuser with this password when none exists, so an unedited
        placeholder means an internet-reachable deployment with an
        ``admin``/``admin`` superuser login.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        password = getattr(django_settings, "ADMIN_PASSWORD", "admin")
        if password in _ADMIN_PASSWORD_PLACEHOLDERS:
            raise ImproperlyConfigured(
                "ADMIN_PASSWORD is set to a placeholder value while "
                "DJANGO_MODE=production. `createadmin` would create a "
                "superuser with a well-known password on first boot. Run "
                "`python manage.py init_env` to generate a strong "
                "ADMIN_PASSWORD, or set one in .env before starting the "
                "platform."
            )

    def _warn_debug_mode(self):
        """Emit a WARNING at boot whenever ``settings.DEBUG`` is True.

        Loud-by-design: a dev box is supposed to log its dev posture; a
        production box with DEBUG=True is a real exposure (verbose error
        pages leak stack traces and request data) and the operator
        deserves the loudest possible signal at startup. Skips the
        bootstrap command set so init_env / generate_vapid_keys runs
        cleanly.
        """
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        if getattr(django_settings, "DEBUG", False):
            mode = os.environ.get("DJANGO_MODE", "unset") or "unset"
            logger.warning(
                "DEBUG=True at boot under DJANGO_MODE=%s. Verbose error "
                "pages and Django Debug Toolbar surfaces are enabled — "
                "do not expose this instance to the public internet.",
                mode,
            )

    def _guard_proxy_body_limit(self):
        """Refuse to start when the proxy would reject uploads the app accepts.

        ``PROXY_MAX_BODY_SIZE`` caps the request body at the edge and
        ``RECORDINGS_MAX_UPLOAD_SIZE`` caps it in the application. If the edge
        ceiling is the lower of the two there is a band of file sizes that pass
        every check the operator knows about and then die at the proxy with a
        bare 413 — no application log line, nothing naming the limit that
        rejected them, and a size band that only shows up with large recordings.
        The two are set in different files and parsed by different code, so
        nothing else would catch the drift.

        The variable is only present when the proxy overlay is in the compose
        file list, so its absence means there is no edge limit to disagree with.
        An unparseable value is left alone: Caddy will reject the same string at
        its own startup, which says more than a guess made here.
        """
        raw = os.environ.get("PROXY_MAX_BODY_SIZE", "").strip()
        if not raw:
            return

        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        proxy_limit = parse_byte_size(raw)
        if proxy_limit is None:
            logger.warning(
                "PROXY_MAX_BODY_SIZE=%r is not a size Caddy will accept; skipping the upload-limit check.",
                raw,
            )
            return

        from django.conf import settings as django_settings

        app_limit = getattr(django_settings, "RECORDINGS_MAX_UPLOAD_SIZE", 0)
        if proxy_limit >= app_limit:
            return

        raise ImproperlyConfigured(
            f"PROXY_MAX_BODY_SIZE={raw!r} resolves to {proxy_limit:,} bytes, which is below "
            f"RECORDINGS_MAX_UPLOAD_SIZE ({app_limit:,} bytes). Uploads between the two would pass "
            f"the application's own limit and then be rejected by the proxy with a bare 413. "
            f"Raise PROXY_MAX_BODY_SIZE to at least {app_limit:,} bytes (note that Caddy reads "
            f"'GB' as 1000^3 and only 'GiB' as 1024^3), or lower RECORDINGS_MAX_UPLOAD_SIZE to match."
        )

    def _guard_placeholder_frontend_url(self):
        """Refuse to start in production while FRONTEND_URL is the dev-server default.

        ``settings.FRONTEND_URL`` is what
        :func:`user.api.v1.ninja.request_password_reset` interpolates into the
        reset link, and what the OIDC redirect URI defaults to. The failure it
        guards is a quiet one: the mail sends, the endpoint returns 200, and the
        user follows a link to a host that does not exist. Nothing in the logs
        marks it, and it is typically discovered by a locked-out user rather than
        by an operator.

        Only the literal ``.env.example`` value is refused, because port 5173 is
        Vite's dev server and never a deployment. A production-mode install
        reached over ``localhost`` on a real port stays valid.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return

        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        if getattr(django_settings, "FRONTEND_URL", "").rstrip("/") != _FRONTEND_URL_PLACEHOLDER:
            return

        raise ImproperlyConfigured(
            f"FRONTEND_URL is still the .env.example placeholder ({_FRONTEND_URL_PLACEHOLDER}). It is the base "
            "of every password-reset link, so leaving it would mail users a link to a Vite dev server that this "
            "deployment does not run. Set it to the URL users reach this deployment at, e.g. "
            "FRONTEND_URL=https://eeg.example.com"
        )

    def _warn_unconfigured_email(self):
        """Warn when production has no mail backend configured.

        ``EMAIL_BACKEND`` defaults to Django's console backend, which writes the
        message to stdout and reports success. Password reset then appears to
        work from every angle except the user's inbox.

        A warning rather than a refusal: a deployment with admin-provisioned
        accounts and no self-service reset is legitimate. It has to be a choice
        though, so silencing it takes a dedicated acknowledgement rather than
        merely having the value present.

        An earlier version keyed off ``EMAIL_BACKEND`` being absent from the
        environment, on the theory that an unset variable means nobody chose.
        That was inert: ``.env.example`` ships the console backend *uncommented*
        and ``init_env`` copies the file, so the variable is set in every
        generated deployment — the warning could never fire on the one path it
        was written for. Resolve the effective backend instead.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return

        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        if "console" not in getattr(django_settings, "EMAIL_BACKEND", ""):
            return

        if config("EMAIL_DISABLED", default=False, cast=bool):
            return

        logger.warning(
            "Production is using Django's console email backend: password-reset mail is written to the "
            "container log and never delivered, while the endpoint reports success. Configure EMAIL_HOST and "
            "the rest of EMAIL_* for an SMTP relay, or set EMAIL_DISABLED=true to acknowledge that this "
            "deployment sends no mail."
        )

    def _warn_local_only_backups(self):
        """Warn when production keeps its only backup copy on the host it protects.

        An empty ``BORG_REMOTE_REPO`` puts every archive in the ``borg-data``
        volume alongside the recordings and the database, so a host loss takes
        all three at once. This is the one failure mode on the pre-launch list
        that is unrecoverable rather than inconvenient, and nothing about a
        running deployment makes it visible — the backups genuinely work.

        A warning rather than a refusal, and it is deliberately the web
        container that says so: an evaluation or demo install with nowhere to
        ship archives is legitimate, and the borg container's own log is not
        where anyone looks until a restore has already failed. Silencing it
        takes the dedicated acknowledgement rather than merely leaving the value
        empty, on the same reasoning as the unconfigured-mail warning.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return
        # The local tier can be switched off, so "no remote" is no longer the
        # same question as "one copy". With both off there is no repository at
        # all: the borg container refuses to start in that state, but a web
        # container that said nothing would leave the only visible signal inside
        # a container the operator has no reason to look at.
        has_remote = bool(os.environ.get("BORG_REMOTE_REPO", "").strip())
        local_enabled = os.environ.get("BACKUP_LOCAL_ENABLED", "").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        if not has_remote and not local_enabled:
            logger.warning(
                "No backup repository is configured: BACKUP_LOCAL_ENABLED is false and BORG_REMOTE_REPO "
                "is empty, so this deployment writes no archives anywhere. The backup container will "
                "refuse to start until one of them is set."
            )
            return
        if has_remote:
            return
        # Read through os.environ like every sibling guard rather than through
        # decouple's bool cast, which raises ValueError on anything outside its
        # truth vocabulary. That exception would surface from AppConfig.ready(),
        # so BACKUP_LOCAL_ONLY=Y — a reasonable spelling of the acknowledgement —
        # would refuse to boot the deployment instead of silencing a warning.
        if _is_truthy(os.environ.get("BACKUP_LOCAL_ONLY", "")):
            return

        logger.warning(
            "BORG_REMOTE_REPO is empty, so backups are written only to the borg-data volume on this host — "
            "losing the host loses the recordings, the database and the backups together. Set it to an "
            "off-host SSH target, or set BACKUP_LOCAL_ONLY=true to acknowledge that this deployment keeps "
            "its only copy locally."
        )

    def _warn_unbounded_workers(self):
        """Warn when the Celery workers run in production with no memory cap.

        A denoise or a leadfield computation allocates in proportion to the
        recording rather than to request volume, so a single large job can
        exhaust the host. What dies then is not the worker: the kernel picks by
        score, and Postgres — large, long-lived, shared-memory heavy — is a
        strong candidate, so an ingest job takes the database down with it.

        A warning rather than a default limit, because the number depends on the
        host and a cap set too low turns a slow job into a killed one, which is
        harder to diagnose than no cap at all. The sizing rule is in
        docs/operations.md. Only ``CELERY_MEM_LIMIT`` is checked: the CPU limit
        bounds throughput rather than survival, and a host does not fall over
        because a worker used every core.

        Fires from the web container too, which does not run the workers. That is
        deliberate — the value is deployment-wide and the web log is where an
        operator looks.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        # Any explicit value silences this, including "0". 0 is compose's own
        # spelling of "no limit", so an operator who has decided to run unbounded
        # writes it down and the warning stops — which is what the message asks
        # for. Only an absent or empty value means nobody chose. An earlier
        # version treated "0" as unset, so following the instruction in the
        # warning produced the same warning on every boot forever.
        if os.environ.get("CELERY_MEM_LIMIT", "").strip():
            return

        logger.warning(
            "CELERY_MEM_LIMIT is unset, so the Celery workers run without a memory cap. A large denoise or "
            "leadfield job can exhaust the host, and the kernel's OOM killer is as likely to take Postgres "
            "as the worker. Set CELERY_MEM_LIMIT (see docs/operations.md for the sizing rule), or set it to 0 "
            "to acknowledge that this deployment runs the workers unbounded."
        )

    def _warn_unbounded_worker_pool(self):
        """Warn when CELERY_CONCURRENCY holds a value celery reads as no value.

        ``--concurrency=0`` is neither "one job at a time" nor "unbounded but
        chosen". Celery's CLI callback is ``value or conf.worker_concurrency``
        and the worker then falls back to ``cpu_count()``, so 0 silently restores
        the host-sized pool that pinning the flag exists to remove — the pool
        being what multiplies a single job's peak into the container's.

        The trap is reachable by analogy rather than by carelessness: 0 *is* the
        documented acknowledgement for ``CELERY_MEM_LIMIT``, and the two settings
        sit next to each other in .env.example. Hence a warning here, on a value
        celery itself accepts without complaint.

        Compose supplies the default when the variable is absent, so only an
        explicit value can be wrong. A non-numeric one is left alone: celery's
        own ``type=int`` rejects it and the worker exits loudly, which needs no
        help from here — and parsing it eagerly would raise from inside
        ``ready()`` and take the boot down with it.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        raw = os.environ.get("CELERY_CONCURRENCY", "").strip()
        if not raw:
            return
        try:
            pool_size = int(raw)
        except ValueError:
            return
        if pool_size > 0:
            return

        logger.warning(
            "CELERY_CONCURRENCY=%s does not bound the Celery worker pool. Unlike CELERY_MEM_LIMIT, where 0 "
            "records a deliberate decision, celery reads a non-positive concurrency as no value at all and "
            "falls back to the host's CPU count — so the pool, and with it the worker container's peak "
            "memory, is sized by the host rather than by the workload. Set a positive pool size instead "
            "(2 suits an 8 GB host; see docs/operations.md for the sizing rule).",
            raw,
        )

    def _warn_healthcheck_host(self):
        """Warn when the container healthcheck cannot reach the app over loopback.

        The ``web`` healthcheck in docker-compose.yml requests
        ``http://127.0.0.1:8000/api/v1/ready`` from inside the container, so the
        Host header is ``127.0.0.1:8000``. An operator who replaces the shipped
        ``ALLOWED_HOSTS`` with their own domain rather than appending to it turns
        every probe into a 400 DisallowedHost, and the container sits permanently
        unhealthy while serving real traffic perfectly well — a failure that
        looks like a broken deployment and is a one-token config edit.

        A warning rather than a refusal: a deployment that runs the image outside
        this compose file, with an external probe, has no reason to carry the
        loopback entry.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        hosts = [str(host).strip() for host in getattr(django_settings, "ALLOWED_HOSTS", [])]
        if any(host in ("127.0.0.1", "*") for host in hosts):
            return

        logger.warning(
            "ALLOWED_HOSTS does not contain 127.0.0.1, so the container healthcheck's loopback request to "
            "/api/v1/ready is answered with 400 DisallowedHost and the web container reports unhealthy "
            "regardless of its actual state. Append 127.0.0.1 to ALLOWED_HOSTS, or replace the healthcheck "
            "if this deployment probes the app from outside the container."
        )

    def _warn_insecure_cookies(self):
        """Log a WARNING when CSRF / session cookies are not marked Secure.

        Soft warning rather than a hard refusal: a dev or staging deploy
        behind a plain-HTTP endpoint legitimately needs the Secure flag
        off (the browser refuses to send Secure cookies over HTTP,
        producing 403 CSRF failures on every form submit). The defaults
        in ``epicurrents/settings/production.py`` keep both cookies
        Secure; the env overrides exist so the operator can opt down
        deliberately. The boot warning makes accidental flips in real
        production visible in the log stream.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        loosened = []
        if not getattr(django_settings, "CSRF_COOKIE_SECURE", True):
            loosened.append("CSRF_COOKIE_SECURE")
        if not getattr(django_settings, "SESSION_COOKIE_SECURE", True):
            loosened.append("SESSION_COOKIE_SECURE")
        if loosened:
            logger.warning(
                "Cookie security flag(s) disabled under DJANGO_MODE=production: %s. "
                "The cookies will be sent over plain HTTP; only acceptable for "
                "HTTP-only dev / staging deployments. Production deployments "
                "behind HTTPS should leave these unset (default True).",
                ", ".join(loosened),
            )

    def _guard_activity_hash_key(self):
        """Refuse to start in production without an audit-trail HMAC key
        for the current write version.

        With no key, ``current_write_hash_version`` falls back to v1 — the
        unkeyed legacy algorithm. That fallback is the right behaviour in
        dev mode (lets local experimentation run without ``init_env``) but
        is a silent downgrade of the audit trail's tamper-evidence in
        production. The boot refusal makes the misconfig loud.
        """
        if os.environ.get("DJANGO_MODE", "").lower() != "production":
            return
        if any(arg in _SKIP_PLACEHOLDER_CHECK_COMMANDS for arg in sys.argv):
            return

        from django.conf import settings as django_settings

        keys = getattr(django_settings, "ACTIVITY_HASH_KEYS", {})
        current = getattr(django_settings, "ACTIVITY_HASH_KEY_CURRENT", None)
        if not keys or current is None or current not in keys:
            raise ImproperlyConfigured(
                "No audit-trail HMAC key configured for "
                f"ACTIVITY_HASH_KEY_CURRENT={current!r} while "
                "DJANGO_MODE=production. Run `python manage.py init_env` "
                "on a fresh deploy (auto-generates ACTIVITY_HASH_KEY_V1), "
                "or set ACTIVITY_HASH_KEY_V{N} in .env to a base64url-"
                "encoded 32-byte secret. Losing this key means no past "
                "audit row will verify — back it up alongside BORG_PASSPHRASE."
            )
