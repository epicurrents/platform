"""Django system checks for the operation registry and the maintenance settings.

The registry takes strings and validates nothing against the running
deployment: a celery-tier operation naming a management command that does not
exist registers fine and fails on first use, from a superuser's browser, with
the job marked failed and nothing to say why. The check resolves each command
at ``manage.py check`` instead, which runs before ``runserver`` and ``migrate``.
Same shape as ``user/checks.py``, and for the same reason: a check runs after
every app's ``ready()``, so it sees the operations a project or plugin added.
"""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.core.management import get_commands

from maintenance.operations import CELERY, forbidden_arg_fields, registered_operations


@register(Tags.compatibility)
def check_operation_registry(app_configs, **kwargs):
    """Every celery-tier operation names a real command; no argument schema smuggles a command in."""
    issues: list = []
    commands = get_commands()
    for operation in registered_operations():
        if operation.executor == CELERY and operation.command not in commands:
            issues.append(
                Error(
                    f"Maintenance operation {operation.key!r} names management command {operation.command!r}, "
                    "which is not installed.",
                    hint="The job would fail at its first request. Name an installed command or drop the registration.",
                    obj=operation.key,
                    id="maintenance.E001",
                )
            )
        forbidden = forbidden_arg_fields(operation.args_schema)
        if forbidden:
            issues.append(
                Error(
                    f"Maintenance operation {operation.key!r} takes argument(s) {sorted(forbidden)}.",
                    hint="An operation's command is a property of its registration, never of the request.",
                    obj=operation.key,
                    id="maintenance.E002",
                )
            )
    return issues


@register(Tags.compatibility)
def check_maintenance_settings(app_configs, **kwargs):
    """The host tier flag without the master flag is a configuration that does nothing."""
    issues: list = []
    if getattr(settings, "REMOTE_UPDATE_ENABLED", False) and not getattr(settings, "REMOTE_MAINTENANCE_ENABLED", False):
        issues.append(
            Warning(
                "REMOTE_UPDATE_ENABLED is on but REMOTE_MAINTENANCE_ENABLED is off, so the maintenance API is not mounted.",
                hint="Set REMOTE_MAINTENANCE_ENABLED=True as well, or turn REMOTE_UPDATE_ENABLED off.",
                id="maintenance.W001",
            )
        )
    return issues
