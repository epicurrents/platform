"""Which annotation rows this app produced, for the annotation-text rule.

A caller reading under a de-identifying grant receives no annotation text they did
not write, because those rows can hold what the serving pipeline strips out of the
recording's bytes. An analysis run's findings are the exception: they are computed
from the (already de-identified) signal rather than transcribed from it, and
withholding them would leave a grantee an analysis view with every finding blank.

``annotations`` cannot ask ``compute`` this directly — the dependency runs the
other way, and the provenance link lives here precisely to keep that app
producer-agnostic — so the answer is registered as a provider from
:meth:`compute.apps.ComputeConfig.ready`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def machine_produced_event_ids(rows: Sequence[Any]) -> set:
    """Return the ids among *rows* that an :class:`~compute.models.AnalysisRun` produced.

    Rows of any other annotation type yield an empty set: only ``annotations.Event``
    carries a ``RunAnnotation`` link, and every annotation type passes through here.
    One query per listing, on the primary keys already in hand.
    """
    from annotations.models import Event

    from .models import RunAnnotation

    event_ids = {row.pk for row in rows if isinstance(row, Event)}
    if not event_ids:
        return set()

    return set(RunAnnotation.objects.filter(event_id__in=event_ids).values_list("event_id", flat=True))
