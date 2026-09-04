"""List federation grants, optionally filtered to a single giver."""

from django.core.management.base import BaseCommand

from epicurrents.models import AccessRight
from federation.management.commands._cli import resolve_user


class Command(BaseCommand):
    help = "List federation grants (peer, remote user, target, expiry)."

    def add_arguments(self, parser):
        parser.add_argument("--giver", help="Only grants issued by this username.")

    def handle(self, *args, **options):
        qs = (
            AccessRight.objects.filter(federated_peer__isnull=False)
            .select_related("federated_peer", "content_type")
            .order_by("-created_at")
        )
        giver = resolve_user(options.get("giver"))
        if giver is not None:
            qs = qs.filter(access_giver=giver)

        grants = list(qs)
        if not grants:
            self.stdout.write("No federation grants found.")
            return
        for g in grants:
            who = g.remote_user_id or "any user"
            perms = "".join(p for p, on in (("r", g.can_read), ("w", g.can_write), ("s", g.can_share)) if on)
            expiry = g.expires_at.isoformat() if g.expires_at else "never"
            self.stdout.write(
                f"#{g.pk}  {g.federated_peer.url} ({who})  "
                f"{g.content_type.app_label}.{g.content_type.model} {g.object_id}  "
                f"[{perms}] expires {expiry}"
            )
