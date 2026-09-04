"""Django app configuration — runs ``assert_local_keys_consistent`` at startup."""

from django.apps import AppConfig


class FederationConfig(AppConfig):
    """Django app configuration for inter-instance federated data sharing."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "federation"

    def ready(self):
        # Fail fast on a half-completed key rotation (env file edited but
        # service not restarted, or vice versa).  No-op when federation is
        # not configured, so non-federated deployments are unaffected.
        from federation.auth import assert_local_keys_consistent

        assert_local_keys_consistent()
