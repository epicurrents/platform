"""The operation registry and the system checks over it."""

import pytest
from django.core.checks import Error, Warning
from django.test import override_settings
from ninja import Schema
from pydantic import ValidationError

from maintenance import operations
from maintenance.checks import check_maintenance_settings, check_operation_registry
from maintenance.operations import Operation, get_operation, register_operation, registered_operations


class Args(Schema):
    """A harmless argument schema."""

    flag: bool = False


class BadArgs(Schema):
    """A schema that lets the request name a command."""

    command: str = ""


@pytest.fixture
def registry(monkeypatch):
    """An empty registry for the duration of the test."""
    fresh: dict = {}
    monkeypatch.setattr(operations, "_REGISTRY", fresh)
    return fresh


def _op(**overrides):
    fields = {
        "key": "tests.noop",
        "executor": "celery",
        "label": "Noop",
        "description": "Nothing.",
        "args_schema": Args,
        "command": "check",
    }
    fields.update(overrides)
    return Operation(**fields)


class TestRegisterOperation:
    def test_registers_and_lists_sorted(self, registry):
        register_operation(_op(key="tests.b"))
        register_operation(_op(key="tests.a"))
        assert [op.key for op in registered_operations()] == ["tests.a", "tests.b"]
        assert get_operation("tests.a").command == "check"
        assert get_operation("tests.missing") is None

    def test_a_duplicate_is_refused(self, registry):
        register_operation(_op())
        with pytest.raises(ValueError, match="already registered"):
            register_operation(_op())

    @pytest.mark.parametrize("key", ["noop", "Tests.noop", "tests.", "tests..noop", "tests.no-op", "1tests.noop"])
    def test_a_malformed_key_is_refused(self, registry, key):
        with pytest.raises(ValueError, match="not of the form"):
            register_operation(_op(key=key))

    def test_an_unknown_executor_is_refused(self, registry):
        with pytest.raises(ValueError, match="executor"):
            register_operation(_op(executor="shell"))

    def test_a_celery_operation_needs_a_command_and_a_host_one_must_not_have_one(self, registry):
        with pytest.raises(ValueError, match="must name a management command"):
            register_operation(_op(command=""))
        with pytest.raises(ValueError, match="agent decides"):
            register_operation(_op(executor="host", command="update"))

    def test_the_args_schema_must_be_a_schema(self, registry):
        with pytest.raises(TypeError):
            register_operation(_op(args_schema=dict))

    def test_a_schema_with_a_command_field_is_refused(self, registry):
        with pytest.raises(ValueError, match="forbidden field"):
            register_operation(_op(args_schema=BadArgs))


class TestCoreOperations:
    def test_the_core_set_is_registered_at_boot(self):
        keys = {op.key for op in registered_operations()}
        assert {
            "activity.verify_audit_integrity",
            "recordings.validate_originals",
            "recordings.refresh_signal_metadata",
            "recordings.deidentification_report",
            "epicurrents.grant_assessments",
            "library.release_dataset",
            "library.dataset_access_report",
            "library.dataset_anonymity_report",
            "platform.update",
            "platform.backup",
            "platform.rollback",
        } <= keys

    def test_the_snapshot_operations_take_a_name_of_the_agents_shape_and_nothing_else(self):
        op = get_operation("platform.backup")
        assert op.executor == "host" and op.requires_step_up and not op.args_schema.model_fields
        op = get_operation("platform.rollback")
        assert op.executor == "host" and op.requires_step_up
        args = op.args_schema(snapshot="backup-20260901-100000")
        assert args.restore_database is True
        for bad in ("../x", "backup", "backup-2026-09-01", "backup-20260901-100000/", "x y-20260901-100000"):
            with pytest.raises(ValidationError):
                op.args_schema(snapshot=bad)

    def test_celery_operations_turn_arguments_into_argv(self):
        op = get_operation("activity.verify_audit_integrity")
        assert op.command_args(op.args_schema()) == []
        assert op.command_args(op.args_schema(derived_window_days=0)) == ["--derived-window-days", "0"]
        op = get_operation("recordings.validate_originals")
        assert op.command_args(op.args_schema(check_sizes=False)) == ["--json", "--no-size-check"]
        op = get_operation("recordings.refresh_signal_metadata")
        assert op.command_args(op.args_schema(dry_run=True)) == ["--dry-run"]
        op = get_operation("library.dataset_access_report")
        assert op.command_args(op.args_schema(dataset="A" * 32)) == ["A" * 32, "--format", "json", "--days", "183"]
        with pytest.raises(ValidationError):
            op.args_schema(dataset="A" * 32, days=0)
        op = get_operation("library.dataset_anonymity_report")
        assert op.command_args(op.args_schema(dataset="A" * 32)) == ["A" * 32, "--format", "json"]
        assert op.command_args(op.args_schema(dataset="A" * 32, release=7)) == [
            "A" * 32,
            "--format",
            "json",
            "--release",
            "7",
        ]
        with pytest.raises(ValidationError):
            op.args_schema(dataset="not-a-hash")

    def test_the_update_operation_takes_a_hash_and_a_bounded_window(self):
        op = get_operation("platform.update")
        assert op.executor == "host" and op.requires_step_up
        with pytest.raises(ValidationError):
            op.args_schema(package_sha256="nope")
        with pytest.raises(ValidationError):
            op.args_schema(package_sha256="a" * 64, verify_window_minutes=2)
        assert op.args_schema(package_sha256="a" * 64, verify_window_minutes=30).verify_window_minutes == 30

    def test_the_read_only_operations_skip_step_up_and_the_writing_one_does_not(self):
        assert not get_operation("activity.verify_audit_integrity").requires_step_up
        assert not get_operation("recordings.validate_originals").requires_step_up
        assert not get_operation("library.dataset_access_report").requires_step_up
        assert not get_operation("library.dataset_anonymity_report").requires_step_up
        assert get_operation("recordings.refresh_signal_metadata").requires_step_up


class TestChecks:
    def test_core_registrations_pass(self):
        assert check_operation_registry(None) == []

    def test_a_missing_command_is_an_error(self, registry):
        register_operation(_op(command="no_such_command_anywhere"))
        issues = check_operation_registry(None)
        assert [issue.id for issue in issues] == ["maintenance.E001"]
        assert isinstance(issues[0], Error)

    def test_a_forbidden_field_is_an_error_even_when_smuggled_past_registration(self, registry):
        registry["tests.bad"] = _op(key="tests.bad", args_schema=BadArgs)
        issues = check_operation_registry(None)
        assert "maintenance.E002" in [issue.id for issue in issues]

    def test_host_tier_without_the_master_flag_warns(self):
        with override_settings(REMOTE_MAINTENANCE_ENABLED=False, REMOTE_UPDATE_ENABLED=True):
            issues = check_maintenance_settings(None)
        assert [issue.id for issue in issues] == ["maintenance.W001"]
        assert isinstance(issues[0], Warning)
        with override_settings(REMOTE_MAINTENANCE_ENABLED=True, REMOTE_UPDATE_ENABLED=True):
            assert check_maintenance_settings(None) == []

    def test_the_checks_are_registered_from_ready(self):
        from pathlib import Path

        from django.core.checks import registry as check_registry

        names = {check.__name__ for check in check_registry.registry.registered_checks}
        assert {"check_operation_registry", "check_maintenance_settings"} <= names
        apps_source = Path(__file__).resolve().parent.parent.joinpath("apps.py").read_text()
        assert "from . import checks" in apps_source


class Nested(Schema):
    """An object argument whose own field would carry a command."""

    shell: str = ""


class TestForbiddenFieldsAnyDepth:
    """A command field is refused wherever it hides: alias, nesting, array items, combinators, any case."""

    def _refuses(self, registry, schema):
        with pytest.raises(ValueError, match="forbidden"):
            register_operation(
                Operation(
                    key="tests.bad", executor="celery", label="x", description="x", args_schema=schema, command="c"
                )
            )

    def test_an_alias(self, registry):
        from pydantic import Field

        class Aliased(Schema):
            harmless: str = Field("", alias="command")

        self._refuses(registry, Aliased)

    def test_a_different_case(self, registry):
        class Shouting(Schema):
            Command: str = ""

        self._refuses(registry, Shouting)

    def test_a_nested_object(self, registry):
        class Outer(Schema):
            options: Nested = Nested()

        self._refuses(registry, Outer)

    def test_array_items_and_unions(self, registry):
        class Listed(Schema):
            steps: list[Nested] = []

        class Either(Schema):
            choice: Nested | int = 0

        self._refuses(registry, Listed)
        self._refuses(registry, Either)

    def test_the_check_finds_the_same(self, registry):
        class Outer(Schema):
            options: Nested = Nested()

        registry["tests.bad"] = Operation(
            key="tests.bad", executor="celery", label="x", description="x", args_schema=Outer, command="check"
        )
        assert any(issue.id == "maintenance.E002" for issue in check_operation_registry(None))
