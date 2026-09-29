"""The contextual-assessment record a sharer may keep on a grant, and the currency of the platform's own assessment.

⚠️ LOAD-BEARING — the sharer's finding and the assessment's currency.
Two contracts live here. ``assessment_visible`` decides who is told that a contextual
finding exists for a grant: the giver, the object's author and superusers. Widening it
tells a grantee that someone concluded they cannot identify anyone from what they
receive, which is a statement about the recipient the platform has no business making
to them, and narrowing it hides the record from the sharer who has to re-run it. And
the constants below are the platform's statement of which guidelines version the
assessment in ``docs/anonymisation-compliance.md`` was written against and when it was
last reviewed: ``grant_assessments`` prints them at the head of every sweep, the
``epicurrents.W020`` check warns when the review is overdue, and the ``anonymisation``
review agent reads the document's copy. A value changed here without the document, or
in the document without this module, leaves two different dates in circulation, which
``epicurrents/tests/test_assessment_currency.py`` pins against; the visibility and the
set-together rule are pinned by ``epicurrents/tests/test_grant_assessments.py``.

EDPB Guidelines 02/2026 let a controller conclude that data is anonymous *for a particular
recipient* through the contextual approach (paragraph 96), and ask for that conclusion to be
written down and dated (paragraph 41), kept with the data, and re-checked when capabilities
change or after an incident (paragraphs 42 and 88). The platform cannot make the finding: it
does not know who the recipient is or what they can reach. It can hold the finding beside the
grant it applies to and make the reassessment triggers observable, which is what
``AccessRight.assessment_reference`` and ``AccessRight.assessment_date`` are for, and what
``manage.py grant_assessments`` reports on.

The record is the sharer's. It is shown to the row's ``access_giver``, the object's author and
superusers, and to nobody else: a grantee never learns whether an assessment about them exists,
and the Art. 15 export carries it under the grants a subject gave, never the grants they
received. It is free text a person typed, so it is registered for erasure as the giver's data,
and it is never written into activity metadata.

Two fields, always together. A date without a reference is a claim with no document behind it,
and a reference without a date cannot be swept for age, so the model constraint, this module's
validation and every write surface accept both or neither.

The platform's own assessment has a currency too. The guidelines are a consultation draft whose
paragraph numbers may move at final adoption, the state of the art in subject identification the
assessment assumes moves on its own, and the six-monthly sweep in ``docs/gdpr-compliance.md`` is
where both are re-read. The constants below say which version the document is written against
and when it was last read in full; :func:`assessment_currency` turns them into the review-by
date the sweep and the system check report. Moving ``ASSESSMENT_REVIEWED_ON`` is the record of a
review having happened, so it moves only when someone has re-read the document, and moves in the
same commit as the document's own Currency block.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.utils import timezone

#: Length of ``AccessRight.assessment_reference``; a document identifier or a URL, not the assessment.
ASSESSMENT_REFERENCE_MAX_LENGTH = 512

#: The instrument the assessment in ``docs/anonymisation-compliance.md`` is written against. The
#: document's Currency block carries the same five values; ``test_assessment_currency`` fails when
#: either copy moves without the other.
GUIDELINES = "EDPB Guidelines 02/2026 on Anonymisation"
#: The version whose paragraph numbers every ``¶ n`` in the code and the documents refers to.
GUIDELINES_VERSION = "1.0"
#: Where that version stands: a consultation draft's numbering may move at final adoption.
GUIDELINES_STATUS = "consultation draft, adopted 7 July 2026"
#: The date the assessment was last read in full against the guidelines and the serving surfaces.
ASSESSMENT_REVIEWED_ON = date(2026, 9, 24)
#: Half a year, the cadence of the GDPR sweep in ``docs/gdpr-compliance.md``.
ASSESSMENT_REVIEW_INTERVAL_DAYS = 183


def assessment_currency(today: date | None = None) -> dict[str, Any]:
    """The platform assessment's own currency: the guidelines version, the last review and the next one due.

    ``review_by`` is the last review plus the sweep interval; ``overdue`` is whether *today* (the
    local date when omitted) has passed it, and ``days_remaining`` is negative by that many days
    once it has. Every value is derived from the constants above, so the sweep, the system check
    and a test read one answer.
    """
    today = today or timezone.localdate()
    review_by = ASSESSMENT_REVIEWED_ON + timedelta(days=ASSESSMENT_REVIEW_INTERVAL_DAYS)
    return {
        "guidelines": GUIDELINES,
        "guidelines_version": GUIDELINES_VERSION,
        "guidelines_status": GUIDELINES_STATUS,
        "reviewed_on": ASSESSMENT_REVIEWED_ON,
        "review_interval_days": ASSESSMENT_REVIEW_INTERVAL_DAYS,
        "review_by": review_by,
        "days_remaining": (review_by - today).days,
        "overdue": today > review_by,
    }


def normalise_assessment(reference: str | None, assessment_date: date | None) -> tuple[str, date | None]:
    """Return the ``(reference, date)`` pair as it is stored, or raise ``ValueError`` naming the problem.

    An empty pair clears the record. The reference is stripped of surrounding whitespace, and the date may
    not lie in the future: an assessment is something that was made, not something scheduled.
    """
    reference = (reference or "").strip()
    if len(reference) > ASSESSMENT_REFERENCE_MAX_LENGTH:
        raise ValueError(f"assessment_reference may be at most {ASSESSMENT_REFERENCE_MAX_LENGTH} characters.")
    if bool(reference) != (assessment_date is not None):
        raise ValueError("assessment_reference and assessment_date go together: give both, or neither to clear.")
    if assessment_date is not None and assessment_date > timezone.localdate():
        raise ValueError("assessment_date may not be in the future.")
    return reference, assessment_date


def ensure_can_assess(request, user, obj, right, *, object_label: str) -> None:
    """Raise 403, security-logged, unless *user* may record the assessment on *right*.

    The same three answers as :func:`assessment_visible`; the caller has already established the
    access-management authority on *obj*, so a refusal here is a delegated sharer reaching for a
    grant they did not give.
    """
    from ninja.errors import HttpError

    from epicurrents.security_log import get_client_ip, log_security_event

    if assessment_visible(right, user, obj):
        return
    log_security_event(
        "permission.denied",
        permission="assess",
        actor_id=user.pk,
        ip=get_client_ip(request),
        object_type=type(obj).__name__,
        object_id=str(obj.pk),
    )
    raise HttpError(
        403, f"Only the grant's giver, the {object_label}'s author or a superuser may record its assessment"
    )


def assessment_visible(right, user, obj=None) -> bool:
    """Whether *user* may see the assessment recorded on *right*.

    The row's giver, the object's author and superusers. *obj* is the granted object when the caller has
    it; without it only the first two answers are available, which is the right answer for a surface that
    lists rights across objects.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or right.access_giver_id == user.pk:
        return True
    return obj is not None and getattr(obj, "author_id", None) == user.pk
