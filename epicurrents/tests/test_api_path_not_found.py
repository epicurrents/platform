"""Contract test: an API-shaped path that matched no route answers 404.

The SPA catch-all serves index.html for anything unmatched, because a client-side
route is indistinguishable from a typo at the server. That default is wrong for
API paths and expensively so: a mistyped write answered 200 with an HTML body,
and every caller checks the status code, so the client recorded a success that
never happened. It had bitten this project more than once — most recently a
revoke route whose trailing slash did not match the library convention, where the
grant survived a "successful" DELETE.

Two properties, and the second is the one that rots:

- An API-shaped path with no route behind it is a 404.
- Everything that is *not* an API path still reaches the SPA, so client-side deep
  links keep working. The fix is only safe while it stays this narrow.

Ordering is the third. ``api_not_found`` is appended below every real mount, and
project and plugin routes are appended too — an earlier version of the URLconf
inserted those at a fixed offset from the end, so adding a fallback would have
silently pushed them behind it and 404'd every project API route.
"""

import pytest
from django.urls import resolve

from epicurrents.views import api_not_found, frontend_view

# One per mount style: root-level, app-prefixed, and the two-segment project /
# plugin shapes. The paths are deliberately deep nonsense rather than a single
# trailing segment: `recordings` and `media` both mount `<hash>` routes directly
# under their prefix, so `/media/api/v1/anything` is a well-formed request for a
# media file and resolves to that endpoint, which then answers on its own terms.
# This view only ever sees paths that match nothing at all.
API_SHAPED = [
    "/api/v1/no-such-route",
    "/api/v1/user/no-such-route",
    "/api/v1/library/collections/1/no-such-route",
    "/recordings/api/v1/definitely/not/a/route",
    "/media/api/v1/definitely/not/a/route",
    "/annotations/api/v1/no-such-route",
    "/compute/api/v1/no-such-route",
    "/project/api/v1/no-such-route",
    "/plugin/dicom/api/v1/no-such-route",
    "/api/v2/from-a-future-version",
    # A mount prefix with its trailing slash dropped. APPEND_SLASH never rescued
    # this: the SPA catch-all resolved it, so Django saw a match and had no
    # reason to try the slashed form.
    "/api/v1",
    "/recordings/api/v1",
]

# Paths the SPA owns. Some are real client-side routes, some are not — the point
# is that the server cannot tell, so all of them must reach index.html.
SPA_SHAPED = [
    "/",
    "/library",
    "/library/collections/3",
    "/datasets/7",
    "/settings/viewer",
    "/annotations/export",
    "/some/route/added/next/year",
]


@pytest.mark.parametrize("path", API_SHAPED)
def test_api_shaped_paths_resolve_to_the_404_view(path):
    assert resolve(path).func is api_not_found


@pytest.mark.parametrize("path", SPA_SHAPED)
def test_spa_paths_still_reach_the_frontend(path):
    assert resolve(path).func is frontend_view


@pytest.mark.django_db
@pytest.mark.parametrize("path", API_SHAPED)
def test_the_response_is_a_json_404(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["Content-Type"] == "application/json"
    assert response.json() == {"detail": "Not found"}


@pytest.mark.django_db
def test_every_method_gets_the_404_not_just_get(client):
    """The hazard was a silent write, so the unsafe methods are the ones that
    matter. A 405 would be no better than a 200 here — the caller has to be able
    to tell that nothing happened."""
    path = "/recordings/api/v1/definitely/not/a/route"
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(path)
        assert response.status_code == 404, method


@pytest.mark.django_db
def test_a_real_route_is_unaffected(client):
    """The 404 view sits below every real mount, so a mounted route still wins.
    /api/v1/health needs no authentication, which keeps this a routing check."""
    assert client.get("/api/v1/health").status_code == 200


@pytest.mark.django_db
def test_an_authenticated_route_still_answers_401_rather_than_404(client):
    """Resolution precedes authentication. If the 404 view were mounted above the
    real routes this would answer 404, and the difference between "no such
    endpoint" and "you are not signed in" would be gone."""
    assert client.get("/api/v1/user/admin/accounts").status_code == 401


def test_no_pattern_is_registered_after_the_spa_catch_all():
    """The catch-all matches everything, so anything below it is unreachable.
    Guards the append-order the URLconf comment asks for."""
    from epicurrents.urls import urlpatterns

    last = urlpatterns[-1]
    assert last.pattern.regex.pattern == r"^(?P<path>.+)$"
    assert last.callback is frontend_view
