"""Management command — roll back a single ObjectChangeLog entry by id."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from activity.audit import rollback_change


class Command(BaseCommand):
    help = "Rollback object state from ObjectChangeLog entry"

    def add_arguments(self, parser):
        """Register rollback command arguments."""

        parser.add_argument("change_id", type=int, help="ObjectChangeLog id to rollback")
        parser.add_argument("--user-id", type=int, required=True, help="User id performing rollback")

    def handle(self, *args, **options):
        """Execute rollback as the provided user for a specific change id."""

        user_id = options["user_id"]
        change_id = options["change_id"]

        user_model = get_user_model()
        try:
            user = user_model.objects.get(pk=user_id)
        except user_model.DoesNotExist as exc:
            raise CommandError(f"User {user_id} does not exist") from exc

        try:
            restored = rollback_change(user=user, change_id=change_id)
        except PermissionError as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            raise CommandError(f"Rollback failed: {exc}") from exc

        if restored is None:
            # rollback a CREATE action —> the created object has been deleted.
            from activity.models import ObjectChangeLog

            change = ObjectChangeLog.objects.select_related("content_type").get(pk=change_id)
            self.stdout.write(
                self.style.SUCCESS(f"Rollback complete: deleted {change.content_type.model} id={change.object_id}")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Rollback complete for {restored._meta.label} pk={restored.pk}"))
