"""Contract test: the Django admin is not reachable.

The admin was the platform's largest unaudited window onto personal data. A
superuser changing a user's email through it wrote the change and produced zero
``Activity`` and zero ``ObjectChangeLog`` rows, because ``/admin/`` matches no
audited path and the signals gate on that; the only trace was
``django_admin_log``, which sits outside the hash chain and is unreachable by
``erase_subject``. It was also a de-identification bypass — ``media/admin.py``
listed and searched the author-private ``MediaFile.original_name``.

Account and group management moved to ``/api/v1/user/admin/``, where all of that
applies by construction. This asserts the old surface stays gone, because
re-adding it is one line and the failure is silent: everything keeps working,
minus the audit trail.

``django.contrib.admin`` itself is still in ``INSTALLED_APPS`` — dropping it also
drops the ``django_admin_log`` table and is tracked separately. What matters here
is that nothing serves it.
"""

import ast
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import resolve

from epicurrents.views import frontend_view

REPO_ROOT = Path(settings.BASE_DIR)


class TestNoAdminRoute:
    def test_the_admin_path_falls_through_to_the_spa(self):
        """Not a 404: ``/admin/`` is not an API-shaped path, so it reaches the
        SPA like any other unknown route. The point is that it does not reach
        Django's admin."""
        assert resolve("/admin/").func is frontend_view

    def test_a_deeper_admin_path_does_too(self):
        assert resolve("/admin/auth/user/").func is frontend_view

    def test_no_route_anywhere_serves_an_admin_view(self):
        """Path-independent, and the assertion that actually carries the claim.

        The two above pin ``/admin/`` because that is the path the removed line
        used and the one a revert would restore. Neither would notice the admin
        remounted at some other prefix, which is a different mistake with the
        same consequence — so this walks every resolved pattern instead and
        asks where its view comes from.
        """
        from django.urls import get_resolver

        def _modules(patterns):
            for pattern in patterns:
                nested = getattr(pattern, "url_patterns", None)
                if nested is not None:
                    yield from _modules(nested)
                elif pattern.callback is not None:
                    yield getattr(pattern.callback, "__module__", "")

        serving_admin = sorted(
            {m for m in _modules(get_resolver().url_patterns) if m.startswith("django.contrib.admin")}
        )
        assert serving_admin == [], f"routes still served by the admin: {serving_admin}"

    def test_the_urlconf_does_not_import_admin(self):
        """Belt to the route checks' braces, and worth stating separately: an
        import with no mount is dead weight that invites the mount back.

        Matches every spelling. ``from django.contrib import admin`` is the one
        that was there, but ``from django.contrib.admin import site`` and
        ``import django.contrib.admin`` reach the same module, and an earlier
        version of this test caught only the first.
        """
        tree = ast.parse((REPO_ROOT / "epicurrents" / "urls.py").read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        offenders = sorted(n for n in names if n == "django.contrib.admin" or n.startswith("django.contrib.admin."))
        assert offenders == [], f"the URLconf still imports the admin: {offenders}"


class TestNoAdminRegistrations:
    def test_no_app_ships_an_admin_module(self):
        """``django.contrib.admin`` autodiscovers ``<app>/admin.py`` at startup.
        A file left behind would register models against an admin site nothing
        serves — harmless today, and exactly the thing someone re-mounts the URL
        to "fix".

        Globbed rather than walked from the app registry, which would look like
        the tidier option. Only one project and its plugins are installed in any
        given process, so the registry cannot see the others' apps; these globs
        cover ``projects/*`` and ``plugins/*`` whatever the settings module says
        is active. Every first-party core app sits one level down, so ``*/`` is
        the whole of that side.
        """
        found = [str(path.relative_to(REPO_ROOT)) for path in REPO_ROOT.glob("*/admin.py") if ".venv" not in path.parts]
        found += [str(path.relative_to(REPO_ROOT)) for path in REPO_ROOT.glob("projects/*/admin.py")]
        found += [str(path.relative_to(REPO_ROOT)) for path in REPO_ROOT.glob("plugins/*/admin.py")]
        assert found == [], f"admin registrations still present: {found}"

    def test_no_first_party_model_is_registered_with_the_admin_site(self):
        """Scoped to this repo's models. Third-party apps register their own —
        ``django_celery_beat`` does — and that is not ours to prevent; the site
        is unserved either way. What matters is that no model of ours is on it.
        """
        from django.apps import apps as django_apps
        from django.contrib import admin

        # Under REPO_ROOT is not enough: the virtualenv lives there too, which
        # would count django.contrib.auth and django_celery_beat as ours.
        first_party = {
            config.label
            for config in django_apps.get_app_configs()
            if Path(config.path).is_relative_to(REPO_ROOT)
            and not {".venv", "site-packages"} & set(Path(config.path).parts)
        }
        registered = sorted(
            f"{model._meta.app_label}.{model.__name__}"
            for model in admin.site._registry
            if model._meta.app_label in first_party
        )
        assert registered == [], f"first-party models registered with the admin site: {registered}"


@pytest.mark.django_db
class TestTheReplacementIsReachable:
    def test_the_account_surface_answers(self, superuser_client):
        """The half that makes the removal safe rather than merely smaller."""
        assert superuser_client[0].get("/api/v1/user/admin/accounts").status_code == 200
