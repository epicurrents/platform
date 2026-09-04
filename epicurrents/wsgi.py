"""
WSGI config for epicurrents project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

from epicurrents.settings_mode import get_settings_module

os.environ.setdefault("DJANGO_SETTINGS_MODULE", get_settings_module())

application = get_wsgi_application()
