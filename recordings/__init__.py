"""Recordings app — EDF/BDF file upload, processing, and delivery.

Provides the ``Recording`` model family (``Recording``, ``RecordingMeta``,
``SignalInfo``), Celery tasks for processing and purging, a REST API at
``/recordings/api/v1/``, and the ``import_recordings`` management command for
bulk ingest from local directories.
"""
