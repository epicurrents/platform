"""Registry-wide authentication sweep over every mounted Ninja API operation.

The recurring defect shape in this codebase is a guarantee enforced on one route out of several.
Per-file contract tests pin known files; this sweep walks whatever the URL resolver actually
mounts — core apps, the active project, enabled plugins — and asserts each operation's view
source shows one of the accepted authentication shapes, with a reviewed allowlist for the
deliberately anonymous endpoints. A new route added anywhere without an auth call fails here
before a reviewer ever sees it.

Accepted shapes, matched against the view function's source:

- A ``_require_*`` helper call — the per-app convention (``_require_auth``, ``_require_staff``,
  ``_require_superuser``, ``_require_federation_auth``, ``_require_access_manager``, …), every
  variant of which resolves the caller before the body runs.
- The share-token pattern — the view handles ``share_token``, raises 401 when neither a session
  nor a token is presented, and routes the object read through the permission layer (asserted
  separately in ``test_share_token_operations_check_permissions``).
- Delegation to a helper listed in ``AUTHENTICATING_DELEGATES``, whose own source is asserted to
  authenticate.
- An entry in ``ANONYMOUS_ALLOWLIST`` naming the reason the endpoint is public. Adding an entry
  is a review decision, not a formality — the reason column is the contract.

The sweep sees only what the active settings mount. Project CI jobs run their own settings, so a
project's routes are swept in the run where they exist.
"""

import inspect

import pytest
from django.urls import get_resolver
from ninja.operation import PathView

# (module, view function name) → why this endpoint is deliberately anonymous.
ANONYMOUS_ALLOWLIST = {
    ("annotations.api.v1.ninja", "healthcheck"): "unauthenticated liveness probe",
    ("epicurrents.api.v1.ninja", "healthcheck"): "unauthenticated liveness probe",
    ("epicurrents.api.v1.ninja", "readiness"): "unauthenticated readiness probe for orchestration",
    ("notifications.api.v1.ninja", "vapid_public_key"): "the VAPID public key is public by definition",
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

_PERMISSION_MARKERS = (
    "ensure_",
    "can_read_object",
    "can_write_object",
    "can_modify_object",
    "can_annotate_object",
    "get_read_access_result",
)


def _iter_operations():
    """Yield (url path, methods, view function) for every mounted Ninja operation, deduplicated.

    Ninja's URL callbacks are wrappers closing over a ``PathView``; the operation objects hang
    off it. Walking the resolver rather than importing known ``api`` objects is the point — a
    new mount is swept without anyone registering it here.
    """

    def walk(patterns, prefix, out):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern.url_patterns, prefix + str(pattern.pattern), out)
            else:
                out.append((prefix + str(pattern.pattern), pattern.callback))

    leaves: list = []
    walk(get_resolver().url_patterns, "", leaves)

    seen: set[int] = set()
    for path, callback in leaves:
        for cell in getattr(callback, "__closure__", None) or []:
            candidate = cell.cell_contents
            if isinstance(candidate, PathView):
                for operation in candidate.operations:
                    if id(operation) in seen:
                        continue
                    seen.add(id(operation))
                    yield path, sorted(operation.methods), operation.view_func


def _classify(view_func) -> str:
    """Return the auth shape the view's source shows: require / token / delegate / none."""
    source = inspect.getsource(view_func)
    if "_require_" in source:
        return "require"
    if "share_token" in source and "401" in source:
        return "token"
    for delegate in AUTHENTICATING_DELEGATES.get(view_func.__module__, ()):
        if delegate in source:
            return "delegate"
    return "none"


@pytest.mark.django_db
class TestApiAuthSweep:
    def test_operations_are_discovered(self):
        """The walker must find the real surface — an extraction regression sweeping zero routes must fail loudly."""
        operations = list(_iter_operations())
        assert len(operations) >= 100, (
            f"Only {len(operations)} Ninja operations discovered; the PathView extraction has "
            "probably broken (a ninja upgrade changing the wrapper closure?). A sweep over "
            "nothing passes vacuously, which is exactly the silent failure this test exists to prevent."
        )

    def test_every_operation_authenticates_or_is_allowlisted(self):
        violations = []
        for path, methods, view_func in _iter_operations():
            key = (view_func.__module__, view_func.__name__)
            if key in ANONYMOUS_ALLOWLIST:
                continue
            if _classify(view_func) == "none":
                violations.append(f"{'/'.join(methods)} {path} -> {key[0]}.{key[1]}")
        assert not violations, (
            "Operations with no recognised authentication shape and no allowlist entry:\n  "
            + "\n  ".join(violations)
            + "\nEither route the caller through the app's _require_* helper, or add a reviewed "
            "ANONYMOUS_ALLOWLIST entry with the reason the endpoint is public."
        )

    def test_share_token_operations_check_permissions(self):
        """A view relying on the share-token shape must route the read through the permission layer."""
        violations = []
        for path, methods, view_func in _iter_operations():
            if (view_func.__module__, view_func.__name__) in ANONYMOUS_ALLOWLIST:
                continue
            if _classify(view_func) != "token":
                continue
            source = inspect.getsource(view_func)
            if not any(marker in source for marker in _PERMISSION_MARKERS):
                violations.append(f"{'/'.join(methods)} {path} -> {view_func.__module__}.{view_func.__name__}")
        assert not violations, "Share-token operations that never consult the permission layer:\n  " + "\n  ".join(
            violations
        )

    def test_delegates_authenticate(self):
        """Every helper the sweep accepts as carrying auth must itself call a _require_* helper."""
        import importlib

        for module_name, helpers in AUTHENTICATING_DELEGATES.items():
            module = importlib.import_module(module_name)
            for helper_name in helpers:
                helper = getattr(module, helper_name)
                assert "_require_" in inspect.getsource(helper), (
                    f"{module_name}.{helper_name} is listed as an authenticating delegate but its "
                    "source no longer calls a _require_* helper"
                )

    def test_allowlist_has_no_stale_entries(self):
        """Every allowlist entry must correspond to a mounted operation, so removals surface here."""
        live = {(vf.__module__, vf.__name__) for _, _, vf in _iter_operations()}
        stale = [key for key in ANONYMOUS_ALLOWLIST if key not in live]
        assert not stale, f"ANONYMOUS_ALLOWLIST entries with no mounted operation: {stale}"
