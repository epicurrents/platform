#!/usr/bin/env bash
# borg_repo.sh — resolve which Borg repository the operator scripts act on.
#
# A deployment may run the local tier, the remote tier, or both. Sourced by
# scripts/backup.sh and scripts/restore.sh so the answer is decided in one place:
# the two disagreeing would mean listing archives from one repository and
# restoring from another, which reads as data loss rather than as a bug.
#
# Prefers local when it is enabled, because it is the fast one and holds the
# same archives. Falls back to the remote when the local tier is switched off.

# Sets BORG_REPO_TARGET, or returns non-zero with a message on stderr.
# shellcheck disable=SC2034  # consumed by the sourcing script, not by this file
resolve_borg_repo() {
    local local_enabled="true" remote=""

    if [ -f .env ]; then
        if grep -qiE '^BACKUP_LOCAL_ENABLED=(0|false|no|off)[[:space:]]*$' .env; then
            local_enabled="false"
        fi
        remote="$(grep -E '^BORG_REMOTE_REPO=' .env | head -1 | cut -d= -f2- | tr -d ' "' || true)"
    fi

    if [ "$local_enabled" = "true" ]; then
        BORG_REPO_TARGET="/backup"
        return 0
    fi
    if [ -n "$remote" ]; then
        BORG_REPO_TARGET="$remote"
        return 0
    fi
    echo "No Borg repository is configured: BACKUP_LOCAL_ENABLED is false and BORG_REMOTE_REPO is empty." >&2
    return 1
}
