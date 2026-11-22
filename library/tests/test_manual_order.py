"""The manual order of a dataset's members: what a manager arranges, and what each reader is shown of it.

A member is arranged (``DatasetItem.position``) or unsorted (null), and every listing of a dataset puts the
arranged members of a folder first, in their positions, and the unsorted ones after them by name. The contract
pinned here: the order expression and its paging, each action of ``POST /datasets/{id}/items/{item}/order`` with
its edges, the compaction and the audit rows, who may arrange and who is served a position, that nothing but a
manager's action writes one, and that a release-gated dataset's unreleased members stay unobservable through it.
The name order of the unsorted members is in ``test_item_order``; the gate itself in ``test_release``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client, override_settings
from django.utils import timezone

from activity.models import Activity, ObjectChangeLog
from conftest import post_json
from library import item_types
from library.item_order import ACTIONS, arrange
from library.item_types import ItemType, register_item_type
from library.models import Dataset, DatasetFolder, DatasetItem
from library.release import folder_ranks
from library.tests.test_release import HASHES, _add, _gated_dataset, _grant, _recording, _release
from recordings.models import Recording

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
TOKEN = "T" * 32
LABELS = ["alpha", "bravo", "charlie", "delta", "echo"]


def _client(user=None) -> Client:
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


def _listing(client, dataset, **params) -> list[dict]:
    response = client.get(f"{DATASETS}/{dataset.object_hash}/items/", params)
    assert response.status_code == 200, response.content
    return response.json()


def _names(rows) -> list:
    return [row["object_name"] for row in rows]


def _order(client, dataset, item, action):
    return post_json(client, f"{DATASETS}/{dataset.object_hash}/items/{item.pk}/order", {"action": action})


def _positions(dataset) -> dict:
    """Each member's position by its recording's display name."""
    names = {str(pk): name for pk, name in Recording.objects.values_list("pk", "display_name")}
    return {names[item.object_id]: item.position for item in DatasetItem.objects.filter(dataset=dataset)}


def _place(positions: dict) -> None:
    """Write positions straight to the rows, as a test's starting state: ``{item: position}``."""
    for item, position in positions.items():
        DatasetItem.objects.filter(pk=item.pk).update(position=position)


@pytest.fixture
def arranged(make_user):
    """An ungated dataset of five recordings added in reverse name order, its author, and a reader of it."""
    author, reader = make_user(), make_user()
    dataset = Dataset.objects.create(author=author, name="ordered")
    _grant(dataset, author, target=author, can_write=True, can_share=True)
    _grant(dataset, author, target=reader)
    items = {}
    for index, label in reversed(list(enumerate(LABELS))):
        items[label] = _add(dataset, _recording(author, index=index, display_name=label))
        DatasetItem.objects.filter(pk=items[label].pk).update(added_at=timezone.now() + timedelta(days=5 - index))
    return {"dataset": dataset, "items": items, "author": author, "reader": reader}


class TestArrange:
    """The pure rule: an arranged sequence and an action in, the arranged sequence out."""

    def test_up_and_down_swap_with_the_neighbour(self):
        assert arrange([1, 2, 3], 3, "up") == [1, 3, 2]
        assert arrange([1, 2, 3], 1, "down") == [2, 1, 3]

    def test_the_edges_change_nothing(self):
        assert arrange([1, 2, 3], 1, "up") == [1, 2, 3]
        assert arrange([1, 2, 3], 3, "down") == [1, 2, 3]

    def test_top_and_bottom(self):
        assert arrange([1, 2, 3], 3, "top") == [3, 1, 2]
        assert arrange([1, 2, 3], 1, "bottom") == [2, 3, 1]

    def test_an_unsorted_member_is_arranged_last_by_up_and_bottom_and_first_by_top(self):
        assert arrange([1, 2], 9, "up") == [1, 2, 9]
        assert arrange([1, 2], 9, "bottom") == [1, 2, 9]
        assert arrange([1, 2], 9, "top") == [9, 1, 2]
        assert arrange([], 9, "up") == [9]

    def test_down_on_an_unsorted_member_changes_nothing(self):
        assert arrange([1, 2], 9, "down") == [1, 2]

    def test_clear_takes_the_member_out_and_is_idempotent(self):
        assert arrange([1, 2, 3], 2, "clear") == [1, 3]
        assert arrange([1, 3], 2, "clear") == [1, 3]

    def test_a_member_that_is_not_shown_is_stepped_over(self):
        assert arrange([1, 2, 3], 3, "up", shown={1, 3}) == [3, 1, 2]
        assert arrange([1, 2, 3], 1, "down", shown={1, 3}) == [2, 3, 1]
        assert arrange([1, 2, 3], 2, "up", shown={2, 3}) == [1, 2, 3]

    def test_an_unknown_action_is_refused(self):
        with pytest.raises(ValueError):
            arrange([1], 1, "first")
        assert set(ACTIONS) == {"up", "down", "top", "bottom", "clear"}


class TestListingOrder:
    def test_arranged_members_come_first_in_position_then_the_unsorted_by_name(self, arranged):
        items = arranged["items"]
        _place({items["echo"]: 1, items["charlie"]: 2})
        for caller in (arranged["author"], arranged["reader"]):
            rows = _listing(_client(caller), arranged["dataset"])
            assert _names(rows) == ["echo", "charlie", "alpha", "bravo", "delta"]

    def test_nothing_arranged_is_the_name_order(self, arranged):
        assert _names(_listing(_client(arranged["reader"]), arranged["dataset"])) == LABELS

    def test_the_order_holds_across_pages(self, arranged):
        items = arranged["items"]
        _place({items["delta"]: 1, items["bravo"]: 2, items["echo"]: 3})
        client = _client(arranged["reader"])
        paged = []
        for offset in (0, 2, 4):
            paged += _listing(client, arranged["dataset"], limit=2, offset=offset)
        assert _names(paged) == ["delta", "bravo", "echo", "alpha", "charlie"]
        assert _names(paged) == _names(_listing(client, arranged["dataset"]))

    def test_unsorted_ties_are_broken_by_the_hash_not_by_addition(self, make_user):
        author = make_user()
        dataset = Dataset.objects.create(author=author, name="ties")
        added = [_add(dataset, _recording(author, index=index, display_name="Same")) for index in (3, 2, 1, 0)]
        _place({added[1]: 1})
        rows = _listing(_client(author), dataset)
        assert [row["object_hash"] for row in rows] == [HASHES[2], HASHES[0], HASHES[1], HASHES[3]]

    def test_a_position_held_twice_falls_back_to_the_name(self, arranged):
        items = arranged["items"]
        _place({items["delta"]: 1, items["bravo"]: 1})
        assert _names(_listing(_client(arranged["reader"]), arranged["dataset"]))[:2] == ["bravo", "delta"]

    def test_a_folder_holds_an_order_of_its_own_and_the_listing_follows_the_tree(self, arranged):
        dataset, items = arranged["dataset"], arranged["items"]
        later = DatasetFolder.objects.create(dataset=dataset, name="aaa later", position=2)
        first = DatasetFolder.objects.create(dataset=dataset, name="zzz first", position=1)
        nested = DatasetFolder.objects.create(dataset=dataset, name="nested", parent=first)
        assert folder_ranks(dataset) == {first.pk: 1, nested.pk: 2, later.pk: 3}
        DatasetItem.objects.filter(pk=items["alpha"].pk).update(folder=later, position=1)
        DatasetItem.objects.filter(pk=items["bravo"].pk).update(folder=first)
        DatasetItem.objects.filter(pk=items["charlie"].pk).update(folder=first, position=1)
        DatasetItem.objects.filter(pk=items["delta"].pk).update(folder=nested)
        # The root first, then each folder as the dataset page walks the tree, arranged members first in each.
        rows = _listing(_client(arranged["reader"]), dataset)
        assert _names(rows) == ["echo", "charlie", "bravo", "delta", "alpha"]
        paged = _listing(_client(arranged["reader"]), dataset, limit=3) + _listing(
            _client(arranged["reader"]), dataset, limit=3, offset=3
        )
        assert _names(paged) == _names(rows)

    def test_the_browser_lists_the_same_order_by_default(self, arranged):
        items = arranged["items"]
        _place({items["echo"]: 1, items["charlie"]: 2})
        client = _client(arranged["reader"])
        url = f"{DATASETS}/{arranged['dataset'].object_hash}/browse/"
        default = [row["display_name"] for row in client.get(url).json()["items"]]
        assert default == ["echo", "charlie", "alpha", "bravo", "delta"]
        assert [row["display_name"] for row in client.get(url, {"sort": "order"}).json()["items"]] == default
        assert [row["display_name"] for row in client.get(url, {"sort": "-order"}).json()["items"]] == default[::-1]
        assert [row["display_name"] for row in client.get(url, {"sort": "name"}).json()["items"]] == LABELS


class TestOrderActions:
    def test_an_unsorted_member_is_brought_into_the_order(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        assert _order(client, dataset, items["delta"], "bottom").status_code == 200
        assert _order(client, dataset, items["bravo"], "up").status_code == 200
        assert _order(client, dataset, items["echo"], "top").status_code == 200
        assert _positions(dataset) == {"echo": 1, "delta": 2, "bravo": 3, "alpha": None, "charlie": None}
        assert _names(_listing(client, dataset)) == ["echo", "delta", "bravo", "alpha", "charlie"]

    def test_down_on_an_unsorted_member_changes_nothing(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["echo"]: 1})
        response = _order(client, dataset, items["alpha"], "down")
        assert response.status_code == 200
        assert response.json()["changed"] == 0
        assert _positions(dataset)["alpha"] is None

    def test_up_and_down_swap_neighbours_and_stop_at_the_edges(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["alpha"]: 1, items["bravo"]: 2, items["charlie"]: 3})
        assert _order(client, dataset, items["charlie"], "up").json()["changed"] == 2
        assert _positions(dataset) == {"alpha": 1, "charlie": 2, "bravo": 3, "delta": None, "echo": None}
        assert _order(client, dataset, items["alpha"], "down").json()["changed"] == 2
        assert _positions(dataset) == {"charlie": 1, "alpha": 2, "bravo": 3, "delta": None, "echo": None}
        assert _order(client, dataset, items["charlie"], "up").json()["changed"] == 0
        assert _order(client, dataset, items["bravo"], "down").json()["changed"] == 0
        assert _positions(dataset) == {"charlie": 1, "alpha": 2, "bravo": 3, "delta": None, "echo": None}

    def test_top_and_bottom_move_an_arranged_member(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["alpha"]: 1, items["bravo"]: 2, items["charlie"]: 3})
        _order(client, dataset, items["charlie"], "top")
        assert _positions(dataset) == {"charlie": 1, "alpha": 2, "bravo": 3, "delta": None, "echo": None}
        _order(client, dataset, items["charlie"], "bottom")
        assert _positions(dataset) == {"alpha": 1, "bravo": 2, "charlie": 3, "delta": None, "echo": None}

    def test_clear_returns_a_member_to_the_unsorted_ones_and_closes_the_gap(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["echo"]: 1, items["alpha"]: 2, items["delta"]: 3})
        assert _order(client, dataset, items["alpha"], "clear").json()["changed"] == 2
        assert _positions(dataset) == {"echo": 1, "delta": 2, "alpha": None, "bravo": None, "charlie": None}
        assert _names(_listing(client, dataset)) == ["echo", "delta", "alpha", "bravo", "charlie"]
        assert _order(client, dataset, items["alpha"], "clear").json()["changed"] == 0

    def test_an_action_renumbers_a_folder_left_with_gaps_or_a_position_held_twice(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["alpha"]: 4, items["bravo"]: 4, items["charlie"]: 9})
        _order(client, dataset, items["echo"], "bottom")
        assert _positions(dataset) == {"alpha": 1, "bravo": 2, "charlie": 3, "echo": 4, "delta": None}
        positions = [position for position in _positions(dataset).values() if position is not None]
        assert sorted(positions) == list(range(1, len(positions) + 1))

    def test_the_answer_is_the_folder_in_listing_order(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["echo"]: 1})
        body = _order(client, dataset, items["charlie"], "bottom").json()
        assert set(body) == {"changed", "folder_id", "items"}
        assert body["folder_id"] is None
        assert body["changed"] == 1
        assert body["items"] == [
            {"id": items["echo"].pk, "position": 1},
            {"id": items["charlie"].pk, "position": 2},
            {"id": items["alpha"].pk, "position": None},
            {"id": items["bravo"].pk, "position": None},
            {"id": items["delta"].pk, "position": None},
        ]
        assert [row["id"] for row in body["items"]] == [row["id"] for row in _listing(client, dataset)]

    def test_an_order_is_a_folders_own(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        folder = DatasetFolder.objects.create(dataset=dataset, name="cases")
        DatasetItem.objects.filter(pk__in=[items["alpha"].pk, items["bravo"].pk]).update(folder=folder)
        _place({items["echo"]: 1, items["delta"]: 2})
        body = _order(client, dataset, items["bravo"], "top").json()
        assert body["folder_id"] == folder.pk
        assert body["items"] == [
            {"id": items["bravo"].pk, "position": 1},
            {"id": items["alpha"].pk, "position": None},
        ]
        assert _positions(dataset) == {"echo": 1, "delta": 2, "bravo": 1, "alpha": None, "charlie": None}

    def test_a_trashed_member_keeps_its_place_and_is_stepped_over(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["alpha"]: 1, items["bravo"]: 2, items["charlie"]: 3})
        Recording.objects.filter(display_name="bravo").update(deleted_at=timezone.now())
        body = _order(client, dataset, items["charlie"], "up").json()
        assert [row["id"] for row in body["items"]][:2] == [items["charlie"].pk, items["alpha"].pk]
        assert _positions(dataset) == {"charlie": 1, "alpha": 2, "bravo": 3, "delta": None, "echo": None}

    def test_an_unknown_action_and_an_unknown_item_are_refused(self, arranged, make_user):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        assert _order(client, dataset, items["alpha"], "first").status_code == 422
        other = Dataset.objects.create(author=arranged["author"], name="other")
        elsewhere = _add(other, _recording(arranged["author"], index=5))
        assert _order(client, dataset, elsewhere, "top").status_code == 404
        assert DatasetItem.objects.get(pk=elsewhere.pk).position is None

    def test_each_changed_row_is_written_to_the_audit_trail_under_the_order_verb(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["alpha"]: 1, items["bravo"]: 2, items["charlie"]: 3})
        item_ct = ContentType.objects.get_for_model(DatasetItem)
        before = ObjectChangeLog.objects.filter(content_type=item_ct).count()
        assert _order(client, dataset, items["charlie"], "top").json()["changed"] == 3
        changes = ObjectChangeLog.objects.filter(content_type=item_ct).order_by("-pk")[:3]
        assert ObjectChangeLog.objects.filter(content_type=item_ct).count() == before + 3
        assert {change.object_id for change in changes} == {str(items[name].pk) for name in LABELS[:3]}
        assert all(change.action == ObjectChangeLog.ACTION_MODIFY for change in changes)
        assert all(set(change.changes) == {"position"} for change in changes)
        activity = Activity.objects.filter(verb="library.dataset.item.order").latest("pk")
        assert activity.metadata == {"action": "top", "changed": 3}
        assert (activity.target_content_type, activity.target_object_id) == (item_ct, str(items["charlie"].pk))

    def test_an_action_that_changes_nothing_writes_no_change_row(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["alpha"]: 1})
        item_ct = ContentType.objects.get_for_model(DatasetItem)
        before = ObjectChangeLog.objects.filter(content_type=item_ct).count()
        assert _order(client, dataset, items["alpha"], "up").json()["changed"] == 0
        assert ObjectChangeLog.objects.filter(content_type=item_ct).count() == before
        assert Activity.objects.filter(verb="library.dataset.item.order").latest("pk").metadata == {
            "action": "up",
            "changed": 0,
        }


class TestWhoMayArrange:
    @pytest.fixture
    def cast(self, arranged, make_user, make_superuser):
        writer, stranger = make_user(), make_user()
        _grant(arranged["dataset"], arranged["author"], target=writer, can_write=True)
        _grant(arranged["dataset"], arranged["author"], token=TOKEN)
        return {**arranged, "writer": writer, "stranger": stranger, "superuser": make_superuser()}

    @pytest.mark.parametrize(
        ("caller", "status"),
        [("author", 200), ("superuser", 200), ("writer", 200), ("reader", 403), ("stranger", 403)],
    )
    def test_the_datasets_managers_alone(self, cast, caller, status):
        item = cast["items"]["delta"]
        assert _order(_client(cast[caller]), cast["dataset"], item, "top").status_code == status
        assert DatasetItem.objects.get(pk=item.pk).position == (1 if status == 200 else None)

    def test_an_anonymous_caller_is_refused_with_or_without_a_share_token(self, cast):
        item = cast["items"]["delta"]
        url = f"{DATASETS}/{cast['dataset'].object_hash}/items/{item.pk}/order"
        assert post_json(_client(), url, {"action": "top"}).status_code == 401
        assert post_json(_client(), f"{url}?share_token={TOKEN}", {"action": "top"}).status_code == 401
        assert DatasetItem.objects.get(pk=item.pk).position is None

    def test_a_reader_presenting_the_share_token_is_refused(self, cast):
        item = cast["items"]["delta"]
        url = f"{DATASETS}/{cast['dataset'].object_hash}/items/{item.pk}/order?share_token={TOKEN}"
        assert post_json(_client(cast["stranger"]), url, {"action": "top"}).status_code == 403
        assert DatasetItem.objects.get(pk=item.pk).position is None

    @override_settings(SESSION_CSRF_ENFORCED=True)
    def test_a_session_write_without_a_csrf_token_is_refused(self, cast):
        client = Client(enforce_csrf_checks=True)
        client.force_login(cast["author"])
        item = cast["items"]["delta"]
        assert _order(client, cast["dataset"], item, "top").status_code == 403
        assert DatasetItem.objects.get(pk=item.pk).position is None


class TestPositionVisibility:
    @pytest.fixture
    def cast(self, arranged, make_user, make_superuser):
        writer, stranger = make_user(), make_user()
        _grant(arranged["dataset"], arranged["author"], target=writer, can_write=True)
        _grant(arranged["dataset"], arranged["author"], token=TOKEN)
        _place({arranged["items"]["echo"]: 1, arranged["items"]["charlie"]: 2})
        return {**arranged, "writer": writer, "stranger": stranger, "superuser": make_superuser()}

    @pytest.mark.parametrize("caller", ["author", "superuser", "writer"])
    def test_a_manager_is_served_the_positions(self, cast, caller):
        rows = _listing(_client(cast[caller]), cast["dataset"])
        assert [(row["object_name"], row["position"]) for row in rows][:3] == [
            ("echo", 1),
            ("charlie", 2),
            ("alpha", None),
        ]

    def test_a_reader_is_served_the_order_and_no_position(self, cast):
        rows = _listing(_client(cast["reader"]), cast["dataset"])
        assert _names(rows) == ["echo", "charlie", "alpha", "bravo", "delta"]
        assert [row["position"] for row in rows] == [None] * 5
        assert [row["id"] for row in rows] == [None] * 5

    def test_a_share_token_reader_is_served_no_position(self, cast):
        for client in (_client(), _client(cast["stranger"])):
            rows = _listing(client, cast["dataset"], share_token=TOKEN)
            assert _names(rows) == ["echo", "charlie", "alpha", "bravo", "delta"]
            assert [row["position"] for row in rows] == [None] * 5


class TestOnlyAManagersActionWritesAPosition:
    def test_an_added_member_is_unsorted_however_many_are_arranged(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["echo"]: 1, items["charlie"]: 2})
        recording = _recording(arranged["author"], index=5, display_name="aardvark")
        _grant(recording, arranged["author"], target=arranged["author"])
        recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        response = post_json(
            client,
            f"{DATASETS}/{dataset.object_hash}/items/",
            {"content_type_id": recording_ct.pk, "object_id": HASHES[5]},
        )
        assert response.status_code == 201, response.content
        assert response.json()["position"] is None
        assert DatasetItem.objects.get(pk=response.json()["id"]).position is None
        assert _names(_listing(client, dataset)) == ["echo", "charlie", "aardvark", "alpha", "bravo", "delta"]

    def test_a_row_created_any_other_way_is_unsorted(self, arranged):
        added = _add(arranged["dataset"], _recording(arranged["author"], index=5))
        assert added.position is None

    def test_moving_to_another_folder_clears_the_position(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        folder = DatasetFolder.objects.create(dataset=dataset, name="cases")
        _place({items["echo"]: 1, items["charlie"]: 2, items["alpha"]: 3})
        url = f"{DATASETS}/{dataset.object_hash}/items/{items['charlie'].pk}/move"
        moved = post_json(client, url, {"folder_id": folder.pk}).json()
        assert (moved["folder_id"], moved["position"]) == (folder.pk, None)
        assert _positions(dataset) == {"echo": 1, "alpha": 3, "charlie": None, "bravo": None, "delta": None}
        # The folder left behind keeps its order; the next action there closes the gap.
        assert _names(_listing(client, dataset))[:2] == ["echo", "alpha"]
        _order(client, dataset, items["alpha"], "bottom")
        assert _positions(dataset)["alpha"] == 2

    def test_placing_an_item_where_it_already_is_keeps_its_position(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["echo"]: 1})
        url = f"{DATASETS}/{dataset.object_hash}/items/{items['echo'].pk}/move"
        assert post_json(client, url, {"folder_id": None}).json()["position"] == 1

    def test_deleting_a_folder_returns_its_items_to_the_root_unsorted(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        folder = DatasetFolder.objects.create(dataset=dataset, name="cases")
        DatasetItem.objects.filter(pk=items["delta"].pk).update(folder=folder, position=1)
        _place({items["echo"]: 1})
        assert client.delete(f"{DATASETS}/{dataset.object_hash}/folders/{folder.pk}/").status_code == 200
        assert _positions(dataset) == {"echo": 1, "delta": None, "alpha": None, "bravo": None, "charlie": None}

    def test_removing_a_member_leaves_the_rest_in_order(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        _place({items["echo"]: 1, items["charlie"]: 2, items["alpha"]: 3})
        assert client.delete(f"{DATASETS}/{dataset.object_hash}/items/{items['charlie'].pk}/").status_code == 200
        assert _names(_listing(client, dataset)) == ["echo", "alpha", "bravo", "delta"]
        _order(client, dataset, items["alpha"], "up")
        assert _positions(dataset) == {"alpha": 1, "echo": 2, "bravo": None, "delta": None}


class TestReleaseGatedDataset:
    @pytest.fixture
    def gated(self, make_user):
        """A gated dataset of four of its author's recordings, the first two released, and a reader of it."""
        curator, reader = make_user(), make_user()
        recordings = [_recording(curator, index=index, display_name=LABELS[index]) for index in range(4)]
        dataset, items = _gated_dataset(curator, *recordings)
        _grant(dataset, curator, target=curator, can_write=True, can_share=True)
        _grant(dataset, curator, target=reader)
        _release(dataset, items[0], items[1])
        return {
            "dataset": dataset,
            "items": dict(zip(LABELS, items, strict=False)),
            "curator": curator,
            "reader": reader,
        }

    def test_a_manager_arranges_released_and_unreleased_members_alike(self, gated):
        dataset, items, client = gated["dataset"], gated["items"], _client(gated["curator"])
        for name in ("delta", "bravo", "charlie", "alpha"):
            assert _order(client, dataset, items[name], "bottom").status_code == 200
        rows = _listing(client, dataset)
        assert [(row["object_name"], row["position"]) for row in rows] == [
            ("delta", 1),
            ("bravo", 2),
            ("charlie", 3),
            ("alpha", 4),
        ]

    def test_a_reader_is_listed_the_released_members_in_the_managers_order_and_nothing_else(self, gated):
        dataset, items = gated["dataset"], gated["items"]
        _place({items["delta"]: 1, items["bravo"]: 2, items["charlie"]: 3, items["alpha"]: 4})
        rows = _listing(_client(gated["reader"]), dataset)
        assert _names(rows) == ["bravo", "alpha"]
        # No position, so the two places held by unreleased members ahead of these are not there to count.
        assert [row["position"] for row in rows] == [None, None]
        assert [row["id"] for row in rows] == [None, None]

    def test_what_a_reader_is_served_does_not_change_with_an_unreleased_members_place(self, gated):
        dataset, items = gated["dataset"], gated["items"]
        _place({items["bravo"]: 1, items["alpha"]: 2})
        before = _listing(_client(gated["reader"]), dataset)
        _place({items["delta"]: 1, items["bravo"]: 2, items["charlie"]: 3, items["alpha"]: 4})
        assert _listing(_client(gated["reader"]), dataset) == before
        _place({items["delta"]: None, items["charlie"]: None})
        assert _listing(_client(gated["reader"]), dataset) == before

    def test_a_reader_cannot_arrange(self, gated):
        response = _order(_client(gated["reader"]), gated["dataset"], gated["items"]["alpha"], "top")
        assert response.status_code == 403

    def test_a_pool_is_arranged_like_any_dataset(self, gated):
        Dataset.objects.filter(pk=gated["dataset"].pk).update(submission_profile="test.pool")
        response = _order(_client(gated["curator"]), gated["dataset"], gated["items"]["bravo"], "top")
        assert response.status_code == 200
        assert DatasetItem.objects.get(pk=gated["items"]["bravo"].pk).position == 1

    def test_a_release_run_leaves_positions_alone(self, gated):
        dataset, items = gated["dataset"], gated["items"]
        _place({items["delta"]: 1})
        _release(dataset, items["charlie"], items["delta"])
        assert _positions(dataset) == {"delta": 1, "alpha": None, "bravo": None, "charlie": None}
        assert _names(_listing(_client(gated["reader"]), dataset)) == ["delta", "alpha", "bravo", "charlie"]


class TestSnapshot:
    def test_reordering_does_not_change_a_snapshots_manifest_hash(self, arranged):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        for index, recording in enumerate(Recording.objects.order_by("pk")):
            Recording.objects.filter(pk=recording.pk).update(content_hash=f"{index:064X}")
        url = f"{DATASETS}/{dataset.object_hash}/snapshots/"
        first = post_json(client, url, {"label": "before"}).json()
        for name, action in (("echo", "top"), ("alpha", "bottom"), ("charlie", "top"), ("echo", "clear")):
            assert _order(client, dataset, items[name], action).status_code == 200
        second = post_json(client, url, {"label": "after"}).json()
        assert second["manifest_hash"] == first["manifest_hash"]
        assert second["manifest"] == first["manifest"]


class TestRegisteredMemberType:
    @pytest.fixture
    def casefile(self, monkeypatch):
        """``Dataset`` registered as a member type, named by a Python callable as a plugin's type is."""
        monkeypatch.setattr(item_types, "_TYPES", {})
        register_item_type(
            ItemType(
                object_type="casefile",
                model_label="library.dataset",
                hash_field="object_hash",
                display_name=lambda obj: obj.name,
                normalise_hash=lambda value: value.strip().upper(),
            )
        )

    def test_a_member_of_a_registered_type_is_arranged_like_any_other(self, arranged, casefile):
        dataset, items, client = arranged["dataset"], arranged["items"], _client(arranged["author"])
        member = Dataset.objects.create(author=arranged["author"], name="a case")
        case = _add(dataset, member)
        assert _order(client, dataset, case, "top").status_code == 200
        assert _order(client, dataset, items["delta"], "bottom").status_code == 200
        rows = _listing(client, dataset)
        assert [(row["object_type"], row["position"]) for row in rows][:2] == [("casefile", 1), ("recording", 2)]
        assert _names(_listing(_client(arranged["reader"]), dataset))[:2] == ["a case", "delta"]
