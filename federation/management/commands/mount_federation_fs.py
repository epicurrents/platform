"""Management command: mount the federated recording FUSE filesystem.

Usage::

    python manage.py mount_federation_fs <mountpoint> --user-id <id>

Options
-------
mountpoint
    Existing directory where the virtual filesystem will be mounted.
--user-id
    Local user ID whose federation credentials are used for outbound JWT signing.
--foreground
    Keep the process in the foreground (default: daemonize).
--debug
    Enable verbose FUSE kernel/operation logging.
--no-threads
    Run single-threaded (useful for debugging, slower for concurrent reads).

Example::

    python manage.py mount_federation_fs /mnt/epicurrents-fed --user-id 1 --foreground
"""

import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Mount a read-only FUSE virtual filesystem exposing recordings shared by "
        "trusted federated peers as ordinary local files.\n\n"
        "Privacy / anonymization is controlled server-side via the apply_middleware "
        "flag on the federation AccessRight grant.  An optional local post-processing "
        "pipeline can be passed programmatically to FederationOperations for "
        "analysis-specific transforms (channel dropping, downsampling, etc.).\n\n"
        "Requires: pip install fusepy  (and libfuse2 on Linux / macFUSE on macOS)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "mountpoint",
            help="Directory to mount the virtual filesystem at (must already exist)",
        )
        parser.add_argument(
            "--user-id",
            required=True,
            dest="user_id",
            help="Local user ID used for signing outbound federation JWTs",
        )
        parser.add_argument(
            "--foreground",
            action="store_true",
            default=False,
            help="Run in the foreground instead of daemonizing",
        )
        parser.add_argument(
            "--debug",
            action="store_true",
            default=False,
            help="Enable verbose FUSE debug output",
        )
        parser.add_argument(
            "--no-threads",
            action="store_true",
            default=False,
            dest="no_threads",
            help="Disable multi-threading (single-threaded mode; useful for debugging)",
        )

    def handle(self, *args, **options):
        # Fail fast with a clear message if fusepy is missing.
        try:
            from fuse import FUSE
        except ImportError:
            raise CommandError(
                "fusepy is not installed.\n"
                "  pip install fusepy\n"
                "  Linux: sudo apt install libfuse2\n"
                "  macOS: install macFUSE from https://osxfuse.github.io/"
            )

        from federation.fuse_fs import FederationOperations

        mountpoint = options["mountpoint"]
        user_id = options["user_id"]
        foreground = options["foreground"]
        debug = options["debug"]
        nothreads = options["no_threads"]

        if not os.path.isdir(mountpoint):
            raise CommandError(f"Mountpoint does not exist or is not a directory: {mountpoint}")

        self.stdout.write(self.style.SUCCESS(f"Mounting federation filesystem at {mountpoint}"))
        self.stdout.write(f"  User ID    : {user_id}")
        self.stdout.write(f"  Foreground : {foreground}")
        self.stdout.write(f"  Threads    : {not nothreads}")
        if not foreground:
            self.stdout.write("  (daemonizing — check system logs for runtime errors)")
        self.stdout.write("")

        ops = FederationOperations(local_user_id=user_id)

        FUSE(
            ops,
            mountpoint,
            foreground=foreground,
            nothreads=nothreads,
            debug=debug,
            ro=True,
            allow_other=False,
        )
