"""The jobs endpoints: requesting, listing, cancelling, confirming and rolling back."""

import json

import pytest
from django.conf import settings

from activity.models import Activity
from maintenance import spool
from maintenance.models import MaintenanceJob, MaintenancePackage
from maintenance.tests.conftest import PASSWORD, place_package_files

BASE = "/api/v1/maintenance"


def _post(client, path, data=None):
    return client.post(path, json.dumps(data or {}), content_type="application/json")


def _package(sha="b" * 64, version="0.9.9"):
    place_package_files(sha, version)
    return MaintenancePackage.objects.create(sha256=sha, version=version)


def _host_job(user, state=MaintenanceJob.State.AWAITING_VERIFICATION, **fields):
    job = MaintenanceJob.objects.create(
        operation="platform.update",
        executor=MaintenanceJob.Executor.HOST,
        requested_by=user,
        state=state,
        in_flight=state in MaintenanceJob.IN_FLIGHT_STATES,
        **fields,
    )
    return job


@pytest.mark.django_db
class TestCreateCeleryJob:
    def test_a_read_only_operation_runs_and_records_its_output(
        self, enabled, superuser_client, django_capture_on_commit_callbacks
    ):
        client, user = superuser_client
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(client, f"{BASE}/jobs", {"operation": "activity.verify_audit_integrity"})
        assert response.status_code == 202, response.content
        body = response.json()
        job = MaintenanceJob.objects.get(job_id=body["job_id"])
        assert job.executor == "celery" and job.requested_by == user
        assert job.state == "succeeded" and not job.in_flight and job.finished_at is not None
        assert "chains_checked" in job.output
        assert body["requested_by"] == user.username

    def test_the_request_is_audited_with_identifiers_only(
        self, enabled, superuser_client, django_capture_on_commit_callbacks
    ):
        client, user = superuser_client
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(client, f"{BASE}/jobs", {"operation": "activity.verify_audit_integrity"})
        row = Activity.objects.filter(verb="maintenance.job.create").get()
        assert row.actor == user
        assert row.metadata == {}, "the target row carries the operation, executor and version"
        job = MaintenanceJob.objects.get(job_id=response.json()["job_id"])
        assert row.target_object_id == str(job.pk)

    def test_an_unknown_operation_is_400(self, enabled, superuser_client):
        client, _ = superuser_client
        assert _post(client, f"{BASE}/jobs", {"operation": "tests.nope"}).status_code == 400
        assert not MaintenanceJob.objects.exists()

    def test_invalid_arguments_are_400_and_name_the_field(self, enabled, superuser_client):
        client, _ = superuser_client
        response = _post(
            client,
            f"{BASE}/jobs",
            {"operation": "activity.verify_audit_integrity", "args": {"derived_window_days": -1}},
        )
        assert response.status_code == 400
        assert "derived_window_days" in response.json()["detail"]
        assert not MaintenanceJob.objects.exists()

    def test_an_operation_needing_step_up_refuses_a_wrong_password(self, enabled, superuser_client):
        client, _ = superuser_client
        response = _post(
            client, f"{BASE}/jobs", {"operation": "recordings.refresh_signal_metadata", "password": "wrong"}
        )
        assert response.status_code == 400
        assert not MaintenanceJob.objects.exists()

    def test_an_operation_needing_step_up_accepts_the_password(
        self, enabled, superuser_client, django_capture_on_commit_callbacks
    ):
        client, _ = superuser_client
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(
                client,
                f"{BASE}/jobs",
                {"operation": "recordings.refresh_signal_metadata", "args": {"dry_run": True}, "password": PASSWORD},
            )
        assert response.status_code == 202, response.content
        assert MaintenanceJob.objects.get().args == {"dry_run": True}

    def test_a_second_request_while_one_is_in_flight_is_409(self, enabled, superuser_client):
        client, user = superuser_client
        _host_job(user, state=MaintenanceJob.State.RUNNING)
        response = _post(client, f"{BASE}/jobs", {"operation": "activity.verify_audit_integrity"})
        assert response.status_code == 409
        assert MaintenanceJob.objects.count() == 1

    def test_the_constraint_backs_the_check(self, enabled, superuser):
        _host_job(superuser, state=MaintenanceJob.State.RUNNING)
        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            MaintenanceJob.objects.create(operation="x.y", executor="celery", in_flight=True)


@pytest.mark.django_db
class TestCreateHostJob:
    def test_writes_the_request_file_on_commit(
        self, host_enabled, superuser_client, django_capture_on_commit_callbacks
    ):
        client, user = superuser_client
        package = _package()
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(
                client,
                f"{BASE}/jobs",
                {"operation": "platform.update", "args": {"package_sha256": package.sha256}, "password": PASSWORD},
            )
        assert response.status_code == 202, response.content
        job = MaintenanceJob.objects.get()
        assert job.executor == "host" and job.state == "requested" and job.package == package
        assert job.target_version == "0.9.9"
        request = json.loads(spool.request_path(job.job_id).read_text())
        assert request == {
            "protocol": 1,
            "job_id": str(job.job_id),
            "operation": "platform.update",
            "requested_by_id": user.pk,
            "requested_at": request["requested_at"],
            # The deployment default is filled in so the agent applies this
            # platform's window rather than its own.
            "args": {
                "package_sha256": package.sha256,
                "verify_window_minutes": settings.REMOTE_UPDATE_VERIFY_WINDOW_MINUTES,
            },
        }

    def test_an_unknown_package_is_400_and_writes_nothing(
        self, host_enabled, superuser_client, django_capture_on_commit_callbacks
    ):
        client, _ = superuser_client
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(
                client,
                f"{BASE}/jobs",
                {"operation": "platform.update", "args": {"package_sha256": "c" * 64}, "password": PASSWORD},
            )
        assert response.status_code == 400
        assert not MaintenanceJob.objects.exists()
        assert not spool.jobs_dir().exists() or not list(spool.jobs_dir().iterdir())

    def test_step_up_is_required(self, host_enabled, superuser_client):
        client, _ = superuser_client
        package = _package()
        response = _post(
            client, f"{BASE}/jobs", {"operation": "platform.update", "args": {"package_sha256": package.sha256}}
        )
        assert response.status_code == 400
        assert not MaintenanceJob.objects.exists()


@pytest.mark.django_db
class TestReadJobs:
    def test_staff_see_jobs_without_output_and_superusers_with_it(self, enabled, staff_client, superuser):
        job = MaintenanceJob.objects.create(
            operation="activity.verify_audit_integrity",
            executor="celery",
            state="succeeded",
            in_flight=False,
            output="secret-ish output",
            requested_by=superuser,
        )
        client, _ = staff_client
        listed = client.get(f"{BASE}/jobs").json()
        assert listed[0]["job_id"] == str(job.job_id) and listed[0]["output"] is None
        assert client.get(f"{BASE}/jobs/{job.job_id}").json()["output"] is None
        assert client.get(f"{BASE}/jobs/{job.job_id}/log").status_code == 403
        client.force_login(superuser)
        assert client.get(f"{BASE}/jobs/{job.job_id}").json()["output"] == "secret-ish output"
        log = client.get(f"{BASE}/jobs/{job.job_id}/log").json()
        assert log["log"] == "secret-ish output" and log["truncated"] is False

    def test_responses_carry_no_primary_keys(self, enabled, staff_client, superuser):
        MaintenanceJob.objects.create(
            operation="a.b", executor="celery", state="succeeded", in_flight=False, requested_by=superuser
        )
        client, _ = staff_client
        body = client.get(f"{BASE}/jobs").json()[0]
        assert "id" not in body and "requested_by_id" not in body and "pk" not in body

    def test_an_unknown_or_malformed_id_is_404(self, enabled, staff_client):
        client, _ = staff_client
        assert client.get(f"{BASE}/jobs/00000000-0000-4000-8000-000000000000").status_code == 404
        assert client.get(f"{BASE}/jobs/not-a-uuid").status_code == 404

    def test_the_host_log_is_tail_bounded(self, enabled, superuser_client, superuser, monkeypatch):
        job = _host_job(superuser, state="succeeded")
        spool.log_path(job.job_id).parent.mkdir(parents=True)
        spool.log_path(job.job_id).write_bytes(b"x" * 100 + b"END")
        monkeypatch.setattr(spool, "LOG_TAIL_BYTES", 10)
        client, _ = superuser_client
        body = client.get(f"{BASE}/jobs/{job.job_id}/log").json()
        assert body["bytes"] == 103 and body["truncated"] is True and body["log"] == "xxxxxxxEND"


@pytest.mark.django_db
class TestCancel:
    def test_a_requested_host_job_is_cancelled_and_its_request_file_removed(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _host_job(user, state="requested")
        spool.write_request(job)
        response = _post(client, f"{BASE}/jobs/{job.job_id}/cancel")
        assert response.status_code == 200 and response.json()["state"] == "cancelled"
        job.refresh_from_db()
        assert job.state == "cancelled" and not job.in_flight and job.finished_at is not None
        assert not spool.request_path(job.job_id).exists()
        assert Activity.objects.filter(verb="maintenance.job.cancel").exists()

    def test_a_job_the_agent_picked_up_cannot_be_cancelled(self, host_enabled, superuser_client, write_status):
        client, user = superuser_client
        job = _host_job(user, state="requested")
        write_status(job.job_id, "accepted")
        assert _post(client, f"{BASE}/jobs/{job.job_id}/cancel").status_code == 409
        job.refresh_from_db()
        assert job.state == "accepted", "the sync at the top of the write applied the agent's state"

    @pytest.mark.parametrize("state", ["running", "succeeded", "awaiting_verification"])
    def test_other_states_are_409(self, host_enabled, superuser_client, state):
        client, user = superuser_client
        job = _host_job(user, state=state)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/cancel").status_code == 409


@pytest.mark.django_db
class TestVerifyAndRollback:
    def test_verify_writes_the_marker_with_a_password(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _host_job(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": "wrong"}).status_code == 400
        assert not spool.marker_path(job.job_id, "verify").exists()
        response = _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD})
        assert response.status_code == 200, response.content
        marker = json.loads(spool.marker_path(job.job_id, "verify").read_text())
        assert marker["by_user_id"] == user.pk and marker["protocol"] == 1
        job.refresh_from_db()
        assert job.verify_requested_at is not None and job.state == "awaiting_verification"

    def test_verify_needs_no_second_factor(self, host_enabled, superuser_client):
        from user import two_factor as tf
        from user.models import TwoFactorCredential

        client, user = superuser_client
        TwoFactorCredential.objects.create(user=user, secret=tf.generate_secret(), confirmed_at="2026-01-01T00:00:00Z")
        job = _host_job(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 200

    @pytest.mark.parametrize("state", ["requested", "running", "succeeded", "rolled_back"])
    def test_verify_outside_the_window_is_409(self, host_enabled, superuser_client, state):
        client, user = superuser_client
        job = _host_job(user, state=state)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 409

    def test_rollback_writes_the_marker_and_a_security_event(self, host_enabled, superuser_client, caplog):
        import logging

        caplog.set_level(logging.WARNING, logger="epicurrents.security")
        client, user = superuser_client
        job = _host_job(user)
        response = _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD})
        assert response.status_code == 200, response.content
        assert spool.marker_path(job.job_id, "rollback").exists()
        job.refresh_from_db()
        assert job.rollback_requested_at is not None
        events = [
            r for r in caplog.records if getattr(r, "security_event_type", "") == "maintenance.rollback_requested"
        ]
        assert len(events) == 1 and events[0].job_id == str(job.job_id)

    def test_rollback_after_success_needs_the_snapshot(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _host_job(user, state="succeeded")
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 409
        job = MaintenanceJob.objects.get()
        job.snapshot = "pre-update-20260920-120000"
        job.save()
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 200

    def test_a_celery_job_has_no_window(self, enabled, superuser_client):
        client, user = superuser_client
        job = MaintenanceJob.objects.create(operation="a.b", executor="celery", state="succeeded", in_flight=False)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 409
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 409

    def test_the_host_tier_flag_does_not_gate_verify_or_rollback(self, enabled, superuser_client):
        """A window opened while the flag was on must still be closable after an operator turns it off."""
        client, user = superuser_client
        job = _host_job(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 200
