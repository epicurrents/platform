"""Every route on every mounted Ninja API must be reachable.

Django resolves URL patterns in the order they were registered and Ninja emits
them in the order the decorators ran, so a path parameter registered earlier
swallows a literal segment registered later: ``{hash}`` compiles to Django's
``str`` converter, which matches any single segment — ``set-mains`` included.
The shadowed route is then not merely misrouted but *invisible*, and the symptom
points elsewhere: the request lands on the earlier path's view, which has no
handler for the method, so the caller gets 405. Resolution precedes
authentication, so even an unauthenticated call answers 405 rather than 401 —
which is how ``recordings``'s ``POST /set-mains`` presented when it was declared
below ``GET /{hash}``.

Ordering is the whole of the fix and nothing in the code makes it visible: a
route added at the bottom of a module, the natural place to add one, is exactly
the route at risk. Hence a test rather than a comment. It walks the repo's
``<app>/api/v1/ninja.py`` convention instead of naming the APIs, so an app added
later is covered on the day it lands.
"""

import importlib

from django.conf import settings
from ninja import NinjaAPI

BASE_DIR = settings.BASE_DIR


def _mounted_apis():
    """Return ``[(dotted_module, NinjaAPI), ...]`` for every app API in the repo."""
    apis = []
    for path in sorted(BASE_DIR.glob("*/api/v1/ninja.py")):
        dotted = f"{path.parts[-4]}.api.v1.ninja"
        api = getattr(importlib.import_module(dotted), "api", None)
        if isinstance(api, NinjaAPI):
            apis.append((dotted, api))
    return apis


def _registered_paths(api):
    """Return one path per registered route, in registration order.

    Reaches into ``NinjaAPI._routers`` / ``Router.path_operations`` — private, but
    it is where registration order lives, and a rename upstream fails this test
    loudly rather than quietly passing it. ``path_operations`` is keyed by path,
    so the several methods sharing ``/{hash}`` collapse to a single entry.
    """
    routers = getattr(api, "_routers", None)
    assert routers is not None, (
        "django-ninja no longer exposes NinjaAPI._routers; update this test to "
        "read registration order from wherever it moved."
    )
    return [prefix.rstrip("/") + path for prefix, router in routers for path in router.path_operations]


def _shadows(earlier: str, later: str) -> bool:
    """True when every URL matching *later* is matched by *earlier* first.

    Same segment count and the same trailing-slash form (``/x/`` and ``/x`` are
    different URLs), with each of *earlier*'s segments either a parameter — which
    matches any single segment — or the identical literal. A partial overlap is
    not shadowing: ``/status/{hash}`` and ``/{hash}/detail`` both match
    ``/status/detail``, but each stays reachable for other inputs.
    """
    if earlier.endswith("/") != later.endswith("/"):
        return False
    a = earlier.strip("/").split("/")
    b = later.strip("/").split("/")
    if len(a) != len(b):
        return False
    return all(seg.startswith("{") or seg == b[i] for i, seg in enumerate(a))


def test_no_route_is_shadowed_by_an_earlier_one():
    violations = []
    for dotted, api in _mounted_apis():
        paths = _registered_paths(api)
        for i, later in enumerate(paths):
            for earlier in paths[:i]:
                if _shadows(earlier, later):
                    violations.append(
                        f"{dotted}: {later!r} is unreachable — {earlier!r} is "
                        f"registered earlier and matches everything it matches. "
                        f"Move {later!r} above it."
                    )
    assert not violations, "\n".join(violations)


def test_finds_the_apis_it_claims_to_check():
    # A convention-walking test that silently matches nothing passes forever.
    # recordings is the one that has actually been bitten, so require at least it.
    dotted = [name for name, _ in _mounted_apis()]
    assert "recordings.api.v1.ninja" in dotted, dotted
    assert len(dotted) >= 5, dotted
