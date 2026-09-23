"""The contextual-assessment record a sharer may keep on a grant.

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
"""

from __future__ import annotations

from datetime import date

from django.utils import timezone

#: Length of ``AccessRight.assessment_reference``; a document identifier or a URL, not the assessment.
ASSESSMENT_REFERENCE_MAX_LENGTH = 512


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
