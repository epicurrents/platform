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


def _heartbeat(snapshots=None, capabilities=("platform.update", "platform.backup", "platform.rollback")):
    spool.write_json_atomic(
        spool.spool_path() / "agent.json",
        {
            "protocol": 1,
            "version": "2",
            "enabled": True,
            "runtime": "docker",
            "last_run": spool.now_iso(),
            "capabilities": list(capabilities),
            "snapshots": snapshots or [],
        },
    )


SNAPSHOT = {
    "name": "backup-20260901-100000",
    "taken_at": "2026-09-01T10:00:00Z",
    "version": "0.1.0",
    "code": True,
    "migrations": None,
}


@pytest.mark.django_db
class TestSnapshotOperations:
    """platform.backup takes nothing; platform.rollback names a snapshot the agent reports."""

    def test_a_rollback_request_names_a_reported_snapshot(
        self, host_enabled, superuser_client, django_capture_on_commit_callbacks
    ):
        client, user = superuser_client
        _heartbeat([SNAPSHOT])
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(
                client,
                f"{BASE}/jobs",
                {
                    "operation": "platform.rollback",
                    "args": {"snapshot": SNAPSHOT["name"], "restore_database": False},
                    "password": PASSWORD,
                },
            )
        assert response.status_code == 202, response.content
        body = response.json()
        assert body["target_version"] == "0.1.0" and body["package_sha256"] is None
        request = spool.read_request(body["job_id"])
        assert request["operation"] == "platform.rollback"
        assert request["args"] == {"snapshot": SNAPSHOT["name"], "restore_database": False}

    def test_a_snapshot_the_agent_does_not_report_is_400(self, host_enabled, superuser_client):
        client, _ = superuser_client
        _heartbeat([SNAPSHOT])
        response = _post(
            client,
            f"{BASE}/jobs",
            {"operation": "platform.rollback", "args": {"snapshot": "backup-20260902-100000"}, "password": PASSWORD},
        )
        assert response.status_code == 400 and "no snapshot by that name" in response.json()["detail"]
        assert not MaintenanceJob.objects.exists()

    def test_a_malformed_snapshot_name_never_reaches_the_spool(self, host_enabled, superuser_client):
        client, _ = superuser_client
        _heartbeat([SNAPSHOT])
        for bad in ("../../etc/passwd", "backup-x", "", "backup-20260901-100000; rm -rf /"):
            response = _post(
                client,
                f"{BASE}/jobs",
                {"operation": "platform.rollback", "args": {"snapshot": bad}, "password": PASSWORD},
            )
            assert response.status_code == 400, bad
            assert "Invalid arguments" in response.json()["detail"], bad
        assert not MaintenanceJob.objects.exists()

    def test_without_a_heartbeat_the_agent_is_left_to_decide(
        self, host_enabled, superuser_client, django_capture_on_commit_callbacks
    ):
        client, _ = superuser_client
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(
                client,
                f"{BASE}/jobs",
                {"operation": "platform.rollback", "args": {"snapshot": SNAPSHOT["name"]}, "password": PASSWORD},
            )
        assert response.status_code == 202, response.content
        assert response.json()["target_version"] == ""

    def test_a_backup_request_takes_no_arguments(
        self, host_enabled, superuser_client, django_capture_on_commit_callbacks
    ):
        client, _ = superuser_client
        _heartbeat()
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(client, f"{BASE}/jobs", {"operation": "platform.backup", "password": PASSWORD})
        assert response.status_code == 202, response.content
        request = spool.read_request(response.json()["job_id"])
        assert request["args"] == {} and request["operation"] == "platform.backup"

    def test_the_row_carries_the_migrations_fact_the_agent_reports(
        self, host_enabled, staff_client, superuser, write_status
    ):
        client, _ = staff_client
        job = _host_job(superuser, state=MaintenanceJob.State.RUNNING)
        write_status(job.job_id, "awaiting_verification", migrations_applied=False)
        body = client.get(f"{BASE}/jobs/{job.job_id}").json()
        assert body["migrations_applied"] is False


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
        spool.write_request(job)
        spool.request_path(job.job_id).rename(spool.claimed_path(job.job_id))
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

    def test_a_job_that_is_not_an_update_has_neither_action(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = MaintenanceJob.objects.create(
            operation="platform.backup",
            executor=MaintenanceJob.Executor.HOST,
            requested_by=user,
            state=MaintenanceJob.State.SUCCEEDED,
            in_flight=False,
            snapshot="backup-20260901-100000",
        )
        response = _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD})
        assert response.status_code == 409 and "Only an update" in response.json()["detail"]
        response = _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD})
        assert response.status_code == 409 and "Only an update" in response.json()["detail"]
        assert not spool.marker_path(job.job_id, "rollback").exists()

    def test_the_host_tier_flag_does_not_gate_verify_or_rollback(self, enabled, superuser_client):
        """A window opened while the flag was on must still be closable after an operator turns it off."""
        client, user = superuser_client
        job = _host_job(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 200


@pytest.mark.django_db
class TestCancelRace:
    """The agent claims by renaming the request; the cancel unlinks it. Exactly one of the two wins."""

    def test_cancel_wins_and_a_stray_status_later_is_ignored(self, host_enabled, superuser_client, write_status):
        client, user = superuser_client
        job = _host_job(user, state="requested")
        spool.write_request(job)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/cancel").status_code == 200
        assert not spool.request_path(job.job_id).exists()
        write_status(job.job_id, "accepted", updated_at="2026-09-20T10:05:00Z")
        write_status(job.job_id, "failed", updated_at="2026-09-20T10:06:00Z")
        spool.sync(force=True)
        job.refresh_from_db()
        assert job.state == "cancelled" and not job.in_flight

    def test_agent_wins_once_it_has_claimed_the_request(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _host_job(user, state="requested")
        spool.write_request(job)
        spool.request_path(job.job_id).rename(spool.claimed_path(job.job_id))
        response = _post(client, f"{BASE}/jobs/{job.job_id}/cancel")
        assert response.status_code == 409
        assert response.json()["detail"] == "The host agent has already picked this request up."
        job.refresh_from_db()
        assert job.state == "requested" and job.in_flight, "the row is left to the agent"
        assert spool.claimed_path(job.job_id).exists()

    def test_the_claim_status_applies_and_keeps_the_job_uncancellable(
        self, host_enabled, superuser_client, write_status
    ):
        client, user = superuser_client
        job = _host_job(user, state="requested")
        spool.write_request(job)
        spool.request_path(job.job_id).rename(spool.claimed_path(job.job_id))
        write_status(job.job_id, "requested", step="check")
        assert _post(client, f"{BASE}/jobs/{job.job_id}/cancel").status_code == 409
        job.refresh_from_db()
        assert job.state == "requested" and job.step == "check"

    def test_the_row_turns_cancelled_only_after_the_unlink(self, host_enabled, superuser_client, monkeypatch):
        client, user = superuser_client
        job = _host_job(user, state="requested")
        spool.write_request(job)
        monkeypatch.setattr(spool, "remove_request", lambda job: False)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/cancel").status_code == 409
        job.refresh_from_db()
        assert job.state == "requested"


def _succeeded_update(user, **fields):
    fields.setdefault("snapshot", "pre-update-20260920-120000")
    return _host_job(user, state=MaintenanceJob.State.SUCCEEDED, verify_requested_at="2026-09-20T12:30:00Z", **fields)


@pytest.mark.django_db
class TestLateRollbackRefusals:
    def test_an_older_update_cannot_be_rolled_back_once_a_newer_one_succeeded(self, host_enabled, superuser_client):
        from datetime import timedelta

        from django.utils import timezone

        client, user = superuser_client
        old = _succeeded_update(user, created_at=timezone.now() - timedelta(days=2))
        _succeeded_update(user, created_at=timezone.now() - timedelta(days=1))
        response = _post(client, f"{BASE}/jobs/{old.job_id}/rollback", {"password": PASSWORD})
        assert response.status_code == 409 and "newest update" in response.json()["detail"]
        assert not spool.marker_path(old.job_id, "rollback").exists()

    def test_a_later_restore_counts_as_newer(self, host_enabled, superuser_client):
        from datetime import timedelta

        from django.utils import timezone

        client, user = superuser_client
        old = _succeeded_update(user, created_at=timezone.now() - timedelta(days=2))
        MaintenanceJob.objects.create(
            operation="platform.rollback", executor="host", state="succeeded", in_flight=False
        )
        assert _post(client, f"{BASE}/jobs/{old.job_id}/rollback", {"password": PASSWORD}).status_code == 409

    def test_a_late_rollback_while_another_job_is_in_flight_is_409(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _succeeded_update(user)
        MaintenanceJob.objects.create(operation="platform.backup", executor="host", state="running", in_flight=True)
        response = _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD})
        assert response.status_code == 409 and "in flight" in response.json()["detail"]

    def test_the_newest_succeeded_update_can_be_rolled_back(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _succeeded_update(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 200


@pytest.mark.django_db
class TestVerifyRollbackExclusion:
    def test_verify_after_the_deadline_is_409(self, host_enabled, superuser_client):
        from datetime import timedelta

        from django.utils import timezone

        client, user = superuser_client
        job = _host_job(user, verify_deadline=timezone.now() - timedelta(seconds=1))
        response = _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD})
        assert response.status_code == 409 and "closed" in response.json()["detail"]
        assert not spool.marker_path(job.job_id, "verify").exists()

    def test_a_second_verify_is_409(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _host_job(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 200
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 409

    def test_rollback_after_verify_in_the_window_is_409(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _host_job(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 200
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 409
        assert not spool.marker_path(job.job_id, "rollback").exists()

    def test_verify_after_rollback_is_409(self, host_enabled, superuser_client):
        client, user = superuser_client
        job = _host_job(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 200
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 409
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 409

    def test_the_compare_and_set_refuses_a_stamp_set_after_the_row_was_read(self, superuser):
        from django.utils import timezone

        job = _host_job(superuser)
        MaintenanceJob.objects.filter(pk=job.pk).update(rollback_requested_at=timezone.now())
        assert not job.transition(
            expect=["awaiting_verification"],
            unset=("verify_requested_at", "rollback_requested_at"),
            verify_requested_at=timezone.now(),
        )

    def test_an_unwritable_marker_withdraws_the_stamp(self, host_enabled, superuser_client, monkeypatch):
        client, user = superuser_client
        job = _host_job(user)

        def refuse(*args, **kwargs):
            raise PermissionError("read-only")

        monkeypatch.setattr(spool, "write_marker", refuse)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 503
        job.refresh_from_db()
        assert job.verify_requested_at is None


@pytest.mark.django_db
class TestStepUpComesLast:
    def test_a_request_refused_for_another_reason_never_checks_the_credential(
        self, host_enabled, superuser_client, monkeypatch
    ):
        from maintenance.api.v1 import ninja

        calls = []
        monkeypatch.setattr(ninja, "confirm_step_up", lambda *args, **kwargs: calls.append(args))
        client, user = superuser_client
        package = _package()
        _host_job(user, state="running")
        response = _post(
            client,
            f"{BASE}/jobs",
            {"operation": "platform.update", "args": {"package_sha256": package.sha256}, "password": PASSWORD},
        )
        assert response.status_code == 409
        assert calls == [], "no one-time code is spent on a request that was going to be refused"

    def test_a_rollback_refused_for_another_reason_never_checks_the_credential(
        self, host_enabled, superuser_client, monkeypatch
    ):
        from maintenance.api.v1 import ninja

        calls = []
        monkeypatch.setattr(ninja, "confirm_step_up", lambda *args, **kwargs: calls.append(args))
        client, user = superuser_client
        job = _succeeded_update(user)
        MaintenanceJob.objects.create(operation="platform.backup", executor="host", state="running", in_flight=True)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": "x"}).status_code == 409
        assert calls == []


@pytest.mark.django_db
class TestLockedWindow:
    @pytest.mark.parametrize("phase", ["verifying"])
    def test_no_job_may_be_requested_while_the_flag_is_up(self, enabled, superuser_client, write_flag, phase):
        write_flag(phase)
        client, _ = superuser_client
        response = _post(client, f"{BASE}/jobs", {"operation": "activity.verify_audit_integrity"})
        assert response.status_code == 409 and phase in response.json()["detail"]
        assert not MaintenanceJob.objects.exists()

    def test_verify_and_rollback_still_work_while_verifying(self, host_enabled, superuser_client, write_flag):
        write_flag("verifying")
        client, user = superuser_client
        job = _host_job(user)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/verify", {"password": PASSWORD}).status_code == 200

    @pytest.mark.parametrize("phase", ["updating", "rolling_back"])
    def test_reads_write_nothing_and_answer_from_the_spool(
        self, host_enabled, superuser_client, write_flag, write_status, phase
    ):
        client, user = superuser_client
        job = _host_job(user, state="running")
        write_status(job.job_id, "rolling_back", step="restore")
        write_flag(phase)
        body = client.get(f"{BASE}/jobs/{job.job_id}").json()
        assert body["state"] == "rolling_back" and body["step"] == "restore", "the spool's view, overlaid"
        assert client.get(f"{BASE}/jobs").json()[0]["state"] == "rolling_back"
        job.refresh_from_db()
        assert job.state == "running", "and nothing written to the database"
        assert not Activity.objects.filter(verb="maintenance.job.sync").exists()


@pytest.mark.django_db
class TestEngagingTheExecutor:
    def test_an_unwritable_spool_refuses_a_host_job_before_creating_it(
        self, host_enabled, superuser_client, monkeypatch
    ):
        client, _ = superuser_client
        package = _package()
        monkeypatch.setattr(spool, "is_writable", lambda: False)
        response = _post(
            client,
            f"{BASE}/jobs",
            {"operation": "platform.update", "args": {"package_sha256": package.sha256}, "password": PASSWORD},
        )
        assert response.status_code == 409 and "not writable" in response.json()["detail"]
        assert not MaintenanceJob.objects.exists()

    def test_a_request_file_that_cannot_be_written_fails_the_job(self, host_enabled, superuser_client, monkeypatch):
        client, _ = superuser_client
        package = _package()

        def refuse(job):
            raise PermissionError("read-only")

        monkeypatch.setattr(spool, "write_request", refuse)
        response = _post(
            client,
            f"{BASE}/jobs",
            {"operation": "platform.update", "args": {"package_sha256": package.sha256}, "password": PASSWORD},
        )
        assert response.status_code == 503
        job = MaintenanceJob.objects.get()
        assert job.state == "failed" and job.reason == "spool_write_failed" and not job.in_flight

    def test_a_dispatch_that_fails_fails_the_job(self, enabled, superuser_client, monkeypatch):
        from maintenance.tasks import run_job

        def refuse(*args, **kwargs):
            raise ConnectionError("broker down")

        monkeypatch.setattr(run_job, "apply_async", refuse)
        client, _ = superuser_client
        response = _post(client, f"{BASE}/jobs", {"operation": "activity.verify_audit_integrity"})
        assert response.status_code == 503
        job = MaintenanceJob.objects.get()
        assert job.state == "failed" and job.reason == "dispatch_failed" and not job.in_flight


def _stale_agent(spool_dir, *, stale=True):
    from datetime import timedelta

    from django.utils import timezone

    seen = timezone.now() - timedelta(seconds=600 if stale else 5)
    spool.write_json_atomic(
        spool_dir / "agent.json",
        {"protocol": 1, "version": "1", "enabled": True, "last_run": seen.isoformat().replace("+00:00", "Z")},
    )


@pytest.mark.django_db
class TestAbandon:
    def test_a_celery_job_can_be_abandoned_and_the_slot_freed(self, enabled, superuser_client, caplog):
        import logging

        caplog.set_level(logging.WARNING, logger="epicurrents.security")
        client, _ = superuser_client
        job = MaintenanceJob.objects.create(operation="tests.x", executor="celery", state="running", in_flight=True)
        response = _post(client, f"{BASE}/jobs/{job.job_id}/abandon", {"password": PASSWORD})
        assert response.status_code == 200, response.content
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "abandoned" and not job.in_flight
        assert Activity.objects.filter(verb="maintenance.job.abandon").exists()
        events = [r for r in caplog.records if getattr(r, "security_event_type", "") == "maintenance.job_abandoned"]
        assert len(events) == 1 and events[0].state_before == "running"

    def test_needs_the_step_up(self, enabled, superuser_client):
        client, _ = superuser_client
        job = MaintenanceJob.objects.create(operation="tests.x", executor="celery", state="running", in_flight=True)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/abandon", {"password": "wrong"}).status_code == 400
        job.refresh_from_db()
        assert job.in_flight

    def test_a_host_job_is_refused_while_the_agent_is_alive(self, host_enabled, superuser_client):
        client, user = superuser_client
        _stale_agent(host_enabled, stale=False)
        job = _host_job(user, state="running")
        assert _post(client, f"{BASE}/jobs/{job.job_id}/abandon", {"password": PASSWORD}).status_code == 409

    def test_a_host_job_can_be_abandoned_once_the_agent_is_gone(self, host_enabled, superuser_client, write_status):
        client, user = superuser_client
        _stale_agent(host_enabled)
        job = _host_job(user, state="requested")
        spool.write_request(job)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/abandon", {"password": PASSWORD}).status_code == 200
        assert not spool.request_path(job.job_id).exists(), "a returning agent finds nothing to pick up"
        write_status(job.job_id, "running")
        spool.sync(force=True)
        job.refresh_from_db()
        assert job.state == "failed" and not job.in_flight, "a late status never puts it back in flight"

    def test_a_settled_job_is_409(self, enabled, superuser_client):
        client, _ = superuser_client
        job = MaintenanceJob.objects.create(operation="tests.x", executor="celery", state="succeeded", in_flight=False)
        assert _post(client, f"{BASE}/jobs/{job.job_id}/abandon", {"password": PASSWORD}).status_code == 409


@pytest.mark.django_db
class TestErasureAcknowledgement:
    def _record(self, when):
        from maintenance.erasures import record_erasure

        assert record_erasure(4242, when, at=when)

    def test_a_rollback_undoing_erasures_is_409_until_acknowledged(self, host_enabled, superuser_client):
        from datetime import UTC, datetime

        client, user = superuser_client
        job = _host_job(user, snapshot="pre-update-20260920-120000")
        self._record(datetime(2026, 9, 20, 12, 30, tzinfo=UTC))
        response = _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD})
        assert response.status_code == 409
        assert response.json()["reason"] == "erasures_since_snapshot" and response.json()["erasures"] == 1
        assert not spool.marker_path(job.job_id, "rollback").exists()
        response = _post(
            client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD, "acknowledge_erasures": True}
        )
        assert response.status_code == 200
        # The audit row keeps what was acknowledged, which the row cannot say
        # once the rollback has settled.
        from activity.models import Activity

        row = Activity.objects.filter(verb="maintenance.job.rollback").get()
        assert row.metadata == {"restores_database": True, "erasures_acknowledged": 1}

    def test_erasures_before_the_snapshot_do_not_count(self, host_enabled, superuser_client):
        from datetime import UTC, datetime

        client, user = superuser_client
        job = _host_job(user, snapshot="pre-update-20260920-120000")
        self._record(datetime(2026, 9, 20, 11, 0, tzinfo=UTC))
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 200

    def test_a_rollback_keeping_the_database_is_not_asked(self, host_enabled, superuser_client):
        from datetime import UTC, datetime

        client, user = superuser_client
        job = _host_job(user, snapshot="pre-update-20260920-120000", migrations_applied=False)
        self._record(datetime(2026, 9, 20, 12, 30, tzinfo=UTC))
        assert _post(client, f"{BASE}/jobs/{job.job_id}/rollback", {"password": PASSWORD}).status_code == 200

    def test_a_restore_operation_is_asked_too(self, host_enabled, superuser_client, django_capture_on_commit_callbacks):
        from datetime import UTC, datetime

        client, _ = superuser_client
        self._record(datetime(2026, 9, 21, 9, 0, tzinfo=UTC))
        body = {"operation": "platform.rollback", "args": {"snapshot": "pre-update-20260920-120000"}}
        response = _post(client, f"{BASE}/jobs", {**body, "password": PASSWORD})
        assert response.status_code == 409 and response.json()["erasures"] == 1
        assert not MaintenanceJob.objects.exists()
        body["args"]["restore_database"] = False
        assert _post(client, f"{BASE}/jobs", {**body, "password": PASSWORD}).status_code == 202
        MaintenanceJob.objects.all().delete()
        body["args"]["restore_database"] = True
        response = _post(client, f"{BASE}/jobs", {**body, "password": PASSWORD, "acknowledge_erasures": True})
        assert response.status_code == 202
