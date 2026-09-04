"""Tests for the placeholder-credential startup refusals in ``EpicurrentsConfig``.

The guards (``_guard_placeholder_db_password`` and
``_guard_placeholder_borg_passphrase``) run from ``AppConfig.ready()`` at app
load time, so this suite calls the methods directly with mocked environment /
settings / argv rather than reloading Django.
"""

import os
import sys
from unittest import mock

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured


def _config():
    return apps.get_app_config("epicurrents")


def _set_password(settings, password: str) -> None:
    settings.DATABASES = {
        **settings.DATABASES,
        "default": {**settings.DATABASES["default"], "PASSWORD": password},
    }


class TestDbPasswordGuard:
    def test_raises_in_production_with_placeholder(self, settings):
        _set_password(settings, "epicurrents")
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured, match="placeholder"):
                    _config()._guard_placeholder_db_password()

    def test_raises_for_change_me_placeholder(self, settings):
        _set_password(settings, "change-me")
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured):
                    _config()._guard_placeholder_db_password()

    def test_raises_for_empty_password(self, settings):
        _set_password(settings, "")
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured):
                    _config()._guard_placeholder_db_password()

    def test_silent_in_production_with_real_password(self, settings):
        _set_password(settings, "an-actual-secret-of-real-length-h3r3")
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_db_password()  # no raise

    def test_silent_in_development_with_placeholder(self, settings):
        _set_password(settings, "epicurrents")
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_db_password()  # no raise

    def test_silent_when_django_mode_unset(self, settings):
        _set_password(settings, "epicurrents")
        os_env = {k: v for k, v in os.environ.items() if k != "DJANGO_MODE"}
        with mock.patch.dict(os.environ, os_env, clear=True):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_db_password()  # no raise

    def test_silent_during_init_env_even_in_production(self, settings):
        """Bootstrap runs init_env BEFORE secrets exist; the guard must
        allow it so the operator can actually generate the password."""
        _set_password(settings, "epicurrents")
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "init_env"]):
                _config()._guard_placeholder_db_password()  # no raise

    def test_silent_during_help_in_production(self, settings):
        _set_password(settings, "epicurrents")
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "help"]):
                _config()._guard_placeholder_db_password()  # no raise


class TestBorgPassphraseGuard:
    def test_raises_in_production_with_change_me(self):
        with (
            mock.patch.dict(
                os.environ,
                {"DJANGO_MODE": "production", "BORG_PASSPHRASE": "change-me"},
            ),
            mock.patch.object(sys, "argv", ["manage.py", "runserver"]),
        ):
            with pytest.raises(ImproperlyConfigured, match="placeholder"):
                _config()._guard_placeholder_borg_passphrase()

    def test_silent_in_production_with_empty_passphrase(self):
        """Empty BORG_PASSPHRASE is an explicit disable, not a placeholder.
        The borg container will fail every cycle loudly in its own logs;
        Django shouldn't block boot on it. Operators with an external
        backup system rely on this."""
        with (
            mock.patch.dict(
                os.environ,
                {"DJANGO_MODE": "production", "BORG_PASSPHRASE": ""},
            ),
            mock.patch.object(sys, "argv", ["manage.py", "runserver"]),
        ):
            _config()._guard_placeholder_borg_passphrase()  # no raise

    def test_silent_in_production_with_real_passphrase(self):
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DJANGO_MODE": "production",
                    "BORG_PASSPHRASE": "Cr3-aRandomBack_upPassphrase-fT5xq",
                },
            ),
            mock.patch.object(sys, "argv", ["manage.py", "runserver"]),
        ):
            _config()._guard_placeholder_borg_passphrase()  # no raise

    def test_silent_in_development_with_placeholder(self):
        with (
            mock.patch.dict(
                os.environ,
                {"DJANGO_MODE": "development", "BORG_PASSPHRASE": "change-me"},
            ),
            mock.patch.object(sys, "argv", ["manage.py", "runserver"]),
        ):
            _config()._guard_placeholder_borg_passphrase()  # no raise

    def test_silent_when_django_mode_unset(self):
        os_env = {k: v for k, v in os.environ.items() if k not in ("DJANGO_MODE", "BORG_PASSPHRASE")}
        os_env["BORG_PASSPHRASE"] = "change-me"
        with mock.patch.dict(os.environ, os_env, clear=True):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_borg_passphrase()  # no raise

    def test_silent_during_init_env_even_in_production(self):
        with (
            mock.patch.dict(
                os.environ,
                {"DJANGO_MODE": "production", "BORG_PASSPHRASE": "change-me"},
            ),
            mock.patch.object(sys, "argv", ["manage.py", "init_env"]),
        ):
            _config()._guard_placeholder_borg_passphrase()  # no raise


class TestSecretKeyGuard:
    """Production boot refusal on a placeholder SECRET_KEY.

    SECRET_KEY signs sessions and password-reset tokens; a publicly-known
    value is full account takeover by cookie forgery.
    """

    def test_raises_in_production_with_change_me(self, settings):
        settings.SECRET_KEY = "change-me"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured, match="placeholder"):
                    _config()._guard_placeholder_secret_key()

    def test_raises_in_production_with_django_insecure_prefix(self, settings):
        settings.SECRET_KEY = "django-insecure-change-me"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured):
                    _config()._guard_placeholder_secret_key()

    def test_raises_in_production_with_empty_key(self, settings):
        settings.SECRET_KEY = ""
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured):
                    _config()._guard_placeholder_secret_key()

    def test_silent_in_production_with_real_key(self, settings):
        settings.SECRET_KEY = "k9#x!v2$realrandomkeygeneratedbyinitenv1234567890"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_secret_key()  # no raise

    def test_silent_in_development_with_placeholder(self, settings):
        settings.SECRET_KEY = "django-insecure-change-me"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_secret_key()  # no raise

    def test_silent_during_init_env_even_in_production(self, settings):
        settings.SECRET_KEY = "change-me"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "init_env"]):
                _config()._guard_placeholder_secret_key()  # no raise


class TestAdminPasswordGuard:
    """Production boot refusal on a placeholder ADMIN_PASSWORD.

    ``createadmin`` runs on every container start and would mint an
    ``admin``/``admin`` superuser on an unedited deployment.
    """

    def test_raises_in_production_with_admin(self, settings):
        settings.ADMIN_PASSWORD = "admin"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured, match="placeholder"):
                    _config()._guard_placeholder_admin_password()

    def test_raises_in_production_with_empty_password(self, settings):
        settings.ADMIN_PASSWORD = ""
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured):
                    _config()._guard_placeholder_admin_password()

    def test_raises_during_createadmin_in_production(self, settings):
        """createadmin is exactly the command that would mint the
        admin/admin superuser — it must not be on the skip list."""
        settings.ADMIN_PASSWORD = "admin"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "createadmin"]):
                with pytest.raises(ImproperlyConfigured):
                    _config()._guard_placeholder_admin_password()

    def test_silent_in_production_with_real_password(self, settings):
        settings.ADMIN_PASSWORD = "a-real-generated-password-1234"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_admin_password()  # no raise

    def test_silent_in_development_with_placeholder(self, settings):
        settings.ADMIN_PASSWORD = "admin"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_admin_password()  # no raise

    def test_silent_during_init_env_even_in_production(self, settings):
        settings.ADMIN_PASSWORD = "admin"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "init_env"]):
                _config()._guard_placeholder_admin_password()  # no raise


class TestActivityHashKeyGuard:
    """Production boot refusal when no audit-trail HMAC key is configured.

    Without a key, ``current_write_hash_version`` falls back to v1 (the
    unkeyed legacy algorithm), which is a silent downgrade of the audit
    trail's tamper-evidence. The guard makes that downgrade loud in
    production while allowing it silently in dev mode (local
    experimentation without ``init_env``).
    """

    def test_raises_in_production_with_no_keys(self, settings):
        settings.ACTIVITY_HASH_KEYS = {}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured, match="audit-trail HMAC key"):
                    _config()._guard_activity_hash_key()

    def test_raises_in_production_when_current_points_at_missing_key(self, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 2  # points at non-existent key
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured, match="audit-trail HMAC key"):
                    _config()._guard_activity_hash_key()

    def test_silent_in_production_when_key_configured(self, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_activity_hash_key()  # no raise

    def test_silent_in_development_with_no_keys(self, settings):
        settings.ACTIVITY_HASH_KEYS = {}
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_activity_hash_key()  # no raise

    def test_silent_during_init_env_in_production(self, settings):
        """init_env writes the first key into .env; the guard must not
        block the very command that fills the gap."""
        settings.ACTIVITY_HASH_KEYS = {}
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "init_env"]):
                _config()._guard_activity_hash_key()  # no raise


class TestInsecureCookieWarning:
    """Boot-time WARN when CSRF / session cookies are not Secure under
    DJANGO_MODE=production. Soft warning, not a hard refusal — operators
    behind HTTP-only deployments legitimately opt down."""

    def test_warns_when_csrf_cookie_secure_false_in_production(self, settings, caplog):
        settings.CSRF_COOKIE_SECURE = False
        settings.SESSION_COOKIE_SECURE = True
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_insecure_cookies()
        assert any("CSRF_COOKIE_SECURE" in record.message for record in caplog.records)

    def test_warns_when_session_cookie_secure_false_in_production(self, settings, caplog):
        settings.CSRF_COOKIE_SECURE = True
        settings.SESSION_COOKIE_SECURE = False
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_insecure_cookies()
        assert any("SESSION_COOKIE_SECURE" in record.message for record in caplog.records)

    def test_warns_with_both_names_when_both_false(self, settings, caplog):
        settings.CSRF_COOKIE_SECURE = False
        settings.SESSION_COOKIE_SECURE = False
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_insecure_cookies()
        record = next(r for r in caplog.records if "CSRF_COOKIE_SECURE" in r.message)
        assert "SESSION_COOKIE_SECURE" in record.message

    def test_silent_when_both_secure(self, settings, caplog):
        settings.CSRF_COOKIE_SECURE = True
        settings.SESSION_COOKIE_SECURE = True
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_insecure_cookies()
        assert not any("COOKIE_SECURE" in record.message for record in caplog.records)

    def test_silent_in_development_mode(self, settings, caplog):
        settings.CSRF_COOKIE_SECURE = False
        settings.SESSION_COOKIE_SECURE = False
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_insecure_cookies()
        assert not any("COOKIE_SECURE" in record.message for record in caplog.records)

    def test_silent_during_init_env(self, settings, caplog):
        settings.CSRF_COOKIE_SECURE = False
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "init_env"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_insecure_cookies()
        assert not any("COOKIE_SECURE" in record.message for record in caplog.records)


class TestDebugModeWarning:
    """Boot-time WARN when ``settings.DEBUG`` is True, regardless of
    DJANGO_MODE. Dev posture is visible in the log stream; production
    deploys that accidentally ship with DEBUG=True get the loudest
    possible signal at startup."""

    def test_warns_when_debug_true_in_production(self, settings, caplog):
        settings.DEBUG = True
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_debug_mode()
        assert any("DEBUG=True" in record.message for record in caplog.records)

    def test_warns_when_debug_true_in_development(self, settings, caplog):
        settings.DEBUG = True
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_debug_mode()
        assert any("DEBUG=True" in record.message for record in caplog.records)

    def test_silent_when_debug_false(self, settings, caplog):
        settings.DEBUG = False
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_debug_mode()
        assert not any("DEBUG=True" in record.message for record in caplog.records)

    def test_silent_during_init_env(self, settings, caplog):
        settings.DEBUG = True
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "init_env"]):
                with caplog.at_level("WARNING", logger="epicurrents.apps"):
                    _config()._warn_debug_mode()
        assert not any("DEBUG=True" in record.message for record in caplog.records)


class TestFrontendUrlGuard:
    """FRONTEND_URL is the base of every password-reset link.

    Left at the .env.example default it points at Vite's dev server, so reset
    mail sends successfully to a host that does not exist — a failure with no
    log line and no failing request, usually found by a locked-out user.
    """

    def test_raises_in_production_with_the_placeholder(self, settings):
        settings.FRONTEND_URL = "http://localhost:5173"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured, match="password-reset"):
                    _config()._guard_placeholder_frontend_url()

    def test_trailing_slash_does_not_evade_it(self, settings):
        settings.FRONTEND_URL = "http://localhost:5173/"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with pytest.raises(ImproperlyConfigured):
                    _config()._guard_placeholder_frontend_url()

    def test_allows_a_real_localhost_deployment(self, settings):
        # A production-mode install reached over localhost is supported — it is
        # what the getting-started verification step does. Only the dev-server
        # port is refused.
        settings.FRONTEND_URL = "http://localhost:8000"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_frontend_url()

    def test_allows_a_configured_url(self, settings):
        settings.FRONTEND_URL = "https://eeg.example.com"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_frontend_url()

    def test_silent_outside_production(self, settings):
        settings.FRONTEND_URL = "http://localhost:5173"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                _config()._guard_placeholder_frontend_url()

    def test_skipped_for_bootstrap_commands(self, settings):
        settings.FRONTEND_URL = "http://localhost:5173"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "init_env"]):
                _config()._guard_placeholder_frontend_url()


class TestUnconfiguredEmailWarning:
    """A warning, not a refusal: a deployment that sends no mail is legitimate,
    but it has to be an acknowledged choice.

    Keyed off the *resolved* backend rather than whether EMAIL_BACKEND appears in
    the environment. The env-presence version was inert, because .env.example
    ships the console backend uncommented and init_env copies it — so the
    variable is set in every generated deployment and the warning never fired.
    """

    def test_warns_on_the_console_backend_in_production(self, settings, caplog):
        settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING"):
                    _config()._warn_unconfigured_email()
        assert "never delivered" in caplog.text

    def test_warns_even_when_the_backend_is_set_explicitly(self, settings, caplog):
        """The regression that made the first version useless.

        Every deployment generated from .env.example has EMAIL_BACKEND set to the
        console backend, so "explicitly set" cannot mean "deliberately chosen".
        """
        settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
        with mock.patch.dict(
            os.environ,
            {"DJANGO_MODE": "production", "EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend"},
        ):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING"):
                    _config()._warn_unconfigured_email()
        assert "never delivered" in caplog.text

    def test_silenced_by_the_acknowledgement(self, settings, caplog):
        settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production", "EMAIL_DISABLED": "true"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING"):
                    _config()._warn_unconfigured_email()
        assert caplog.text == ""

    def test_silent_with_a_real_backend(self, settings, caplog):
        settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING"):
                    _config()._warn_unconfigured_email()
        assert caplog.text == ""

    def test_silent_outside_production(self, settings, caplog):
        settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
                with caplog.at_level("WARNING"):
                    _config()._warn_unconfigured_email()
        assert caplog.text == ""


class TestLocalOnlyBackupWarning:
    """The one failure mode on the pre-launch list that is unrecoverable rather
    than inconvenient, and the one nothing about a running deployment reveals —
    local-only backups work perfectly right up until the host is gone.
    """

    def _warn(self, env, argv=("manage.py", "runserver")):
        base = {"DJANGO_MODE": "production", "BORG_REMOTE_REPO": "", "BACKUP_LOCAL_ONLY": ""}
        with mock.patch.dict(os.environ, {**base, **env}):
            with mock.patch.object(sys, "argv", list(argv)):
                return _config()._warn_local_only_backups()

    def test_warns_in_production_without_a_remote_repo(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({})
        assert "BORG_REMOTE_REPO is empty" in caplog.text

    def test_silent_with_a_remote_repo(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"BORG_REMOTE_REPO": "borg@evidence.example.com:/srv/borg"})
        assert caplog.text == ""

    def test_whitespace_only_repo_is_not_a_remote(self, caplog):
        """A value of spaces is what a half-finished .env edit leaves behind, and
        it must not read as a configured destination."""
        with caplog.at_level("WARNING"):
            self._warn({"BORG_REMOTE_REPO": "   "})
        assert "BORG_REMOTE_REPO is empty" in caplog.text

    def test_silenced_by_the_acknowledgement(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"BACKUP_LOCAL_ONLY": "true"})
        assert caplog.text == ""

    def test_other_affirmative_spellings_are_accepted(self, caplog):
        for value in ("1", "yes", "on", "TRUE", "Yes"):
            caplog.clear()
            with caplog.at_level("WARNING"):
                self._warn({"BACKUP_LOCAL_ONLY": value})
            assert caplog.text == "", value

    def test_an_unparseable_acknowledgement_warns_and_does_not_raise(self, caplog):
        """These guards run inside AppConfig.ready(), so an exception here is a
        deployment that will not boot. Reading through decouple's bool cast did
        exactly that on any value outside its truth vocabulary, which meant
        BACKUP_LOCAL_ONLY=Y took the platform down instead of silencing a
        warning. Unrecognised spellings must degrade to "not acknowledged"."""
        for value in ("maybe", "Y", "sure", "0", "false"):
            caplog.clear()
            with caplog.at_level("WARNING"):
                self._warn({"BACKUP_LOCAL_ONLY": value})
            assert "BORG_REMOTE_REPO is empty" in caplog.text, value

    def test_silent_outside_production(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"DJANGO_MODE": "development"})
        assert caplog.text == ""

    def test_silent_during_bootstrap_commands(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({}, argv=("manage.py", "init_env"))
        assert caplog.text == ""

    def test_no_repository_at_all_warns_even_though_the_container_refuses(self, caplog):
        # With the local tier switchable, "no remote" stopped being the same
        # question as "only one copy". Both off means no archive is written
        # anywhere; the borg container refuses to start, but that message lands
        # inside a container the operator has no reason to open.
        with caplog.at_level("WARNING"):
            self._warn({"BACKUP_LOCAL_ENABLED": "false", "BORG_REMOTE_REPO": ""})
        assert "No backup repository is configured" in caplog.text

    def test_acknowledgement_does_not_silence_the_no_repository_case(self, caplog):
        # BACKUP_LOCAL_ONLY acknowledges keeping the only copy locally. It says
        # nothing about keeping no copy at all, so it must not suppress this.
        with caplog.at_level("WARNING"):
            self._warn({"BACKUP_LOCAL_ENABLED": "false", "BORG_REMOTE_REPO": "", "BACKUP_LOCAL_ONLY": "true"})
        assert "No backup repository is configured" in caplog.text

    def test_remote_only_is_not_warned_about(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"BACKUP_LOCAL_ENABLED": "false", "BORG_REMOTE_REPO": "ssh://borg@host/srv/borg/repo"})
        assert caplog.text == ""


class TestUnboundedWorkerWarning:
    """The workers are the only tier that can exhaust the host, and what the OOM
    killer takes when they do is as likely to be Postgres as the worker itself.
    """

    def _warn(self, env, argv=("manage.py", "runserver")):
        base = {"DJANGO_MODE": "production", "CELERY_MEM_LIMIT": ""}
        with mock.patch.dict(os.environ, {**base, **env}):
            with mock.patch.object(sys, "argv", list(argv)):
                return _config()._warn_unbounded_workers()

    def test_warns_in_production_without_a_limit(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({})
        assert "CELERY_MEM_LIMIT is unset" in caplog.text

    def test_silent_with_a_limit(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_MEM_LIMIT": "6g"})
        assert caplog.text == ""

    def test_zero_is_the_acknowledgement_the_warning_asks_for(self, caplog):
        """The warning tells the operator to set 0 to acknowledge running
        unbounded, so 0 has to actually silence it. An earlier version treated 0
        as unset, which made following the instruction produce the same warning
        on every boot — and the test written alongside it pinned that."""
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_MEM_LIMIT": "0"})
        assert caplog.text == ""

    def test_whitespace_only_still_warns(self, caplog):
        """A half-finished .env edit leaves spaces, which is not a decision."""
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_MEM_LIMIT": "   "})
        assert "CELERY_MEM_LIMIT is unset" in caplog.text

    def test_silent_outside_production(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"DJANGO_MODE": "development"})
        assert caplog.text == ""

    def test_silent_during_bootstrap_commands(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({}, argv=("manage.py", "init_env"))
        assert caplog.text == ""


class TestUnboundedWorkerPoolWarning:
    """``CELERY_MEM_LIMIT=0`` is the documented way to acknowledge running the
    workers unbounded, and ``CELERY_CONCURRENCY`` sits directly above it in
    .env.example — but 0 there means the opposite of a decision. Celery's CLI
    callback is ``value or conf.worker_concurrency`` and the worker then falls
    back to ``cpu_count()``, so the pool silently returns to being sized by the
    host. Read out of the installed celery while this was written.
    """

    def _warn(self, env, argv=("manage.py", "runserver")):
        base = {"DJANGO_MODE": "production", "CELERY_CONCURRENCY": ""}
        with mock.patch.dict(os.environ, {**base, **env}):
            with mock.patch.object(sys, "argv", list(argv)):
                return _config()._warn_unbounded_worker_pool()

    def test_zero_warns_because_celery_reads_it_as_unset(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_CONCURRENCY": "0"})
        assert "CELERY_CONCURRENCY=0 does not bound" in caplog.text

    def test_negative_warns_too(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_CONCURRENCY": "-1"})
        assert "does not bound" in caplog.text

    def test_silent_for_a_positive_pool_size(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_CONCURRENCY": "4"})
        assert caplog.text == ""

    def test_silent_when_unset_because_compose_supplies_the_default(self, caplog):
        """Absent is the normal case: the base compose file interpolates
        ``${CELERY_CONCURRENCY:-2}``, so nothing in .env means a pool of 2."""
        with caplog.at_level("WARNING"):
            self._warn({})
        assert caplog.text == ""

    def test_a_non_numeric_value_does_not_raise(self, caplog):
        """Celery's own ``type=int`` rejects it and the worker exits loudly, so
        there is nothing to add — but parsing it eagerly would raise from inside
        AppConfig.ready() and take the boot down over a warning."""
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_CONCURRENCY": "two"})
        assert caplog.text == ""

    def test_silent_outside_production(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_CONCURRENCY": "0", "DJANGO_MODE": "development"})
        assert caplog.text == ""

    def test_silent_during_bootstrap_commands(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn({"CELERY_CONCURRENCY": "0"}, argv=("manage.py", "init_env"))
        assert caplog.text == ""


class TestHealthcheckHostWarning:
    """The compose healthcheck requests /api/v1/ready over loopback, so dropping
    127.0.0.1 from ALLOWED_HOSTS turns every probe into a 400 DisallowedHost and
    the web container reports unhealthy while serving traffic normally.
    """

    def _warn(self, hosts, mode="production", argv=("manage.py", "runserver")):
        from django.test import override_settings

        with override_settings(ALLOWED_HOSTS=hosts):
            with mock.patch.dict(os.environ, {"DJANGO_MODE": mode}):
                with mock.patch.object(sys, "argv", list(argv)):
                    return _config()._warn_healthcheck_host()

    def test_warns_when_the_loopback_entry_was_replaced(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn(["eeg.example.com"])
        assert "127.0.0.1" in caplog.text

    def test_silent_when_the_loopback_entry_is_present(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn(["eeg.example.com", "127.0.0.1"])
        assert caplog.text == ""

    def test_silent_on_the_wildcard(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn(["*"])
        assert caplog.text == ""

    def test_silent_outside_production(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn(["eeg.example.com"], mode="development")
        assert caplog.text == ""

    def test_silent_during_bootstrap_commands(self, caplog):
        with caplog.at_level("WARNING"):
            self._warn(["eeg.example.com"], argv=("manage.py", "init_env"))
        assert caplog.text == ""
