"""Django app configuration for the maintenance app."""

from django.apps import AppConfig


class MaintenanceConfig(AppConfig):
    """Registers the core operations and the audit and export handling of the job rows."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "maintenance"

    def ready(self):
        """Register the core operations, the export classification of the user links, and the output mask.

        The rows hold ids and hashes, nothing to scrub, so there is no erasure
        registration. ``output`` is a command's captured stdout, which may quote
        whatever the command printed, so it is masked out of the audit trail.
        """
        from activity.audit import register_masked_fields
        from user.export import register_export_relation

        from . import checks  # noqa: F401
        from .operations import register_core_operations

        register_core_operations()
        register_masked_fields("maintenance.maintenancejob", {"output"})
        register_export_relation(
            "maintenance.maintenancejob",
            "requested_by",
            fields=("job_id", "operation", "state", "created_at"),
            title="Maintenance jobs you requested",
        )
        register_export_relation(
            "maintenance.maintenancepackage",
            "uploaded_by",
            fields=("sha256", "version", "uploaded_at"),
            title="Update packages you uploaded",
        )
