"""Management command — copy production data into the development database via an intermediate JSON dump.

Refuses to run in production mode.  Excludes contenttypes, auth.permission,
admin.logentry, and sessions.session by default.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Copy data from production database to development database using an intermediate JSON dump"
    default_excludes = (
        "contenttypes",
        "auth.permission",
        "admin.logentry",
        "sessions.session",
    )

    def add_arguments(self, parser):
        """Register command options for dump path, exclusions, and flush behavior."""

        parser.add_argument(
            "--output",
            type=str,
            help="Optional path for intermediate JSON dump file",
        )
        parser.add_argument(
            "--no-flush",
            action="store_true",
            help="Do not flush development database before loading dumped data",
        )
        parser.add_argument(
            "--keep-dump",
            action="store_true",
            help="Keep temporary dump file when --output is not provided",
        )
        parser.add_argument(
            "--exclude",
            action="append",
            default=[],
            help="Additional app_label or app_label.ModelName to exclude from dumpdata (repeatable)",
        )
        parser.add_argument(
            "--no-default-excludes",
            action="store_true",
            help="Do not apply built-in exclusions (contenttypes, auth.permission, admin.logentry, sessions.session)",
        )

    def handle(self, *args, **options):
        """Run production dump and load it into current development database."""

        settings_module = getattr(settings, "SETTINGS_MODULE", "")
        if settings_module.endswith(".production"):
            raise CommandError("Do not run this command in production mode. Use DJANGO_MODE=development.")

        dump_path = self._resolve_dump_path(options)
        keep_dump = options["keep_dump"] or options["output"] is not None
        excludes = list(options["exclude"])
        if not options["no_default_excludes"]:
            excludes = [*self.default_excludes, *excludes]

        self.stdout.write(self.style.NOTICE(f"Dumping production DB to {dump_path}"))
        self._run_production_dump(dump_path, excludes)

        if not options["no_flush"]:
            self.stdout.write(self.style.NOTICE("Flushing development database"))
            call_command("flush", interactive=False, database="default")

        self.stdout.write(self.style.NOTICE("Loading dump into development database"))
        call_command("loaddata", str(dump_path), database="default")

        if not keep_dump:
            dump_path.unlink(missing_ok=True)

        self.stdout.write(self.style.SUCCESS("Production data copied into development database"))

    def _resolve_dump_path(self, options) -> Path:
        """Return user-provided dump path or create temporary dump file path."""

        if options["output"]:
            return Path(options["output"]).expanduser().resolve()

        fd, temp_path = tempfile.mkstemp(prefix="prod_dump_", suffix=".json")
        os.close(fd)
        return Path(temp_path)

    def _run_production_dump(self, dump_path: Path, excludes: list[str]) -> None:
        """Execute dumpdata under production mode and write into JSON file."""

        base_dir = Path(settings.BASE_DIR)
        manage_py = base_dir / "manage.py"
        env = os.environ.copy()
        env["DJANGO_MODE"] = "production"
        env.pop("DJANGO_SETTINGS_MODULE", None)

        command = [
            sys.executable,
            str(manage_py),
            "dumpdata",
            "--database",
            "default",
            "--output",
            str(dump_path),
            "--natural-foreign",
            "--natural-primary",
        ]

        for excluded in excludes:
            command.extend(["--exclude", excluded])

        result = subprocess.run(
            command,
            cwd=str(base_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            raise CommandError(f"Production dump failed: {message}")
