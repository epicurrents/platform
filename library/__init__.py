"""Library app — Collections, Datasets, and Tags with generic item membership.

Registers read-permission extensions in ``ready()`` so that
``can_read_object`` transparently honours Dataset membership and Collection
share inheritance for any content type, without modifying the recordings or
other apps.
"""
