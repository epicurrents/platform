"""The gates on the maintenance API: the master flag, the tiers, the host-tier flag, the throttle scope."""

import json

import pytest
from django.test import override_settings

from epicurrents.throttle import _scope_for_path
from maintenance import spool
from maintenance.models import MaintenanceJob

BASE = "/api/v1/maintenance"
JOB_ID = "00000000-0000-4000-8000-000000000000"

READ_ROUTES = [f"{BASE}/status", f"{BASE}/operations", f"{BASE}/jobs", f"{BASE}/jobs/{JOB_ID}"]
SUPERUSER_READ_ROUTES = [f"{BASE}/jobs/{JOB_ID}/log"]
WRITE_ROUTES = [
    f"{BASE}/jobs",
    f"{BASE}/jobs/{JOB_ID}/cancel",
    f"{BASE}/jobs/{JOB_ID}/verify",
    f"{BASE}/jobs/{JOB_ID}/rollback",
]


def _post(client, path, data=None):
    return client.post(path, json.dumps(data or {}), content_type="application/json")


@pytest.mark.django_db
class TestMasterFlag:
    @pytest.mark.parametrize("path", READ_ROUTES + SUPERUSER_READ_ROUTES)
    def test_reads_answer_404_while_off_even_for_a_superuser(self, spool_dir, superuser_client, path):
        client, _ = superuser_client
        with override_settings(REMOTE_MAINTENANCE_ENABLED=False):
            assert client.get(path).status_code == 404

    @pytest.mark.parametrize("path", WRITE_ROUTES)
    def test_writes_answer_404_while_off_even_for_a_superuser(self, spool_dir, superuser_client, path):
        client, _ = superuser_client
        with override_settings(REMOTE_MAINTENANCE_ENABLED=False):
            assert _post(client, path).status_code == 404

    @pytest.mark.parametrize("path", READ_ROUTES + WRITE_ROUTES)
    def test_off_answers_404_before_401(self, spool_dir, client, path):
        with override_settings(REMOTE_MAINTENANCE_ENABLED=False):
            response = client.get(path) if path in READ_ROUTES else _post(client, path)
        assert response.status_code == 404

    def test_the_flags_default_off(self):
        from epicurrents.settings import common

        assert common.REMOTE_MAINTENANCE_ENABLED is False
        assert common.REMOTE_UPDATE_ENABLED is False


@pytest.mark.django_db
class TestTiers:
    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_anonymous_reads_are_401(self, enabled, client, path):
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_a_plain_user_is_403(self, enabled, auth_client, path):
        client, _ = auth_client
        assert client.get(path).status_code == 403

    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_staff_may_read(self, enabled, staff_client, path):
        client, _ = staff_client
        assert client.get(path).status_code in (200, 404)

    @pytest.mark.parametrize("path", SUPERUSER_READ_ROUTES + WRITE_ROUTES)
    def test_staff_may_not_write_or_read_logs(self, enabled, staff_client, path):
        client, _ = staff_client
        response = client.get(path) if path in SUPERUSER_READ_ROUTES else _post(client, path)
        assert response.status_code == 403

    @pytest.mark.parametrize("path", WRITE_ROUTES)
    def test_a_plain_user_may_not_write(self, enabled, auth_client, path):
        client, _ = auth_client
        assert _post(client, path).status_code == 403


@pytest.mark.django_db
class TestHostTierFlag:
    def test_a_host_operation_is_refused_while_the_host_tier_is_off(self, enabled, superuser_client):
        client, _ = superuser_client
        response = _post(client, f"{BASE}/jobs", {"operation": "platform.update", "args": {"package_sha256": "a" * 64}})
        assert response.status_code == 403
        assert "REMOTE_UPDATE_ENABLED" in response.json()["detail"]
        assert not MaintenanceJob.objects.exists()

    def test_the_operations_listing_marks_the_host_tier_unavailable(self, enabled, staff_client):
        client, _ = staff_client
        listing = {op["key"]: op for op in client.get(f"{BASE}/operations").json()}
        assert listing["platform.update"]["available"] is False
        assert listing["activity.verify_audit_integrity"]["available"] is True

    def test_a_host_operation_the_agent_does_not_carry_out_is_unavailable(self, host_enabled, staff_client):
        client, _ = staff_client
        spool.write_json_atomic(
            spool.spool_path() / "agent.json",
            {
                "protocol": 1,
                "version": "1",
                "enabled": True,
                "last_run": spool.now_iso(),
                "capabilities": ["platform.update"],
            },
        )
        listing = {op["key"]: op for op in client.get(f"{BASE}/operations").json()}
        assert listing["platform.update"]["available"] is True
        assert listing["platform.backup"]["available"] is False
        assert listing["platform.rollback"]["available"] is False
        assert listing["activity.verify_audit_integrity"]["available"] is True

    def test_before_any_heartbeat_the_tier_alone_decides(self, host_enabled, staff_client):
        client, _ = staff_client
        listing = {op["key"]: op for op in client.get(f"{BASE}/operations").json()}
        assert all(listing[key]["available"] for key in ("platform.update", "platform.backup", "platform.rollback"))

    def test_and_available_once_it_is_on(self, host_enabled, staff_client):
        client, _ = staff_client
        listing = {op["key"]: op for op in client.get(f"{BASE}/operations").json()}
        assert listing["platform.update"]["available"] is True

    def test_status_reports_both_flags(self, host_enabled, staff_client):
        client, _ = staff_client
        body = client.get(f"{BASE}/status").json()
        assert body["remote_maintenance_enabled"] is True and body["remote_update_enabled"] is True


class TestThrottleScope:
    def test_job_routes_have_their_own_scope_and_packages_count_as_uploads(self):
        assert _scope_for_path(f"{BASE}/jobs") == "maintenance"
        assert _scope_for_path(f"{BASE}/jobs/{JOB_ID}/rollback") == "maintenance"
        assert _scope_for_path(f"{BASE}/packages") == "upload"
        assert _scope_for_path(f"{BASE}/status") == "default"


@pytest.mark.django_db
class TestRefusalsAreSecurityLogged:
    def _denials(self, caplog):
        return [r for r in caplog.records if getattr(r, "security_event_type", "") == "permission.denied"]

    def test_an_anonymous_caller_is_logged(self, enabled, client, caplog):
        import logging

        caplog.set_level(logging.WARNING, logger="epicurrents.security")
        assert client.get(f"{BASE}/status").status_code == 401
        [event] = self._denials(caplog)
        assert event.permission == "maintenance.authenticated" and event.actor_id is None

    def test_a_plain_user_is_logged_with_the_tier_they_lacked(self, enabled, auth_client, caplog):
        import logging

        caplog.set_level(logging.WARNING, logger="epicurrents.security")
        client, user = auth_client
        assert client.get(f"{BASE}/status").status_code == 403
        assert _post(client, f"{BASE}/jobs").status_code == 403
        events = self._denials(caplog)
        assert [e.permission for e in events] == ["maintenance.staff", "maintenance.superuser"]
        assert {e.actor_id for e in events} == {user.pk} and events[0].path == f"{BASE}/status"
