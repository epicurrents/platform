"""Django app configuration — registers the dataset read-permission extensions."""

from django.apps import AppConfig


class LibraryConfig(AppConfig):
    """Django app configuration for the library domain (Collections, Datasets, etc.)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "library"

    def ready(self):
        from epicurrents.permissions import (
            register_federated_read_extension,
            register_read_permission_extension,
        )
        from library.permissions import (
            can_read_via_dataset,
            can_read_via_dataset_federated,
            federated_dataset_visible_terms,
        )

        # Dataset membership: a can_read AccessRight on a Dataset grants read
        # access to every contained item. This is the platform's only
        # container-share extension — collections are author-private.
        register_read_permission_extension(can_read_via_dataset)

        # The same rule for a federated peer, which the registry above cannot
        # express: its checkers take a local user, and a peer has none. Both
        # halves are registered together so a listing cannot drift from the
        # per-object answer.
        register_federated_read_extension(
            check=can_read_via_dataset_federated,
            visible_terms=federated_dataset_visible_terms,
        )

        # Art. 15 subject export: snapshots a user authored are their activity
        # record. The manifest itself is deliberately NOT exported — it holds
        # content hashes of other subjects' recordings, and the manifest_hash
        # column already proves what the author sealed without republishing
        # the member list into an export document.
        from user.export import register_export_relation

        register_export_relation(
            "library.datasetsnapshot",
            "author",
            fields=("label", "manifest_hash", "created_at"),
        )
