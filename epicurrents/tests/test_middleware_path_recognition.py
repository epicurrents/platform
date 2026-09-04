"""Contract tests for ``ApiActivityLoggingMiddleware`` path recognition.

⚠️ LOAD-BEARING — this file is the contract test for the audit-trail
coverage decision in ``epicurrents/middleware.py``. If a change to the
URL config or the middleware regex makes any of these fail, do NOT
"fix" the test to match — that is the exact failure mode that left
``/recordings/api/v1/...``, ``/annotations/api/v1/...``,
``/compute/api/v1/...``, and ``/project/api/v1/...`` unaudited between
the 2026-05-25 activity audit and the audit-coverage fix. Make the
middleware regex match the new mount, or rename the mount.

The two halves:

1. Regex unit checks — known paths that must and must not match
   ``_API_PATH_RE``. Cheap; runs without the DB.
2. URL-config enumeration — walks ``epicurrents.urls.urlpatterns``
   recursively, finds every leaf path string that contains an
   ``api/v<N>/`` segment, and asserts ``_API_PATH_RE`` matches. This
   is the structural backstop: a new API mount added anywhere in the
   project that the regex doesn't recognise fails CI immediately.
"""

import re

import pytest
from django.urls import URLPattern, URLResolver, get_resolver

from epicurrents.middleware import _API_PATH_RE

# ---------------------------------------------------------------------------
# Direct regex checks
# ---------------------------------------------------------------------------

KNOWN_API_PATHS = [
    "/api/v1/health",
    "/api/v1/user/me",
    "/api/v1/activity/changes/",
    "/api/v1/notifications/vapid-public-key",
    "/api/v1/library/collections/",
    "/api/v1/federation/peers/",
    "/annotations/api/v1/events/",
    "/compute/api/v1/eeg/leadfield/",
    "/recordings/api/v1/list",
    "/project/api/v1/sessions",
    "/plugin/dicom/api/v1/dicom/studies/",  # plugin mount: literal plugin/ + name
    "/plugin/someplugin/api/v2/x",  # any plugin name, forward-compat version
    "/api/v2/anything",  # forward-compat: vN with N != 1 is still API
]

KNOWN_NON_API_PATHS = [
    "/",
    "/admin/",
    "/admin/auth/user/",
    "/static/admin/css/base.css",
    "/viewer/index.html",
    "/.well-known/epicurrents-federation.json",
    "/some-spa-route",
    "/api",  # bare "/api" without a version segment is not an API path
    "/foo/bar/api/v1/baz",  # two arbitrary segments — only literal plugin/ earns two
    "/plugin/a/b/api/v1/x",  # plugin prefix allows exactly one name segment
    "/api/foo/v1/baz",  # version not in the v<N> slot
]


# Paths that are at the *root* of an API namespace but have no trailing slash
# / endpoint segment. These will 404 at view dispatch but are still part of
# the API surface and should be logged so the attempt shows up in the audit
# trail. Listing them explicitly so the intent is documented.
KNOWN_API_ROOT_PATHS = [
    "/api/v1",
    "/recordings/api/v1",
    "/annotations/api/v1",
    "/plugin/dicom/api/v1",
]


@pytest.mark.parametrize("path", KNOWN_API_PATHS)
def test_known_api_path_matches(path):
    assert _API_PATH_RE.match(path) is not None, (
        f"{path!r} should be recognised as an API request by the audit-trail middleware but the regex did not match."
    )


@pytest.mark.parametrize("path", KNOWN_NON_API_PATHS)
def test_known_non_api_path_does_not_match(path):
    assert _API_PATH_RE.match(path) is None, (
        f"{path!r} should NOT be recognised as an API request — matching it "
        f"would silently widen audit-trail coverage to a non-API surface."
    )


@pytest.mark.parametrize("path", KNOWN_API_ROOT_PATHS)
def test_api_root_without_trailing_slash_still_matches(path):
    """Hitting ``/api/v1`` (no trailing slash) is unrouted but is still
    an API surface request — logging it surfaces the malformed attempt
    in the audit trail rather than dropping it silently."""
    assert _API_PATH_RE.match(path) is not None


# ---------------------------------------------------------------------------
# URL-config enumeration backstop
# ---------------------------------------------------------------------------

_API_SEGMENT_RE = re.compile(r"(^|/)api/v\d+(/|$)")


def _walk_patterns(patterns, prefix=""):
    """Yield (full_path_string, callback) tuples for every URL leaf."""
    for entry in patterns:
        # ``str(entry.pattern)`` returns a textual representation of the
        # pattern (e.g. ``"api/v1/"`` for ``path("api/v1/", ...)``). This
        # is what we need to reconstruct the mounted path; Django does not
        # expose a public "regenerate the mount string" API, so we work
        # off the pattern's __str__ deliberately.
        segment = str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _walk_patterns(entry.url_patterns, prefix + segment)
        elif isinstance(entry, URLPattern):
            yield prefix + segment, entry.callback


def test_every_mounted_api_path_matches_middleware_regex():
    """The audit-trail contract: every URL path that names an api/v<N>/
    segment in the actual urlpatterns tree must be recognised by
    ``_API_PATH_RE``.

    This is the test that would have caught the 2026-05-25 regression.
    """
    resolver = get_resolver()
    mismatches = []
    matched_count = 0
    for full_path, _callback in _walk_patterns(resolver.url_patterns):
        # Normalise to a leading slash so it looks like a request path.
        candidate = "/" + full_path.lstrip("/")
        if not _API_SEGMENT_RE.search(candidate):
            continue
        matched_count += 1
        if _API_PATH_RE.match(candidate) is None:
            mismatches.append(candidate)

    assert matched_count > 0, (
        "No API mounts found in urlpatterns — the enumeration is broken; this test cannot do its job."
    )
    assert not mismatches, (
        "URL patterns name api/v<N>/ paths that the middleware regex does "
        "NOT recognise. The audit trail will silently skip these surfaces:\n"
        + "\n".join(f"  - {p}" for p in sorted(mismatches))
    )


# ---------------------------------------------------------------------------
# ACTIVITY_PATH_SKIP_LIST — operational-endpoint exemptions
# ---------------------------------------------------------------------------
#
# Some paths match _API_PATH_RE but are deliberately exempt from creating
# Activity rows: operational endpoints whose volume would drown the
# data-interaction signal. The skip list lives in
# ACTIVITY_PATH_SKIP_LIST and is consulted by ApiActivityLoggingMiddleware
# before the row is created. Adding to the list narrows the audit
# surface — per AGENTS.md's "enumerate prior matches" rule, every entry
# below names the prior caller that used to produce an Activity row and
# now no longer does.


@pytest.mark.django_db
def test_skip_list_default_entries_do_not_produce_activity_row(rf, settings):
    """The three default skip entries must not create an Activity row.

    Each entry is one of the operational endpoints documented in
    .review/exemptions/audit-trail-completeness.md — health checks and
    the public VAPID key. None of them touch user data; the per-request
    Activity row would be pure noise.
    """
    from activity.models import Activity
    from epicurrents.middleware import ApiActivityLoggingMiddleware

    def view(request):
        from django.http import HttpResponse

        return HttpResponse(status=200)

    middleware = ApiActivityLoggingMiddleware(view)

    for path in settings.ACTIVITY_PATH_SKIP_LIST:
        before = Activity.objects.count()
        request = rf.get(path)
        middleware(request)
        after = Activity.objects.count()
        assert after == before, (
            f"{path!r} is in ACTIVITY_PATH_SKIP_LIST but produced an "
            f"Activity row anyway (count went {before} -> {after})."
        )


@pytest.mark.django_db
def test_api_path_not_on_skip_list_still_produces_activity_row(rf, settings):
    """The skip list is exact-match — neighbouring API paths still log."""
    from activity.models import Activity
    from epicurrents.middleware import ApiActivityLoggingMiddleware

    def view(request):
        from django.http import HttpResponse

        return HttpResponse(status=200)

    middleware = ApiActivityLoggingMiddleware(view)

    # Adjacent paths that would be caught by a careless prefix match but must
    # NOT be skipped by an exact-match skip list.
    adjacent_paths = [
        "/api/v1/healthz",  # near-miss suffix
        "/api/v1/health/sub",  # sub-path under exempted root
        "/annotations/api/v1/healthcheck",  # near-miss suffix
        "/api/v1/notifications/vapid-public-key-rotate",  # near-miss suffix
    ]
    for path in adjacent_paths:
        before = Activity.objects.count()
        request = rf.get(path)
        middleware(request)
        after = Activity.objects.count()
        assert after == before + 1, (
            f"{path!r} is not on ACTIVITY_PATH_SKIP_LIST but the middleware "
            f"did not produce an Activity row (count went {before} -> {after})."
        )


@pytest.mark.django_db
def test_skip_list_can_be_extended_at_runtime(rf, settings):
    """Operators can extend the skip list via settings override."""
    from activity.models import Activity
    from epicurrents.middleware import ApiActivityLoggingMiddleware

    def view(request):
        from django.http import HttpResponse

        return HttpResponse(status=200)

    extra_skip = "/api/v1/some-operational-endpoint"
    settings.ACTIVITY_PATH_SKIP_LIST = (
        *settings.ACTIVITY_PATH_SKIP_LIST,
        extra_skip,
    )
    middleware = ApiActivityLoggingMiddleware(view)

    before = Activity.objects.count()
    middleware(rf.get(extra_skip))
    assert Activity.objects.count() == before, (
        f"{extra_skip!r} was added to ACTIVITY_PATH_SKIP_LIST at runtime "
        f"but the middleware still produced an Activity row."
    )


@pytest.mark.django_db
def test_skip_list_paths_match_the_url_config():
    """Every default skip-list entry must correspond to a real mounted URL.

    A skip entry that doesn't point at an actual endpoint is dead weight
    at best, a forward-compat trap at worst (the operator may assume
    coverage exists for a path that's actually 404). This test walks
    the URL config and confirms each ``ACTIVITY_PATH_SKIP_LIST`` entry
    resolves to a registered Ninja endpoint.
    """
    from django.conf import settings
    from django.urls import resolve

    for path in settings.ACTIVITY_PATH_SKIP_LIST:
        try:
            resolve(path)
        except Exception as exc:  # pragma: no cover — explicit failure
            raise AssertionError(
                f"ACTIVITY_PATH_SKIP_LIST entry {path!r} does not resolve to "
                f"any URL config — remove the entry or fix the path: {exc}"
            )
