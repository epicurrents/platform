"""End-to-end check that ``ApiActivityLoggingMiddleware`` creates an
``Activity`` row on a real HTTP request to every API mount.

This is the integration backstop to the path-recognition contract test
in ``test_middleware_path_recognition.py``: the contract test verifies
the regex matches the URL config, this test verifies the rest of the
chain (middleware runs, Activity insert succeeds, request context is
threaded into signals) still works end-to-end.

The chosen endpoints are GET requests that return without requiring a
valid payload. Authentication is intentionally **not** provided — the
middleware runs before view auth checks, so a 401 / 403 response is
fine and even desirable: it confirms the audit trail records the
*attempt*, not just successful calls.
"""

import pytest

from activity.models import Activity

# One endpoint per API mount prefix. Adding a new top-level API mount
# means adding an entry here so the integration coverage is enforced.
#
# Each probe MUST point at an endpoint that is not on
# ``ACTIVITY_PATH_SKIP_LIST`` — the probe is asserting "API requests
# produce an Activity row", and skip-list paths intentionally do not.
# Skip-list coverage is tested separately below.
API_MOUNT_PROBES = [
    ("/api/v1/user/me", "user"),
    ("/api/v1/activity/changes/", "activity"),
    ("/api/v1/notifications/subscribe", "notifications"),
    ("/api/v1/library/collections/", "library"),
    ("/api/v1/federation/peers/", "federation"),
    ("/annotations/api/v1/content-types", "annotations"),
    ("/compute/api/v1/eeg/leadfield/", "compute"),
    ("/recordings/api/v1/", "recordings"),
]


@pytest.mark.django_db
@pytest.mark.parametrize("path,app_label", API_MOUNT_PROBES)
def test_api_request_creates_activity_row(client, path, app_label):
    before = Activity.objects.count()
    client.get(path)
    after = Activity.objects.count()
    assert after == before + 1, (
        f"GET {path} (app: {app_label}) did not produce an Activity row. "
        f"This means ApiActivityLoggingMiddleware did not classify the "
        f"path as an API request — the audit trail is silently empty for "
        f"this app's surface."
    )


@pytest.mark.django_db
def test_non_api_request_does_not_create_activity_row(client):
    """Negative control: hitting a non-API URL must NOT create an
    Activity row. Without this assertion the audit table would slowly
    fill with rows for static-file requests and SPA loads.

    An SPA deep link rather than the ``/admin/login/`` this used to request:
    the admin is no longer mounted, so that path is now just an arbitrary
    string, and a negative control is worth more when it names traffic the
    deployment actually serves."""
    before = Activity.objects.count()
    client.get("/library/collections/3")
    after = Activity.objects.count()
    assert after == before, (
        "GET /library/collections/3 created an Activity row — the path matcher "
        "now classifies a non-API URL as API and is over-logging."
    )


@pytest.mark.django_db
def test_api_request_records_method_and_path(client):
    """The Activity row carries ``method`` and ``path`` so an external
    SIEM rule can group by them. Confirm both are populated."""
    client.get("/api/v1/activity/changes/")
    activity = Activity.objects.latest("created_at")
    assert activity.method == "GET"
    assert activity.path == "/api/v1/activity/changes/"


@pytest.mark.django_db
def test_skip_list_path_does_not_create_activity_row(client, settings):
    """Operational endpoints listed in ``ACTIVITY_PATH_SKIP_LIST`` must
    not produce an Activity row.

    Companion test to the ``API_MOUNT_PROBES`` assertions above — those
    probes intentionally avoid skip-list paths so they can assert "API
    requests produce a row". This test asserts the inverse on the
    skip-list itself so the policy is enforced from both sides.
    """
    for path in settings.ACTIVITY_PATH_SKIP_LIST:
        before = Activity.objects.count()
        client.get(path)
        after = Activity.objects.count()
        assert after == before, (
            f"GET {path} is on ACTIVITY_PATH_SKIP_LIST but the middleware "
            f"created an Activity row anyway (count went {before} -> {after})."
        )


@pytest.mark.django_db
def test_middleware_does_not_clobber_explicit_target_object_id(rf):
    """Endpoints that set ``activity.target_object_id`` themselves must keep
    the value through the middleware's exit code.

    Before this fix, the middleware always wrote ``target_object_id =
    object_id`` where ``object_id`` was extracted from URL kwargs and
    defaulted to ``None``. An endpoint that resolved a target row from
    the request body or query string — every POST-without-URL-kwarg
    write endpoint, including ``recordings.upload`` and notifications'
    ``subscribe`` — would have its explicit assignment silently reset
    to ``None`` on the way out.
    """
    from activity.request_context import get_current_activity
    from epicurrents.middleware import ApiActivityLoggingMiddleware

    def view(request):
        from django.http import HttpResponse

        activity = get_current_activity()
        assert activity is not None, "test setup: middleware did not create an Activity row"
        # Simulate an endpoint that knows its own target.
        activity.target_object_id = "42"
        activity.save(update_fields=["target_object_id"])
        return HttpResponse(status=200)

    middleware = ApiActivityLoggingMiddleware(view)
    # /api/v1/activity/changes/ does not include a pk / id / *_id kwarg,
    # so the middleware has nothing to fill in on exit — the endpoint's
    # explicit "42" must survive.
    request = rf.get("/api/v1/activity/changes/")
    middleware(request)

    activity = Activity.objects.latest("created_at")
    assert activity.target_object_id == "42", (
        "Middleware clobbered an explicit target_object_id set by the view. "
        f"Expected '42', got {activity.target_object_id!r}."
    )


@pytest.mark.django_db
def test_middleware_still_extracts_target_object_id_from_url_kwargs(client):
    """The URL-kwargs extraction must keep working for endpoints whose
    target lives in the path — the fix is only about not clobbering
    explicit values, not removing the kwarg extraction entirely."""
    # ``/api/v1/activity/rollback/{change_id}`` has a ``change_id`` URL
    # kwarg. An unauthenticated request 401s without touching the
    # change-log machinery, but the middleware still extracts the kwarg
    # on the way out and writes it to the Activity row.
    client.post("/api/v1/activity/rollback/999")

    activity = Activity.objects.filter(path="/api/v1/activity/rollback/999").latest("created_at")
    assert activity.target_object_id == "999", (
        "Middleware no longer extracts target_object_id from URL kwargs. "
        f"Expected '999', got {activity.target_object_id!r}."
    )
