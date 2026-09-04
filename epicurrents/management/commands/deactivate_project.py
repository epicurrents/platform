"""Management command: deactivate_project

Archives the active project's database tables by renaming them with a
``_archived_<name>_`` prefix, then records the deactivation in
``projects/.state.json``.

The archived tables are not dropped — they remain in the database and can be
restored with ``activate_project <name>`` (default restore mode) or
discarded with ``remove_project_data <name>``.

Prerequisites
-------------
``EPICURRENTS_PROJECT`` must still be set to the project name while this
command runs so that Django can enumerate the project app's models (and
therefore its table names).  Remove the variable from ``.env`` **after**
the command completes, then restart the server.

The server must not be running while this command executes.

Example
-------
::

    # 1. Deactivate (EPICURRENTS_PROJECT still set)
    EPICURRENTS_PROJECT=clinic_eeg python manage.py deactivate_project

    # 2. Remove EPICURRENTS_PROJECT from .env, then restart the server.
"""

from datetime import datetime, timezone

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from epicurrents.project_loader import (
    ARCHIVE_PREFIX,
    get_active_project,
    get_project_state,
    rename_db_table,
    set_project_state,
)


class Command(BaseCommand):
    help = "Archive the active project's database tables so the server can run without it."

    def handle(self, *args, **options):
        name = get_active_project()
        if not name:
            raise CommandError(
                "EPICURRENTS_PROJECT is not set. Set it to the project you want to deactivate and rerun."
            )

        # Confirm the project app is loaded (requires EPICURRENTS_PROJECT to be set
        # so the app appears in INSTALLED_APPS at startup).
        try:
            app_config = apps.get_app_config(name)
        except LookupError:
            raise CommandError(
                f"App {name!r} is not in INSTALLED_APPS. "
                "Make sure EPICURRENTS_PROJECT is set correctly and the project has a valid apps.py."
            )

        # Collect all tables owned by the project app, including auto-generated
        # many-to-many through tables.
        model_tables = [model._meta.db_table for model in app_config.get_models()]
        # Also pick up any M2M through tables not directly on the model list.
        m2m_tables = []
        for model in app_config.get_models():
            for field in model._meta.get_fields():
                if field.many_to_many and hasattr(field, "remote_field") and field.remote_field:
                    through = getattr(field.remote_field, "through", None)
                    if through and through._meta.app_label == name:
                        m2m_tables.append(through._meta.db_table)

        all_tables = list(dict.fromkeys(model_tables + m2m_tables))  # deduplicate, preserve order

        if not all_tables:
            self.stdout.write(self.style.WARNING(f"Project {name!r} has no models — nothing to archive."))
            return

        # Check that none are already archived (guard against double-deactivation).
        with connection.cursor() as cursor:
            existing = set(connection.introspection.table_names(cursor))

        to_archive = [t for t in all_tables if t in existing]
        already_archived = [t for t in all_tables if t not in existing]

        if already_archived:
            self.stderr.write(
                self.style.WARNING(
                    "The following tables were not found (may already be archived):\n"
                    + "\n".join(f"  {t}" for t in already_archived)
                )
            )

        if not to_archive:
            self.stderr.write(
                self.style.ERROR(f"No live tables found for project {name!r}. Is the project already deactivated?")
            )
            return

        self.stdout.write(f"Archiving {len(to_archive)} table(s) for project {name!r}…")
        with connection.cursor() as cursor:
            for table in to_archive:
                archived_name = f"{ARCHIVE_PREFIX}{table}"
                rename_db_table(cursor, table, archived_name)
                self.stdout.write(f"  {table}  →  {archived_name}")

        # Record deactivation.
        state = get_project_state()
        state[name] = {
            "status": "archived",
            "archived_at": datetime.now(timezone.utc).isoformat(),
        }
        set_project_state(state)

        self.stdout.write(
            self.style.SUCCESS(
                f"Project {name!r} deactivated. Remove EPICURRENTS_PROJECT from .env, then restart the server."
            )
        )
