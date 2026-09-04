"""Management command: remove_project_data

Permanently drops the archived tables for a project.  This is irreversible —
use it only when you are certain the archived data is no longer needed.

To prevent accidental data loss the command requires you to type the project
name as a confirmation prompt.  Pass ``--yes`` to skip the prompt in
non-interactive scripts (use with care).

Only archived tables (prefixed with ``_archived_<name>_``) are affected.
Live tables (i.e. those belonging to a currently active project) are never
touched by this command.

Example
-------
::

    python manage.py remove_project_data clinic_eeg
    # → prompts: Type the project name to confirm: clinic_eeg

    python manage.py remove_project_data clinic_eeg --yes   # non-interactive
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from epicurrents.project_loader import (
    ARCHIVE_PREFIX,
    get_project_state,
    set_project_state,
)


class Command(BaseCommand):
    help = "Permanently drop archived tables for a project. IRREVERSIBLE."

    def add_arguments(self, parser):
        parser.add_argument(
            "name",
            help="Project whose archived tables should be dropped.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            default=False,
            help="Skip the interactive confirmation prompt.",
        )

    def handle(self, *args, **options):
        name = options["name"]
        skip_confirm = options["yes"]

        # Discover archived tables.
        prefix = f"{ARCHIVE_PREFIX}{name}_"
        with connection.cursor() as cursor:
            all_tables = connection.introspection.table_names(cursor)

        archived = [t for t in all_tables if t.startswith(prefix)]

        if not archived:
            state = get_project_state().get(name, {})
            if state.get("status") == "archived":
                self.stderr.write(
                    self.style.WARNING(
                        f"State file marks {name!r} as archived but no archived tables were found. "
                        "Clearing state entry."
                    )
                )
                self._clear_state(name)
            else:
                self.stderr.write(
                    self.style.WARNING(f"No archived tables found for project {name!r}. Nothing to remove.")
                )
            return

        self.stdout.write(
            self.style.WARNING(
                f"This will PERMANENTLY DROP {len(archived)} table(s):\n"
                + "\n".join(f"  {t}" for t in archived)
                + "\nThis action cannot be undone."
            )
        )

        if not skip_confirm:
            try:
                answer = input("\nType the project name to confirm: ").strip()
            except EOFError:
                raise CommandError("Non-interactive stdin — use --yes to skip the prompt.")
            if answer != name:
                raise CommandError("Confirmation did not match. Aborting.")

        with connection.cursor() as cursor:
            # Identifier quoting goes through connection.ops.quote_name so a
            # pathological character in a table name (only reachable via custom
            # Meta.db_table since the prefix match would otherwise filter it
            # out) cannot break out of the identifier and inject SQL.
            quote = connection.ops.quote_name
            for table in archived:
                cursor.execute(f"DROP TABLE IF EXISTS {quote(table)}")
                self.stdout.write(f"  Dropped {table}")

        self._clear_state(name)
        self.stdout.write(self.style.SUCCESS(f"Archived data for project {name!r} removed."))

    def _clear_state(self, name: str) -> None:
        state = get_project_state()
        state.pop(name, None)
        set_project_state(state)
