"""Tests for ``epicurrents.middleware.SecurityHeadersMiddleware``.

The middleware reads its policy from settings at instantiation, so each case
constructs it inside an ``override_settings`` block rather than going through
the test client (whose middleware stack is built once with the test settings).
"""

from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from epicurrents.middleware import SecurityHeadersMiddleware


def _response(**setting_overrides):
    """Run the middleware under the given settings and return the response."""
    with override_settings(**setting_overrides):
        mw = SecurityHeadersMiddleware(lambda request: HttpResponse("ok"))
        return mw(RequestFactory().get("/"))


def test_csp_is_report_only_by_default():
    resp = _response(CONTENT_SECURITY_POLICY="default-src 'self'", CSP_REPORT_ONLY=True)
    assert resp["Content-Security-Policy-Report-Only"] == "default-src 'self'"
    assert "Content-Security-Policy" not in resp


def test_csp_enforced_when_report_only_off():
    resp = _response(CONTENT_SECURITY_POLICY="default-src 'self'", CSP_REPORT_ONLY=False)
    assert resp["Content-Security-Policy"] == "default-src 'self'"
    assert "Content-Security-Policy-Report-Only" not in resp


def test_permissions_policy_emitted():
    resp = _response(PERMISSIONS_POLICY="camera=(), microphone=()")
    assert resp["Permissions-Policy"] == "camera=(), microphone=()"


def test_no_headers_when_policies_empty():
    """Development default: empty policy strings mean the middleware is inert."""
    resp = _response(CONTENT_SECURITY_POLICY="", PERMISSIONS_POLICY="", CSP_REPORT_ONLY=True)
    assert "Content-Security-Policy" not in resp
    assert "Content-Security-Policy-Report-Only" not in resp
    assert "Permissions-Policy" not in resp


def test_does_not_clobber_upstream_header():
    """A policy set upstream (proxy / plugin) is preserved via setdefault."""
    with override_settings(CONTENT_SECURITY_POLICY="default-src 'self'", CSP_REPORT_ONLY=False):

        def get_response(request):
            r = HttpResponse("ok")
            r["Content-Security-Policy"] = "default-src 'none'"
            return r

        mw = SecurityHeadersMiddleware(get_response)
        resp = mw(RequestFactory().get("/"))
    assert resp["Content-Security-Policy"] == "default-src 'none'"


class TestProductionCspBaseline:
    """Pins the directives the shipped SPA actually needs to render.

    The default policy is not decoration: it is enforced in production, and a
    directive that is too narrow fails silently in the browser rather than in
    any test here. That is how `connect-src 'self'` shipped and left a production
    deployment with no icons at all — the page worked, the console did not, and
    nothing server-side was wrong.
    """

    def _directive(self, name):
        from epicurrents.settings import production

        for part in production.CONTENT_SECURITY_POLICY.split(";"):
            part = part.strip()
            if part.startswith(f"{name} "):
                return part
        raise AssertionError(f"{name} is absent from the production CSP baseline")

    def test_connect_src_allows_data_uris_for_icons(self):
        # WebAwesome fetches every <wa-icon> from a data:image/svg+xml URL so it
        # can run its fill mutator, and fetch is governed by connect-src rather
        # than img-src. Drop this and the entire icon set disappears.
        assert "data:" in self._directive("connect-src")

    def test_connect_src_names_no_external_origin(self):
        # The point of the tightening that introduced the bug. data: and 'self'
        # reach no host; anything else here is a third-party connection from a
        # deployment that may hold PHI.
        directive = self._directive("connect-src")
        assert "http://" not in directive
        assert "https://" not in directive

    def test_img_src_still_allows_data_and_blob(self):
        directive = self._directive("img-src")
        assert "data:" in directive
        assert "blob:" in directive

    def test_object_src_is_none_and_frames_are_denied(self):
        assert self._directive("object-src") == "object-src 'none'"
        assert self._directive("frame-ancestors") == "frame-ancestors 'none'"
