"""Set (renew or clear) a federation grant's expiry from the command line."""

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from activity.models import Activity
from activity.system_activity import with_system_activity
from federation import services
from federation.management.commands._cli import resolve_user
from federation.services import FederationServiceError


class Command(BaseCommand):
    help = "Set a federation grant's expiry (renew), or clear it with --no-expiry."

    def add_arguments(self, parser):
        parser.add_argument("--grant-id", type=int, required=True, help="AccessRight id of the federation grant.")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--expires", help="New expiry as an ISO 8601 datetime.")
        group.add_argument("--no-expiry", action="store_true", help="Make the grant non-expiring.")
        parser.add_argument(
            "--actor", help="Username to attribute + authorise the change (optional; operator otherwise)."
        )

    def handle(self, *args, **options):
        actor = resolve_user(options.get("actor"))
        expires_at = None
        if not options["no_expiry"]:
            expires_at = parse_datetime(options["expires"])
            if expires_at is None:
                raise CommandError(f"Could not parse --expires as an ISO 8601 datetime: {options['expires']}")

        try:
            with with_system_activity("federation.grant.renew", interface=Activity.Interface.COMMAND, actor=actor):
                grant = services.get_grant(options["grant_id"])
                services.renew_grant(grant=grant, actor=actor, expires_at=expires_at)
        except FederationServiceError as exc:
            raise CommandError(exc.message)

        when = "never (non-expiring)" if expires_at is None else expires_at.isoformat()
        self.stdout.write(self.style.SUCCESS(f"Grant #{options['grant_id']} expiry set to {when}."))
