"""Record, update or clear a federation grant's contextual assessment from the command line."""

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from activity.models import Activity
from activity.system_activity import with_system_activity
from federation import services
from federation.management.commands._cli import resolve_user
from federation.services import FederationServiceError


class Command(BaseCommand):
    help = "Record the giver's contextual assessment on a federation grant, or clear it with --clear."

    def add_arguments(self, parser):
        parser.add_argument("--grant-id", type=int, required=True, help="AccessRight id of the federation grant.")
        parser.add_argument("--reference", default="", help="Reference to the documented assessment (with --date).")
        parser.add_argument("--date", help="Date the assessment was made or re-run, as YYYY-MM-DD (with --reference).")
        parser.add_argument("--clear", action="store_true", help="Remove the recorded assessment.")
        parser.add_argument(
            "--actor", help="Username to attribute + authorise the change (optional; operator otherwise)."
        )

    def handle(self, *args, **options):
        actor = resolve_user(options.get("actor"))
        if options["clear"] and (options["reference"] or options["date"]):
            raise CommandError("--clear takes no --reference or --date.")
        if not options["clear"] and not (options["reference"] and options["date"]):
            raise CommandError("Give both --reference and --date, or --clear.")
        assessment_date = None
        if options["date"]:
            assessment_date = parse_date(options["date"])
            if assessment_date is None:
                raise CommandError(f"Could not parse --date as YYYY-MM-DD: {options['date']}")

        try:
            with with_system_activity("federation.grant.assess", interface=Activity.Interface.COMMAND, actor=actor):
                grant = services.get_grant(options["grant_id"])
                services.record_assessment(
                    grant=grant, actor=actor, reference=options["reference"], assessment_date=assessment_date
                )
        except FederationServiceError as exc:
            raise CommandError(exc.message)

        if options["clear"]:
            self.stdout.write(self.style.SUCCESS(f"Grant #{options['grant_id']} assessment cleared."))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Grant #{options['grant_id']} assessment recorded, dated {assessment_date}.")
            )
