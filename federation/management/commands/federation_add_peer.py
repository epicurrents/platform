"""Register a federated peer from the command line and fetch its public key."""

from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity
from activity.system_activity import with_system_activity
from federation import services
from federation.management.commands._cli import resolve_user
from federation.services import FederationServiceError, key_fingerprint


class Command(BaseCommand):
    help = "Register a federated peer by URL and fetch its public key (created untrusted)."

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True, help="Peer HTTPS base URL (or its MagicDNS URL).")
        parser.add_argument("--display-name", default="", help="Optional human label for the peer.")
        parser.add_argument("--user", help="Username to record as the registrar (optional).")

    def handle(self, *args, **options):
        added_by = resolve_user(options.get("user"))
        try:
            with with_system_activity("federation.peer.create", interface=Activity.Interface.COMMAND, actor=added_by):
                peer = services.register_peer(
                    url=options["url"], display_name=options["display_name"], added_by=added_by
                )
        except FederationServiceError as exc:
            raise CommandError(exc.message)

        fingerprint = key_fingerprint(peer.public_key)
        self.stdout.write(self.style.SUCCESS(f"Registered peer #{peer.pk}: {peer.url} (untrusted)"))
        self.stdout.write(f"Key fingerprint (SHA-256): {fingerprint}")
        self.stdout.write("Verify this fingerprint out-of-band, then trust with:")
        self.stdout.write(f"  python manage.py federation_trust_peer --peer {peer.pk} --fingerprint {fingerprint}")
