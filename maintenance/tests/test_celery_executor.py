"""The celery-tier executor: what runs, under which scope, and what the row keeps."""

import pytest
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings
from ninja import Schema

from activity.audit import serialize_instance
from activity.models import Activity, ObjectChangeLog
from maintenance import operations
from maintenance.models import MaintenanceJob
from maintenance.operations import Operation, register_operation
from maintenance.tasks import run_job


class EchoArgs(Schema):
    """Arguments of the stub command."""

    count: int = 1
    fail: bool = False


def _echo_args(args: EchoArgs) -> list[str]:
    argv = ["--count", str(args.count)]
    if args.fail:
        argv.append("--fail")
    return argv


class EchoCommand(BaseCommand):
    """A stub that prints its arguments and can be told to fail."""

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1)
        parser.add_argument("--fail", action="store_true")
        parser.add_argument("--actor-id", type=int, default=None)

    def handle(self, *args, **options):
        if options["actor_id"] is not None:
            self.stdout.write(f"actor {options['actor_id']}")
        for i in range(options["count"]):
            self.stdout.write(f"line {i}")
        if options["fail"]:
            raise CommandError("told to fail")


@pytest.fixture
def echo(monkeypatch):
    """Register ``tests.echo`` on a fresh registry, backed by the stub command."""
    from django.core import management

    fresh: dict = {}
    monkeypatch.setattr(operations, "_REGISTRY", fresh)
    register_operation(
        Operation(
            key="tests.echo",
            executor="celery",
            label="Echo",
            description="Stub.",
            args_schema=EchoArgs,
            command="tests_echo",
            command_args=_echo_args,
        )
    )
    monkeypatch.setattr(management, "load_command_class", lambda app, name: EchoCommand())
    monkeypatch.setattr(management, "get_commands", lambda: {"tests_echo": "maintenance"})
    return fresh


@pytest.fixture
def echo_with_actor(echo):
    """``tests.echo`` registered to receive its requester through ``--actor-id``."""
    import dataclasses

    echo["tests.echo"] = dataclasses.replace(echo["tests.echo"], actor_arg="--actor-id")
    return echo


def _job(**fields):
    defaults = {"operation": "tests.echo", "executor": "celery", "args": {"count": 3}}
    defaults.update(fields)
    return MaintenanceJob.objects.create(**defaults)


@pytest.mark.django_db
class TestRunJob:
    def test_runs_the_registered_command_with_typed_argv(self, echo):
        job = _job()
        assert run_job(job.pk) == {"ran": True, "state": "succeeded"}
        job.refresh_from_db()
        assert job.state == "succeeded" and not job.in_flight
        assert job.output == "line 0\nline 1\nline 2\n"

    def test_an_operation_with_an_actor_argument_is_told_who_requested_it(self, echo_with_actor, user):
        job = _job(requested_by=user, args={"count": 1})
        run_job(job.pk)
        job.refresh_from_db()
        assert job.output == f"actor {user.pk}\nline 0\n"

    def test_an_operation_without_one_is_not(self, echo, user):
        job = _job(requested_by=user, args={"count": 1})
        run_job(job.pk)
        job.refresh_from_db()
        assert job.output == "line 0\n"
        assert job.started_at is not None and job.finished_at is not None

    def test_a_failing_command_marks_the_job_failed_with_its_output(self, echo):
        job = _job(args={"count": 1, "fail": True})
        run_job(job.pk)
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "command_failed"
        assert "line 0" in job.output and "told to fail" in job.output

    def test_output_is_tail_bounded(self, echo):
        job = _job(args={"count": 50})
        with override_settings(MAINTENANCE_JOB_OUTPUT_LIMIT=20):
            run_job(job.pk)
        job.refresh_from_db()
        assert job.output.startswith("…[truncated]…\n") and job.output.endswith("line 49\n")
        assert len(job.output) == len("…[truncated]…\n") + 20

    def test_runs_under_an_audited_scope_targeting_the_job(self, echo):
        job = _job()
        run_job(job.pk)
        row = Activity.objects.filter(verb="maintenance.job.run").get()
        assert row.interface == Activity.Interface.CELERY
        assert row.target_object_id == str(job.pk)
        assert row.metadata == {}

    def test_the_transitions_are_in_the_change_log_with_output_masked(self, echo):
        job = _job()
        run_job(job.pk)
        rows = list(ObjectChangeLog.objects.filter(object_id=str(job.pk), action="modify").order_by("pk"))
        assert [r.changes["state"]["to"] for r in rows] == ["running", "succeeded"]
        final = rows[-1]
        assert "line 0" not in str(final.changes) and "line 0" not in str(final.before_state)
        job.refresh_from_db()
        assert job.output and serialize_instance(job)["output"] != job.output, "the mask applies to the payload"

    def test_a_cancelled_job_is_not_run(self, echo):
        job = _job(state="cancelled", in_flight=False)
        assert run_job(job.pk) == {"ran": False}
        job.refresh_from_db()
        assert job.state == "cancelled" and job.output == ""

    def test_a_host_job_is_never_run_here(self, echo):
        job = _job(executor="host", operation="platform.update", args={})
        assert run_job(job.pk) == {"ran": False}
        job.refresh_from_db()
        assert job.state == "requested"

    def test_an_unregistered_operation_is_refused(self, echo):
        job = _job(operation="tests.gone")
        assert run_job(job.pk) == {"ran": False}
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "refused_operation"

    def test_bad_arguments_on_the_row_fail_the_job_rather_than_the_worker(self, echo):
        job = _job(args={"count": "many"})
        run_job(job.pk)
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "command_failed"

    def test_completion_notifies_and_records_it(
        self, echo, no_push, make_superuser, django_capture_on_commit_callbacks
    ):
        root = make_superuser()
        job = _job()
        with django_capture_on_commit_callbacks(execute=True):
            run_job(job.pk)
        job.refresh_from_db()
        assert job.last_notified_state == "succeeded"
        assert [n["user_id"] for n in no_push] == [root.pk]


@pytest.mark.django_db
class TestTransition:
    def test_a_stale_expectation_does_not_win(self):
        job = _job()
        MaintenanceJob.objects.filter(pk=job.pk).update(state="cancelled", in_flight=False)
        assert job.transition(expect=["requested"], state="running") is False
        job.refresh_from_db()
        assert job.state == "cancelled"

    def test_in_flight_follows_the_state(self):
        job = _job()
        assert job.transition(expect=["requested"], state="succeeded", in_flight=True)
        job.refresh_from_db()
        assert job.in_flight is False


def test_the_audit_integrity_command_exists_and_reports():
    from django.core.management import get_commands

    assert get_commands()["verify_audit_integrity"] == "activity"


class ExitingCommand(BaseCommand):
    """A stub that exits the way ``validate_originals`` does."""

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1)
        parser.add_argument("--fail", action="store_true")

    def handle(self, *args, **options):
        import sys

        self.stdout.write("checked")
        sys.exit(1 if options["fail"] else 0)


@pytest.fixture
def exiting(echo, monkeypatch):
    from django.core import management

    monkeypatch.setattr(management, "load_command_class", lambda app, name: ExitingCommand())
    return echo


@pytest.mark.django_db
class TestEveryOutcomeIsRecorded:
    def test_a_non_zero_exit_fails_the_job_instead_of_leaving_it_running(self, exiting):
        job = _job(args={"fail": True})
        run_job(job.pk)
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "command_failed" and not job.in_flight
        assert "exited with status 1" in job.output

    def test_a_zero_exit_is_a_success(self, exiting):
        job = _job()
        run_job(job.pk)
        job.refresh_from_db()
        assert job.state == "succeeded" and "checked" in job.output

    def test_an_interrupt_is_recorded_and_re_raised(self, echo, monkeypatch):
        from maintenance import tasks

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(tasks, "call_command", interrupted)
        job = _job()
        with pytest.raises(KeyboardInterrupt):
            run_job(job.pk)
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "interrupted"

    def test_nothing_starts_while_the_maintenance_flag_is_up(self, echo, write_flag):
        write_flag("verifying")
        job = _job()
        assert run_job(job.pk) == {"ran": False}
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "maintenance_lock"
        assert "line 0" not in job.output


@pytest.mark.django_db
class TestReaper:
    def _aged(self, state, *, minutes):
        from datetime import timedelta

        from django.utils import timezone

        then = timezone.now() - timedelta(minutes=minutes)
        return _job(state=state, in_flight=True, created_at=then, started_at=then if state == "running" else None)

    def test_a_job_past_its_limit_is_failed_as_stale(self, echo, caplog):
        from maintenance.tasks import reap_stale_jobs

        job = self._aged("running", minutes=60 + 20)
        assert reap_stale_jobs() == 1
        job.refresh_from_db()
        assert job.state == "failed" and job.reason == "stale" and not job.in_flight
        assert Activity.objects.filter(verb="maintenance.job.reap").exists()

    def test_a_request_no_worker_picked_up_is_reaped_too(self, echo):
        from maintenance.tasks import reap_stale_jobs

        job = self._aged("requested", minutes=60 + 20)
        assert reap_stale_jobs() == 1
        job.refresh_from_db()
        assert job.reason == "stale"

    def test_a_job_within_its_limit_is_left_alone(self, echo):
        from maintenance.tasks import reap_stale_jobs

        job = self._aged("running", minutes=30)
        assert reap_stale_jobs() == 0
        job.refresh_from_db()
        assert job.state == "running"

    def test_host_jobs_are_never_reaped_here(self, echo):
        from datetime import timedelta

        from django.utils import timezone

        from maintenance.tasks import reap_stale_jobs

        MaintenanceJob.objects.create(
            operation="platform.update",
            executor="host",
            state="running",
            in_flight=True,
            created_at=timezone.now() - timedelta(days=1),
        )
        assert reap_stale_jobs() == 0

    def test_the_beat_task_reaps_and_does_nothing_while_updating(self, echo, spool_dir, write_flag):
        from maintenance.tasks import sync_spool

        job = self._aged("running", minutes=60 + 20)
        write_flag("updating")
        assert sync_spool() == {"skipped": "locked"}
        job.refresh_from_db()
        assert job.state == "running"
        write_flag("verifying")
        assert sync_spool()["stale"] == 1
