"""Template context processors that expose deployment-posture state.

``debug_mode`` surfaces ``settings.DEBUG`` to every template under the
``debug_mode`` name, so a template can render a "DEV MODE" banner without
duplicating the settings lookup. Distinct from Django's built-in
``django.template.context_processors.debug``, which exposes the flag only
when the request IP is in ``INTERNAL_IPS`` — an IP filter that does not fit
a banner meant to be visible to whoever is looking at the page.

Its one consumer was the Django admin chrome, which the platform no longer
mounts; the SPA reads the same signal from ``/api/v1/health`` instead. Kept
rather than removed because it is three lines and any template added later
wants exactly this, but nothing renders it today.
"""

from django.conf import settings


def debug_mode(request):
    """Expose ``settings.DEBUG`` to templates under the key ``debug_mode``."""

    return {"debug_mode": bool(getattr(settings, "DEBUG", False))}
