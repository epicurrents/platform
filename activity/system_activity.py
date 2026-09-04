"""Audited-scope entry point for Celery tasks and management commands.

⚠️ LOAD-BEARING — audit coverage for every non-HTTP write, and the
``target_identifier`` contract.
Nothing else opens an audited scope outside a request, so narrowing what
this context manager sets strips ``ObjectChangeLog`` coverage from every
Celery task and management command at once — the same silent failure as
``epicurrents/middleware.py`` on the request side, with no error to
notice. Separately, ``target_identifier`` must remain a *locator*: it
held ``str(target)`` until 2026-08-26, which published
``Recording.original_name`` into a permanent column on every processed
recording, past the field mask that keeps it out of ``ObjectChangeLog``,
past the author-private gate on the API, and past every erasure path —
none of which touch that column. Anything rendered rather than
referenced here cannot be recalled. See AGENTS.md → *Load-bearing files*
before modifying; the contract test is
``activity/tests/test_system_activity_identifier.py``.

Celery tasks and management commands have no HTTP request, so
``ApiActivityLoggingMiddleware`` never runs and the audit-signal layer
treats their ORM writes as un-audited.  ``with_system_activity`` is the
opt-in: opening the context manager creates one ``Activity`` row that
covers the whole operation and flips the audit-context ContextVars so
nested signal-driven writes attribute to that row exactly the way they
would in an HTTP request.

Bulk ORM operations (``QuerySet.update``, ``bulk_create``,
``QuerySet.delete``) still bypass the signals — they fire no
``post_save`` and ``pre_delete`` only for non-fast-path deletes. Inside
a ``with_system_activity`` scope, those paths must additionally call
``record_create_change`` / ``record_modify_change`` / ``record_delete_change``
from ``activity.audit`` to land in ``ObjectChangeLog``. The scope gives
those explicit rows the parent ``Activity`` link; the helpers read it
via ``get_current_activity``.

The audit-context flag set here is the same one the HTTP middleware
flips: ``current_is_audited_context``. Nesting an ``with_system_activity``
inside an HTTP request is supported (the inner scope supersedes for the
duration of its body and the outer state is restored on exit), though in
practice this happens only when a view executes a Celery-style task
synchronously, which is rare.
"""

import logging
from contextlib import contextmanager

from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError
from django.db.utils import ProgrammingError

from .models import Activity
from .request_context import reset_request_context, set_request_context

logger = logging.getLogger(__name__)


@contextmanager
def with_system_activity(
    verb: str,
    *,
    interface: str,
    target=None,
    metadata: dict | None = None,
    actor=None,
    project: str = "",
):
    """Open an audited scope for a non-request caller.

    Creates an ``Activity`` row whose ``interface`` distinguishes the
    caller (Celery vs. management command) and exposes it to the audit
    signals for the duration of the ``with`` block.  ``verb`` follows the
    ``<app>.<resource>.<action>`` taxonomy already used by HTTP endpoints
    (e.g. ``recordings.process``, ``recordings.import``).  ``target`` is
    the primary model instance the operation acts on, when one exists;
    its content_type and pk are recorded so the audit timeline can be
    filtered by target.  ``metadata`` carries operation-specific context
    that isn't already in the target snapshot (counts, parameters).
    ``actor`` lets a management command opt to record a triggering user
    when one is known (e.g. ``--user`` flag); leave ``None`` when the
    operation is genuinely system-initiated.
    """

    if interface not in Activity.Interface.values:
        raise ValueError(
            f"with_system_activity: unknown interface {interface!r}; choose from {Activity.Interface.values}"
        )

    target_ct = None
    target_id = ""
    target_identifier = ""
    if target is not None:
        # An unsaved instance produces an Activity row with content_type
        # set but no object id — an unresolvable generic-FK state that
        # silently corrupts audit timeline queries. Refuse loudly instead.
        if target.pk is None:
            raise ValueError("with_system_activity: target must be a saved instance (target.pk is None)")
        target_ct = ContentType.objects.get_for_model(target, for_concrete_model=False)
        target_id = str(target.pk)
        # A locator, not a rendering. This used to be ``str(target)``, which
        # writes whatever the model's ``__str__`` happens to include into a
        # permanent field no erasure path touches — and the models passed here
        # render themselves with the very fields the platform treats as PHI:
        # ``Recording.__str__`` embeds ``original_name``, ``ImportJob.__str__``
        # embeds ``source_path``. So every processed recording published its
        # uploaded filename to the audit trail, past the mask that keeps it out
        # of ``ObjectChangeLog`` and past the author-private gate on the API.
        # The form below matches what ``create_chained_change_log`` already
        # writes for change-log targets, and carries the same information: the
        # content type and pk are the two columns beside it.
        target_identifier = f"{target_ct.app_label}.{target_ct.model}:{target_id}"[:512]

    # Mirror ApiActivityLoggingMiddleware's graceful degradation: a
    # failure to insert the Activity row (rolling deploy with a
    # missing column, transient DB outage) must not crash the wrapped
    # task. Yield with no audited scope so the body still runs; the
    # audit gap is logged at WARNING for operator visibility.
    try:
        activity = Activity.objects.create(
            actor=actor,
            interface=interface,
            verb=verb,
            method="",
            path="",
            project=project,
            status_code=None,
            target_content_type=target_ct,
            target_object_id=target_id,
            target_identifier=target_identifier,
            metadata=metadata or {},
        )
    except (DatabaseError, ProgrammingError):
        logger.warning(
            "with_system_activity: failed to create Activity row for verb=%s "
            "interface=%s — yielding without an audited scope",
            verb,
            interface,
            exc_info=True,
        )
        yield None
        return

    tokens = set_request_context(user=actor, activity=activity, is_audited=True)
    try:
        yield activity
    finally:
        reset_request_context(tokens)
