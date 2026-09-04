"""Contract test for the interactive-API-docs gating.

Every Ninja mount takes ``docs_url`` from ``settings.API_DOCS_URL``, which
is non-None only when DEBUG is on. The schema endpoint discloses the full
endpoint surface to unauthenticated callers, so a production deployment
must not serve it. Test settings run with DEBUG off, which makes this an
end-to-end check of the production posture.
"""

import pytest
from django.conf import settings


def test_api_docs_url_is_disabled_without_debug():
    assert settings.DEBUG is False
    assert settings.API_DOCS_URL is None


def _body(response) -> bytes:
    if response.streaming:
        return b"".join(response.streaming_content)
    return response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mount",
    [
        "/api/v1/user",
        "/api/v1/activity",
        "/api/v1/library",
        "/api/v1/federation",
        "/api/v1/notifications",
        "/recordings/api/v1",
        "/media/api/v1",
        "/annotations/api/v1",
        "/compute/api/v1",
    ],
)
def test_docs_endpoints_are_not_served(client, mount):
    # A 200 is what serving the schema would look like, so the status carries
    # half the property and the body carries the other half. Asserting the exact
    # status would over-specify: most of these mounts answer 404 now that an
    # unmounted API path no longer falls through to the SPA, but `recordings` and
    # `media` mount `<hash>` routes directly under their prefix, so `/docs` is a
    # well-formed request for a recording named "docs" and their own endpoint
    # answers it. Either way the schema is not served, which is the claim.
    docs = client.get(f"{mount}/docs")
    assert docs.status_code != 200
    assert b"swagger" not in _body(docs).lower()
    schema = client.get(f"{mount}/openapi.json")
    assert schema.status_code != 200
    assert b'"openapi"' not in _body(schema)
