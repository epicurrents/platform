"""System user utilities.

The system user is an inactive account used as the author for machine-generated
annotations, interruptions, and other objects created during automated processing.
It cannot log in (is_active=False) and has no special permissions.
"""

_SYSTEM_USERNAME = "__system__"


def get_system_user():
    """Return the system user, creating it if it doesn't exist yet.

    The account is permanently inactive so it can never be used to authenticate.
    Call this from Celery tasks or signal handlers that need to attribute
    automatically-generated objects to a non-human author.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username=_SYSTEM_USERNAME,
        defaults={
            "is_active": False,
            "is_staff": False,
            "is_superuser": False,
            "first_name": "System",
            "last_name": "",
        },
    )
    return user
