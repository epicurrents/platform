"""Management command: activate_project

Activates a project by ensuring its database tables exist and are in place.
Two activation modes are supported:

``--restore`` (default)
    Rename archived tables (``_archived_<name>_*``) back to their original
    names, then run ``migrate`` to apply any pending migrations.  Use this
    when switching back to a project that was previously deactivated with
    ``deactivate_project``.

``--fresh``
    Leave any archived tables untouched (they remain available for manual
    recovery).  Clear the project's migration history from
    ``django_migrations`` so that ``migrate`` recreates all tables from
    scratch.  Use this when you want a clean slate for the project.

Prerequisites
-------------
``EPICURRENTS_PROJECT`` **must be set to the project name** in the
environment (or ``.env``) before running this command.  The settings loader
uses this variable to add the project app to ``INSTALLED_APPS`` so that
``migrate`` is aware of the project's migrations.

The server must not be running while this command executes.

Examples
--------
::

    EPICURRENTS_PROJECT=clinic_eeg python manage.py activate_project clinic_eeg
    EPICURRENTS_PROJECT=clinic_eeg python manage.py activate_project clinic_eeg --fresh
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from epicurrents.project_loader import (
    _BASE_DIR,
    ARCHIVE_PREFIX,
    get_active_project,
    get_project_state,
    rename_db_table,
    set_project_state,
)


class Command(BaseCommand):
    help = "Activate a project, restoring its archived tables or starting fresh."

    def add_arguments(self, parser):
        parser.add_argument(
            "name",
            help="Project name — must match a directory under projects/ and EPICURRENTS_PROJECT.",
        )
        parser.add_argument(
            "--fresh",
            action="store_true",
            default=False,
            help=(
                "Start with empty tables. Archived tables (if any) are preserved "
                "but a new schema is created from scratch. Cannot be undone without "
                "manually restoring the archived tables."
            ),
        )

    def handle(self, *args, **options):
        name = options["name"]

        # Validate project directory. A project lives in its own repository, so
        # an absent directory usually means it has not been cloned yet rather
        # than that the name is wrong — say both, since the remedy differs.
        project_dir = _BASE_DIR / "projects" / name
        if not project_dir.is_dir():
            raise CommandError(
                f"projects/{name}/ does not exist. Projects live in their own repositories: clone it "
                f"into projects/{name}/ first, or set EPICURRENTS_PROJECT_REPO in .env and let "
                "scripts/bootstrap.sh do it. If you meant a different project, check the spelling "
                "against the directories under projects/."
            )

        # Validate that EPICURRENTS_PROJECT matches so the project app is loaded.
        active = get_active_project()
        if active != name:
            raise CommandError(
                f"EPICURRENTS_PROJECT is set to {active!r}, not {name!r}. "
                f"Set EPICURRENTS_PROJECT={name} in your environment before running this command "
                "so the project app is included in INSTALLED_APPS."
            )

        if options["fresh"]:
            self._activate_fresh(name)
        else:
            self._activate_restore(name)

        # Update state file.
        state = get_project_state()
        state[name] = {"status": "active", "archived_at": None}
        set_project_state(state)
        self.stdout.write(self.style.SUCCESS(f"Project {name!r} is now active."))

    # ── Restore mode ──────────────────────────────────────────────────────────

    def _activate_restore(self, name: str) -> None:
        """Rename archived tables back to their original names, then migrate.

        Archive detection assumes the project's tables follow Django's
        default ``<app_label>_<model>`` naming.  A project that overrides
        ``Meta.db_table`` to a name that does NOT start with the project's
        app label will produce archived tables this prefix match misses,
        and the restore will silently leave them archived.  Operators
        using custom ``db_table`` should restore those tables manually
        via SQL ``ALTER TABLE`` before running ``activate_project``.
        """
        with connection.cursor() as cursor:
            all_tables = connection.introspection.table_names(cursor)

        prefix = f"{ARCHIVE_PREFIX}{name}_"
        archived = [t for t in all_tables if t.startswith(prefix)]

        if archived:
            self.stdout.write(f"Restoring {len(archived)} archived table(s) for project {name!r}…")
            with connection.cursor() as cursor:
                for table in archived:
                    # Strip the leading ARCHIVE_PREFIX to get the original name.
                    original = table[len(ARCHIVE_PREFIX) :]
                    rename_db_table(cursor, table, original)
                    self.stdout.write(f"  {table}  →  {original}")
        else:
            state = get_project_state().get(name, {})
            if state.get("status") == "archived":
                self.stderr.write(
                    self.style.WARNING(
                        f"State file says {name!r} is archived but no archived tables were found. "
                        "Running migrate — tables will be created from scratch."
                    )
                )
            else:
                self.stdout.write(
                    f"No archived tables found for {name!r}. Running migrate to create tables (first activation)."
                )

        self.stdout.write("Running migrate…")
        call_command("migrate", verbosity=1)

    # ── Fresh mode ────────────────────────────────────────────────────────────

    def _activate_fresh(self, name: str) -> None:
        """Clear migration history for the project app, then recreate tables."""
        # The app label is the last segment of the dotted app name.
        app_label = name

        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM django_migrations WHERE app = %s", [app_label])
            deleted = cursor.rowcount

        if deleted:
            self.stdout.write(f"Cleared {deleted} migration record(s) for app {app_label!r}.")
        else:
            self.stdout.write(f"No migration records found for {app_label!r} (first activation or already clear).")

        self.stdout.write("Running migrate to create fresh tables…")
        call_command("migrate", verbosity=1)
