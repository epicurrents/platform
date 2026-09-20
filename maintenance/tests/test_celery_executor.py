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

    def handle(self, *args, **options):
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

    def test_completion_notifies_and_records_it(self, echo, no_push, make_superuser):
        root = make_superuser()
        job = _job()
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
