"""Well-known endpoint for federation identity publication.

Remote instances fetch ``/.well-known/epicurrents-federation.json`` to obtain
this instance's Ed25519 public key, which they use to verify inbound JWTs.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from federation.auth import is_federation_configured


@require_GET
def federation_well_known(request) -> JsonResponse:
    """Return this instance's federation identity document.

    Response JSON::

        {
            "federation_public_key": "<base64url-encoded Ed25519 public key>",
            "federation_public_key_next": "<base64url>"  // only when rotation announced
        }

    The ``federation_public_key_next`` field is included only when
    ``FEDERATION_PUBLIC_KEY_NEXT`` is set on this instance — that signals a
    rotation overlap window: peers should cache both keys so that when this
    instance promotes the "next" key to current, their cache still validates
    tokens signed with it.  See federation/README.md → "Key rotation".

    Returns 404 if federation is not configured on this instance.
    """
    if not is_federation_configured():
        return JsonResponse({"detail": "Federation is not enabled on this instance"}, status=404)

    from django.conf import settings

    public_key = getattr(settings, "FEDERATION_PUBLIC_KEY", "").strip()
    payload = {"federation_public_key": public_key}
    public_key_next = getattr(settings, "FEDERATION_PUBLIC_KEY_NEXT", "").strip()
    if public_key_next:
        payload["federation_public_key_next"] = public_key_next
    return JsonResponse(payload)
