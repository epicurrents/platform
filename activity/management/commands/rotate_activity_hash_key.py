"""Bump ACTIVITY_HASH_KEY_CURRENT to a newly-staged key version.

The operator workflow:

1. Generate a new key:
   ``python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"``
2. Add ``ACTIVITY_HASH_KEY_V{N+1}=<the new key>`` to ``.env``. Keep the
   previous ``ACTIVITY_HASH_KEY_V{N}`` line — it must stay reachable for
   verification of rows already written under it.
3. Run ``python manage.py rotate_activity_hash_key`` to write the new
   ``ACTIVITY_HASH_KEY_CURRENT`` line into ``.env``.
4. Restart the platform so new audit rows are written under the new key.

The command refuses to bump if the next-version key is not present in
settings, so the operator cannot accidentally roll forward without
staging the key first.
"""

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Bump ACTIVITY_HASH_KEY_CURRENT in .env to the next key version. "
        "Refuses if the next version is not yet present in settings."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--target-version",
            type=int,
            default=None,
            help=(
                "Specific version to roll forward to. Defaults to "
                "max(ACTIVITY_HASH_KEYS), which is the highest staged key."
            ),
        )
        parser.add_argument(
            "--env-file",
            type=str,
            default=".env",
            help="Path to the .env file to update (default: .env).",
        )

    def handle(self, *args, **options):
        keys = getattr(settings, "ACTIVITY_HASH_KEYS", {}) or {}
        current = getattr(settings, "ACTIVITY_HASH_KEY_CURRENT", None)

        if not keys:
            raise CommandError(
                "No ACTIVITY_HASH_KEY_V{N} entries are configured. Add the key to .env first, then re-run this command."
            )

        target = options["target_version"]
        if target is None:
            target = max(keys)

        if target == current:
            self.stdout.write(f"ACTIVITY_HASH_KEY_CURRENT is already {target}; nothing to do.")
            return

        if target not in keys:
            raise CommandError(
                f"ACTIVITY_HASH_KEY_V{target} is not configured. Stage the "
                "key in .env (and restart the platform so settings sees it) "
                "before rotating ACTIVITY_HASH_KEY_CURRENT."
            )

        env_path = Path(options["env_file"])
        if not env_path.exists():
            raise CommandError(f".env file not found: {env_path}")

        text = env_path.read_text()
        pattern = re.compile(r"^ACTIVITY_HASH_KEY_CURRENT\s*=.*$", re.MULTILINE)
        replacement = f"ACTIVITY_HASH_KEY_CURRENT={target}"

        if pattern.search(text):
            new_text = pattern.sub(replacement, text)
        elif re.search(r"^#\s*ACTIVITY_HASH_KEY_CURRENT", text, re.MULTILINE):
            new_text = re.sub(
                r"^#\s*ACTIVITY_HASH_KEY_CURRENT.*$",
                replacement,
                text,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            new_text = text.rstrip() + f"\n{replacement}\n"

        env_path.write_text(new_text)

        self.stdout.write(
            self.style.SUCCESS(
                f"ACTIVITY_HASH_KEY_CURRENT rolled forward from {current} to {target}. "
                "Restart the platform so new audit rows are written under the new key."
            )
        )
        self.stdout.write(
            f"Previous key version {current} must stay in .env (or be archived "
            "externally) until all rows under it have been purged from "
            "ObjectChangeLog."
        )
