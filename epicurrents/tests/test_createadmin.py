"""Tests for ``epicurrents.management.commands.createadmin``.

The management command the migrate service runs on first container
start to create a superuser from ``ADMIN_USERNAME`` / ``ADMIN_PASSWORD``
/ ``ADMIN_EMAIL`` settings.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from epicurrents.management.commands.createadmin import create_admin


@pytest.fixture
def admin_settings(settings):
    settings.ADMIN_USERNAME = "rootadmin"
    settings.ADMIN_PASSWORD = "rootpass"
    settings.ADMIN_EMAIL = "root@example.test"
    return settings


@pytest.mark.django_db
def test_creates_superuser_when_none_exists(admin_settings):
    created, message = create_admin()
    assert created is True
    assert "rootadmin" in message
    user = get_user_model().objects.get(username="rootadmin")
    assert user.is_superuser is True
    assert user.is_active is True
    assert user.email == "root@example.test"


@pytest.mark.django_db
def test_noop_when_superuser_already_exists(admin_settings, make_superuser):
    make_superuser(username="prior", password="prior")
    created, message = create_admin()
    assert created is False
    assert "already exists" in message.lower()
    # Did not touch the prior superuser.
    assert get_user_model().objects.filter(is_superuser=True).count() == 1


@pytest.mark.django_db
def test_refuses_when_username_collides_with_non_superuser(admin_settings, make_user):
    # A non-superuser named "rootadmin" prevents the management command from
    # claiming the username — surfaces the collision instead of clobbering.
    make_user(username="rootadmin", password="other")
    created, message = create_admin()
    assert created is False
    assert "rootadmin" in message
    # Prior user is unchanged.
    user = get_user_model().objects.get(username="rootadmin")
    assert user.is_superuser is False


@pytest.mark.django_db
def test_callable_via_call_command(admin_settings):
    """Calling through Django's management dispatch produces the same result."""
    call_command("createadmin")
    assert get_user_model().objects.filter(username="rootadmin", is_superuser=True).exists()
