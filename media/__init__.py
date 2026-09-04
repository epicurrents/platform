"""Non-signal media files (documents today; image / audio / video later).

Sister app to ``recordings``. Where ``Recording`` carries biosignal files
with their own processing pipeline, ``MediaFile`` carries opaque media that
the viewer dispatches to per-format readers. Library / access / audit-trail
plumbing is GenericFK-based and shared with ``Recording``.
"""

default_app_config = "media.apps.MediaConfig"
