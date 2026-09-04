"""Art. 15 subject access export — everything the platform holds about one user.

The mirror of :mod:`user.management.commands.erase_user`. Erasure answers "remove
what you hold about me"; this answers "show me what you hold about me", and the
two have to agree about what that is. Where they disagree, one of them is wrong,
and the answer is now written down in one place — :data:`RELATION_HANDLING`
classifies every reverse relation pointing at the user model, so a model added
later cannot be silently left out of either.

Three rules decide what goes in.

**Credentials never do.** Not the password hash, not the TOTP secret or its
recovery codes, not a push subscription's keys. Those are excluded by reusing
``activity.audit``'s masked-field registry rather than a second list: it already
declares, per model, which fields must never reach the audit trail in the clear,
and the same fields must never reach an export. A project registering a new
credential field gets it excluded here for free. The registry is consulted at
call time, so a late registration still applies.

**Other people's data never does either**, even when it is reachable from the
subject's own rows. The sharp case is ``ObjectChangeLog``: the subject's edits
are their personal data, but the ``before_state``/``changes`` payloads describe
the object they edited, which may be someone else's recording or account. So the
change log is exported as the *fact* of each change — what was touched, when,
which action — and never its payloads. Art. 15(4) says the right to a copy must
not adversely affect the rights of others; this is that line, drawn concretely.

**Recording and media *files* are not included.** The export is about the account
holder, and those files are clinical data about the people recorded, who are a
different set of data subjects entirely (see the Notice B half of
docs/privacy-notice-template.md). The metadata rows are exported, so the subject
can see what they uploaded and when, and the bytes stay behind the ordinary
download endpoints where the usual access checks apply.

The payload is JSON-serialisable and self-describing: it names what was excluded
and why, so a reader can tell a deliberate omission from a gap.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from activity.audit import registered_masked_fields

#: Version of the export payload's shape. Bump when a consumer would have to
#: change; the format is a documented deliverable once a subject has received one.
EXPORT_FORMAT_VERSION = 1


class _Handling:
    """How one reverse relation is treated. See :data:`RELATION_HANDLING`."""

    def __init__(
        self,
        mode: str,
        *,
        reason: str = "",
        fields: tuple[str, ...] = (),
        title: str = "",
        include_masked: tuple[str, ...] = (),
    ):
        self.mode = mode
        self.reason = reason
        self.fields = fields
        self.title = title
        self.include_masked = include_masked


def _export(*fields: str, title: str = "", include_masked: tuple[str, ...] = ()) -> _Handling:
    """Include these rows, limited to *fields*, under a human heading.

    *include_masked* names fields that are registered for audit masking but that
    the subject is nonetheless entitled to see in their own export. The two are
    not the same question, and reusing one registry for both is what made this
    parameter necessary: ``register_masked_fields`` carries credentials
    (``password``, a TOTP secret) that nobody may see, and *author-private*
    values (``Recording.original_name``) that are hidden from grantees precisely
    because they belong to the author — who is the subject of this export.

    Default-deny with a named exception, rather than the reverse, so a field
    added to the masking registry later is excluded until someone decides
    otherwise. The system check rejects a name here that is not actually masked,
    which would be dead configuration reading as a deliberate decision.
    """
    return _Handling("export", fields=fields, title=title, include_masked=include_masked)


def _omit(reason: str, *, title: str = "") -> _Handling:
    """Leave these rows out, for a reason the document will state."""
    return _Handling("omit", reason=reason, title=title)


#: Every reverse relation on the user model, and what the export does with it.
#:
#: Keyed by ``"<app_label>.<ModelName>:<field name>"`` because two relations can
#: come from one model — ``AccessRight`` points at a user twice, once as the
#: grantor and once as the grantee, and they mean different things.
#:
#: A relation missing from this map is a test failure, not a default. That is the
#: whole point: the failure mode of an export is silent omission, and a new model
#: carrying personal data would otherwise join the schema without joining the
#: export, with nothing to notice until a subject asked and got an incomplete
#: answer.
RELATION_HANDLING: dict[str, _Handling] = {
    # ── The account itself, and what the subject configured ──────────────────
    "user.userpreference:user": _export("scope", "values", "created_at", "modified_at", title="Your saved settings"),
    "user.externalidentity:user": _export(
        "provider",
        "issuer",
        "subject",
        "email",
        "email_verified",
        "created_at",
        "last_login_at",
        title="External sign-in identities",
    ),
    # Everything but the secret and the recovery codes, which the masked-field
    # registry strips anyway; listing the safe fields explicitly means a new
    # column on this model is omitted until someone decides it is safe.
    "user.twofactorcredential:user": _export("confirmed_at", "created_at", title="Two-factor authentication"),
    "notifications.pushsubscription:user": _export("endpoint", "created_at", title="Push notification subscriptions"),
    # ── What the subject created ─────────────────────────────────────────────
    # original_name is masked in the audit trail as author-private PHI, not as a
    # credential — and the subject of this export *is* the author, so they are
    # entitled to the filename they uploaded.
    # original_name is masked in the audit trail as author-private PHI rather than
    # as a credential, and the subject of this export *is* the author — so they
    # are entitled to the filename they uploaded. Without the opt-in the field
    # was declared here and silently dropped, which is the incomplete-document
    # failure this module exists to avoid, produced by its own credential filter.
    "recordings.recording:author": _export(
        "display_name",
        "original_name",
        "file_extension",
        "file_size",
        "status",
        "created_at",
        "deleted_at",
        title="Recordings you uploaded",
        include_masked=("original_name",),
    ),
    "media.mediafile:author": _export(
        "original_name",
        "media_type",
        "file_size",
        "created_at",
        "deleted_at",
        title="Media files you uploaded",
        include_masked=("original_name",),
    ),
    "library.collection:author": _export(
        "name", "description", "created_at", "deleted_at", title="Collections you created"
    ),
    "library.dataset:author": _export("name", "description", "created_at", "deleted_at", title="Datasets you created"),
    "library.tag:author": _export("name", "description", "created_at", title="Tags you created"),
    "annotations.annotation:author": _export(
        "object_hash", "name", "content", "created_at", "modified_at", title="Annotations you wrote"
    ),
    "annotations.event:author": _export(
        "object_hash",
        "name",
        "event_class",
        "timestamp",
        "duration",
        "value",
        "created_at",
        "modified_at",
        title="Events you marked",
    ),
    "annotations.interruption:author": _export(
        "object_hash", "timestamp", "duration", "created_at", "modified_at", title="Interruptions you marked"
    ),
    "annotations.label:author": _export(
        "object_hash", "name", "value", "created_at", "modified_at", title="Labels you applied"
    ),
    "recordings.importjob:owner": _export(
        "status", "pipeline_label", "created_at", "completed_at", title="Bulk imports you ran"
    ),
    # ── Sharing, both directions ─────────────────────────────────────────────
    # Who the subject shared with, and who shared with them — as the terms of
    # each grant and when it was made, without the counterpart's identity or the
    # object shared. Naming the other party would disclose a third person inside
    # this subject's export, and identifying the object would often mean naming a
    # recording that belongs to someone else again. Art. 15(1)(c) is satisfied by
    # the recipients section of the privacy notice rather than row by row here.
    "epicurrents.accessright:access_giver": _export(
        "can_read", "can_write", "can_share", "expires_at", "created_at", title="Access you granted to others"
    ),
    "epicurrents.accessright:access_target": _export(
        "can_read", "can_write", "can_share", "expires_at", "created_at", title="Access others granted to you"
    ),
    # ── What the subject did ─────────────────────────────────────────────────
    "activity.activity:actor": _export(
        "verb",
        "interface",
        "method",
        "path",
        "status_code",
        "target_identifier",
        "created_at",
        title="Your activity log",
    ),
    # Deliberately no before_state / changes / extra_payload — see the module
    # docstring. The subject's action is theirs; the object's contents may not be.
    "activity.objectchangelog:performed_by": _export("action", "object_id", "created_at", title="Changes you made"),
    "compute.pipelinerunaudit:actor": _export("action", "created_at", title="Analysis runs you started"),
    # ── Not the subject's personal data ──────────────────────────────────────
    "federation.federatedpeer:added_by": _omit(
        "records which operator added a peer instance; the row is about the peer, "
        "and the subject's involvement is already in their activity log",
        title="Federated peers you added",
    ),
    # Declared on the user model rather than pointing at it — see _m2m_key.
    "user:groups": _export("name", title="Groups you belong to"),
    "user:user_permissions": _omit(
        "individual Django permissions, which this deployment grants through groups "
        "and staff/superuser flags rather than per user; the flags are in the account "
        "section above",
        title="Individual permissions",
    ),
    "admin.logentry:user": _omit(
        "Django admin's own audit table, which this deployment does not use as an "
        "administrative surface; equivalent actions appear in the activity log",
        title="Django admin actions",
    ),
}


def register_export_relation(
    model_label: str, field_name: str, *, fields=(), omit_reason: str = "", title: str = ""
) -> None:
    """Declare how a relation to the user model is treated by the subject export.

    For projects and plugins, called from ``AppConfig.ready()``. The set of things
    pointing at a user depends on what is installed — the active project alone
    adds between one and eight relations — so the core map below cannot be the
    whole answer, and a relation nobody classified is a subject access request
    that silently returns less than it should.

    Give either *fields* (export these columns) or *omit_reason* (leave the rows
    out, and say why in the payload). Naming neither, or both, is a configuration
    error that ``manage.py check`` reports; see :mod:`user.checks`.

    ``model_label`` is the lowercase ``app_label.model_name`` pair, as
    ``register_subject_pii`` takes.
    """
    key = f"{model_label.lower()}:{field_name}"
    if omit_reason:
        RELATION_HANDLING[key] = _omit(omit_reason, title=title)
    else:
        RELATION_HANDLING[key] = _export(*fields, title=title)


def _model_label(relation) -> str:
    """The key :data:`RELATION_HANDLING` uses for one reverse relation."""
    return f"{relation.related_model._meta.label_lower}:{relation.field.name}"


def _m2m_key(field) -> str:
    """The key for a many-to-many field declared *on* the user model.

    A separate form because these are invisible to ``_meta.related_objects`` —
    the relation is declared on the user side, so nothing points back and the
    coverage walk never sees it. ``groups`` is the one that matters: which groups
    you belong to is personal data, and AGENTS.md already records the same table
    as a blind spot for the audit signals, which is how it was missed here too.
    """
    return f"user:{field.name}"


def unclassified_relations() -> list[str]:
    """Reverse relations on the user model that :data:`RELATION_HANDLING` misses.

    Exposed so a test can assert it is empty. A non-empty result means personal
    data may exist that no export would return and no reviewer was asked about.
    """
    user_model = get_user_model()
    keys = [_model_label(relation) for relation in user_model._meta.related_objects]
    keys += [_m2m_key(field) for field in user_model._meta.many_to_many]
    return sorted(key for key in keys if key not in RELATION_HANDLING)


def _safe_fields(model, wanted: tuple[str, ...], include_masked: tuple[str, ...] = ()) -> list[str]:
    """*wanted*, minus anything registered as a credential for this model.

    Consulted per call rather than resolved once at import, so a project that
    registers a masked field in its ``AppConfig.ready`` is honoured even though
    this module was imported first.
    """
    masked = registered_masked_fields(model._meta.label_lower) - set(include_masked)
    return [name for name in wanted if name not in masked]


def _serialise_rows(user, relation, handling: _Handling) -> list[dict[str, Any]]:
    """Rows of one related model belonging to *user*, limited to safe fields."""
    model = relation.related_model
    fields = _safe_fields(model, handling.fields, handling.include_masked)
    if not fields:
        return []
    queryset = model._default_manager.filter(**{relation.field.name: user}).order_by("pk")
    return [{name: row[name] for name in fields} for row in queryset.values(*fields)]


def export_user_data(user) -> dict[str, Any]:
    """Everything the platform holds about *user*, as a JSON-serialisable dict.

    The account fields come first, then one entry per exported relation. Omitted
    relations are listed with their reason rather than dropped, so the payload
    distinguishes "we hold nothing of this kind" from "we chose not to include
    it" — a distinction a subject is entitled to see.
    """
    user_model = get_user_model()
    account_fields = _safe_fields(
        user_model,
        (
            "username",
            "first_name",
            "last_name",
            "email",
            "date_joined",
            "last_login",
            "is_staff",
            "is_superuser",
            "is_active",
        ),
    )

    data: dict[str, Any] = {
        "format_version": EXPORT_FORMAT_VERSION,
        "subject_id": user.pk,
        "account": {name: getattr(user, name) for name in account_fields},
        "data": {},
        "omitted": {},
        "notes": [
            (
                "Credential material (password hash, two-factor secret and recovery "
                "codes, push subscription keys) is excluded and cannot be exported."
            ),
            (
                "Change-log entries list the fact of each change you made, not the "
                "contents of the object you changed, which may describe someone else."
            ),
            (
                "Recording and media file contents are not included; download them from "
                "the application. The metadata rows here list what you uploaded."
            ),
        ],
    }

    for field in sorted(user_model._meta.many_to_many, key=lambda f: f.name):
        key = _m2m_key(field)
        handling = RELATION_HANDLING.get(key)
        if handling is None:
            data["omitted"][key] = "not classified for export — this is a bug, please report it"
        elif handling.mode == "omit":
            data["omitted"][key] = handling.reason
        else:
            fields = _safe_fields(field.related_model, handling.fields)
            related = getattr(user, field.name).all().order_by("pk")
            data["data"][key] = [dict(zip(fields, values)) for values in related.values_list(*fields)]

    for relation in sorted(user_model._meta.related_objects, key=lambda r: _model_label(r)):
        key = _model_label(relation)
        handling = RELATION_HANDLING.get(key)
        if handling is None:
            # Defensive: the test asserts this never happens, but an export that
            # silently skipped an unknown relation is the failure being avoided.
            data["omitted"][key] = "not classified for export — this is a bug, please report it"
            continue
        if handling.mode == "omit":
            data["omitted"][key] = handling.reason
            continue
        data["data"][key] = _serialise_rows(user, relation, handling)

    return data


def _heading(key: str, handling: _Handling) -> str:
    """A human title for one section, derived when none was registered.

    Projects are not obliged to supply one — a fallback that reads acceptably
    beats a required argument nobody fills in thoughtfully.
    """
    if handling.title:
        return handling.title
    model_label, field_name = key.rsplit(":", 1)
    return f"{model_label.split('.')[-1].title()} ({field_name})"


def _format_value(value) -> str:
    """One field value, rendered for a person rather than a parser."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True)
    return str(value)


def export_user_text(user) -> str:
    """The export as a plain-text document, which is what a subject receives.

    Text rather than JSON because the recipient is a person exercising a right,
    not a system consuming a feed: a document with headings can be read and
    checked, where a JSON tree asks the subject to parse it before they can tell
    whether it is complete. :func:`export_user_json` remains for the Art. 20
    machine-readable case, and both render the same
    :func:`export_user_data` payload — the classification is the hard part and
    is shared, so the two cannot describe different sets of data.
    """
    payload = export_user_data(user)
    out: list[str] = []

    def rule(char: str = "=") -> None:
        out.append(char * 72)

    rule()
    out.append("PERSONAL DATA EXPORT")
    rule()
    out.append("")
    out.append(f"Subject:   {user.get_username()} (internal id {user.pk})")
    out.append(f"Generated: {timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    out.append("")
    out.append("This document lists the personal data this system holds about you.")
    out.append("")

    out.append("WHAT IS NOT INCLUDED")
    rule("-")
    for note in payload["notes"]:
        out.append(f"  * {note}")
    out.append("")

    out.append("ACCOUNT")
    rule("-")
    width = max(len(name) for name in payload["account"]) if payload["account"] else 0
    for name, value in sorted(payload["account"].items()):
        out.append(f"  {name.replace('_', ' ').capitalize():<{width}}  {_format_value(value)}")
    out.append("")

    sections = sorted(payload["data"].items(), key=lambda item: _heading(item[0], RELATION_HANDLING[item[0]]).lower())
    for key, rows in sections:
        heading = _heading(key, RELATION_HANDLING[key])
        out.append(f"{heading.upper()} ({len(rows)})")
        rule("-")
        if not rows:
            out.append("  Nothing held.")
            out.append("")
            continue
        # One line per row, columns in the order the relation registered them
        # rather than alphabetical: the registration order is the one a reader
        # was meant to scan, and a field-per-line form turns an ordinary
        # activity log into thousands of lines nobody reads.
        columns = list(rows[0].keys())
        out.append("  " + " | ".join(name.replace("_", " ").capitalize() for name in columns))
        for row in rows:
            out.append("  " + " | ".join(_format_value(row[name]) for name in columns))
        out.append("")

    out.append("HELD BUT NOT EXPORTED, AND WHY")
    rule("-")
    for key, reason in sorted(payload["omitted"].items()):
        handling = RELATION_HANDLING.get(key)
        heading = _heading(key, handling) if handling else key
        out.append(f"  {heading}:")
        out.append(f"       {reason}")
    out.append("")

    return "\n".join(out) + "\n"


def export_user_json(user, *, indent: int = 2) -> str:
    """:func:`export_user_data` rendered as JSON, dates and decimals included."""
    return json.dumps(export_user_data(user), cls=DjangoJSONEncoder, indent=indent, sort_keys=True)


def resolve_subject(*, username: str | None = None, user_id: int | None = None):
    """Find the subject of an export by username or id, or return ``None``.

    Mirrors ``erase_user``'s lookup so the two commands identify a person the
    same way.
    """
    user_model = get_user_model()
    if username:
        return user_model.objects.filter(username=username).first()
    if user_id is not None:
        return user_model.objects.filter(pk=user_id).first()
    return None
