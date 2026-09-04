"""Re-fetch a federated peer's public key (after the peer rotates keys)."""

from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity
from activity.system_activity import with_system_activity
from federation import services
from federation.management.commands._cli import resolve_peer, resolve_user
from federation.services import FederationServiceError, key_fingerprint


class Command(BaseCommand):
    help = "Re-fetch and store a federated peer's public key from its well-known URL."

    def add_arguments(self, parser):
        parser.add_argument("--peer", required=True, help="Peer id or URL.")
        parser.add_argument("--user", help="Username to attribute the change to (optional).")

    def handle(self, *args, **options):
        peer = resolve_peer(options["peer"])
        actor = resolve_user(options.get("user"))
        try:
            with with_system_activity("federation.peer.refresh_key", interface=Activity.Interface.COMMAND, actor=actor):
                peer, key_changed = services.refresh_peer_key(peer)
        except FederationServiceError as exc:
            raise CommandError(exc.message)

        self.stdout.write(self.style.SUCCESS(f"Refreshed key for peer #{peer.pk} ({peer.url})."))
        self.stdout.write(f"Key fingerprint (SHA-256): {key_fingerprint(peer.public_key)}")
        if key_changed:
            self.stdout.write(
                self.style.WARNING(
                    "The key CHANGED. If you did not expect a rotation, verify out-of-band before continuing to trust it."
                )
            )
