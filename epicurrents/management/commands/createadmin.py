"""Management command — create a superuser from ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL if none exists.

Run automatically by ``entrypoint.sh`` on first container start; no-op when
any superuser is already present.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


def create_admin():
    """Create an admin user if none exists. Returns (created: bool, message: str)."""

    user_model = get_user_model()
    existing_admins = user_model.objects.filter(is_superuser=True).count()
    if existing_admins > 0:
        return False, "Admin user already exists. No new admin created."

    username = getattr(settings, "ADMIN_USERNAME", "admin")
    password = getattr(settings, "ADMIN_PASSWORD", "admin")
    email = getattr(settings, "ADMIN_EMAIL", "admin@epicurrents.local")

    if user_model.objects.filter(username=username).exists():
        return (
            False,
            f"User with username '{username}' already exists. Admin account cannot be created.",
        )

    admin = user_model.objects.create_superuser(username=username, password=password, email=email)
    admin.is_active = True
    admin.save(update_fields=["is_active"])
    return True, f"Admin user '{username}' created successfully."


class Command(BaseCommand):
    help = "Creates an admin user with ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL settings when none exists."

    def handle(self, *args, **options):
        try:
            created, message = create_admin()
            if created:
                self.stdout.write(self.style.SUCCESS(message))
            else:
                notice = getattr(self.style, "NOTICE", None)
                if notice:
                    self.stdout.write(notice(message))
                else:
                    self.stdout.write(message)
        except Exception as exc:
            raise CommandError(str(exc))
