"""The manual order of a dataset's members: what each order action does to a folder's arranged sequence.

A member is either arranged, with a ``DatasetItem.position``, or unsorted, with none, and a listing puts the
arranged members of a folder first (``library.release.dataset_member_ordering``). The order belongs to one folder
of the dataset, the root being one: a position says where a member stands among the arranged members beside it on
the dataset page, and the flat listing the viewer opens follows the same page top to bottom.

Nothing here runs when a member is added. A member arrives unsorted and stays so until a manager places it, since
a position handed out on arrival would be an arrival order under another name.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

#: The order actions, as the endpoint's ``action`` names them.
ACTIONS = ("up", "down", "top", "bottom", "clear")


def arrange(sequence: Sequence[int], item_id: int, action: str, shown: Collection[int] | None = None) -> list[int]:
    """The arranged sequence of a folder after *action* on *item_id*; ids in order, *item_id* absent once cleared.

    *sequence* is the folder's arranged members in their current order. *shown* is the members the manager's
    listing shows, where some are not (a trashed recording keeps its membership and its place): ``up`` and
    ``down`` step over a member that is not shown, so the action moves the row past the neighbour the manager
    sees. ``None`` means every member is shown.

    - ``up`` and ``down`` on an arranged member swap it with its shown neighbour, and change nothing at the edge.
    - ``top`` and ``bottom`` put the member first or last among the arranged, whether or not it was arranged.
    - ``up`` on an unsorted member arranges it, last: the arranged block ends just above the unsorted one, so
      that is the next place up. ``down`` on an unsorted member changes nothing, there being nothing below the
      arranged block to move into.
    - ``clear`` returns the member to the unsorted ones, and changes nothing for one already there.
    """
    if action not in ACTIONS:
        raise ValueError(action)
    current = list(sequence)
    rest = [pk for pk in current if pk != item_id]
    if action == "clear":
        return rest
    if action == "top":
        return [item_id, *rest]
    if action == "bottom":
        return [*rest, item_id]
    if item_id not in current:
        return [*rest, item_id] if action == "up" else current
    index = current.index(item_id)
    candidates = range(index - 1, -1, -1) if action == "up" else range(index + 1, len(current))
    target = next((i for i in candidates if shown is None or current[i] in shown), None)
    if target is None:
        return current
    # Up lands in the neighbour's slot; down lands after the neighbour, whose index fell by one with the removal.
    rest.insert(target, item_id)
    return rest
