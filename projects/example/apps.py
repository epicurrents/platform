"""AppConfig for the *example* project.

Every project must define an AppConfig in ``apps.py``.  Key conventions:

- ``name`` must be ``"projects.<project_name>"``.
- ``label`` must be ``"<project_name>"`` (the last dotted segment).  Django
  derives this automatically, but being explicit prevents surprises if the
  default ever changes.  The label is also used as the database table prefix
  (e.g. ``example_recordingnote``).
- ``label`` must not collide with any existing Django app label (``admin``,
  ``auth``, ``recordings``, ``library``, etc.).
- ``ready()`` is the correct place to register EDF middleware classes and
  read-permission extensions.  Always do these imports *inside* ``ready()``
  to avoid circular-import issues at startup.
"""

from django.apps import AppConfig


class ExampleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "projects.example"
    label = "example"
    requires_platform = ">=0.1,<0.2"

    def ready(self):
        # ── EDF middleware registration ────────────────────────────────────────
        # Import and register any EDF middleware this project contributes.
        # The middleware will be injected into the MiddlewarePipeline used by
        # both the HTTP download API and the FUSE virtual filesystem.
        #
        # NOTE: A register_edf_middleware() extension registry does not exist
        # yet in the core — this is a placeholder showing where you would call
        # it once that registry is added.  For now, projects that need custom
        # EDF middleware should override the RECORDING_PIPELINES setting in
        # their settings.py to point to a custom MiddlewarePipeline factory.
        #
        # from federation.middleware import register_edf_middleware
        # from projects.example.middleware import InstitutionWatermarkMiddleware
        # register_edf_middleware(InstitutionWatermarkMiddleware())

        # ── Read-permission extensions ─────────────────────────────────────────
        # Register a custom read-permission extension if this project needs
        # access-control logic beyond the built-in AccessRight / Dataset checks.
        # The callable receives (user, obj, share_token) and returns bool.
        # It is only consulted when no direct AccessRight row matches.
        #
        # from epicurrents.permissions import register_read_permission_extension
        # from projects.example.permissions import can_read_via_project_rule
        # register_read_permission_extension(can_read_via_project_rule)

        # ── GDPR audit-payload handling ────────────────────────────────────────
        # Every project model is audited automatically, so fields that carry
        # personal data need one of two registrations (see AGENTS.md →
        # "Personal data in audited models must be registered for erasure"):
        #
        # - register_subject_pii: fields identifying a platform *user*, keyed
        #   by a user FK — scrubbed from audit payloads on account erasure.
        # - register_masked_fields: credentials and data-subject identifiers
        #   with no user FK (patient names, clinical free text) — masked out
        #   of audit payloads at write time.
        #
        # RecordingNote demonstrates the second case: clinical notes and the
        # site identifier concern the recording's subject, not a platform
        # account, so masking is the only lever that keeps them out of the
        # permanent audit trail.
        from activity.audit import register_masked_fields

        register_masked_fields("example.recordingnote", {"notes", "site_id"})

        # Subject-access export. `notes` and `site_id` are registered as masked
        # fields above, so the export drops them even if named here — the review
        # timestamps are what belongs to the reviewer.
        from user.export import register_export_relation

        register_export_relation(
            "example.recordingnote",
            "reviewed_by",
            fields=("reviewed_at", "created_at", "modified_at"),
        )
