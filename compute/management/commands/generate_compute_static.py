"""Management command — (re)generate all compute static assets for SW caching.

An umbrella over the individual compute-static generators. These assets — lead-field
blobs today, more to come — are generated at deploy and not committed (they are
gitignored, and the set will grow beyond what belongs in git history), so this is the
single entry point to (re)produce them. Output goes to the vendored asset tree under
``VENDOR_DIR``, served by ``epicurrents.views.vendor_view`` and cacheable by the service
worker; it is not part of ``collectstatic`` or the Vite build.

Run by ``scripts/bootstrap.sh`` once the stack is up and by ``scripts/update.sh`` on
every update. Both need a migrated database: the generators refresh the cache rows the
compute API serves from, so this cannot run before migrations.

For now it delegates to :mod:`~compute.management.commands.generate_static_leadfields`.
Add further generators to ``SUBCOMMANDS`` as compute gains more static outputs.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

#: The compute-static generators this umbrella runs, in order. Each writes into its own
#: subdirectory of the vendored tree with sensible defaults.
SUBCOMMANDS = ["generate_static_leadfields"]


class Command(BaseCommand):
    help = "(Re)generate all compute static assets (lead fields, …) for SW caching."

    def handle(self, *args, **options):
        for name in SUBCOMMANDS:
            self.stdout.write(self.style.MIGRATE_HEADING(f"→ {name}"))
            call_command(name)
        self.stdout.write(self.style.SUCCESS(f"Done — ran {len(SUBCOMMANDS)} compute-static generator(s)."))
