"""Management command — (re)generate all compute static assets for SW caching.

An umbrella over the individual compute-static generators. These assets — lead-field
blobs today, more to come — are **generated on the host, not committed** (they're
gitignored; the set will grow beyond what belongs in git history), so this is the
single entry point to (re)produce them: run it on deploy/build, before
``collectstatic``, and the service worker caches the results.

For now it delegates to :mod:`~compute.management.commands.generate_static_leadfields`.
Add further generators to ``SUBCOMMANDS`` as compute gains more static outputs.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

#: The compute-static generators this umbrella runs, in order. Each writes into its
#: own ``compute/static/<...>/`` source subdir with sensible defaults.
SUBCOMMANDS = ["generate_static_leadfields"]


class Command(BaseCommand):
    help = "(Re)generate all compute static assets (lead fields, …) for SW caching."

    def handle(self, *args, **options):
        for name in SUBCOMMANDS:
            self.stdout.write(self.style.MIGRATE_HEADING(f"→ {name}"))
            call_command(name)
        self.stdout.write(self.style.SUCCESS(f"Done — ran {len(SUBCOMMANDS)} compute-static generator(s)."))
