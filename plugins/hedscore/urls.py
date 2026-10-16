"""API endpoints for the *hedscore* plugin, mounted at ``/plugin/hedscore/api/v1/``.

Endpoints
---------
GET  /policy    The pinned schema release and the branches this deployment accepts.

The picker's terms are not served here. The viewer generates its index from the same pinned release at build time,
and the host application hands this policy to it so the picker offers only what the validator accepts
(``docs/engineering-notes/hed-score-integration.md`` §4).
"""

from django.conf import settings
from django.urls import path
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from plugins.hedscore.schema import SCHEMA_PIN, load_schema

api = NinjaAPI(
    title="HED-SCORE Plugin API",
    version="1",
    urls_namespace="hedscore-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)


def _require_auth(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


class PolicyOut(Schema):
    library: str
    version: str
    with_standard: str
    sha256: str
    allowed_branches: list[str]


@api.get("/policy", auth=None, response=PolicyOut)
def get_policy(request):
    """The schema release the validator pins and the branches it accepts, all of them when none are configured.

    Configuration rather than anyone's data, so every authenticated caller may read it. The digest lets the viewer
    confirm its index was generated from the same file.
    """
    _require_auth(request)
    configured = list(getattr(settings, "HEDSCORE_ALLOWED_BRANCHES", ()))
    log_activity(verb="hedscore.policy.read", metadata={"restricted": bool(configured)})
    return PolicyOut(
        library=SCHEMA_PIN.library,
        version=SCHEMA_PIN.version,
        with_standard=SCHEMA_PIN.with_standard,
        sha256=SCHEMA_PIN.sha256,
        allowed_branches=configured or list(load_schema().branches),
    )


urlpatterns = [path("", api.urls)]
