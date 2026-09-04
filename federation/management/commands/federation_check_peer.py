"""Diagnose reachability, TLS, key, and mutual trust with a federated peer.

Two levels. Level 1 fetches the peer's well-known document — exercising DNS,
the strict-TLS context, and the SSRF guard — and (for a registered peer)
compares the fetched key against the stored one. Level 2 signs a short-lived
JWT and probes the peer's inbound endpoint for a bogus object: a 404 means the
peer accepted our identity (auth passed, object absent), a 401 means it has not
registered / trusted us or holds a stale key. Over a tailnet, Level 2 needs the
outbound tailnet path to exist (see the federation-tailscale engineering note);
until then it reports the peer as unreachable, which is itself the diagnostic.
"""

import urllib.error
import urllib.request

from django.core.management.base import BaseCommand, CommandError

from federation.auth import (
    _build_tls_context,
    check_url_is_safe,
    create_jwt,
    fetch_peer_public_key,
    get_local_instance_url,
    get_local_private_key,
    is_federation_configured,
)
from federation.management.commands._cli import resolve_peer
from federation.services import key_fingerprint


class Command(BaseCommand):
    help = "Check reachability, TLS, key, and mutual trust with a federated peer."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--peer", help="Registered peer id or URL.")
        group.add_argument("--url", help="Peer URL to check without a registered row.")
        parser.add_argument("--timeout", type=int, default=10, help="Per-request timeout in seconds.")
        parser.add_argument("--no-probe", action="store_true", help="Skip the authenticated round-trip (Level 2).")

    def handle(self, *args, **options):
        peer = None
        if options.get("peer"):
            peer = resolve_peer(options["peer"])
            url = peer.url
        else:
            url = options["url"].strip().rstrip("/")

        self.stdout.write(f"Checking {url}")
        self._check_reachability(url, peer, options["timeout"])
        if not options["no_probe"]:
            self._probe_mutual_trust(url, options["timeout"])

    def _check_reachability(self, url, peer, timeout):
        try:
            key_b64, _ = fetch_peer_public_key(url, timeout=timeout)
        except ValueError as exc:
            raise CommandError(f"well-known fetch failed: {exc}")
        self.stdout.write(self.style.SUCCESS("  [ok] well-known reachable, TLS valid, key present"))
        self.stdout.write(f"       key fingerprint: {key_fingerprint(key_b64)}")
        if peer is not None and key_b64 != peer.public_key:
            self.stdout.write(
                self.style.WARNING(
                    "  [warn] fetched key DIFFERS from the registered key — run federation_refresh_peer_key, or investigate a MITM"
                )
            )
        elif peer is not None:
            self.stdout.write(self.style.SUCCESS("  [ok] key matches the registered key"))

    def _probe_mutual_trust(self, url, timeout):
        if not is_federation_configured():
            self.stdout.write(
                self.style.WARNING("  [skip] this instance has no federation keys/URL — cannot probe mutual trust")
            )
            return

        probe_url = f"{url}/api/v1/federation/inbound/objects/1/0/"
        try:
            check_url_is_safe(probe_url)
        except ValueError as exc:
            raise CommandError(f"probe URL rejected by SSRF guard: {exc}")

        token = create_jwt(
            get_local_private_key(),
            issuer=get_local_instance_url(),
            audience=url,
            subject="healthcheck",
            ttl=60,
        )
        request = urllib.request.Request(probe_url, headers={"Authorization": f"FederatedBearer {token}"})
        try:
            urllib.request.urlopen(request, timeout=timeout, context=_build_tls_context())
            status = 200
        except urllib.error.HTTPError as exc:
            status = exc.code
        except urllib.error.URLError as exc:
            raise CommandError(f"probe request failed (outbound path unreachable?): {exc}")

        if status in (200, 404):
            self.stdout.write(
                self.style.SUCCESS(f"  [ok] peer accepts our identity (probe returned {status}; auth passed)")
            )
        elif status == 401:
            self.stdout.write(
                self.style.ERROR(
                    "  [fail] peer returned 401 — it has not registered/trusted us, or our key is stale there"
                )
            )
        elif status == 429:
            self.stdout.write(self.style.WARNING("  [warn] peer returned 429 (rate limited) — auth likely OK"))
        else:
            self.stdout.write(self.style.WARNING(f"  [warn] unexpected probe status {status}"))
