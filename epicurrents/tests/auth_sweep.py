"""Route walker and classifier behind the API authentication sweep, shared by the platform and its projects.

The platform's sweep in :mod:`epicurrents.tests.test_api_auth_sweep` covers every route it can name the helpers of;
a project's routes are swept by the project's own test, which imports these functions and keeps its own allowlist,
because the reasons an endpoint of the project is public, and the helpers that authenticate for it, are the
project's to state. Not a test module, so pytest collects nothing from it.

Accepted shapes, matched against the view function's source:

- A ``_require_*`` helper call — the per-app convention (``_require_auth``, ``_require_staff``,
  ``_require_superuser``, ``_require_federation_auth``, ``_require_access_manager``, …), every
  variant of which resolves the caller before the body runs.
- The share-token pattern — the view handles ``share_token``, raises 401 when neither a session
  nor a token is presented, and routes the object read through the permission layer.
- Delegation to a helper named in the caller's delegate table, whose own source is asserted to
  authenticate.
- An allowlist entry naming the reason the endpoint is public. Adding one is a review decision,
  not a formality — the reason is the contract.
"""

import importlib
import inspect
from collections.abc import Iterator

from django.urls import get_resolver
from ninja.operation import PathView

PERMISSION_MARKERS = (
    "ensure_",
    "can_read_object",
    "can_write_object",
    "can_modify_object",
    "can_annotate_object",
    "get_read_access_result",
)


def iter_operations() -> Iterator[tuple[str, list[str], object]]:
    """Yield (url path, methods, view function) for every mounted Ninja operation, deduplicated.

    Ninja's URL callbacks are wrappers closing over a ``PathView``; the operation objects hang
    off it. Walking the resolver rather than importing known ``api`` objects is the point — a
    new mount is swept without anyone registering it.
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


def classify(view_func, delegates: dict[str, tuple[str, ...]]) -> str:
    """Return the auth shape the view's source shows: require / token / delegate / none."""
    source = inspect.getsource(view_func)
    if "_require_" in source:
        return "require"
    if "share_token" in source and "401" in source:
        return "token"
    for delegate in delegates.get(view_func.__module__, ()):
        if delegate in source:
            return "delegate"
    return "none"


def unauthenticated(operations, allowlist: dict, delegates: dict[str, tuple[str, ...]]) -> list[str]:
    """Operations showing no authentication shape and no allowlist entry, as readable lines."""
    violations = []
    for path, methods, view_func in operations:
        key = (view_func.__module__, view_func.__name__)
        if key in allowlist:
            continue
        if classify(view_func, delegates) == "none":
            violations.append(f"{'/'.join(methods)} {path} -> {key[0]}.{key[1]}")
    return violations


def unchecked_share_token_reads(operations, allowlist: dict, delegates: dict[str, tuple[str, ...]]) -> list[str]:
    """Share-token operations whose source never consults the permission layer."""
    violations = []
    for path, methods, view_func in operations:
        if (view_func.__module__, view_func.__name__) in allowlist:
            continue
        if classify(view_func, delegates) != "token":
            continue
        if not any(marker in inspect.getsource(view_func) for marker in PERMISSION_MARKERS):
            violations.append(f"{'/'.join(methods)} {path} -> {view_func.__module__}.{view_func.__name__}")
    return violations


def delegates_that_do_not_authenticate(delegates: dict[str, tuple[str, ...]]) -> list[str]:
    """Delegate helpers whose own source no longer calls a ``_require_*`` helper."""
    broken = []
    for module_name, helpers in delegates.items():
        module = importlib.import_module(module_name)
        for helper_name in helpers:
            if "_require_" not in inspect.getsource(getattr(module, helper_name)):
                broken.append(f"{module_name}.{helper_name}")
    return broken


def stale_allowlist_entries(operations, allowlist: dict) -> list[tuple[str, str]]:
    """Allowlist entries with no mounted operation, so a removal surfaces."""
    live = {(view_func.__module__, view_func.__name__) for _, _, view_func in operations}
    return [key for key in allowlist if key not in live]
