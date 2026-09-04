"""Library models — Collections, Datasets, Tags, and their generic item associations.

``Collection``
    Named folder-like container that can be nested.  Access rights walk up the
    parent chain at query time (any ancestor grant is sufficient).

``CollectionItem``
    Generic membership record linking any object to a Collection.

``Dataset``
    Flat named set of objects.  A ``can_read`` AccessRight on a Dataset
    propagates read access to every item via the permission extension in
    ``library.permissions.can_read_via_dataset``.

``DatasetItem``
    Generic membership record linking any object to a Dataset.

``DatasetFolder``
    Presentation-only folder tree inside a Dataset for organising its items.

``Tag``
    Hierarchical label (adjacency list) that can be applied to any object.

``TaggedItem``
    Association between a Tag and any object.
"""

import secrets

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Collection(models.Model):
    """A named container that groups objects and other Collections.

    Collections form a tree via the ``parent`` FK. When a parent is
    **hard-deleted** the SET_NULL on ``parent`` fires and children become
    root-level collections in the DB. **Soft-delete** (setting ``deleted_at``)
    is recursive: the API's ``delete_collection`` trashes the collection, its
    sub-collections, and every ``CollectionItem`` beneath them under one shared
    timestamp, so the whole subtree moves to the trash together. Structure is
    preserved — ``parent_id`` and membership rows stay intact — so
    ``restore_collection`` lifts exactly that subtree back out. A referenced
    object (e.g. a Recording) is never trashed by this; only its membership is,
    so a recording whose sole collection is trashed surfaces at the library
    root and stays a first-class, deletable object.

    Collections are the author's private organisational tree: reads and
    writes are gated to the author and superusers, and no ``AccessRight``
    targets a collection (see ``library.permissions``). Sharing what a
    collection organises goes through the Collection → Dataset export —
    datasets are the platform's only sharing unit.

    Items are stored as generic references in ``CollectionItem`` and can point
    to any model (Recordings today, Datasets or others in future). Items keep
    their own ``AccessRight`` rows; collection membership grants nothing.
    """

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="collections",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
    )

    # Reverse GenericRelations so hard-delete cascades cleanly through every
    # reference row that targets this collection via a GenericForeignKey.
    # Soft-delete (setting deleted_at) does not trigger these — only an actual
    # row removal does. No AccessRight relation: collections are author-private
    # and nothing may grant on them. The annotation set is included even though
    # Collections are not typically annotated by the current UI, because the
    # annotations API accepts any content type as a target.
    tagged_items = GenericRelation("library.TaggedItem")
    annotations = GenericRelation(
        "annotations.Annotation",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    events = GenericRelation(
        "annotations.Event",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    interruptions = GenericRelation(
        "annotations.Interruption",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    labels = GenericRelation(
        "annotations.Label",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )

    deleted_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["author", "created_at"]),
            models.Index(fields=["parent", "created_at"]),
        ]

    def __str__(self):
        return self.name


class CollectionItem(models.Model):
    """A generic object membership in a Collection.

    The referenced object can be any model (Recording, Dataset, …) identified
    by ``content_type`` + ``object_id``. Among **active** memberships an object
    appears in at most one collection (and once within it) — both uniqueness
    constraints are partial on ``deleted_at IS NULL``.

    Trashing a collection soft-deletes its memberships too (``deleted_at``), as
    part of the recursive trash: the membership is retained so the collection
    restores intact and the object can show where it came from, but it drops
    out of the uniqueness constraints so the object can be re-filed elsewhere
    while its collection sits in the trash.
    """

    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        related_name="items",
    )
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
    )
    object_id = models.CharField(max_length=255)
    content_object = GenericForeignKey("content_type", "object_id")

    added_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    def __str__(self) -> str:
        return f"CollectionItem({self.content_type_id}/{self.object_id} in collection={self.collection_id})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "content_type", "object_id"],
                condition=models.Q(deleted_at__isnull=True),
                name="library_item_unique_per_collection",
            ),
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                condition=models.Q(deleted_at__isnull=True),
                name="library_item_unique_per_object",
            ),
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]


class Dataset(models.Model):
    """A flat, named set of objects whose AccessRights propagate to all items.

    Unlike Collections, Datasets:
    - Are single-level (no parent/children hierarchy).
    - Propagate their ``can_read`` AccessRight to every contained item.
      Granting read on a Dataset grants read on all its items without
      modifying each item's own AccessRights.  Write access is never
      inherited — items require their own ``can_write`` AccessRight.

    This is a bulk-access convenience: share a large set of Recordings (or
    any other model) with a user or group in one operation.

    Access is enforced transparently: ``can_read_object`` checks Dataset
    membership via the extension registered in ``library.apps.LibraryConfig``
    so that all existing API endpoints (recordings, etc.) honour it without
    modification.
    """

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="datasets",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    # Opaque public identifier, generated at save. Retires the integer-PK
    # exposure that made datasets the one de-identification exception: a
    # sequential PK leaks creation order and count, a random token leaks
    # nothing. The PK stays internal (FKs, admin); external addressing moves
    # to this hash.
    object_hash = models.CharField(max_length=32, unique=True, editable=False)
    # Per-dataset viewer-config overrides: a flat dotted-path → value map applied
    # on top of the deployment's project-level config when this dataset is opened
    # in the viewer. Same shape as epicurrents.ViewerConfigOverride.overrides.
    viewer_config = models.JSONField(default=dict, blank=True)

    # Reverse GenericRelations so hard-delete cascades cleanly through every
    # reference row that targets this dataset via a GenericForeignKey.
    # Annotation types are included on the same rationale as Collection — the
    # annotations API accepts any content type as a target.
    access_rights = GenericRelation("epicurrents.AccessRight")
    collection_memberships = GenericRelation("library.CollectionItem")
    tagged_items = GenericRelation("library.TaggedItem")
    annotations = GenericRelation(
        "annotations.Annotation",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    events = GenericRelation(
        "annotations.Event",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    interruptions = GenericRelation(
        "annotations.Interruption",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    labels = GenericRelation(
        "annotations.Label",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )

    deleted_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["author", "created_at"]),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.object_hash:
            self.object_hash = secrets.token_hex(16).upper()
        super().save(*args, **kwargs)


class DatasetMeta(models.Model):
    """Governance metadata sidecar for a Dataset — the fields whose list is settled.

    Holds the SPDX licence pair; the rest of the eventual field list (contributors, funding,
    DOIs, subject-group description) lands when designed, as columns here. A sidecar rather than
    Dataset columns so that growth never touches the dataset table or its serializer defaults.
    Created on first write through the dataset PATCH endpoint; absence means "nothing declared".
    """

    dataset = models.OneToOneField(
        Dataset,
        on_delete=models.CASCADE,
        related_name="meta",
    )
    # SPDX licence identifier (e.g. "CC-BY-4.0") and the URL of the licence
    # text. Free-form strings — validation against the SPDX list is a viewer
    # or export concern, not a storage constraint.
    license_spdx = models.CharField(max_length=64, blank=True, default="")
    license_url = models.URLField(max_length=512, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"DatasetMeta(dataset={self.dataset_id} license={self.license_spdx!r})"


class DatasetSnapshot(models.Model):
    """A create-only, verifiable record of a dataset's membership at a point in time.

    Pins member *identities* (content hashes), not bytes: recordings are immutable and
    content-addressed, so "model X scored Y on snapshot Z" is checkable against the manifest
    without copying data. Because only hashes are pinned, a snapshot survives member purge or
    subject erasure as unsatisfiable but still verifiable — it can prove what the set was without
    holding anything erasure removed, so erasure wins by construction and reproducibility
    degrades honestly.

    No update or delete endpoint exists; rows are written once and audited like everything else.
    Read access inherits the dataset's. ``manifest`` is the canonically-ordered member list;
    ``manifest_hash`` seals its canonical serialisation (membership only — organisation is
    presentation, not identity).
    """

    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.CASCADE,
        related_name="snapshots",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dataset_snapshots",
    )
    label = models.CharField(max_length=255, blank=True, default="")
    # Canonically-ordered member identities: [{"content_type": "app.model",
    # "identity": "<content or object hash>"}], sorted by (content_type,
    # identity). Built by the snapshot endpoint; never edited.
    manifest = models.JSONField()
    # SHA-256 hex over the canonical JSON serialisation of ``manifest``.
    manifest_hash = models.CharField(max_length=64, editable=False)
    # Opaque public identifier for URLs, same class as Dataset.object_hash.
    object_hash = models.CharField(max_length=32, unique=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["dataset", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"DatasetSnapshot({self.object_hash} of dataset={self.dataset_id})"

    def save(self, *args, **kwargs):
        if not self.object_hash:
            self.object_hash = secrets.token_hex(16).upper()
        super().save(*args, **kwargs)


class DatasetFolder(models.Model):
    """A named organisational folder inside a Dataset.

    A deliberately dumb tree: folders exist to present a shared dataset's items in a structure,
    nothing more. The non-goals are the point — no AccessRight target, no hash identity, no
    reverse GenericRelations, no annotation or tag attachment. Visibility is the dataset's and
    nothing else's, so the model adds zero permission surface.

    ``parent`` cascades: deleting a folder removes its subtree of folder rows, while the items
    inside fall back to the dataset root (``DatasetItem.folder`` is SET_NULL). Structure is cheap
    and regenerable, membership is precious — no folder operation can touch membership or data.
    """

    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.CASCADE,
        related_name="folders",
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    # Grantee-visible author text, the same hygiene class as display_name.
    name = models.CharField(max_length=255)
    # Sibling sort key; the listing order breaks ties on name.
    position = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["dataset", "parent"], name="library_folder_ds_parent_idx"),
        ]

    def __str__(self) -> str:
        return f"DatasetFolder({self.name!r} in dataset={self.dataset_id})"


class DatasetItem(models.Model):
    """A generic object in a Dataset.

    Identified by ``content_type`` + ``object_id`` (same pattern as
    AccessRight and CollectionItem).  An object may appear in a given
    dataset at most once.

    The index on ``(content_type, object_id)`` supports the reverse lookup
    "which datasets contain this object?" used by the permission extension.
    """

    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.CASCADE,
        related_name="items",
    )
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
    )
    object_id = models.CharField(max_length=255)
    content_object = GenericForeignKey("content_type", "object_id")
    # Optional placement in the dataset's folder tree; null means the dataset
    # root. SET_NULL on folder delete — the item falls back to the root with
    # its membership untouched. Single location by construction: the item row
    # is already unique per dataset.
    folder = models.ForeignKey(
        DatasetFolder,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="items",
    )

    added_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"DatasetItem({self.content_type_id}/{self.object_id} in dataset={self.dataset_id})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dataset", "content_type", "object_id"],
                name="library_dataset_item_unique_per_dataset",
            )
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]


class Tag(models.Model):
    """A hierarchical label that can be applied to any object.

    Tags form a tree via the ``parent`` FK (adjacency list).  The tag
    taxonomy is global — all authenticated users can browse and apply tags.
    Only the tag author (or a superuser) may edit or delete the tag
    definition itself.

    Items are associated through ``TaggedItem``.  Querying items by tag
    optionally includes descendants (see ``_get_tag_subtree_ids`` in the
    API layer), so tagging an item with ``EEG > Artifact`` automatically
    surfaces it under ``EEG`` as well.

    When a parent tag is hard-deleted, children become root-level tags
    (``parent`` SET_NULL).  Tags are not soft-deleted.
    """

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tags",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["author", "created_at"], name="library_tag_author_created_idx"),
            models.Index(fields=["parent", "created_at"], name="library_tag_parent_created_idx"),
        ]

    def __str__(self):
        return self.name


class TaggedItem(models.Model):
    """Association between a ``Tag`` and any object.

    Identified by ``content_type`` + ``object_id`` (same pattern as
    AccessRight, CollectionItem, and DatasetItem).  An object may carry a
    given tag at most once.

    The index on ``(content_type, object_id)`` supports the reverse lookup
    "which tags does this object have?" (e.g. for display in item detail views).
    """

    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="tagged_items")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.CharField(max_length=255)
    content_object = GenericForeignKey("content_type", "object_id")

    tagged_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"TaggedItem({self.content_type_id}/{self.object_id} tag={self.tag_id})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tag", "content_type", "object_id"],
                name="library_tagged_item_unique_per_tag",
            )
        ]
        indexes = [
            models.Index(
                fields=["content_type", "object_id"],
                name="library_tagged_item_ct_obj_idx",
            ),
        ]
