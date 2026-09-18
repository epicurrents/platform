"""Management command — create a superuser from ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL if none exists.

Run automatically by ``entrypoint.sh`` on first container start; no-op when
any superuser is already present.

``ADMIN_PASSWORD`` applies at creation and never again. An operator who edits it
in ``.env`` to reset a forgotten password gets a stack that restarts cleanly, a
file that reads as though the new value were in force, and a login that answers
401 — so the no-op path says what it did not do, and names the command that
does.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


def create_admin():
    """Create an admin user if none exists. Returns (created: bool, message: str)."""

    user_model = get_user_model()
    existing_admins = user_model.objects.filter(is_superuser=True).count()
    if existing_admins > 0:
        return False, (
            "Admin user already exists. No new admin created, and ADMIN_PASSWORD was not applied — "
            "an existing account keeps the password it was created with. "
            "To change it: manage.py changepassword <username>"
        )

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
                # WARNING, not NOTICE: every no-op here means a credential the
                # operator believes is in force is not, and this runs inside the
                # migrate container where the line has one chance to be noticed.
                self.stdout.write(self.style.WARNING(message))
        except Exception as exc:
            raise CommandError(str(exc))
