"""Revoke a federation grant from the command line."""

from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity
from activity.system_activity import with_system_activity
from federation import services
from federation.management.commands._cli import resolve_user
from federation.services import FederationServiceError


class Command(BaseCommand):
    help = "Revoke a federation grant by id."

    def add_arguments(self, parser):
        parser.add_argument("--grant-id", type=int, required=True, help="AccessRight id of the federation grant.")
        parser.add_argument(
            "--actor", help="Username to attribute + authorise the revoke (optional; operator otherwise)."
        )

    def handle(self, *args, **options):
        actor = resolve_user(options.get("actor"))
        try:
            with with_system_activity("federation.grant.revoke", interface=Activity.Interface.COMMAND, actor=actor):
                grant = services.get_grant(options["grant_id"])
                services.revoke_grant(grant=grant, actor=actor)
        except FederationServiceError as exc:
            raise CommandError(exc.message)

        self.stdout.write(self.style.SUCCESS(f"Grant #{options['grant_id']} revoked."))
