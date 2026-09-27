"""The operation registry: what a superuser may request, and what runs it.

An operation is a declaration, not a command string. It names the executor tier,
a typed argument schema the API validates the request body against, and — for
the celery tier — the management command plus a function that turns validated
arguments into argv. Nothing free-form reaches an executor: the API accepts only
a registered key, the celery executor runs only the command the registration
names, and the host agent accepts only the keys it allowlists itself.

Projects and plugins register their own operations from ``AppConfig.ready()``.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ninja import Field, Schema

CELERY = "celery"
HOST = "host"
EXECUTORS = (CELERY, HOST)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")

# Field names an argument schema must not carry. An operation's command is a
# property of its registration; a schema that lets the request name one turns
# the registry back into a command string with extra steps.
FORBIDDEN_ARG_FIELDS = frozenset({"command", "argv", "shell", "cmd", "script"})


def _no_args(args) -> list[str]:
    return []


def _schema_property_names(node) -> set[str]:
    """Every property name anywhere in a JSON schema: nested objects, array items, combinators and ``$defs``."""
    names: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(str(name) for name in properties)
        for value in node.values():
            names |= _schema_property_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _schema_property_names(item)
    return names


def forbidden_arg_fields(args_schema) -> set[str]:
    """The names in ``args_schema`` that match :data:`FORBIDDEN_ARG_FIELDS`, compared case-insensitively.

    Looks past the top level: a field's alias, a nested schema's properties, an
    array's items and every branch of ``anyOf`` / ``oneOf`` / ``allOf``, read off
    the JSON schema by field name and by alias. A ``Command`` field or a
    ``{"options": {"shell": ...}}`` object carries a command as surely as a
    top-level ``command`` does.
    """
    names: set[str] = set()
    for name, info in args_schema.model_fields.items():
        names.add(name)
        for alias in (info.alias, info.validation_alias, info.serialization_alias):
            if isinstance(alias, str):
                names.add(alias)
    # A schema pydantic cannot render raises here, at registration, rather than
    # later from the operations listing, which publishes the same JSON schema.
    for by_alias in (True, False):
        names |= _schema_property_names(args_schema.model_json_schema(by_alias=by_alias))
    return {name for name in names if name.lower() in FORBIDDEN_ARG_FIELDS}


@dataclass(frozen=True)
class Operation:
    """One registered operation.

    ``args_schema`` is a Ninja ``Schema``; the API validates the request's
    ``args`` object against it and publishes its JSON schema so the UI can build
    a form. ``command_args`` receives the validated schema instance and returns
    the argv the management command gets. ``requires_step_up`` asks for a fresh
    credential confirmation at request time; the default is on, and an operation
    turns it off only when it changes nothing. ``soft_time_limit`` is seconds.
    """

    key: str
    executor: str
    label: str
    description: str
    args_schema: type[Schema]
    command: str = ""
    command_args: Callable[[Schema], list[str]] = field(default=_no_args)
    requires_step_up: bool = True
    soft_time_limit: int = 3600


_REGISTRY: dict[str, Operation] = {}


def register_operation(operation: Operation) -> None:
    """Add ``operation`` to the registry, refusing anything malformed or already registered."""
    if not _KEY_RE.match(operation.key):
        raise ValueError(f"Operation key {operation.key!r} is not of the form 'scope.name'.")
    if operation.executor not in EXECUTORS:
        raise ValueError(f"Operation {operation.key!r} names executor {operation.executor!r}; use 'celery' or 'host'.")
    if operation.executor == CELERY and not operation.command:
        raise ValueError(f"Operation {operation.key!r} runs on the celery tier and must name a management command.")
    if operation.executor == HOST and operation.command:
        raise ValueError(
            f"Operation {operation.key!r} runs on the host tier; the agent decides what runs, not a command."
        )
    if not (isinstance(operation.args_schema, type) and issubclass(operation.args_schema, Schema)):
        raise TypeError(f"Operation {operation.key!r} must declare a ninja.Schema subclass as args_schema.")
    forbidden = forbidden_arg_fields(operation.args_schema)
    if forbidden:
        raise ValueError(f"Operation {operation.key!r} argument schema carries forbidden field(s) {sorted(forbidden)}.")
    if operation.key in _REGISTRY:
        raise ValueError(f"Operation {operation.key!r} is already registered.")
    _REGISTRY[operation.key] = operation


def get_operation(key: str) -> Operation | None:
    """Return the registered operation for ``key``, or ``None``."""
    return _REGISTRY.get(key)


def registered_operations() -> list[Operation]:
    """Every registered operation, ordered by key."""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


# ── Core operations ──────────────────────────────────────────────────────────


class NoArgs(Schema):
    """An operation that takes nothing."""


class VerifyAuditIntegrityArgs(Schema):
    """Arguments of ``activity.verify_audit_integrity``."""

    derived_window_days: int | None = Field(
        None,
        ge=0,
        le=3650,
        description="Days of derived-state rows to recompute; 0 skips that phase. Default: the deployment setting.",
    )


def _verify_audit_integrity_args(args: VerifyAuditIntegrityArgs) -> list[str]:
    if args.derived_window_days is None:
        return []
    return ["--derived-window-days", str(args.derived_window_days)]


class ValidateOriginalsArgs(Schema):
    """Arguments of ``recordings.validate_originals``."""

    check_sizes: bool = Field(True, description="Compare on-disk sizes with the manifest.")


def _validate_originals_args(args: ValidateOriginalsArgs) -> list[str]:
    argv = ["--json"]
    if not args.check_sizes:
        argv.append("--no-size-check")
    return argv


class RefreshSignalMetadataArgs(Schema):
    """Arguments of ``recordings.refresh_signal_metadata``."""

    dry_run: bool = Field(False, description="Report drifted recordings without writing anything.")


def _refresh_signal_metadata_args(args: RefreshSignalMetadataArgs) -> list[str]:
    return ["--dry-run"] if args.dry_run else []


class PlatformUpdateArgs(Schema):
    """Arguments of ``platform.update``: an already-uploaded package, by hash."""

    package_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$", description="sha256 of an uploaded package.")
    verify_window_minutes: int | None = Field(
        None,
        ge=5,
        le=1440,
        description="Minutes to wait for a superuser's confirmation before rolling back. Default: the deployment setting.",
    )


# A snapshot name as update.sh writes them: a label, a UTC date and a time. The
# agent reports the names and checks a request against the same shape.
SNAPSHOT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*-[0-9]{8}-[0-9]{6}$"


class PlatformRollbackArgs(Schema):
    """Arguments of ``platform.rollback``: a snapshot on the host, by name, and whether the database goes with it."""

    snapshot: str = Field(
        ...,
        pattern=SNAPSHOT_NAME_PATTERN,
        description="A snapshot on the host, by the name the agent reports.",
    )
    restore_database: bool = Field(
        True,
        description=(
            "Restore the database from the snapshot as well; everything written since it is lost. Off, the "
            "database is kept, which the agent allows only when no migration was applied since the snapshot."
        ),
    )


def register_core_operations() -> None:
    """Register the platform's own operations; called from ``MaintenanceConfig.ready``."""
    register_operation(
        Operation(
            key="activity.verify_audit_integrity",
            executor=CELERY,
            label="Verify audit-trail integrity",
            description="Walk every audit chain and recompute derived-state digests; reports any break, gap or mismatch.",
            args_schema=VerifyAuditIntegrityArgs,
            command="verify_audit_integrity",
            command_args=_verify_audit_integrity_args,
            requires_step_up=False,
        )
    )
    register_operation(
        Operation(
            key="recordings.validate_originals",
            executor=CELERY,
            label="Validate the originals volume",
            description="Compare the originals volume's manifests and file sizes with the recordings the database knows. Reads metadata only.",
            args_schema=ValidateOriginalsArgs,
            command="validate_originals",
            command_args=_validate_originals_args,
            requires_step_up=False,
        )
    )
    register_operation(
        Operation(
            key="recordings.refresh_signal_metadata",
            executor=CELERY,
            label="Refresh signal metadata",
            description="Re-derive recording and channel metadata from the files on disk for every recording that has drifted.",
            args_schema=RefreshSignalMetadataArgs,
            command="refresh_signal_metadata",
            command_args=_refresh_signal_metadata_args,
        )
    )
    register_operation(
        Operation(
            key="mail.send_test",
            executor=CELERY,
            label="Send a test email",
            description=(
                "Send a test message to every active superuser, to check that the configured relay accepts and "
                "delivers mail. Reports the relay, the sender and hashed recipients; no address is shown."
            ),
            args_schema=NoArgs,
            command="send_test_email",
            requires_step_up=False,
        )
    )
    register_operation(
        Operation(
            key="platform.update",
            executor=HOST,
            label="Update the platform",
            description="Apply an uploaded, signed package with a verification window and automatic rollback. Needs the host agent.",
            args_schema=PlatformUpdateArgs,
        )
    )
    register_operation(
        Operation(
            key="platform.backup",
            executor=HOST,
            label="Take a snapshot",
            description=(
                "Snapshot the code, the database and the configuration on the host, without interrupting the "
                "platform. The newest three are kept. Needs the host agent."
            ),
            args_schema=NoArgs,
        )
    )
    register_operation(
        Operation(
            key="platform.rollback",
            executor=HOST,
            label="Roll back to a snapshot",
            description=(
                "Restore a snapshot the host holds: the code, and the database unless it is kept. The platform is "
                "suspended while it runs. Needs the host agent."
            ),
            args_schema=PlatformRollbackArgs,
        )
    )
