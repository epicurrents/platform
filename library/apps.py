"""Django app configuration — registers the dataset read-permission extensions, the release gates and the pool groups."""

from django.apps import AppConfig


class LibraryConfig(AppConfig):
    """Django app configuration for the library domain (Collections, Datasets, etc.)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "library"

    def ready(self):
        from epicurrents.permissions import (
            register_federated_read_extension,
            register_read_permission_extension,
            register_read_visibility_gate,
        )
        from library import checks  # noqa: F401  — registers the originals-volume check
        from library.permissions import (
            can_read_via_dataset,
            can_read_via_dataset_federated,
            federated_dataset_visible_terms,
        )
        from library.release import dataset_hidden_from_reader, member_hidden_from_reader

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

        # Release gating. A member of a release-gated dataset is invisible until a
        # release run publishes it and to any request carrying a share token; the
        # gate runs before any grant is read, so every surface that resolves
        # through the permission layer honours it. The dataset itself is hidden
        # from share-token callers so a join link lists no members either.
        register_read_visibility_gate("recordings.recording", member_hidden_from_reader)
        register_read_visibility_gate("library.dataset", dataset_hidden_from_reader)

        # A submission pool's group exists for the pool alone: registering it as
        # dedicated makes the grant, role and group-delete endpoints refuse it.
        from library.pools import POOL_GROUP_KIND, resolve_pool_groups
        from user.dedicated_groups import register_dedicated_group_resolver

        register_dedicated_group_resolver(POOL_GROUP_KIND, resolve_pool_groups)

        from django.db.models.signals import post_delete

        from library.models import Dataset
        from library.pools import remove_group_with_dataset

        post_delete.connect(remove_group_with_dataset, sender=Dataset, dispatch_uid="library.pool_group_with_dataset")

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
        # A release run a user performed is their activity record. The sign-off
        # list holds primary keys, opaque on both subject surfaces; the
        # assessment reference is free text the runner typed, so it is theirs on
        # both: exported under their runs, scrubbed from the permanent trail
        # with their account, as AccessRight.assessment_reference is for the
        # giver. The live row keeps the reference with a null author.
        register_export_relation(
            "library.datasetrelease",
            "author",
            fields=("released_on", "profile_version", "member_count", "assessment_reference", "created_at"),
        )
        from activity.erasure import register_subject_pii

        register_subject_pii(
            "library.datasetrelease",
            owner_field="author_id",
            pii_fields={"assessment_reference"},
        )
