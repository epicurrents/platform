"""Which datasets the public dataset listing names, and why a dataset cannot be listed.

A dataset is listed through one of its share tokens (``Dataset.public_listing``): the listing names the dataset and
publishes that token, so anyone can browse and open it without an account. Every read behind the listing is an
ordinary share-token read; the listing adds no caller the endpoints do not already serve.

What may be listed is narrower than what may be shared by link, because the listing publishes the link on the
platform's own authority rather than the sharer passing it on. A listing republishes data its publisher already
releases openly: the dataset is ungated, the token's grant is de-identifying, and every member is a recording that
carries ``Recording.public_source``. The rules are applied when a token is listed and again on every read of the
listing, so a member added without a source, a token that expires or a rollback that restores an old value takes the
dataset off the listing until it qualifies again, without anyone having to notice.
"""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from library.models import Dataset, DatasetItem

#: Why a dataset cannot be listed through a token, by code, as the API reports it.
PROBLEMS = {
    "disabled": "This deployment does not serve the public dataset listing.",
    "trashed": "The dataset is in the trash.",
    "release_gated": "A release-gated dataset cannot be shared by link.",
    "not_share_token": "Only a share token of this dataset can be listed.",
    "no_read": "The share token does not grant read access.",
    "original_data": "The share token serves original data; only a de-identifying token can be listed.",
    "expired": "The share token has expired.",
    "no_recordings": "The dataset holds no recordings.",
    "other_members": "Only recordings can be listed; the dataset holds other members.",
    "unsourced_recordings": "Every recording must name the published dataset it was taken from.",
}


def listing_problems(dataset: Dataset, right) -> list[dict]:
    """Every reason *dataset* cannot be listed through the access right *right*, empty when it can.

    Each is ``{"code": ..., "count": ...}``, ``count`` being the number of members a member problem concerns and
    null otherwise. Trashed members are left out, since no caller reads them; every other member counts, a failed
    recording included, because the rule is on what the dataset holds rather than on what a reader sees today.
    """
    from recordings.models import Recording

    problems: list[dict] = []

    def add(code: str, count: int | None = None) -> None:
        problems.append({"code": code, "count": count})

    if not settings.LIBRARY_PUBLIC_DATASET_LISTING:
        add("disabled")
    if dataset.deleted_at is not None:
        add("trashed")
    if dataset.release_gated:
        add("release_gated")
    dataset_ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
    if (
        right is None
        or not (right.public_share_token or "").strip()
        or right.content_type_id != dataset_ct.pk
        or right.object_id != str(dataset.pk)
    ):
        add("not_share_token")
    else:
        if not right.can_read:
            add("no_read")
        if not right.apply_middleware:
            add("original_data")
        if right.expires_at is not None and right.expires_at <= timezone.now():
            add("expired")

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    items = DatasetItem.objects.filter(dataset=dataset)
    recording_ids = [
        int(object_id)
        for object_id in items.filter(content_type=recording_ct).values_list("object_id", flat=True)
        if str(object_id).isdigit()
    ]
    recordings = Recording.objects.filter(pk__in=recording_ids, deleted_at__isnull=True)
    other = items.exclude(content_type=recording_ct).count()
    if other:
        add("other_members", other)
    if not recordings.exists():
        add("no_recordings")
    unsourced = recordings.filter(public_source="").count()
    if unsourced:
        add("unsourced_recordings", unsourced)
    return problems


def listed_datasets() -> list[Dataset]:
    """The datasets the public listing names: listed through a token and qualifying today, by name.

    Empty when the deployment does not serve the listing.
    """
    if not settings.LIBRARY_PUBLIC_DATASET_LISTING:
        return []
    candidates = (
        Dataset.objects.filter(public_listing__isnull=False, deleted_at__isnull=True, release_gated=False)
        .select_related("public_listing", "meta")
        .order_by("name", "object_hash")
    )
    return [dataset for dataset in candidates if not listing_problems(dataset, dataset.public_listing)]
