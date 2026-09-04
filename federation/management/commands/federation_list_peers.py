"""List registered federated peers with trust state and key fingerprints."""

from django.core.management.base import BaseCommand

from federation.models import FederatedPeer
from federation.services import key_fingerprint


class Command(BaseCommand):
    help = "List federated peers (id, URL, trust state, key fingerprint)."

    def handle(self, *args, **options):
        peers = list(FederatedPeer.objects.order_by("url"))
        if not peers:
            self.stdout.write("No federated peers registered.")
            return
        for peer in peers:
            trust = "trusted" if peer.is_trusted else "UNTRUSTED"
            label = f" [{peer.display_name}]" if peer.display_name else ""
            self.stdout.write(f"#{peer.pk}  {peer.url}{label}  ({trust})")
            self.stdout.write(f"      key {key_fingerprint(peer.public_key)}")
