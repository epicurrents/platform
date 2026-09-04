"""Trust (or untrust) a federated peer, optionally verifying its key fingerprint."""

from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity
from activity.system_activity import with_system_activity
from federation import services
from federation.management.commands._cli import resolve_peer, resolve_user
from federation.services import FederationServiceError, key_fingerprint


class Command(BaseCommand):
    help = "Set a federated peer's trust flag; --fingerprint enforces the out-of-band key check."

    def add_arguments(self, parser):
        parser.add_argument("--peer", required=True, help="Peer id or URL.")
        parser.add_argument(
            "--fingerprint",
            help="Expected SHA-256 key fingerprint; the trust flip fails if it does not match.",
        )
        parser.add_argument("--untrust", action="store_true", help="Clear the trust flag instead of setting it.")
        parser.add_argument("--user", help="Username to attribute the change to (optional).")

    def handle(self, *args, **options):
        peer = resolve_peer(options["peer"])
        actor = resolve_user(options.get("user"))
        trusted = not options["untrust"]
        try:
            with with_system_activity("federation.peer.update", interface=Activity.Interface.COMMAND, actor=actor):
                services.set_peer_trust(peer, trusted=trusted, expected_fingerprint=options.get("fingerprint"))
        except FederationServiceError as exc:
            raise CommandError(exc.message)

        state = "trusted" if trusted else "untrusted"
        self.stdout.write(self.style.SUCCESS(f"Peer #{peer.pk} ({peer.url}) is now {state}."))
        if trusted and not options.get("fingerprint"):
            self.stdout.write(
                self.style.WARNING(
                    f"Trusted without a fingerprint check. Current key fingerprint: {key_fingerprint(peer.public_key)}"
                )
            )
