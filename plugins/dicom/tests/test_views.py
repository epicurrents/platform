"""Tests for the OHIF viewer view at ``/plugin/dicom/ohif/``.

Covers the serving rules the built viewer depends on: real files with the types
the browser insists on, client-side routes falling back to index.html, a missing
asset answering 404 rather than the HTML document, and the not-built state.
"""

import pytest

from plugins.dicom.views import DEMO_ORIGIN

VIEWER_URL = "/plugin/dicom/ohif/"


@pytest.fixture
def ohif_dist(settings, tmp_path):
    """A stand-in OHIF dist with an index, a script, a module worker and a WASM file."""
    dist = tmp_path / "ohif-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>OHIF</title>")
    (dist / "app-config.js").write_text("window.config = { dataSources: [] };")
    (dist / "app.bundle.abc.js").write_text("console.log(1)")
    (dist / "assets" / "decode.worker.mjs").write_text("self.onmessage = null")
    (dist / "assets" / "codec.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    settings.DICOM_OHIF_DIST_PATH = str(dist)
    return dist


@pytest.mark.django_db
class TestOhifViewer:
    def test_bare_mount_is_the_study_list(self, client, ohif_dist):
        response = client.get(VIEWER_URL)
        assert response.status_code == 200
        assert response["Content-Type"] == "text/html"

    def test_serves_the_index_for_a_client_route(self, client, ohif_dist):
        response = client.get(f"{VIEWER_URL}viewer")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/html"

    def test_client_route_falls_back_to_the_index(self, client, ohif_dist):
        response = client.get(f"{VIEWER_URL}viewer/dicomjson", {"url": "/x/"})
        assert response.status_code == 200
        assert b"<title>OHIF</title>" in b"".join(response.streaming_content)

    @pytest.mark.parametrize(
        ("subpath", "content_type"),
        [
            ("app.bundle.abc.js", "text/javascript"),
            ("assets/decode.worker.mjs", "text/javascript"),
            ("assets/codec.wasm", "application/wasm"),
        ],
    )
    def test_serves_assets_with_their_types(self, client, ohif_dist, subpath, content_type):
        response = client.get(f"{VIEWER_URL}{subpath}")
        assert response.status_code == 200
        assert response["Content-Type"] == content_type

    def test_missing_asset_is_a_404_not_the_index(self, client, ohif_dist):
        assert client.get(f"{VIEWER_URL}app.bundle.stale.js").status_code == 404

    def test_traversal_is_refused(self, client, ohif_dist):
        (ohif_dist.parent / "secret.txt").write_text("no")
        assert client.get(f"{VIEWER_URL}..%2Fsecret.txt").status_code == 404

    def test_unbuilt_viewer_answers_503(self, client, settings, tmp_path):
        settings.DICOM_OHIF_DIST_PATH = str(tmp_path / "missing")
        assert client.get(f"{VIEWER_URL}viewer").status_code == 503

    def test_bare_mount_redirects_to_the_study_list(self, client, ohif_dist):
        response = client.get("/plugin/dicom/")
        assert response.status_code == 302
        assert response["Location"] == "/dicom/studies"


STRICT = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; object-src 'none'"


@pytest.mark.django_db
class TestOhifContentSecurityPolicy:
    """The codec worker needs 'unsafe-eval'; only responses from the OHIF mount get it."""

    @pytest.fixture(autouse=True)
    def enforced_policy(self, settings):
        settings.CONTENT_SECURITY_POLICY = STRICT
        settings.CSP_REPORT_ONLY = False

    @pytest.mark.parametrize("subpath", ["viewer", "viewer/dicomjson", "app.bundle.abc.js"])
    def test_ohif_responses_allow_eval_in_scripts_only(self, client, ohif_dist, subpath):
        policy = client.get(f"{VIEWER_URL}{subpath}")["Content-Security-Policy"]
        directives = dict(d.strip().split(" ", 1) for d in policy.split(";"))
        assert directives["script-src"] == "'self' 'wasm-unsafe-eval' 'unsafe-eval'"
        assert directives["default-src"] == "'self'"
        assert directives["object-src"] == "'none'"

    def test_platform_responses_keep_the_strict_policy(self, client, ohif_dist):
        assert client.get("/api/v1/")["Content-Security-Policy"] == STRICT

    def test_report_only_mode_is_followed(self, client, ohif_dist, settings):
        settings.CSP_REPORT_ONLY = True
        response = client.get(f"{VIEWER_URL}viewer")
        assert "'unsafe-eval'" in response["Content-Security-Policy-Report-Only"]
        assert "Content-Security-Policy" not in response

    def test_policy_without_script_src_derives_it_from_default_src(self, client, ohif_dist, settings):
        settings.CONTENT_SECURITY_POLICY = "default-src 'self'"
        policy = client.get(f"{VIEWER_URL}viewer")["Content-Security-Policy"]
        assert policy == "default-src 'self'; script-src 'self' 'unsafe-eval'; frame-ancestors 'self'"

    def test_no_policy_configured_adds_none(self, client, ohif_dist, settings):
        settings.CONTENT_SECURITY_POLICY = ""
        assert "Content-Security-Policy" not in client.get(f"{VIEWER_URL}viewer")


@pytest.mark.django_db
class TestOhifFraming:
    """The radiology view frames the OHIF mount; only the platform's own pages may, and only that mount."""

    @pytest.fixture(autouse=True)
    def enforced_policy(self, settings):
        settings.CONTENT_SECURITY_POLICY = f"{STRICT}; frame-ancestors 'none'"
        settings.CSP_REPORT_ONLY = False

    @pytest.mark.parametrize("subpath", ["", "viewer/dicomjson", "app.bundle.abc.js"])
    def test_ohif_responses_allow_same_origin_framing(self, client, ohif_dist, subpath):
        response = client.get(f"{VIEWER_URL}{subpath}")
        directives = dict(d.strip().split(" ", 1) for d in response["Content-Security-Policy"].split(";"))
        assert directives["frame-ancestors"] == "'self'"
        assert response["X-Frame-Options"] == "SAMEORIGIN"

    def test_platform_responses_still_refuse_framing(self, client, ohif_dist):
        response = client.get("/api/v1/")
        assert "frame-ancestors 'none'" in response["Content-Security-Policy"]
        assert response["X-Frame-Options"] == "DENY"

    def test_policy_without_frame_ancestors_gains_the_same_origin_one(self, client, ohif_dist, settings):
        settings.CONTENT_SECURITY_POLICY = STRICT
        policy = client.get(f"{VIEWER_URL}viewer")["Content-Security-Policy"]
        assert policy.endswith("; frame-ancestors 'self'")

    def test_no_policy_configured_still_sets_same_origin_framing(self, client, ohif_dist, settings):
        settings.CONTENT_SECURITY_POLICY = ""
        assert client.get(f"{VIEWER_URL}viewer")["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.django_db
class TestDemoOverlay:
    """The development demo overlay is served only with both DICOM_OHIF_DEMO_DATA and DEBUG on."""

    @pytest.fixture(autouse=True)
    def enforced_policy(self, settings):
        settings.CONTENT_SECURITY_POLICY = STRICT
        settings.CSP_REPORT_ONLY = False

    def test_overlay_appended_to_the_config_in_development(self, client, ohif_dist, settings):
        settings.DEBUG, settings.DICOM_OHIF_DEMO_DATA = True, True
        response = client.get(f"{VIEWER_URL}app-config.js")
        body = response.content.decode()
        assert body.startswith("window.config = { dataSources: [] };")
        assert "sourceName: 'demo'" in body
        assert response["Content-Type"] == "text/javascript"

    def test_demo_origin_allowed_for_connections_only(self, client, ohif_dist, settings):
        settings.DEBUG, settings.DICOM_OHIF_DEMO_DATA = True, True
        policy = client.get(f"{VIEWER_URL}viewer")["Content-Security-Policy"]
        directives = dict(d.strip().split(" ", 1) for d in policy.split(";"))
        assert directives["connect-src"] == f"'self' {DEMO_ORIGIN}"
        assert DEMO_ORIGIN not in directives["script-src"]
        assert DEMO_ORIGIN not in directives["default-src"]

    @pytest.mark.parametrize(("debug", "flag"), [(False, True), (True, False)])
    def test_overlay_not_served_unless_both_are_on(self, client, ohif_dist, settings, debug, flag):
        settings.DEBUG, settings.DICOM_OHIF_DEMO_DATA = debug, flag
        config = b"".join(client.get(f"{VIEWER_URL}app-config.js").streaming_content).decode()
        assert config == "window.config = { dataSources: [] };"
        assert DEMO_ORIGIN not in client.get(f"{VIEWER_URL}viewer")["Content-Security-Policy"]

    def test_overlay_on_a_dist_without_config_is_a_404(self, client, ohif_dist, settings):
        settings.DEBUG, settings.DICOM_OHIF_DEMO_DATA = True, True
        (ohif_dist / "app-config.js").unlink()
        assert client.get(f"{VIEWER_URL}app-config.js").status_code == 404
