#!/usr/bin/env python3
"""Emit borgmatic's runtime config at container start.

Replaces the previous heredoc-based YAML generation in
``borgmatic/entrypoint.sh``. The heredoc interpolated
``${DB_USERNAME}`` / ``${DB_PASSWORD}`` / ``${BORG_REMOTE_REPO}`` from
the environment unescaped, so a newline-bearing value injected
arbitrary YAML keys into borgmatic's parsed config — and from there
into borgmatic's ``before_backup`` / ``after_backup`` shell hooks. A
compromised ``.env`` could turn the backup container into an
arbitrary-shell-command-execution surface.

The emitter dumps via ``yaml.safe_dump``, which quotes-and-escapes
every value. Repo paths additionally reject newline / carriage-return
bytes (they cannot legitimately appear in an SSH/SFTP repo URL and
hint at hand-crafted attack input).

Secrets are not written to YAML at all. PostgreSQL credentials reach
``pg_dump`` via the standard libpq env vars ``PGUSER`` / ``PGPASSWORD``
that the borg service already exports.

The emitter also owns the failure-visibility config. A backup that has
been failing for six weeks is indistinguishable from one that has been
working until a restore is attempted, so every run reports twice: an
``after: error`` command hook writes a greppable line to the container
log, and when ``BORG_MONITOR_URL`` is set the Healthchecks hook pings an
external monitor. The monitor is the stronger half — a hook only fires
when borgmatic runs, whereas a monitor expecting a periodic ping alerts
on silence, which is also what a stopped stack looks like.

The monitor is a third-party destination, so the ping carries no content
unless ``BORG_MONITOR_SEND_LOGS`` opts in. See the processor table in
docs/gdpr-compliance.md.
"""

from __future__ import annotations

import os
import re
import sys

import yaml

DEFAULT_ACTIVE_DIR = "/run/borgmatic-active"

# Token prefixing every backup-failure line, both from the borgmatic error hook
# and from the entrypoint's fallback when borgmatic itself fails to start. It is
# the string a log shipper or SIEM rule matches on, so it is deliberately
# unlikely to appear in any other output — treat it as operator-visible API and
# do not rename it without updating borgmatic/entrypoint.sh and any alert rules
# built against it.
FAILURE_TOKEN = "EPICURRENTS_BACKUP_FAILURE"


def _reject_control_chars(name: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        sys.stderr.write(
            f"FATAL: {name} contains newline / carriage-return characters; "
            "refusing to start. This usually means the value in .env was "
            "pasted as a multi-line block or carries an injection payload.\n"
        )
        sys.exit(1)
    return value


def _parse_bool(name: str, value: str, default: bool) -> bool:
    """Parse an operator-set boolean, refusing anything outside the vocabulary.

    A misspelling must not silently pick the default: ``BACKUP_LOCAL_ENABLED=flase``
    quietly keeping the local repository is a deployment that believes it has
    turned a tier off and has not, and the difference is invisible until someone
    looks at what the disk is holding.
    """
    if not value:
        return default
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    sys.stderr.write(f"FATAL: {name}={value!r} is not a boolean. Use true/false, yes/no, on/off or 1/0.\n")
    sys.exit(1)


def _validate_archive_prefix(value: str) -> str:
    """Return a prefix safe to use in an archive name and in borgmatic's match glob.

    Borgmatic derives the ``--glob-archives`` it passes to ``borg prune`` from
    ``archive_name_format``, so the prefix appears in a glob as well as in a
    name. Anything that is special to that glob (``*``, ``?``, brackets) would
    make retention match a set nobody intended, and a brace would collide with
    borg's own placeholder syntax.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        sys.stderr.write(
            f"FATAL: BORG_ARCHIVE_PREFIX={value!r} is not usable. Use letters, digits, "
            "dot, underscore or hyphen, starting with a letter or digit.\n"
        )
        sys.exit(1)
    return value


def _parse_int(name: str, value: str, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        sys.stderr.write(f"FATAL: {name}={value!r} is not an integer.\n")
        sys.exit(1)


def main() -> None:
    active_dir = os.environ.get("BORGMATIC_ACTIVE_DIR", DEFAULT_ACTIVE_DIR)
    remote_repo = _reject_control_chars("BORG_REMOTE_REPO", os.environ.get("BORG_REMOTE_REPO", "").strip())
    monitor_url = _reject_control_chars("BORG_MONITOR_URL", os.environ.get("BORG_MONITOR_URL", "").strip())
    send_logs = os.environ.get("BORG_MONITOR_SEND_LOGS", "").strip().lower() in ("1", "true", "yes", "on")

    local_enabled = _parse_bool("BACKUP_LOCAL_ENABLED", os.environ.get("BACKUP_LOCAL_ENABLED", ""), True)
    archive_prefix = _validate_archive_prefix(os.environ.get("BORG_ARCHIVE_PREFIX", "").strip() or "epicurrents")

    keep_daily = _parse_int("BACKUP_KEEP_DAILY", os.environ.get("BACKUP_KEEP_DAILY", ""), 7)
    keep_weekly = _parse_int("BACKUP_KEEP_WEEKLY", os.environ.get("BACKUP_KEEP_WEEKLY", ""), 4)
    keep_monthly = _parse_int("BACKUP_KEEP_MONTHLY", os.environ.get("BACKUP_KEEP_MONTHLY", ""), 6)

    # Two tiers, independently selectable. The local repository is fast to
    # restore from and survives nothing the host does not; the remote one is
    # append-only and survives the host entirely. Most deployments want both.
    repositories = []
    if local_enabled:
        repositories.append({"path": "/backup", "label": "local"})
    if remote_repo:
        repositories.append({"path": remote_repo, "label": "remote"})
    if not repositories:
        # Fail closed. borgmatic accepts an empty repository list and runs
        # cleanly against nothing, reporting success — a deployment would look
        # backed up while no archive existed anywhere, which is worse than any
        # error this could raise instead.
        sys.stderr.write(
            "FATAL: no backup repository is configured. BACKUP_LOCAL_ENABLED is false and "
            "BORG_REMOTE_REPO is empty, so there is nowhere to write archives. Set one of them.\n"
        )
        sys.exit(1)

    config = {
        # Only the uploads subtree of the media volume is the permanent
        # tier; staging holds in-flight uploads that are moved or deleted
        # within a single request.
        "source_directories": ["/data/recordings", "/data/media/uploads"],
        "repositories": repositories,
        # PostgreSQL credentials reach pg_dump via PGUSER / PGPASSWORD env
        # vars (set on the borg service in docker-compose). Keeping them
        # out of the YAML removes the injection surface entirely.
        "postgresql_databases": [
            {
                "name": "all",
                "hostname": "db",
            }
        ],
        # Explicit, because the default is "{hostname}-{now}" and this runs in a
        # container whose hostname is its ID — a new one on every recreate. That
        # is not merely an ugly archive name: borgmatic derives the prune glob
        # from this same format, so with the default it passes
        # `--glob-archives {hostname}-*` and prunes only what the CURRENT
        # container wrote. Every archive from every previous container is
        # unmatched and kept forever, and the repository grows without bound
        # while retention appears to be configured and running.
        #
        # A fixed prefix also means two deployments must not share one
        # repository unless they set different prefixes, or each will prune the
        # other's archives.
        "archive_name_format": f"{archive_prefix}-{{now:%Y-%m-%dT%H:%M:%S.%f}}",
        "checks": [{"name": "repository"}, {"name": "archives"}],
        "keep_daily": keep_daily,
        "keep_weekly": keep_weekly,
        "keep_monthly": keep_monthly,
        # borgmatic 2.x replaced before_backup / after_backup / on_error with a
        # single "commands" list; the old keys still parse but are deprecated.
        # `after: error` is the one that earns its place — the other two are
        # breadcrumbs marking run boundaries in the log.
        "commands": [
            {"before": "repository", "when": ["create"], "run": ['echo "Starting Epicurrents backup"']},
            {"after": "repository", "when": ["create"], "run": ['echo "Backup complete"']},
            {
                "after": "error",
                # Interpolated by borgmatic and shell-quoted before substitution,
                # so a repository path or error text carrying shell metacharacters
                # cannot break out of the echo.
                "run": [
                    f'echo "{FAILURE_TOKEN} repository={{repository_label}} error={{error}}" >&2',
                ],
            },
        ],
    }

    if monitor_url:
        # The default ping carries no content, only the fact of a start / finish /
        # failure — the monitor is a third-party destination, and "a backup failed"
        # is the whole signal it needs. send_logs adds the tail of the run (archive
        # statistics, repository and stored-file paths, database errors), which
        # helps whoever is paged but ships deployment detail off-platform, so it is
        # opt-in and registered as a processor flow in docs/gdpr-compliance.md.
        config["healthchecks"] = {"ping_url": monitor_url, "send_logs": send_logs}

    os.makedirs(active_dir, exist_ok=True)
    # Wipe any previous runtime YAML so a removed BORG_REMOTE_REPO does
    # not leave a stale remote.yaml in the merged config directory.
    for name in os.listdir(active_dir):
        if name.endswith((".yaml", ".yml")):
            os.unlink(os.path.join(active_dir, name))

    target = os.path.join(active_dir, "config.yaml")
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=True)

    if remote_repo:
        print(f"Remote backup enabled: {remote_repo}")
    else:
        print(
            "WARNING: BORG_REMOTE_REPO is unset, so archives are written only to the borg-data "
            "volume on this host. Losing the host loses the recordings, the database and the "
            "backups together. Set BORG_REMOTE_REPO to an off-host SSH target.",
            file=sys.stderr,
        )

    if monitor_url:
        print("Backup monitoring enabled.")
    else:
        print(
            "WARNING: BORG_MONITOR_URL is unset. Backup failures are written to this container's "
            f"log ({FAILURE_TOKEN}) and nowhere else, and a stack that is stopped entirely reports "
            "nothing at all. Point BORG_MONITOR_URL at a Healthchecks-compatible ping URL so "
            "silence raises an alert.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
