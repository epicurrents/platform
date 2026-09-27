"""The maintenance lock: the flag reader and the middleware's policy per phase, caller and method."""

import json

import pytest
from django.conf import settings
from django.test import override_settings

from activity.models import Activity
from maintenance import lock
from maintenance.lock import MaintenanceLock, current_lock, read_lock

HEALTH = "/api/v1/health"
READY = "/api/v1/ready"
ME = "/api/v1/user/me"
LOGIN = "/api/v1/user/login"
LOGOUT = "/api/v1/user/logout"
STATUS = "/api/v1/maintenance/status"
PROBE = "/api/v1/maintenance/lock"
WRITE = "/api/v1/notifications/subscribe"
WELL_KNOWN = "/.well-known/epicurrents-federation.json"


def _post(client, path):
    return client.post(path, json.dumps({}), content_type="application/json")


# ── The reader ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestFlagReader:
    def test_no_file_means_no_lock(self, spool_dir):
        assert read_lock() is None
        assert current_lock() is None

    def test_a_well_formed_flag_is_parsed(self, write_flag):
        write_flag("verifying", expected_until="2026-09-20T10:30:00Z", job_id="abc")
        found = read_lock()
        assert found == MaintenanceLock(
            phase="verifying",
            since="2026-09-20T10:00:00Z",
            expected_until="2026-09-20T10:30:00Z",
            message="The platform is being updated.",
            job_id="abc",
        )

    @pytest.mark.parametrize(
        "content", ["not json", "[1, 2]", '{"protocol": 2, "phase": "updating"}', '{"phase": "later"}']
    )
    def test_an_unreadable_flag_fails_closed_as_updating(self, spool_dir, content):
        lock.lock_path().write_text(content)
        found = read_lock()
        assert found.phase == "updating" and found.malformed
        assert found.message == lock.DEFAULT_MESSAGE

    def test_a_blank_message_gets_the_default(self, write_flag):
        write_flag("updating", message="   ")
        assert read_lock().message == lock.DEFAULT_MESSAGE

    def test_the_read_is_cached_for_a_second(self, write_flag):
        write_flag("updating")
        assert current_lock().phase == "updating"
        lock.lock_path().unlink()
        assert current_lock() is not None, "served from the cache"
        lock.invalidate_cache()
        assert current_lock() is None

    def test_retry_after_counts_to_the_expected_end(self):
        from datetime import datetime, timezone

        now = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
        assert MaintenanceLock(phase="updating", expected_until="2026-09-20T10:05:00Z").retry_after(now) == 300
        assert MaintenanceLock(phase="updating", expected_until="2026-09-20T09:00:00Z").retry_after(now) == 1
        assert MaintenanceLock(phase="updating").retry_after(now) == lock.DEFAULT_RETRY_AFTER


# ── The middleware ───────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestLockMiddlewarePolicy:
    def test_without_a_flag_nothing_changes(self, spool_dir, client):
        assert client.get(HEALTH).status_code == 200
        assert client.get(ME).status_code == 200

    @pytest.mark.parametrize("phase", ["updating", "rolling_back"])
    def test_anonymous_api_requests_are_refused_while_updating(self, write_flag, client, phase):
        write_flag(phase)
        assert client.get(ME).status_code == 503
        assert _post(client, LOGIN).status_code == 503

    @pytest.mark.parametrize("phase", ["updating", "rolling_back"])
    def test_a_signed_in_user_is_refused_everything_while_updating(self, write_flag, auth_client, phase):
        write_flag(phase)
        client, _ = auth_client
        assert client.get(ME).status_code == 503
        assert _post(client, WRITE).status_code == 503

    @pytest.mark.parametrize("phase", ["updating", "rolling_back"])
    def test_a_superuser_may_read_but_not_write_while_updating(self, write_flag, superuser_client, phase):
        write_flag(phase)
        client, _ = superuser_client
        assert client.get(ME).status_code == 200
        assert _post(client, WRITE).status_code == 503

    def test_while_verifying_reads_pass_and_writes_are_refused(self, write_flag, auth_client):
        write_flag("verifying")
        client, _ = auth_client
        assert client.get(ME).status_code == 200
        assert _post(client, WRITE).status_code == 503

    def test_while_verifying_login_and_logout_pass(self, write_flag, client, user):
        write_flag("verifying")
        response = _post(client, LOGIN)
        assert response.status_code != 503
        response = _post(client, LOGOUT)
        assert response.status_code != 503

    def test_while_verifying_a_superuser_is_exempt(self, write_flag, superuser_client):
        write_flag("verifying")
        client, _ = superuser_client
        response = _post(client, WRITE)
        assert response.status_code != 503

    @pytest.mark.parametrize("phase", ["updating", "verifying", "rolling_back"])
    def test_the_probes_answer_in_every_phase(self, write_flag, client, phase):
        write_flag(phase)
        assert client.get(HEALTH).status_code == 200
        assert client.get(READY).status_code in (200, 503)
        assert client.get(READY).json().get("detail") != "maintenance"

    @pytest.mark.parametrize("phase", ["updating", "verifying", "rolling_back"])
    @pytest.mark.parametrize("path", [PROBE, PROBE + "/"])
    def test_the_lock_probe_answers_in_every_phase(self, write_flag, client, phase, path):
        """The SPA polls the probe to see the phase change and the flag come down; refusing it hides the release."""
        write_flag(phase)
        response = client.get(path)
        assert response.status_code != 503
        assert response.json().get("detail") != "maintenance"

    @pytest.mark.parametrize("phase", ["updating", "verifying", "rolling_back"])
    def test_documents_and_assets_pass_in_every_phase(self, write_flag, client, phase):
        write_flag(phase)
        assert client.get("/").status_code != 503, "the SPA document must render the message"
        assert client.get("/login").status_code != 503
        assert client.get(WELL_KNOWN).status_code != 503, "federation peers keep the key document"
        assert client.get("/static/nothing.js").status_code != 503

    def test_an_unsafe_non_api_request_is_refused(self, write_flag, client):
        write_flag("updating")
        assert client.post("/", {}).status_code == 503

    def test_a_malformed_flag_locks_like_updating(self, spool_dir, superuser_client):
        lock.lock_path().write_text("{")
        lock.invalidate_cache()
        client, _ = superuser_client
        assert client.get(ME).status_code == 200
        assert _post(client, WRITE).status_code == 503


@pytest.mark.django_db
class TestLockResponse:
    def test_body_and_headers(self, write_flag, client):
        write_flag("updating", expected_until="2999-01-01T00:00:00Z", job_id="job-1")
        response = client.get(ME)
        assert response.status_code == 503
        assert response.json() == {
            "detail": "maintenance",
            "phase": "updating",
            "since": "2026-09-20T10:00:00Z",
            "expected_until": "2999-01-01T00:00:00Z",
            "message": "The platform is being updated.",
        }
        assert int(response["Retry-After"]) > 1000
        assert response["Cache-Control"] == "no-store"

    def test_retry_after_defaults_without_an_expected_end(self, write_flag, client):
        write_flag("updating")
        assert client.get(ME)["Retry-After"] == str(lock.DEFAULT_RETRY_AFTER)

    def test_a_refused_request_writes_no_activity_row(self, write_flag, auth_client):
        write_flag("updating")
        client, _ = auth_client
        before = Activity.objects.count()
        assert _post(client, WRITE).status_code == 503
        assert Activity.objects.count() == before

    def test_a_refused_request_burns_no_throttle_budget(self, write_flag, auth_client, monkeypatch):
        from epicurrents import middleware

        calls = []
        monkeypatch.setattr(middleware, "check_request_throttle", lambda request: calls.append(request) or None)
        write_flag("updating")
        client, _ = auth_client
        assert client.get(ME).status_code == 503
        assert calls == []


class TestMiddlewareOrdering:
    def test_the_lock_middleware_is_registered(self):
        assert "epicurrents.middleware.MaintenanceLockMiddleware" in settings.MIDDLEWARE

    def test_it_runs_after_authentication_and_before_throttle_and_audit(self):
        order = list(settings.MIDDLEWARE)
        lock_idx = order.index("epicurrents.middleware.MaintenanceLockMiddleware")
        assert order.index("django.contrib.auth.middleware.AuthenticationMiddleware") < lock_idx, (
            "the policy reads request.user"
        )
        assert lock_idx < order.index("epicurrents.middleware.ApiThrottleMiddleware"), "a refusal burns no budget"
        assert lock_idx < order.index("epicurrents.middleware.ApiActivityLoggingMiddleware"), (
            "a refusal writes no Activity row"
        )


@pytest.mark.django_db
def test_the_flag_lives_where_update_sh_writes_it(spool_dir):
    """update.sh writes update/maintenance.json; the middleware must look at the same name."""
    from pathlib import Path

    script = Path(settings.BASE_DIR, "scripts", "update.sh").read_text()
    assert 'MAINTENANCE_FLAG="$UPDATE_DIR/maintenance.json"' in script
    assert lock.lock_path() == spool_dir / "maintenance.json"


@pytest.mark.django_db
def test_the_spool_default_is_the_update_directory():
    with override_settings(MAINTENANCE_SPOOL_PATH=str(settings.BASE_DIR / "update")):
        assert lock.spool_path().name == "update"


@pytest.mark.django_db
def test_a_spool_the_process_cannot_enter_is_not_a_lock(spool_dir, client):
    """A permissions slip on the directory must not become a platform-wide 503."""
    import os

    if os.geteuid() == 0:
        pytest.skip("root can enter any directory")
    lock.lock_path().write_text('{"protocol": 1, "phase": "updating"}')
    spool_dir.chmod(0o000)
    lock.invalidate_cache()
    try:
        assert read_lock() is None
        assert client.get(ME).status_code == 200
    finally:
        spool_dir.chmod(0o755)


@pytest.mark.django_db
class TestLockProbe:
    """``GET /api/v1/maintenance/lock``: public, ungated, exempt, no Activity row, the same shape always."""

    def test_no_flag(self, spool_dir, client):
        response = client.get(PROBE)
        assert response.status_code == 200
        assert response.json() == {
            "locked": False,
            "phase": None,
            "since": None,
            "expected_until": None,
            "message": None,
        }
        assert "no-store" in response["Cache-Control"]

    @pytest.mark.parametrize("phase", ["updating", "verifying", "rolling_back"])
    def test_each_phase(self, write_flag, client, phase):
        write_flag(phase, expected_until="2026-09-20T10:30:00Z")
        assert client.get(PROBE).json() == {
            "locked": True,
            "phase": phase,
            "since": "2026-09-20T10:00:00Z",
            "expected_until": "2026-09-20T10:30:00Z",
            "message": "The platform is being updated.",
        }

    def test_a_malformed_flag_reads_as_updating(self, spool_dir, client):
        lock.lock_path().write_text("{")
        lock.invalidate_cache()
        body = client.get(PROBE).json()
        assert body["locked"] is True and body["phase"] == "updating" and body["message"] == lock.DEFAULT_MESSAGE

    def test_answers_with_the_feature_off(self, write_flag, client):
        with override_settings(REMOTE_MAINTENANCE_ENABLED=False):
            write_flag("updating")
            assert client.get(PROBE).json()["locked"] is True

    def test_writes_no_activity_row(self, write_flag, superuser_client):
        client, _ = superuser_client
        write_flag("verifying")
        before = Activity.objects.count()
        assert client.get(PROBE).status_code == 200
        assert client.get(PROBE + "/").status_code in (200, 404)
        assert Activity.objects.count() == before
        assert PROBE in settings.ACTIVITY_PATH_SKIP_LIST and PROBE + "/" in settings.ACTIVITY_PATH_SKIP_LIST
