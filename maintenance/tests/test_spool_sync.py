"""Projecting the spool onto the job rows, including after a database restore erased them."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from activity.models import Activity, ObjectChangeLog
from maintenance import spool
from maintenance.models import MaintenanceJob, MaintenancePackage
from maintenance.tests.conftest import place_package_files


def _host_job(user=None, state="requested", **fields):
    return MaintenanceJob.objects.create(
        operation="platform.update",
        executor="host",
        requested_by=user,
        state=state,
        in_flight=state in MaintenanceJob.IN_FLIGHT_STATES,
        **fields,
    )


@pytest.mark.django_db
class TestApplyStatus:
    def test_a_newer_status_is_applied(self, spool_dir, superuser, write_status, no_push):
        job = _host_job(superuser)
        write_status(
            job.job_id,
            "running",
            step="build",
            started_at="2026-09-20T10:01:00Z",
            installed_version_before="0.1.1",
            target_version="0.1.2",
            agent_version="1",
            snapshot="pre-update-20260920-100100",
        )
        counts = spool.sync(force=True)
        assert counts["applied"] == 1
        job.refresh_from_db()
        assert job.state == "running" and job.in_flight and job.step == "build"
        assert job.started_at.isoformat() == "2026-09-20T10:01:00+00:00"
        assert job.snapshot == "pre-update-20260920-100100" and job.agent_version == "1"
        assert job.spool_updated_at.isoformat() == "2026-09-20T10:05:00+00:00"

    def test_an_older_status_is_ignored(self, spool_dir, superuser, write_status, no_push):
        job = _host_job(superuser, state="running", spool_updated_at=timezone.now())
        write_status(job.job_id, "failed", updated_at="2020-01-01T00:00:00Z")
        assert spool.sync(force=True)["applied"] == 0
        job.refresh_from_db()
        assert job.state == "running"

    def test_a_terminal_status_clears_in_flight_and_stamps_finished(self, spool_dir, superuser, write_status, no_push):
        job = _host_job(superuser, state="running")
        write_status(job.job_id, "rolled_back", reason="deadline", finished_at="2026-09-20T10:40:00Z")
        spool.sync(force=True)
        job.refresh_from_db()
        assert job.state == "rolled_back" and not job.in_flight and job.reason == "deadline"
        assert job.finished_at is not None

    def test_an_unknown_state_or_missing_timestamp_is_ignored(self, spool_dir, superuser, write_status, no_push):
        job = _host_job(superuser)
        write_status(job.job_id, "exploded")
        assert spool.sync(force=True)["applied"] == 0
        spool.write_json_atomic(spool.status_path(job.job_id), {"protocol": 1, "state": "running"})
        assert spool.sync(force=True)["applied"] == 0
        job.refresh_from_db()
        assert job.state == "requested"

    def test_a_newer_protocol_is_refused(self, spool_dir, superuser, write_status, no_push):
        job = _host_job(superuser)
        write_status(job.job_id, "running", protocol=2)
        assert spool.sync(force=True)["applied"] == 0
        job.refresh_from_db()
        assert job.state == "requested"

    def test_nothing_to_do_opens_no_audited_scope(self, spool_dir, superuser, no_push):
        _host_job(superuser)
        before = Activity.objects.count()
        assert spool.sync(force=True) == {
            "applied": 0,
            "created": 0,
            "orphaned": 0,
            "notified": 0,
            "skipped": False,
            "packages": {"created": 0, "pruned": 0, "swept": 0},
        }
        assert Activity.objects.count() == before

    def test_a_change_is_audited_under_the_sync_verb(self, spool_dir, superuser, write_status, no_push):
        job = _host_job(superuser)
        write_status(job.job_id, "running")
        spool.sync(force=True)
        row = Activity.objects.filter(verb="maintenance.job.sync").get()
        assert row.interface == Activity.Interface.CELERY
        assert row.metadata == {"applied": 1, "created": 0, "orphaned": 0}
        assert ObjectChangeLog.objects.filter(object_id=str(job.pk), action="modify").exists()

    def test_the_cache_lock_coalesces_callers(self, spool_dir, superuser, write_status, no_push):
        job = _host_job(superuser)
        write_status(job.job_id, "running")
        assert spool.sync()["applied"] == 1
        write_status(job.job_id, "succeeded", updated_at="2026-09-20T10:06:00Z")
        assert spool.sync()["skipped"] is True
        assert spool.sync(force=True)["applied"] == 1


@pytest.mark.django_db
class TestRestore:
    def test_rows_are_recreated_from_the_spool_after_a_database_restore(
        self, spool_dir, superuser, write_status, no_push
    ):
        place_package_files("d" * 64, "0.2.0")
        package = MaintenancePackage.objects.create(sha256="d" * 64, version="0.2.0")
        job = _host_job(superuser, args={"package_sha256": package.sha256}, package=package)
        spool.write_request(job)
        write_status(
            job.job_id, "awaiting_verification", verify_deadline="2026-09-20T10:35:00Z", target_version="0.2.0"
        )
        job_id, requested_at = job.job_id, json.loads(spool.request_path(job.job_id).read_text())["requested_at"]
        # The restore: every row written after the dump is gone.
        MaintenanceJob.objects.all().delete()

        counts = spool.sync(force=True)
        assert counts["created"] == 1
        recreated = MaintenanceJob.objects.get(job_id=job_id)
        assert recreated.requested_by == superuser and recreated.package == package
        assert recreated.operation == "platform.update" and recreated.executor == "host"
        assert recreated.state == "awaiting_verification" and recreated.in_flight
        assert recreated.verify_deadline.isoformat() == "2026-09-20T10:35:00+00:00"
        assert recreated.created_at.isoformat().replace("+00:00", "Z") == requested_at
        assert recreated.last_notified_state == "awaiting_verification"

    def test_a_request_for_a_deleted_user_still_comes_back(self, spool_dir, make_user, no_push):
        user = make_user()
        job = _host_job(user)
        spool.write_request(job)
        MaintenanceJob.objects.all().delete()
        user.delete()
        spool.sync(force=True)
        assert MaintenanceJob.objects.get(job_id=job.job_id).requested_by is None

    def test_a_second_in_flight_row_is_not_recreated(self, spool_dir, superuser, no_push):
        job = _host_job(superuser)
        spool.write_request(job)
        MaintenanceJob.objects.all().delete()
        _host_job(superuser, state="running")
        counts = spool.sync(force=True)
        assert counts["created"] == 0
        assert MaintenanceJob.objects.count() == 1


@pytest.mark.django_db
class TestOrphans:
    def test_an_old_in_flight_row_with_no_files_is_failed_as_orphaned(self, spool_dir, superuser, no_push):
        job = _host_job(superuser, state="running", created_at=timezone.now() - timedelta(minutes=5))
        assert spool.sync(force=True)["orphaned"] == 1
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "orphaned" and not job.in_flight

    def test_a_fresh_row_is_left_alone_for_its_on_commit_write(self, spool_dir, superuser, no_push):
        job = _host_job(superuser)
        assert spool.sync(force=True)["orphaned"] == 0
        job.refresh_from_db()
        assert job.state == "requested"

    def test_celery_rows_are_never_orphaned_by_the_spool(self, spool_dir, no_push):
        job = MaintenanceJob.objects.create(
            operation="a.b", executor="celery", state="running", created_at=timezone.now() - timedelta(hours=1)
        )
        assert spool.sync(force=True)["orphaned"] == 0
        job.refresh_from_db()
        assert job.state == "running"


@pytest.mark.django_db
class TestNotifications:
    def test_each_attention_state_notifies_every_superuser_once(
        self, spool_dir, make_superuser, make_user, write_status, no_push
    ):
        first, second = make_superuser(), make_superuser()
        make_user()
        job = _host_job(first)
        write_status(job.job_id, "running")
        spool.sync(force=True)
        assert no_push == [], "running is not an attention state"
        write_status(job.job_id, "awaiting_verification", updated_at="2026-09-20T10:06:00Z", target_version="0.1.2")
        spool.sync(force=True)
        assert {n["user_id"] for n in no_push} == {first.pk, second.pk}
        assert all(n["data"] == {"type": "maintenance", "job_id": str(job.job_id)} for n in no_push)
        assert "0.1.2" in no_push[0]["title"]
        write_status(job.job_id, "awaiting_verification", updated_at="2026-09-20T10:07:00Z", step="health")
        spool.sync(force=True)
        assert len(no_push) == 2, "the same state again is not announced again"

    def test_mail_goes_out_when_a_backend_is_configured(self, spool_dir, superuser, write_status, no_push, settings):
        from django.core import mail

        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        superuser.email = "root@example.org"
        superuser.save()
        job = _host_job(superuser)
        write_status(job.job_id, "rollback_failed")
        spool.sync(force=True)
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["root@example.org"]
        assert "shell" in mail.outbox[0].body

    def test_no_mail_through_the_console_backend(self, spool_dir, superuser, write_status, no_push, settings):
        from django.core import mail

        settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
        superuser.email = "root@example.org"
        superuser.save()
        job = _host_job(superuser)
        write_status(job.job_id, "failed")
        spool.sync(force=True)
        assert mail.outbox == []

    def test_state_changes_reach_the_security_log(self, spool_dir, superuser, write_status, no_push, caplog):
        import logging

        caplog.set_level(logging.WARNING, logger="epicurrents.security")
        job = _host_job(superuser)
        write_status(job.job_id, "failed", reason="refused_signature")
        spool.sync(force=True)
        events = [r for r in caplog.records if getattr(r, "security_event_type", "") == "maintenance.job_state"]
        assert len(events) == 1
        assert events[0].state == "failed" and events[0].reason == "refused_signature"


@pytest.mark.django_db
class TestSpoolFiles:
    def test_writes_are_atomic_and_leave_no_temporary_file(self, spool_dir, superuser):
        job = _host_job(superuser)
        spool.write_request(job)
        spool.write_marker(job, "verify", by_user_id=superuser.pk)
        names = sorted(p.name for p in spool.jobs_dir().iterdir())
        assert names == [f"{job.job_id}.json", f"{job.job_id}.verify"]

    def test_files_with_unusable_names_are_skipped(self, spool_dir, no_push):
        spool.jobs_dir().mkdir()
        (spool.jobs_dir() / "notes.json").write_text('{"protocol": 1}')
        (spool.jobs_dir() / "x.status.json").write_text('{"protocol": 1}')
        assert spool.sync(force=True)["created"] == 0

    def test_the_agent_summary(self, spool_dir):
        assert spool.agent_summary()["installed"] is False
        spool.write_json_atomic(
            spool.spool_path() / "agent.json",
            {"protocol": 1, "version": "1", "enabled": True, "runtime": "docker", "last_run": "2020-01-01T00:00:00Z"},
        )
        summary = spool.agent_summary()
        assert summary["installed"] and summary["enabled"] and summary["stale"] is True
        spool.write_json_atomic(
            spool.spool_path() / "agent.json",
            {"protocol": 1, "version": "1", "enabled": False, "last_run": spool.now_iso()},
        )
        assert spool.agent_summary()["stale"] is False

    def test_prune_removes_the_files_of_long_finished_jobs_only(self, spool_dir, superuser):
        from maintenance.tasks import prune_spool

        old = _host_job(superuser, state="succeeded", finished_at=timezone.now() - timedelta(days=40))
        recent = _host_job(superuser, state="succeeded", finished_at=timezone.now() - timedelta(days=1))
        for job in (old, recent):
            spool.write_request(job)
            spool.log_path(job.job_id).write_text("log")
        assert prune_spool() == {"removed": 2}
        assert not spool.request_path(old.job_id).exists()
        assert spool.request_path(recent.job_id).exists() and spool.log_path(recent.job_id).exists()
        assert MaintenanceJob.objects.count() == 2, "rows are the audit record and stay"


@pytest.mark.django_db
class TestCleanSlateFindings:
    def test_a_status_that_would_put_a_second_job_in_flight_is_skipped_not_fatal(
        self, spool_dir, superuser, write_status, no_push
    ):
        stale = _host_job(superuser, state="failed", reason="orphaned", finished_at=timezone.now())
        live = MaintenanceJob.objects.create(operation="a.b", executor="celery", state="running")
        write_status(stale.job_id, "running")
        counts = spool.sync(force=True)
        assert counts["applied"] == 0
        stale.refresh_from_db()
        live.refresh_from_db()
        assert stale.state == "failed" and live.state == "running"

    def test_the_sync_writes_only_the_fields_a_status_owns(self, spool_dir, superuser, write_status, no_push):
        job = _host_job(superuser, state="awaiting_verification")
        write_status(job.job_id, "awaiting_verification", step="health")
        stamp = timezone.now()
        # A verify request lands between the sync loading the row and saving it.
        MaintenanceJob.objects.filter(pk=job.pk).update(verify_requested_at=stamp)
        spool.sync(force=True)
        job.refresh_from_db()
        assert job.step == "health" and job.verify_requested_at == stamp

    def test_only_in_flight_and_named_rows_are_loaded(
        self, spool_dir, superuser, write_status, no_push, django_assert_num_queries
    ):
        for _ in range(3):
            _host_job(superuser, state="succeeded", finished_at=timezone.now())
        assert spool.sync(force=True)["applied"] == 0
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            spool.sync(force=True)
        sql = " ".join(q["sql"] for q in captured.captured_queries)
        assert '"in_flight"' in sql and "job_id" in sql
