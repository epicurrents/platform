"""The erasure record: account erasures written where a database restore cannot reach them.

A rollback that restores the database brings back every row the dump holds,
including accounts erased after the dump was taken — an Art. 17 erasure undone
by an operation nobody meant as one. The database cannot remember the erasure
for itself, since the restore is what forgets it, so ``erase_user`` appends a
line to ``erasures.jsonl`` in the spool, which ``update.sh`` excludes from every
snapshot and restore:

    {"at": "<ISO time of the erasure>", "user_id": <pk>, "date_joined": "<ISO time>"}

Primary keys and timestamps only, per AGENTS.md → *Activity metadata carries
identifiers, never names*: the record outlives the account and must not become
personal data of its own. ``date_joined`` is the guard against a reused primary
key: a restore resets the sequence, and a new account can be given the number
of an erased one.

Two consumers. A rollback request that would restore the database answers 409
with the count of erasures since the snapshot until the caller acknowledges
them, and once such a rollback settles, :func:`reapply` erases again every
recorded account that came back, through ``erase_user`` itself so the account
goes the whole sanctioned way — files, sessions, cascade, audit scrub.
"""

import json
import logging
import os
from datetime import UTC, datetime

from django.utils import timezone

from maintenance.lock import parse_timestamp, spool_path

logger = logging.getLogger(__name__)

RECORD_NAME = "erasures.jsonl"
# A line is a few dozen bytes; anything much longer was not written here.
_LINE_LIMIT = 512


def record_path():
    """The erasure record in the spool root."""
    return spool_path() / RECORD_NAME


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def read_records() -> list[dict]:
    """Every well-formed line of the record, as ``{"at", "user_id", "date_joined"}`` with parsed times.

    A missing file is an empty record. A line that does not parse is skipped
    with a warning, never fatal: the record is read by request endpoints.
    """
    path = record_path()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("The erasure record cannot be read: %s", exc)
        return []
    records = []
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line) if len(line) <= _LINE_LIMIT else None
        except ValueError:
            data = None
        at = parse_timestamp(data.get("at")) if isinstance(data, dict) else None
        joined = parse_timestamp(data.get("date_joined")) if isinstance(data, dict) else None
        user_id = data.get("user_id") if isinstance(data, dict) else None
        if at is None or joined is None or not isinstance(user_id, int) or isinstance(user_id, bool):
            logger.warning("Line %d of the erasure record is not a record; skipped", number)
            continue
        records.append({"at": at, "user_id": user_id, "date_joined": joined})
    return records


def record_erasure(user_id: int, date_joined: datetime, *, at: datetime | None = None) -> bool:
    """Append one erasure to the record; ``False`` when it could not be written.

    Opened append-only and without following a symlink, written as one line in
    one ``write``. An account already recorded with the same ``date_joined`` is
    not recorded twice, which keeps a re-erasure after a restore from doubling
    the count a later rollback request reports. The spool root is never created
    here: a deployment without one has no remote rollback to protect against.
    """
    for record in read_records():
        if record["user_id"] == user_id and record["date_joined"] == date_joined:
            return True
    line = json.dumps(
        {"at": _iso(at or timezone.now()), "user_id": user_id, "date_joined": _iso(date_joined)}, sort_keys=True
    )
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(record_path(), flags, 0o640)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        logger.error(
            "Account %s was erased but the erasure could not be recorded in the maintenance spool (%s); a database "
            "restore to a snapshot older than this erasure would bring the account back.",
            user_id,
            exc.strerror or exc,
        )
        return False
    return True


def erasures_since(moment: datetime | None) -> int:
    """How many distinct accounts were erased after ``moment``; every recorded one when it is unknown."""
    users = {record["user_id"] for record in read_records() if moment is None or record["at"] > moment}
    return len(users)


def resurrected() -> list:
    """Accounts that exist again although the record says they were erased: same primary key, same ``date_joined``."""
    from django.contrib.auth import get_user_model

    records = read_records()
    if not records:
        return []
    joined_by_pk: dict[int, set] = {}
    for record in records:
        joined_by_pk.setdefault(record["user_id"], set()).add(record["date_joined"])
    found = []
    for user in get_user_model().objects.filter(pk__in=joined_by_pk.keys()):
        if user.date_joined in joined_by_pk[user.pk]:
            found.append(user)
    return found


def reapply() -> dict:
    """Erase again every recorded account a restore brought back, through ``erase_user``.

    A failure on one account is logged and the rest are still erased; the next
    call, from the next rollback or by hand, tries the failed ones again.
    """
    import io

    from django.core.management import call_command

    counts = {"erased": 0, "failed": 0}
    for user in resurrected():
        try:
            call_command("erase_user", user.get_username(), user_id=user.pk, yes=True, stdout=io.StringIO())
        except Exception:
            logger.exception("Re-erasing account %s after a restore failed", user.pk)
            counts["failed"] += 1
            continue
        counts["erased"] += 1
    if counts["erased"] or counts["failed"]:
        logger.warning(
            "Erasures re-applied after a database restore: %d account(s) erased again, %d failed",
            counts["erased"],
            counts["failed"],
        )
    return counts
