"""Test suite for the *example* project template.

These tests are part of what the template teaches: a project ships its own tests, run with the
project's ``settings_test`` module so its models exist before the test database is created::

    DJANGO_SETTINGS_MODULE=projects.example.settings_test pytest projects/example/tests/

They double as the platform's automated proof of the project-plugin extension contract — settings
merge, URL slot mounting, the ``requires_platform`` pin, the ``ready()`` registration hooks, and
the EDF-middleware subclassing surface — exercised through the template every new project copies.
"""
