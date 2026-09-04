"""Test settings for the *example* project.

When running tests against a project plugin, the project's models must be in
``INSTALLED_APPS`` *before* the test database is created — a runtime
``settings`` fixture override is too late for migrations to discover them.
The fix is a project-specific settings module that extends the platform's
``test_platform`` settings and appends the project app.

Run the project's tests with::

    DJANGO_SETTINGS_MODULE=projects.example.settings_test pytest projects/example/tests/

Copy this file when scaffolding your own project, replacing ``example`` with
your project name in the ``INSTALLED_APPS`` line below.
"""

from epicurrents.settings.test_platform import *

INSTALLED_APPS = INSTALLED_APPS + ["projects.example"]
