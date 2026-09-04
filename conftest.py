"""Root conftest — collection policy and shared fixtures for every test module."""

import importlib
import itertools
import json
import os
from pathlib import Path

import pytest
from django.core.cache import cache
from django.test import Client

# --------------------------------------------------------------------------- #
# Collection policy                                                           #
# --------------------------------------------------------------------------- #
#
# What is and is not part of the platform suite belongs here rather than in a
# command line. It used to live in a remembered set of ``--ignore`` flags in the
# README, and that failed the way remembered lists fail: when the DICOM plugin
# moved out of ``projects/`` (which was excluded) into ``plugins/`` (which was
# not), a plain ``pytest`` began dying with six errors during *collection*.
#
# Collection-stage failures are the ones worth designing against, because no
# marker can rescue them. A skip mark is consulted after a module has been
# imported; these modules raise while importing, so the run never reaches the
# point where a mark would be read, and every other test in the suite is lost
# with them. The only mechanisms that act early enough are the ones below.


def _foreign_settings_trees() -> list[str]:
    """Test trees that need a settings module other than the platform's.

    A plugin's or project's tests require *its own* settings: its app is absent
    from the platform's ``INSTALLED_APPS``, so importing its models under
    ``epicurrents.settings.test_platform`` raises ``RuntimeError`` at import time
    (hence ``plugins/dicom/settings_test.py`` and its siblings).

    The condition is written against that cause rather than as a list of paths:
    a tree is skipped when the active ``DJANGO_SETTINGS_MODULE`` is not the one
    that declares it, and collected when it is. Walking the directories instead
    of naming them means a plugin added tomorrow is covered on the day it lands,
    which is precisely the drift that broke this.

    ``collect_ignore`` prunes only *recursive discovery*, so a path named
    explicitly — ``pytest plugins/dicom/tests/`` — is still collected either way.
    The entries here decide what a bare ``pytest`` sweeps up, nothing more.
    """
    active = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    root = Path(__file__).parent
    trees: list[str] = []
    for parent in ("plugins", "projects"):
        for child in sorted((root / parent).glob("*")):
            if not (child / "tests").is_dir():
                continue
            if active.startswith(f"{parent}.{child.name}."):
                continue
            trees.append(f"{parent}/{child.name}/tests")
    return trees


collect_ignore = [
    *_foreign_settings_trees(),
]


def pytest_collection_modifyitems(config, items):
    """Make ``require_fuse`` skip, rather than merely being registered.

    ``pytest.ini`` has declared the marker since it was introduced but nothing
    ever acted on it, so the only way past ``federation/tests/test_fuse_fs.py``
    was ``--ignore``-ing the file — which also silenced it on the machines that
    can actually run it. Now the mark does its job in both directions.

    Importing ``fuse`` is the honest probe. Asking whether the *package* is
    installed answers the wrong question: it is installed everywhere, because it
    is in ``requirements.txt``, and it raises ``OSError`` at import when the
    shared library behind it is missing. Attempting the import is the only check
    that distinguishes a usable FUSE from a merely present one.
    """
    try:
        importlib.import_module("fuse")
    except (ImportError, OSError):
        pass
    else:
        return

    skip = pytest.mark.skip(reason="needs a usable libfuse (libfuse2 / macFUSE)")
    for item in items:
        if "require_fuse" in item.keywords:
            item.add_marker(skip)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the in-process cache before every test to prevent state bleed."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client():
    """Unauthenticated Django test client."""
    return Client()


@pytest.fixture
def make_user(db):
    """Factory fixture — call to create a regular user with given kwargs.

    The ``username`` parameter defaults to a unique auto-generated name so
    multiple calls within the same test never collide.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    _counter = itertools.count(1)

    def _make(username=None, password="testpass123", **kwargs):
        if username is None:
            username = f"testuser_{next(_counter)}"
        return User.objects.create_user(username=username, password=password, **kwargs)

    return _make


@pytest.fixture
def make_superuser(db):
    """Factory fixture — call to create a superuser.

    The ``username`` parameter defaults to a unique auto-generated name so
    multiple calls within the same test never collide.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    _counter = itertools.count(1)

    def _make(username=None, password="adminpass123", **kwargs):
        if username is None:
            username = f"admin_{next(_counter)}"
        return User.objects.create_superuser(username=username, password=password, **kwargs)

    return _make


@pytest.fixture
def user(make_user):
    """A single default regular user."""
    return make_user()


@pytest.fixture
def superuser(make_superuser):
    """A single default superuser."""
    return make_superuser()


@pytest.fixture
def auth_client(user):
    """Authenticated test client logged in as the default regular user."""
    c = Client()
    c.force_login(user)
    return c, user


@pytest.fixture
def superuser_client(superuser):
    """Authenticated test client logged in as a superuser."""
    c = Client()
    c.force_login(superuser)
    return c, superuser


def post_json(client, url, data):
    """POST JSON data and return response."""
    return client.post(url, json.dumps(data), content_type="application/json")


def patch_json(client, url, data):
    """PATCH JSON data and return response."""
    return client.patch(url, json.dumps(data), content_type="application/json")


def delete_json(client, url, data=None):
    """DELETE with optional JSON body."""
    if data is not None:
        return client.delete(url, json.dumps(data), content_type="application/json")
    return client.delete(url)
