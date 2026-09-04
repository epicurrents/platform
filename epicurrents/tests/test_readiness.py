"""Contract tests for the readiness probe at ``/api/v1/ready``.

The endpoint exists to answer a question ``/api/v1/health`` cannot: whether this
web container can actually serve, as opposed to merely still running. The
properties that matter are that a dependency failure produces a 503 (a probe
that reports healthy through an outage is worse than no probe, because it is
believed), that the two probes stay distinct (a readiness check folded into the
liveness endpoint would restart every container at once on a transient database
blip), and that the failure response names no host, port or credential — it is
unauthenticated.
"""

from unittest import mock

import pytest


@pytest.mark.django_db
class TestReadyWhenDependenciesAnswer:
    def test_returns_200_and_marks_each_check_ok(self, client):
        response = client.get("/api/v1/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"] == {"database": "ok", "cache": "ok"}


@pytest.mark.django_db
class TestNotReadyWhenADependencyFails:
    def test_database_failure_returns_503(self, client):
        with mock.patch("epicurrents.api.v1.ninja.connections") as connections:
            connections.__getitem__.return_value.cursor.side_effect = RuntimeError("connection refused")
            response = client.get("/api/v1/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not ready"
        assert body["checks"]["database"] == "error"

    def test_cache_failure_returns_503(self, client):
        with mock.patch("epicurrents.api.v1.ninja.cache.get", side_effect=RuntimeError("connection refused")):
            response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["cache"] == "error"

    def test_failure_body_leaks_no_connection_detail(self, client):
        """The exception text quotes host names, ports and user names, and this
        endpoint takes no credentials — the detail belongs in the container log."""
        with mock.patch("epicurrents.api.v1.ninja.cache.get", side_effect=RuntimeError("db.internal:5432 as epi")):
            response = client.get("/api/v1/ready")
        assert "db.internal" not in response.content.decode()
        assert "5432" not in response.content.decode()


@pytest.mark.django_db
class TestLivenessStaysIndependent:
    def test_health_still_answers_while_dependencies_are_down(self, client):
        """Liveness must not fail on a dependency outage: a restart does not bring
        the database back, and restarting every web container at once during a
        blip turns a recoverable incident into an outage."""
        with mock.patch("epicurrents.api.v1.ninja.connections") as connections:
            connections.__getitem__.return_value.cursor.side_effect = RuntimeError("connection refused")
            response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.django_db
class TestProbeSurvivesTheHttpsRedirect:
    """Production sets ``SECURE_SSL_REDIRECT``, and the healthcheck reaches the
    app over plain HTTP on loopback with no ``X-Forwarded-Proto``. Without the
    exemption the probe is answered with a 301 to an ``https://`` URL gunicorn
    does not speak, so it fails on every run and the container reports unhealthy
    while serving traffic normally. Nothing under the test settings would show
    it, because the redirect only exists in production.
    """

    def test_production_settings_exempt_the_probe_path(self):
        """Read the exemption out of the production settings module rather than
        restating it, so a rename of the endpoint has to update both."""
        import re

        from epicurrents.settings import production

        exempt = getattr(production, "SECURE_REDIRECT_EXEMPT", [])
        assert any(re.search(pattern, "api/v1/ready") for pattern in exempt), (
            "the readiness path is not exempt from SECURE_SSL_REDIRECT; the web container's "
            "healthcheck would receive a 301 and report unhealthy in every production deployment"
        )

    def _production_exempt(self):
        """The real patterns, not a restatement of them.

        Both tests below used to set the pattern they then asserted against,
        which made them a tautology: widening production.py to ``^api/v1/`` —
        dropping the entire core API out of the HTTPS redirect — left the whole
        module green.
        """
        from epicurrents.settings import production

        return list(getattr(production, "SECURE_REDIRECT_EXEMPT", []))

    def test_probe_answers_200_under_the_redirect(self, client, settings):
        settings.SECURE_SSL_REDIRECT = True
        settings.SECURE_REDIRECT_EXEMPT = self._production_exempt()
        response = client.get("/api/v1/ready")
        assert response.status_code == 200

    def test_the_exemption_does_not_cover_the_rest_of_the_api(self, client, settings):
        """A pattern loose enough to catch other endpoints would drop them out of
        the HTTPS redirect along with the probe."""
        settings.SECURE_SSL_REDIRECT = True
        settings.SECURE_REDIRECT_EXEMPT = self._production_exempt()
        assert client.get("/api/v1/health").status_code == 301
        assert client.get("/api/v1/ready/sub").status_code == 301
        assert client.get("/api/v1/user/me").status_code == 301


@pytest.mark.django_db
class TestProbeIsUnauthenticatedAndUnaudited:
    def test_anonymous_caller_is_served(self, client):
        assert client.get("/api/v1/ready").status_code == 200

    def test_creates_no_activity_row(self, client):
        """The probe runs every 15 s per container; an Activity row each time
        would drown the data-interaction signal the audit trail exists for."""
        from activity.models import Activity

        before = Activity.objects.count()
        client.get("/api/v1/ready")
        assert Activity.objects.count() == before
