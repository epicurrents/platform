"""Create a federation grant on an object for a peer from the command line."""

import argparse

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from activity.models import Activity
from activity.system_activity import with_system_activity
from federation import services
from federation.management.commands._cli import resolve_peer, resolve_recording, resolve_user
from federation.services import FederationServiceError


class Command(BaseCommand):
    help = "Grant a peer (optionally a specific remote user) access to an object."

    def add_arguments(self, parser):
        parser.add_argument("--peer", required=True, help="Peer id or URL to grant to.")
        parser.add_argument(
            "--giver", required=True, help="Username issuing the grant (needs share rights on the object)."
        )
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--recording",
            help="Recording to share: the 32-character hash from its URL, or its content_hash.",
        )
        target.add_argument("--content-type", help="'app_label.model' of the target (with --object-id).")
        parser.add_argument("--object-id", help="Target object primary key (with --content-type).")
        parser.add_argument(
            "--remote-user", default="", help="Remote user id on the peer; empty (default) is any user from that peer."
        )
        parser.add_argument(
            "--read", action=argparse.BooleanOptionalAction, default=True, help="Grant read (default on)."
        )
        parser.add_argument("--write", action="store_true", help="Grant write.")
        parser.add_argument("--share", action="store_true", help="Grant re-share.")
        parser.add_argument(
            "--apply-middleware",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=(
                "De-identify EDF/BDF content served under this grant. On by "
                "default for federated grants; pass --no-apply-middleware to "
                "serve raw bytes (a deliberate cross-controller PHI "
                "disclosure)."
            ),
        )
        parser.add_argument("--expires", help="Expiry as an ISO 8601 datetime; omit for no expiry.")

    def handle(self, *args, **options):
        peer = resolve_peer(options["peer"])
        giver = resolve_user(options["giver"])
        content_type, object_id = self._resolve_target(options)
        expires_at = self._parse_expires(options.get("expires"))

        try:
            with with_system_activity("federation.grant.create", interface=Activity.Interface.COMMAND, actor=giver):
                grant = services.create_grant(
                    giver=giver,
                    peer=peer,
                    content_type=content_type,
                    object_id=object_id,
                    remote_user_id=options["remote_user"],
                    can_read=options["read"],
                    can_write=options["write"],
                    can_share=options["share"],
                    apply_middleware=options["apply_middleware"],
                    expires_at=expires_at,
                )
        except FederationServiceError as exc:
            raise CommandError(exc.message)

        who = options["remote_user"] or "any user"
        self.stdout.write(
            self.style.SUCCESS(
                f"Grant #{grant.pk} created: {peer.url} ({who}) -> {content_type.app_label}.{content_type.model} {object_id}"
            )
        )

    def _resolve_target(self, options):
        if options.get("recording"):
            from recordings.models import Recording

            rec = resolve_recording(options["recording"])
            return ContentType.objects.get_for_model(Recording), str(rec.pk)

        if not options.get("object_id"):
            raise CommandError("--content-type requires --object-id.")
        try:
            app_label, model = options["content_type"].split(".", 1)
        except ValueError:
            raise CommandError("--content-type must be 'app_label.model'.")
        ct = ContentType.objects.filter(app_label=app_label, model=model).first()
        if ct is None:
            raise CommandError(f"Content type not found: {options['content_type']}")
        return ct, options["object_id"]

    @staticmethod
    def _parse_expires(value):
        if not value:
            return None
        parsed = parse_datetime(value)
        if parsed is None:
            raise CommandError(f"Could not parse --expires as an ISO 8601 datetime: {value}")
        return parsed
