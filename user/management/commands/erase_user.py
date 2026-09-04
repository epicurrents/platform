"""Account erasure command — the GDPR Art. 17 fulfilment path for a user.

⚠️ LOAD-BEARING — GDPR Art. 17 subject-erasure pathway for accounts.
This command is the sanctioned way to erase a user: it removes the
account row and everything that cascades from it, unlinks the owned
recording / media files that a bare ``User.delete()`` would orphan on
disk, flushes the subject's sessions, and finishes by scrubbing the
subject's personal data out of the audit trail via
``activity.erasure.erase_subject``. Erasing a user any other way
(admin, shell) skips the file unlinks and the audit scrub and leaves
the request unfulfillable. Contract test:
``user/tests/test_erase_user.py``. See AGENTS.md → *Load-bearing
files* before modifying.
"""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity
from activity.system_activity import with_system_activity


class Command(BaseCommand):
    """Erase a user account and scrub their personal data from the audit trail."""

    help = (
        "GDPR Art. 17 account erasure: delete the user row (cascading to "
        "owned data), unlink owned recording/media files, flush the user's "
        "sessions, and scrub personal data from the audit trail. Without "
        "--yes only the inventory of affected data is printed. Use "
        "--user-id (without a username) to scrub the audit trail of an "
        "account that was already deleted through another path."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "username",
            nargs="?",
            default=None,
            help="Username of the account to erase.",
        )
        parser.add_argument(
            "--user-id",
            type=int,
            default=None,
            help=(
                "Primary key of the subject. Required when the User row is "
                "already gone; the audit trail is scrubbed without touching "
                "user-owned data."
            ),
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Actually perform the erasure instead of printing the inventory.",
        )

    def handle(self, *args, **options):
        username = options["username"]
        user_id = options["user_id"]
        if username is None and user_id is None:
            raise CommandError("Provide a username or --user-id.")

        User = get_user_model()
        user = None
        if username is not None:
            user = User.objects.filter(username=username).first()
            if user is None:
                raise CommandError(
                    f"No user named {username!r}. If the account was already "
                    "deleted, re-run with --user-id to scrub the audit trail."
                )
            if user_id is not None and user.pk != user_id:
                raise CommandError("--user-id does not match the resolved user's primary key.")
            user_id = user.pk

        if user is None:
            self._scrub_only(user_id, confirmed=options["yes"])
            return

        inventory = self._inventory(user)
        self._print_inventory(user, inventory)
        preserved = self._preserved_stored_names(user)

        if not options["yes"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing was deleted. Re-run with --yes to erase."))
            return

        with with_system_activity(
            "user.account.erase",
            interface=Activity.Interface.COMMAND,
            metadata={"user_id": user_id},
        ):
            self._unlink_owned_files(user)
            sessions_flushed = self._flush_sessions(user_id)
            user.delete()
            from activity.erasure import erase_subject

            summary = erase_subject(user_id)

        self.stdout.write(self.style.SUCCESS(f"User {user_id} erased."))
        self.stdout.write(f"Sessions flushed: {sessions_flushed}")
        for label, count in summary.items():
            self.stdout.write(f"Audit rows scrubbed [{label}]: {count}")
        if preserved:
            # The originals volume is strictly write-only for the platform
            # (see AGENTS.md); removal of preserved uploads is an out-of-band
            # operator action, so surface exactly which entries to reconcile.
            self.stdout.write(
                self.style.WARNING(
                    "Preservation is enabled — the write-only originals volume "
                    "may hold preserved uploads for these stored names; "
                    "reconcile it out-of-band to complete the erasure:"
                )
            )
            for stored_name in preserved:
                self.stdout.write(f"  {stored_name}")

    def _scrub_only(self, user_id: int, *, confirmed: bool):
        """Audit-trail-only path for an account deleted outside this command."""
        User = get_user_model()
        if User.objects.filter(pk=user_id).exists():
            raise CommandError(
                f"User {user_id} still exists — run with the username to perform a full account erasure."
            )
        if not confirmed:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run — would scrub the audit trail for deleted user {user_id}. Re-run with --yes to erase."
                )
            )
            return
        with with_system_activity(
            "user.account.erase",
            interface=Activity.Interface.COMMAND,
            metadata={"user_id": user_id, "scrub_only": True},
        ):
            from activity.erasure import erase_subject

            summary = erase_subject(user_id)
        self.stdout.write(self.style.SUCCESS(f"Audit trail scrubbed for deleted user {user_id}."))
        for label, count in summary.items():
            self.stdout.write(f"Audit rows scrubbed [{label}]: {count}")

    def _inventory(self, user) -> dict:
        """Count the user-owned rows the cascade will remove."""
        return {
            "recordings": user.recordings.count(),
            "media files": user.media_files.count(),
            "collections": user.collections.count(),
            "datasets": user.datasets.count(),
            "tags": user.tags.count(),
            "push subscriptions": user.push_subscriptions.count(),
            "external identities": user.external_identities.count(),
        }

    def _print_inventory(self, user, inventory: dict):
        self.stdout.write(f"Erasing user id={user.pk} will remove:")
        for label, count in inventory.items():
            self.stdout.write(f"  {label}: {count}")
        self.stdout.write(
            "plus every other row cascading from the user FK (access rights, "
            "annotations, library memberships, project data)."
        )

    def _preserved_stored_names(self, user) -> list:
        """Stored names of owned recordings when originals preservation is on.

        Returns an empty list when ``RECORDINGS_PRESERVE_MODE`` is ``"off"``.
        The names key the operator's out-of-band reconciliation of the
        write-only originals volume; the platform itself never reads it.
        """
        from django.conf import settings

        if getattr(settings, "RECORDINGS_PRESERVE_MODE", "off") == "off":
            return []
        return list(user.recordings.values_list("stored_name", flat=True))

    def _unlink_owned_files(self, user):
        """Unlink recording and media files before the cascade removes the rows.

        FK cascade deletes the DB rows but never touches the filesystem, so
        skipping this step strands PHI-bearing files on disk with nothing in
        the database pointing at them. A failed unlink aborts the command
        before any DB deletion — files already unlinked stay gone, and a
        re-run is safe because missing files are skipped.
        """
        paths = [r.file_path for r in user.recordings.all()]
        paths += [m.file_path for m in user.media_files.all()]
        for raw_path in paths:
            path = Path(raw_path)
            if not path.exists():
                continue
            try:
                path.unlink()
            except OSError as exc:
                raise CommandError(
                    f"Could not delete file {raw_path}: {exc}. Aborting before "
                    "any database rows are removed; re-run once the file is "
                    "removable."
                ) from exc

    def _flush_sessions(self, user_id: int) -> int:
        """Delete every DB session belonging to the subject."""
        flushed = 0
        for session in Session.objects.iterator():
            if session.get_decoded().get("_auth_user_id") == str(user_id):
                session.delete()
                flushed += 1
        return flushed
