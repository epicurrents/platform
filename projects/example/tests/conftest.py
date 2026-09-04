"""pytest configuration for the example project test suite.

Run with the example project settings:

    DJANGO_SETTINGS_MODULE=projects.example.settings_test pytest projects/example/tests/

The ``use_example_urlconf`` fixture mounts the example API routes for the duration of each test
without touching the global URL configuration. The platform's root ``conftest.py`` fixtures
(``user``, ``make_user``, ``auth_client``, ...) apply here as everywhere else in the repository.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker


@pytest.fixture(autouse=True)
def use_example_urlconf(settings):
    settings.ROOT_URLCONF = "projects.example.tests.urls"


@pytest.fixture
def recording(db, user):
    """A READY recording owned by ``user``, resolvable by its public hash.

    The public ``hash`` is the 32-character prefix of ``stored_name``; fix it so tests can build
    the URL without parsing a response first. ``can_read_object`` does not auto-grant on
    authorship, so the author gets the explicit ``AccessRight`` row the upload endpoint would
    have created.
    """
    from epicurrents.models import AccessRight

    rec = baker.make(
        "recordings.Recording",
        author=user,
        status="READY",
        stored_name="A" * 32 + ".edf",
    )
    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(rec, for_concrete_model=False),
        object_id=str(rec.pk),
        access_giver=user,
        access_target=user,
        can_read=True,
        can_write=True,
    )
    return rec


@pytest.fixture
def note_url(recording):
    return f"/project/api/v1/notes/{'A' * 32}"
