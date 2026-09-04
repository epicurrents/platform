"""Management command — populate ``.env`` with generated secrets.

Creates ``.env`` from ``.env.example`` if absent; otherwise updates in
place, filling only keys whose current value is empty (or still equals
the example placeholder).  ``--force`` regenerates every secret regardless
— destructive, no backup is taken.
"""

import secrets
import string
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management.utils import get_random_secret_key
from py_vapid import Vapid01, b64urlencode


def _random_passphrase(words: int = 5) -> str:
    """Return a random diceware-style passphrase using printable ASCII words."""
    alphabet = string.ascii_lowercase + string.digits
    return "-".join("".join(secrets.choice(alphabet) for _ in range(8)) for _ in range(words))


# docker compose interpolates the .env it loads, and the copy it hands a
# container through ``env_file`` goes through the same pass. A ``$`` in a
# generated secret is therefore read as a variable reference and replaced —
# with nothing, since the name it accidentally forms is unset. The container
# receives a value the file does not contain, and this command prints the one
# that never reaches anything.
#
# Being mangled *consistently* is not the same as being harmless. db and web
# agree, because both arrive through compose; what breaks is every reader that
# does not. The ADMIN_PASSWORD printed below does not log in. ``psql`` with the
# value from .env is refused. A SECRET_KEY restored from that file invalidates
# every session and password-reset token it previously signed.
#
# Regenerating rather than escaping (``$$``) keeps the file true for all of
# those readers at the cost of a couple of bits of entropy. Escaping would only
# move the discrepancy to whoever reads .env without compose in front of them.
_COMPOSE_INTERPOLATION_CHAR = "$"


def _compose_safe(generate):
    """Wrap a secret generator so it cannot return a value compose would rewrite.

    Applied at the registry rather than inside each generator because the rule
    belongs to the destination — anything written into .env — and not to any one
    source. ``get_random_secret_key`` is Django's, with an alphabet this command
    does not control and which yields a ``$`` about three times in five.
    """

    def wrapped() -> str:
        for _ in range(100):
            value = generate()
            if _COMPOSE_INTERPOLATION_CHAR not in value:
                return value
        # Unreachable for any sane alphabet; a loud failure beats writing a
        # value that will not survive the trip to the container.
        raise CommandError(f"could not generate a secret without {_COMPOSE_INTERPOLATION_CHAR!r} after 100 attempts")

    return wrapped


def _random_password(length: int = 24) -> str:
    # `$` is absent by construction rather than filtered by _compose_safe: there
    # is no reason to draw from a character this command would then reject.
    alphabet = string.ascii_letters + string.digits + "!@#%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _generate_vapid_keypair() -> tuple[str, str]:
    """Return (public_key, private_key) as URL-safe base64 strings."""
    vapid = Vapid01()
    vapid.generate_keys()
    public_raw = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    return b64urlencode(public_raw), b64urlencode(private_raw)


def _generate_federation_keypair() -> tuple[str, str]:
    """Return (public_key, private_key) as URL-safe base64url strings (Ed25519, 43 chars each)."""
    from federation.auth import generate_keypair

    return generate_keypair()


def _random_hmac_key() -> str:
    """Return 32 random bytes encoded as URL-safe base64 (no padding) — the
    on-disk shape for ACTIVITY_HASH_KEY_V{N} entries in .env."""
    return b64urlencode(secrets.token_bytes(32))


# Keys replaced with a single generated value. DB_PASSWORD is here because the
# database is part of the stack (the compose `db` service is seeded from the
# same value Django connects with), not an external service with a pre-existing
# password — so the platform owns it and generates it like the other secrets.
# Every entry is wrapped so no generated value can carry a character docker
# compose would interpolate away — see _compose_safe for what that costs a
# deployment when it happens.
_KEY_REPLACEMENTS: dict[str, callable] = {
    "SECRET_KEY": _compose_safe(get_random_secret_key),
    "BORG_PASSPHRASE": _compose_safe(_random_passphrase),
    "ADMIN_PASSWORD": _compose_safe(_random_password),
    "DB_PASSWORD": _compose_safe(_random_password),
    "REDIS_PASSWORD": _compose_safe(_random_password),
    "ACTIVITY_HASH_KEY_V1": _compose_safe(_random_hmac_key),
}

_VAPID_PUBLIC_KEY = "WEBPUSH_VAPID_PUBLIC_KEY"
_VAPID_PRIVATE_KEY = "WEBPUSH_VAPID_PRIVATE_KEY"
_FED_PUBLIC_KEY = "FEDERATION_PUBLIC_KEY"
_FED_PRIVATE_KEY = "FEDERATION_PRIVATE_KEY"


class Command(BaseCommand):
    help = (
        "Populate a .env file with generated secrets.\n\n"
        "If .env does not exist it is created from .env.example.\n"
        "If .env already exists it is updated IN PLACE — only keys whose "
        "current value is empty are filled in; all other values are left "
        "untouched.  Use --force to regenerate every secret regardless."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate all secrets even if they are already set.",
        )
        parser.add_argument(
            "--output",
            default=None,
            help="Path to write the .env file (default: <project root>/.env).",
        )

    def handle(self, *args, **options):
        example_path = settings.BASE_DIR / ".env.example"
        output_path = Path(options["output"]) if options["output"] else settings.BASE_DIR / ".env"
        force = options["force"]

        # ── Choose source ─────────────────────────────────────────────────────
        if output_path.exists():
            source_path = output_path
            self.stdout.write(
                f"Updating {output_path} ({'regenerating all secrets' if force else 'filling empty values only'})"
            )
        else:
            if not example_path.exists():
                self.stderr.write(self.style.ERROR(f".env.example not found at {example_path}"))
                return
            source_path = example_path
            self.stdout.write(f"Creating {output_path} from {example_path}")

        lines = source_path.read_text().splitlines(keepends=True)

        # ── Load example placeholders for comparison ──────────────────────────
        # When updating an existing .env, a value is treated as "unset" if it
        # is empty OR still equals the placeholder from .env.example (e.g.
        # SECRET_KEY=change-me).  This lets init_env be run before manual edits
        # without clobbering values the operator has already customised.
        example_placeholders: dict[str, str] = {}
        if example_path.exists():
            for line in example_path.read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, _, v = stripped.partition("=")
                example_placeholders[k.strip()] = v.strip()

        def _needs_fill(key: str, value: str) -> bool:
            """True when the value should be replaced with a generated secret."""
            if force:
                return True
            if not value:
                return True
            # Still matches the example placeholder — operator hasn't edited it yet.
            return value == example_placeholders.get(key, object())

        # ── First pass: collect existing values ───────────────────────────────
        existing: dict[str, str] = {}
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, val = stripped.partition("=")
            existing[key.strip()] = val.strip()

        # ── Decide whether to generate each keypair ───────────────────────────
        # A keypair is regenerated if EITHER key needs filling, so both keys
        # always come from the same underlying material.
        gen_vapid = _needs_fill(_VAPID_PUBLIC_KEY, existing.get(_VAPID_PUBLIC_KEY, "")) or _needs_fill(
            _VAPID_PRIVATE_KEY, existing.get(_VAPID_PRIVATE_KEY, "")
        )
        gen_fed = _needs_fill(_FED_PUBLIC_KEY, existing.get(_FED_PUBLIC_KEY, "")) or _needs_fill(
            _FED_PRIVATE_KEY, existing.get(_FED_PRIVATE_KEY, "")
        )

        vapid_pub, vapid_priv = _generate_vapid_keypair() if gen_vapid else ("", "")
        fed_pub, fed_priv = _generate_federation_keypair() if gen_fed else ("", "")

        # ── Second pass: build output lines ──────────────────────────────────
        generated: list[tuple[str, str]] = []
        out_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                out_lines.append(line)
                continue

            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            indent = line[: len(line) - len(line.lstrip())]

            # Leave the line untouched if the value is already customised.
            if not _needs_fill(key, value):
                out_lines.append(line)
                continue

            if key == _VAPID_PUBLIC_KEY and gen_vapid:
                generated.append((key, vapid_pub))
                out_lines.append(f"{indent}{key}={vapid_pub}\n")
            elif key == _VAPID_PRIVATE_KEY and gen_vapid:
                generated.append((key, vapid_priv))
                out_lines.append(f"{indent}{key}={vapid_priv}\n")
            elif key == _FED_PUBLIC_KEY and gen_fed:
                generated.append((key, fed_pub))
                out_lines.append(f"{indent}{key}={fed_pub}\n")
            elif key == _FED_PRIVATE_KEY and gen_fed:
                generated.append((key, fed_priv))
                out_lines.append(f"{indent}{key}={fed_priv}\n")
            elif key in _KEY_REPLACEMENTS:
                new_value = _KEY_REPLACEMENTS[key]()
                generated.append((key, new_value))
                out_lines.append(f"{indent}{key}={new_value}\n")
            else:
                out_lines.append(line)

        output_path.write_text("".join(out_lines))

        self.stdout.write(self.style.SUCCESS(f"\nWritten to {output_path}"))
        if generated:
            self.stdout.write("\nGenerated secrets:")
            for key, value in generated:
                self.stdout.write(f"  {key}={value}")
            self.stdout.write(
                "\n"
                + self.style.WARNING(
                    "Shown once, and kept only in the file above. If you piped or "
                    "tee'd this run into a log, that log now holds every secret in "
                    "plaintext and nothing will clean it up — delete it."
                )
            )
        else:
            self.stdout.write("\nNo empty secrets found — nothing was generated.")
        self.stdout.write(
            "\n"
            + self.style.WARNING(
                "Review the file and fill in any remaining placeholders "
                "(ALLOWED_HOSTS, email settings, etc.) before starting services."
            )
        )
