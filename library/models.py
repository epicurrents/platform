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
from datetime import UTC, datetime

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
    # Release gating. A member of a gated dataset is hidden from every reader
    # but the dataset's managers until a release run publishes it, no member
    # resolves for a request carrying a share token, and a released member
    # is served by its release month in place of its upload time. The gate
    # itself is in ``library.release``; the flag only says the rule applies.
    release_gated = models.BooleanField(
        default=False,
        help_text=(
            "Hide members until a release run publishes them, refuse share-token callers "
            "and serve the release month in place of the upload time."
        ),
    )
    # A submission pool is a release-gated dataset with a registered ingest
    # profile: members of its dedicated group submit prepared recordings through
    # the validating submission path (``recordings.submissions``). The rules on
    # configuring, locking and dissolving a pool are in ``library.pools``.
    # Contributors are not managers: they see nothing of the pool.
    submission_profile = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Key of the registered ingest profile submissions are checked against; empty when not a pool.",
    )
    # The group exists for the pool alone and is created and removed with it.
    # PROTECT, because deleting it would silently close the pool: the admin
    # endpoint refuses a pool group, and this keeps any other path from trying.
    submission_group = models.OneToOneField(
        "auth.Group",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submission_pool",
        help_text="The pool's dedicated group, whose members may submit prepared recordings.",
    )
    submissions_open = models.BooleanField(
        default=False,
        help_text="Whether the pool accepts new submissions; closing it leaves the spool and the ledgers alone.",
    )

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


class DatasetRelease(models.Model):
    """One release run of a release-gated dataset, and the record the run leaves behind.

    A run publishes the members it selected by pointing their ``DatasetItem.release`` at this
    row. ``release_month`` is the first instant of the run's month in UTC and is what every
    reader who is not a manager sees in place of a member's upload time, so the row is the
    only source of that value. The remaining fields are the process record EDPB Guidelines
    02/2026 ¶ 41 asks to be kept with a release: the preparation profile the members were
    checked against, the de-identification pass versions of what was released, the
    equivalence-class conditions in force, who signed the run off (``DatasetReleaseSignOff``
    rows, never names) and a reference to the written assessment. The platform fills the
    version range and the count; the rest comes from the project's release selector or the
    command line.
    """

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="releases")
    # Who ran the release; null for a scheduled run with no actor.
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dataset_releases",
    )
    released_on = models.DateField()
    release_month = models.DateTimeField(editable=False)
    profile_version = models.CharField(max_length=64, blank=True, default="")
    deidentification_versions = models.JSONField(default=list, blank=True)
    k = models.PositiveIntegerField(null=True, blank=True)
    m = models.PositiveIntegerField(null=True, blank=True)
    # Free text the runner typed; cleared on the live row when the runner's account is erased
    # (library.apps), and scrubbed from the audit trail with it.
    assessment_reference = models.CharField(max_length=512, blank=True, default="")
    member_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["dataset", "released_on"]),
        ]

    def __str__(self) -> str:
        return f"DatasetRelease({self.dataset_id} on {self.released_on})"

    def save(self, *args, **kwargs):
        if self.released_on and not self.release_month:
            self.release_month = month_start(self.released_on)
        super().save(*args, **kwargs)


class DatasetReleaseSignOff(models.Model):
    """One user's sign-off of one release run: a curator whose approvals it released, or an officer who signed it.

    A row rather than a list of primary keys on the run, so the sign-off is a relation to the user
    the Art. 15 subject export and ``user.checks`` can see. Null once the account is deleted; the
    run keeps the count of who signed it.
    """

    release = models.ForeignKey(DatasetRelease, on_delete=models.CASCADE, related_name="sign_offs")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dataset_release_sign_offs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["release", "user"],
                condition=models.Q(user__isnull=False),
                name="library_release_sign_off_one_per_user",
            ),
        ]

    def __str__(self) -> str:
        return f"DatasetReleaseSignOff(release={self.release_id} by={self.user_id})"


def month_start(day) -> datetime:
    """The first instant, in UTC, of the month *day* falls in."""
    return datetime(day.year, day.month, 1, tzinfo=UTC)


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
    # The release run that published this member; null means unreleased, which in
    # a release-gated dataset hides the member from everyone but its managers.
    # RESTRICT: deleting a release on its own would silently unpublish its members,
    # while deleting the dataset (or its author) takes the release and the items
    # together, which PROTECT would refuse.
    release = models.ForeignKey(
        DatasetRelease,
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="items",
    )
    # The de-identification pass version the member carried when a run released it, so
    # the anonymity report can tell a member re-written since from one released at a
    # newer version by the same run. Null for unreleased members and non-recordings.
    released_deidentification_version = models.PositiveIntegerField(null=True, blank=True)

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


class MemberApproval(models.Model):
    """One curator's approval of one member of a release-gated dataset for release.

    A curator is a dataset manager (``library.release.is_dataset_manager``). A project's release
    selector asks for a number of approvals from distinct curators before a member is published
    (``library.release.approved_items``), and the run records the approvers as its sign-off. A
    veto is not a row: it removes the member at once, audited with its reason code, deleting a
    pooled recording outright, since one a curator found identifying has no reason to stay. The
    row holds no free text, because a typed reason tends to describe the feature that identifies
    the patient.
    """

    item = models.ForeignKey(DatasetItem, on_delete=models.CASCADE, related_name="approvals")
    # Null once the curator's account is deleted; the approval still counts for the member.
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="member_approvals",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["item", "reviewer"],
                condition=models.Q(reviewer__isnull=False),
                name="library_approval_one_per_reviewer",
            ),
        ]

    def __str__(self) -> str:
        return f"MemberApproval(item={self.item_id} by={self.reviewer_id})"


class VetoReason(models.TextChoices):
    """Why a curator vetoed a member: a closed list, so no reason in the trail describes the patient."""

    RARE_CONDITION = "rare_condition", "Rare condition or syndrome"
    SKULL_DEFECT = "skull_defect", "Skull defect or breach rhythm"
    DEVICE = "device", "Implanted or external device"
    UNUSUAL_PROTOCOL = "unusual_protocol", "Unusual protocol"
    OTHER = "other", "Other identifying content"


class Tag(models.Model):
    """A hierarchical label that can be applied to any object.

    Tags form a tree via the ``parent`` FK (adjacency list).  Creating one is
    reserved for staff unless ``LIBRARY_TAG_CREATION_REQUIRES_STAFF`` is off,
    and what a caller can list, read and apply is what ``library.tag_scope``
    says they can reach: curated tags, their own, and the tags on objects they
    can read.  Only the tag author (or a superuser) may edit or delete the
    tag definition itself.

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
    # Stamped at creation from the author's staff flag. A curated tag is the
    # deployment's vocabulary and is listed to every authenticated user; an
    # uncurated one is listed only to its author and to readers of the objects
    # it decorates (library.tag_scope). Stamped rather than derived from the
    # author's current flag so a later promotion or demotion does not silently
    # change who sees what was typed.
    curated = models.BooleanField(default=False)

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
