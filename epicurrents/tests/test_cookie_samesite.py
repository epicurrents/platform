"""Contract test for the declared SameSite cookie posture.

The Ninja API mounts run with CSRF tokens disabled, so cross-site request
protection on state-changing endpoints rests on the SameSite attribute
keeping the session cookie off cross-site requests. This test pins the
explicit declaration in ``epicurrents/settings/common.py`` — if either
setting disappears or relaxes to ``"None"``, every cookie-authenticated
write endpoint becomes CSRF-able and nothing else fails.
"""

from django.conf import settings


def test_session_cookie_samesite_is_lax():
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"


def test_csrf_cookie_samesite_is_lax():
    assert settings.CSRF_COOKIE_SAMESITE == "Lax"
