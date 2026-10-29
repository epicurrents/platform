"""Management command — publish a version of the deployment's user agreement.

The text is read from a file or from standard input, so a deployment whose code is baked into its images publishes from
the host without a mount::

    docker compose exec -T web python manage.py publish_agreement --label 2026-11 < agreement.md

A new version asks every account to accept again. ``--no-reaccept`` publishes a correction instead: it is shown from
then on, and an earlier acceptance still counts. A published version is never edited or removed; a change is a new
label. The run is audited under ``user.agreement.publish`` with the new row's id and the digest of the text; the label
an operator typed stays on the row.
"""

import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from activity.models import Activity
from activity.system_activity import with_system_activity
from user import agreement as user_agreement


class Command(BaseCommand):
    help = (
        "Publish a version of the user agreement from a Markdown file, or from standard input when no file is "
        "given.\n\n"
        "Every account must accept the new version unless --no-reaccept marks it as a correction."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", help="Markdown file holding the agreement; standard input if omitted.")
        parser.add_argument("--label", required=True, help="Label for this version, unique among published ones.")
        parser.add_argument(
            "--no-reaccept",
            action="store_true",
            help="Publish a correction: accounts that accepted an earlier version are not asked again.",
        )

    def handle(self, *args, **options):
        if options["file"]:
            try:
                text = Path(options["file"]).read_text(encoding="utf-8")
            except OSError as exc:
                raise CommandError(f"Cannot read {options['file']}: {exc.strerror}") from exc
        else:
            text = sys.stdin.read()
        reaccept = not options["no_reaccept"]
        # Checked before the audited scope opens, so a refused run leaves no record of a publication.
        try:
            label = user_agreement.check_publication(options["label"], text, reaccept=reaccept)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        with (
            with_system_activity("user.agreement.publish", interface=Activity.Interface.COMMAND) as activity,
            transaction.atomic(),
        ):
            agreement = user_agreement.publish(label, text, reaccept=reaccept)
            if activity is not None:
                Activity.objects.filter(pk=activity.pk).update(
                    metadata={"agreement_id": agreement.pk, "digest": agreement.digest, "reaccept": reaccept},
                )
        self.stdout.write(
            f"Published user agreement {agreement.version} ({agreement.digest[:12]}), "
            + ("every account must accept it." if reaccept else "a correction; earlier acceptances still count.")
        )
