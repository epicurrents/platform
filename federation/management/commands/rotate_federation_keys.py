"""Management command to generate a new Ed25519 federation keypair.

Usage
-----
Print new keys to stdout (safe to review before applying)::

    python manage.py rotate_federation_keys

Two operational modes when writing to .env:

**Recommended: two-phase rotation with an overlap window.**  Avoids the brief
outage of a one-step rotation by publishing the new key alongside the old one
while still signing with the old, giving peers time to refresh their cache
before this instance starts signing with the new key.

    python manage.py rotate_federation_keys --announce   # phase 1: publish NEXT
    # ... wait for peers to call POST /peers/{id}/refresh-key/ ...
    python manage.py rotate_federation_keys --promote    # phase 2: NEXT → current

**Emergency / one-step rotation.**  Replaces both keys immediately; every
remote instance must call ``POST /api/v1/federation/peers/{id}/refresh-key/``
before they will accept tokens signed by the new private key.  Use this only
when a key is suspected to be compromised — the overlap flow is otherwise
strictly better.

    python manage.py rotate_federation_keys --apply
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from federation.auth import generate_keypair


class Command(BaseCommand):
    help = (
        "Generate a new Ed25519 federation keypair.  "
        "Prints the new values by default; use --apply for one-step rotation "
        "or --announce / --promote for the two-phase overlap flow."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help=(
                "One-step rotation: write the new keys directly into .env, "
                "replacing FEDERATION_PUBLIC_KEY / FEDERATION_PRIVATE_KEY. "
                "Breaks outbound traffic until every peer refreshes."
            ),
        )
        mode.add_argument(
            "--announce",
            action="store_true",
            help=(
                "Phase 1 of overlap rotation: generate a new pair and write "
                "it to FEDERATION_PUBLIC_KEY_NEXT / FEDERATION_PRIVATE_KEY_NEXT. "
                "Current signing key is unchanged; well-known endpoint starts "
                "publishing both keys.  Follow with --promote after peers refresh."
            ),
        )
        mode.add_argument(
            "--promote",
            action="store_true",
            help=(
                "Phase 2 of overlap rotation: promote the NEXT pair to current "
                "and clear the NEXT slots.  Does not generate new keys — "
                "uses whatever --announce wrote.  Outbound signing switches "
                "to the new key after restart."
            ),
        )
        parser.add_argument(
            "--env",
            default=None,
            metavar="PATH",
            help="Path to the .env file to update (default: <project root>/.env).",
        )

    def handle(self, *args, **options):
        if options["promote"]:
            self._promote(options)
            return

        public_key, private_key = generate_keypair()

        if not (options["apply"] or options["announce"]):
            self._print_dry_run(public_key, private_key)
            return

        env_path = self._env_path(options)
        if not env_path.exists():
            self.stderr.write(
                self.style.ERROR(f".env file not found at {env_path}. Copy the values above into your .env manually.")
            )
            return

        text = env_path.read_text()

        if options["apply"]:
            text = self._write_pair(
                text,
                pub_var="FEDERATION_PUBLIC_KEY",
                priv_var="FEDERATION_PRIVATE_KEY",
                pub_value=public_key,
                priv_value=private_key,
            )
            env_path.write_text(text)
            self.stdout.write(self.style.SUCCESS(f"Keys written to {env_path}"))
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "IMPORTANT: One-step rotation — restart web and celery, then "
                    "ask all remote instance administrators to refresh this peer's "
                    "public key via:\n"
                    "  POST /api/v1/federation/peers/{id}/refresh-key/\n"
                    "Outbound traffic will fail at every peer until they refresh."
                )
            )
            return

        # --announce
        text = self._write_pair(
            text,
            pub_var="FEDERATION_PUBLIC_KEY_NEXT",
            priv_var="FEDERATION_PRIVATE_KEY_NEXT",
            pub_value=public_key,
            priv_value=private_key,
        )
        env_path.write_text(text)
        self.stdout.write(self.style.SUCCESS(f"NEXT pair written to {env_path}"))
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "Phase 1 of overlap rotation complete.  Restart web to start "
                "publishing the NEXT key at /.well-known/.  Ask peers to call "
                "POST /api/v1/federation/peers/{id}/refresh-key/ — they will "
                "fetch and cache both keys.  When all peers have refreshed, "
                "run 'rotate_federation_keys --promote' to switch signing "
                "to the new key."
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _print_dry_run(self, public_key: str, private_key: str) -> None:
        self.stdout.write("")
        self.stdout.write("New federation keypair:")
        self.stdout.write(f"  FEDERATION_PUBLIC_KEY={public_key}")
        self.stdout.write(f"  FEDERATION_PRIVATE_KEY={private_key}")
        self.stdout.write("")
        self.stdout.write(
            "Dry-run mode — keys have NOT been written anywhere.\n"
            "Re-run with --announce (recommended) to start an overlap rotation, "
            "--apply for an immediate one-step rotation, or copy the values "
            "above into your .env manually."
        )

    def _env_path(self, options) -> Path:
        return Path(options["env"]) if options["env"] else settings.BASE_DIR / ".env"

    def _write_pair(
        self,
        text: str,
        *,
        pub_var: str,
        priv_var: str,
        pub_value: str,
        priv_value: str,
    ) -> str:
        text, pub_replaced = _replace(pub_var, pub_value, text)
        text, priv_replaced = _replace(priv_var, priv_value, text)
        if not pub_replaced:
            text += f"\n{pub_var}={pub_value}\n"
        if not priv_replaced:
            text += f"\n{priv_var}={priv_value}\n"
        return text

    def _promote(self, options) -> None:
        env_path = self._env_path(options)
        if not env_path.exists():
            raise CommandError(f".env file not found at {env_path}")

        text = env_path.read_text()
        next_pub = _read(text, "FEDERATION_PUBLIC_KEY_NEXT")
        next_priv = _read(text, "FEDERATION_PRIVATE_KEY_NEXT")
        if not (next_pub and next_priv):
            raise CommandError(
                "Cannot promote: FEDERATION_PUBLIC_KEY_NEXT and/or "
                "FEDERATION_PRIVATE_KEY_NEXT is not set.  Run --announce first."
            )

        # Promote NEXT into current.
        text = self._write_pair(
            text,
            pub_var="FEDERATION_PUBLIC_KEY",
            priv_var="FEDERATION_PRIVATE_KEY",
            pub_value=next_pub,
            priv_value=next_priv,
        )
        # Clear NEXT slots.
        text, _ = _replace("FEDERATION_PUBLIC_KEY_NEXT", "", text)
        text, _ = _replace("FEDERATION_PRIVATE_KEY_NEXT", "", text)
        env_path.write_text(text)

        self.stdout.write(self.style.SUCCESS(f"Promoted NEXT keys in {env_path}"))
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "Phase 2 of overlap rotation complete.  Restart web and celery "
                "to begin signing with the new key.  Peers that have refreshed "
                "during the announce window will verify successfully via their "
                "cached public_key_next; any peer that has not refreshed should "
                "be reminded to do so, after which the rotation is complete."
            )
        )


def _replace(var: str, value: str, source: str) -> tuple[str, bool]:
    pattern = re.compile(rf"^({re.escape(var)}\s*=\s*).*$", re.MULTILINE)
    new_source, count = pattern.subn(rf"\g<1>{value}", source)
    return new_source, count > 0


def _read(text: str, var: str) -> str:
    pattern = re.compile(rf"^{re.escape(var)}\s*=\s*(.*?)\s*$", re.MULTILINE)
    m = pattern.search(text)
    return m.group(1) if m else ""
