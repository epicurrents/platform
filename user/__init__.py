"""User app — custom User model and session-based authentication API.

Provides the ``User`` model (``AUTH_USER_MODEL``) which extends
``AbstractUser`` without additional fields so that future customisation is
possible without a migration squash.  All authentication endpoints live in
``user/api/v1/ninja.py``.
"""
