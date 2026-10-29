"""Registry-wide authentication sweep over every mounted Ninja API operation.

The recurring defect shape in this codebase is a guarantee enforced on one route out of several.
Per-file contract tests pin known files; this sweep walks whatever the URL resolver actually
mounts — core apps and enabled plugins — and asserts each operation's view source shows one of
the accepted authentication shapes in :mod:`epicurrents.tests.auth_sweep`, with a reviewed
allowlist for the deliberately anonymous endpoints. A new route added anywhere without an auth
call fails here before a reviewer ever sees it.

A project's routes are left to the project's own sweep, which imports the same walker and keeps
its own allowlist. Platform code cannot name a project's helpers or say why one of its endpoints
is public, so a project's routes swept here could never pass.
"""

import pytest

from epicurrents.tests.auth_sweep import (
    delegates_that_do_not_authenticate,
    iter_operations,
    stale_allowlist_entries,
    unauthenticated,
    unchecked_share_token_reads,
)

# (module, view function name) → why this endpoint is deliberately anonymous.
ANONYMOUS_ALLOWLIST = {
    ("annotations.api.v1.ninja", "healthcheck"): "unauthenticated liveness probe",
    ("epicurrents.api.v1.ninja", "healthcheck"): "unauthenticated liveness probe",
    ("epicurrents.api.v1.ninja", "readiness"): "unauthenticated readiness probe for orchestration",
    ("maintenance.api.v1.ninja", "get_lock"): "maintenance flag probe the signed-out SPA polls for the release",
    ("notifications.api.v1.ninja", "vapid_public_key"): "the VAPID public key is public by definition",
    ("user.api.v1.ninja", "agreement_endpoint"): "the user agreement is read before an account accepts it, or exists",
    ("user.api.v1.ninja", "auth_config"): "pre-login discovery of enabled login providers",
    ("user.api.v1.ninja", "login_endpoint"): "login is how a session begins",
    ("user.api.v1.ninja", "login_two_factor_endpoint"): "second step of the login flow",
    ("user.api.v1.ninja", "logout_endpoint"): "logout of an absent session is a harmless no-op",
    ("user.api.v1.ninja", "me_endpoint"): "auth-state probe; reports logged-out in the body by design",
    ("user.api.v1.ninja", "oidc_start"): "entry point of the external login flow",
    ("user.api.v1.ninja", "oidc_callback"): "provider redirect target; validates the OIDC state itself",
    ("user.api.v1.ninja", "request_password_reset"): "password reset exists for callers who cannot log in",
    ("user.api.v1.ninja", "confirm_password_reset"): "second step of the reset flow, gated by the token",
}

# module → helper names that carry authentication for views that delegate to them.
# Each helper's own source is asserted to authenticate in test_delegates_authenticate.
AUTHENTICATING_DELEGATES = {
    "annotations.api.v1.ninja": ("_list_for_target",),
}


def _platform_operations():
    """Every mounted operation whose view is not a project's."""
    return [op for op in iter_operations() if not op[2].__module__.startswith("projects.")]


@pytest.mark.django_db
class TestApiAuthSweep:
    def test_operations_are_discovered(self):
        """The walker must find the real surface — an extraction regression sweeping zero routes must fail loudly."""
        operations = _platform_operations()
        assert len(operations) >= 100, (
            f"Only {len(operations)} Ninja operations discovered; the PathView extraction has "
            "probably broken (a ninja upgrade changing the wrapper closure?). A sweep over "
            "nothing passes vacuously, which is exactly the silent failure this test exists to prevent."
        )

    def test_every_operation_authenticates_or_is_allowlisted(self):
        violations = unauthenticated(_platform_operations(), ANONYMOUS_ALLOWLIST, AUTHENTICATING_DELEGATES)
        assert not violations, (
            "Operations with no recognised authentication shape and no allowlist entry:\n  "
            + "\n  ".join(violations)
            + "\nEither route the caller through the app's _require_* helper, or add a reviewed "
            "ANONYMOUS_ALLOWLIST entry with the reason the endpoint is public."
        )

    def test_share_token_operations_check_permissions(self):
        """A view relying on the share-token shape must route the read through the permission layer."""
        violations = unchecked_share_token_reads(_platform_operations(), ANONYMOUS_ALLOWLIST, AUTHENTICATING_DELEGATES)
        assert not violations, "Share-token operations that never consult the permission layer:\n  " + "\n  ".join(
            violations
        )

    def test_delegates_authenticate(self):
        """Every helper the sweep accepts as carrying auth must itself call a _require_* helper."""
        broken = delegates_that_do_not_authenticate(AUTHENTICATING_DELEGATES)
        assert not broken, f"Listed as authenticating delegates but no longer call a _require_* helper: {broken}"

    def test_allowlist_has_no_stale_entries(self):
        """Every allowlist entry must correspond to a mounted operation, so removals surface here."""
        stale = stale_allowlist_entries(_platform_operations(), ANONYMOUS_ALLOWLIST)
        assert not stale, f"ANONYMOUS_ALLOWLIST entries with no mounted operation: {stale}"
