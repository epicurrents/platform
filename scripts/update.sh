#!/usr/bin/env bash
# update.sh — update a running Epicurrents deployment and recreate its stack.
#
# Two source modes, then a shared tail (back up → build → migrate → recreate),
# always on the production compose overlay.
#
#   --from archive   (default) Apply a distribution tarball. The newest
#                    ./update/epicurrents*.tar.gz is extracted over the
#                    deployment, preserving .env and runtime data. No host
#                    toolchain needed — the distribution ships prebuilt bundles.
#   --from repo      Pull from git and rebuild the frontend on the host (the
#                    path a git-checkout deployment or CI uses).
#
# Recovery:
#   --rollback       Restore the most recent pre-update snapshot — database,
#                    .env and code — rebuild the image from the restored code,
#                    and recreate the stack. The rebuild is not optional: the
#                    image carries the code, so without it the recreate runs the
#                    new code against the restored database and re-applies the
#                    migrations being rolled back.
#
# Usage:
#   ./update.sh                          archive mode, newest ./update/epicurrents*.tar.gz
#   ./update.sh --archive ./foo.tar.gz   archive mode, explicit file
#   ./update.sh --from repo              repo mode, git pull + frontend build
#   ./update.sh --from repo --no-pull    repo mode, rebuild the current checkout
#   ./update.sh --no-backup              skip the pre-update snapshot (not advised —
#                                        it is what --rollback restores from)
#   ./update.sh --rollback               undo the last update (database + .env)
#
set -euo pipefail

# ── Locate the deployment root ────────────────────────────────────────────────
# In a git checkout this script lives in scripts/; in a distribution it is
# bundled at the deployment root next to start.sh. Detect by the compose file.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    ROOT="$SCRIPT_DIR"
else
    ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
cd "$ROOT"

# Every compose call uses the production overlay, deliberately and without a
# toggle. update.sh only ever updates an already-deployed stack, and a deployed
# stack is production by definition — development happens on a local machine
# against the plain dev compose, never by updating a remote VM in place. So there
# is no dev mode here: the overlay is a constant, not a flag. Array form keeps the
# flags from word-splitting (and shellcheck quiet).
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
# The TLS proxy overlay is selected the same way bootstrap.sh selects it — a
# PROXY_DOMAIN value in .env. This is not cosmetic: a stack brought up with the
# overlay has to be updated with it too, or `up -d` treats the running caddy
# container as an orphan and the deployment loses its TLS terminator mid-update.
PROXY_ENABLED=false
if [ -f .env ] && grep -qE '^PROXY_DOMAIN=[^[:space:]]' .env; then
    COMPOSE+=(-f docker-compose.proxy.yml)
    PROXY_ENABLED=true
fi
UPDATE_DIR="./update"
BACKUP_DIR="./backups"
KEEP_BACKUPS=3

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
warn() { printf '    \033[33m!\033[0m  %s\n'  "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,28p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'
}

require_value() {
    # $1 = flag name, $2 = the candidate value (empty if the flag was last).
    # Reject a missing or flag-like value so 'update.sh --from' fails with a
    # clear message instead of a cryptic 'shift' error under set -e.
    case "$2" in
        ""|-*) die "Option '$1' requires a value." ;;
    esac
}

# ── Arguments ─────────────────────────────────────────────────────────────────
MODE=archive
ARCHIVE=""
REF=""
PULL=true
BACKUP=true
ROLLBACK=false
ASSUME_YES=false

while [ $# -gt 0 ]; do
    case "$1" in
        --from)      require_value --from "${2:-}";    MODE="$2"; shift 2 ;;
        --from=*)    MODE="${1#*=}"; shift ;;
        --archive)   require_value --archive "${2:-}"; ARCHIVE="$2"; shift 2 ;;
        --archive=*) ARCHIVE="${1#*=}"; shift ;;
        --ref)       require_value --ref "${2:-}";     REF="$2"; shift 2 ;;
        --ref=*)     REF="${1#*=}"; shift ;;
        --no-pull)   PULL=false; shift ;;
        --no-backup) BACKUP=false; shift ;;
        --backup)    BACKUP=true; shift ;;
        --rollback)  ROLLBACK=true; shift ;;
        --yes|-y)    ASSUME_YES=true; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           die "Unknown argument: $1 (try --help)" ;;
    esac
done

[ -f .env ] || die "No .env in $ROOT — initialize the deployment first (./start.sh in a distribution, or 'python manage.py init_env' in a checkout)."
command -v docker >/dev/null 2>&1 || die "docker is required but not found."

# ── Helpers ───────────────────────────────────────────────────────────────────

confirm() {
    # $1 = prompt. Returns 0 to proceed. --yes bypasses the prompt.
    if [ "$ASSUME_YES" = true ]; then
        return 0
    fi
    printf '\033[1m%s [y/N] \033[0m' "$1"
    read -r reply
    [[ "${reply:-N}" =~ ^[Yy]$ ]]
}

ensure_db_up() {
    if "${COMPOSE[@]}" ps db 2>/dev/null | grep -q "running\|Up"; then
        return 0
    fi
    warn "Database container is not running; starting it."
    "${COMPOSE[@]}" up -d db
    printf '    Waiting for PostgreSQL'
    for _ in $(seq 1 60); do
        if "${COMPOSE[@]}" exec -T db pg_isready -q 2>/dev/null; then
            printf '\n'
            return 0
        fi
        printf '.'
        sleep 1
    done
    printf '\n'
    die "PostgreSQL did not become ready within 60s."
}

find_latest_complete_snapshot() {
    # The newest snapshot that can actually be restored from — not simply the
    # newest. A run that dies between the code snapshot (step 0) and the
    # database dump (step 2) leaves a code-only directory behind, and that
    # directory is the newest; refusing on it would block rollback to the
    # perfectly good snapshot sitting behind it, which is the opposite of what
    # a recovery path should do when it meets damage.
    #
    # Warnings go to stderr deliberately: this runs inside a command
    # substitution, so anything on stdout becomes part of the returned path.
    # A glob rather than `ls -t`: the directory names are UTC timestamps this
    # script writes, so lexical order is chronological, and a glob cannot be
    # confused by a name containing whitespace. Iterated backwards for
    # newest-first.
    local dirs=("$BACKUP_DIR"/pre-update-*)
    local i d
    for ((i = ${#dirs[@]} - 1; i >= 0; i--)); do
        d="${dirs[i]}"
        [ -d "$d" ] || continue   # no matches: the glob stayed literal
        if [ ! -f "$d/db.sql.gz" ] || [ ! -f "$d/.env" ]; then
            warn "Skipping incomplete snapshot $(basename "$d") (no database or .env)" >&2
            continue
        fi
        if [ -f "$d/code.tar.gz" ] && ! tar -tzf "$d/code.tar.gz" >/dev/null 2>&1; then
            warn "Skipping snapshot $(basename "$d") — its code archive is unreadable" >&2
            continue
        fi
        printf '%s' "$d"
        return 0
    done
    return 1
}

prune_backups() {
    # Keep the newest $KEEP_BACKUPS pre-update snapshots; drop the rest. ls -t
    # over our own timestamped dir names is fine — no untrusted filenames here.
    # shellcheck disable=SC2012
    ls -1dt "$BACKUP_DIR"/pre-update-* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do
        rm -rf "$old"
    done || true
    # Drop snapshots left half-written by a run that died before the dump. They
    # can never be restored from, and they crowd out the ones that can.
    # shellcheck disable=SC2012
    ls -1dt "$BACKUP_DIR"/pre-update-* 2>/dev/null | while read -r d; do
        [ -f "$d/db.sql.gz" ] || { warn "Discarding half-written snapshot $(basename "$d")"; rm -rf "$d"; }
    done || true
}

# ── Rollback path ─────────────────────────────────────────────────────────────

if [ "$ROLLBACK" = true ]; then
    # shellcheck disable=SC2012  # mtime sort over our own snapshot dir names.
    # Every piece is verified before anything is touched — including that the
    # code archive actually reads, since it is restored *after* the database
    # and a truncated one would otherwise strand the deployment half rolled
    # back. An incomplete snapshot is skipped rather than fatal; see
    # find_latest_complete_snapshot.
    latest="$(find_latest_complete_snapshot || true)"
    [ -n "$latest" ] || die "No complete pre-update snapshot found under $BACKUP_DIR (a snapshot needs db.sql.gz and .env, and a readable code.tar.gz if it has one)."
    info "Rolling back to $(basename "$latest")"
    [ -f "$latest/MANIFEST" ] && cat "$latest/MANIFEST"
    confirm "Restore database + .env from this snapshot? Current data will be overwritten." \
        || die "Rollback aborted."
    ensure_db_up
    info "Stopping application services"
    "${COMPOSE[@]}" stop web celery celery-beat || true
    info "Restoring database (single transaction — all or nothing)"
    # --single-transaction + ON_ERROR_STOP: the restore commits or rolls back as
    # one unit, so a failure leaves the database exactly as it was rather than
    # half-restored. On failure we stop here — .env is untouched and the stack is
    # not recreated — so the operator never lands in a partially-recovered state.
    # SC2016: $POSTGRES_* must expand inside the db container's shell, not here.
    # shellcheck disable=SC2016
    if ! gunzip -c "$latest/db.sql.gz" \
            | "${COMPOSE[@]}" exec -T db sh -c 'psql --single-transaction -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
                >/dev/null; then
        die "Database restore FAILED and was rolled back — the database is unchanged, .env was not touched, and the stack was not recreated. Investigate before retrying."
    fi
    ok "Database restored"
    cp "$latest/.env" ./.env
    ok ".env restored"

    # Code after the database, deliberately. A failed database restore leaves
    # everything untouched (above); a failure here leaves the old database under
    # new code, which the recreate below resolves by re-migrating — consistent,
    # if not what was asked for. The reverse order would leave new code with no
    # way back.
    if [ -f "$latest/code.tar.gz" ]; then
        info "Restoring code"
        # A replace, not an overlay. Extracting the snapshot over the tree would
        # restore every old file and delete none of the new ones — so a
        # migration added by the update survives, the recreate applies it again,
        # and a rollback of a destructive migration destroys the data it just
        # recovered. rsync --delete is what makes the tree actually match the
        # snapshot.
        #
        # The excludes are load-bearing in the other direction: --delete against
        # the deployment root would otherwise remove the snapshot being restored
        # from, the archive drop directory, and .env.
        #
        # -m on extraction: do not restore mtimes, so a root-owned path in a
        # future snapshot cannot fail an entire recovery on "Cannot utime".
        rtmp="$(mktemp -d)"
        if tar -xzmf "$latest/code.tar.gz" -C "$rtmp" \
            && rsync -a --delete \
                --exclude=".env" \
                --exclude="backups/" \
                --exclude="update/" \
                --exclude="static/" \
                --exclude="frontend/vendor/" \
                --exclude="frontend/node_modules/" \
                --exclude=".git/" \
                "$rtmp"/ ./; then
            rm -rf "$rtmp"
            ok "Code restored"
        else
            rm -rf "$rtmp"
            die "Code restore FAILED. The database and .env are already rolled back; the tree is in an unknown state. Re-apply a known archive before starting the stack."
        fi
        # The images carry the code (Dockerfile: COPY . .), so restoring the
        # tree changes nothing that runs until they are rebuilt. Every service
        # with a build section, not just web: migrate declares its own, so
        # `build web` leaves the migrate image holding the code being rolled
        # back — and the recreate below then applies the very migrations the
        # rollback just undid, to the data it just restored. Observed exactly
        # that before this line said `build` instead of `build web`.
        info "Rebuilding images from the restored code"
        "${COMPOSE[@]}" build
        ok "Images rebuilt"
        CODE_RESTORED=true
    else
        CODE_RESTORED=false
        warn "Snapshot $(basename "$latest") predates code snapshots — restoring data only."
        warn "The recreate below will run the current code against the restored database, and"
        warn "any migration newer than the snapshot will be re-applied. If one of them destroyed"
        warn "data, it is about to destroy it again. Re-apply a known-good archive first."
    fi

    info "Recreating containers"
    "${COMPOSE[@]}" up -d --force-recreate web celery celery-beat
    echo
    if [ "$CODE_RESTORED" = true ]; then
        ok "Rollback complete — database, .env and code restored."
    else
        warn "Rolled back the database and .env, not the code/image."
        ok "Rollback complete."
    fi
    exit 0
fi

# ── Mode validation ───────────────────────────────────────────────────────────

case "$MODE" in
    archive|repo) ;;
    *) die "Unknown --from mode: '$MODE' (expected 'archive' or 'repo')." ;;
esac

info "Updating from $MODE (backup: $([ "$BACKUP" = true ] && echo on || echo off), overlay: production)"

# ── Preflight: the tree must belong to the account the containers run as ──────
# web, celery, celery-beat and migrate all run as uid/gid 1000 against a bind mount
# of this directory, so a tree owned by anyone else comes up and then fails on its
# first write, far from the cause. The way it happens is an archive: tar records
# the *builder's* uid, and an update applied as root preserves it, so a package
# built on a laptop can hand a deployment a tree its own account cannot write.
# Checked here, before anything is touched, rather than discovered later.
#
# Linux only, which is what gating on `stat -c` amounts to: Docker Desktop maps
# ownership inside its own VM, where none of this applies. Fatal in archive mode,
# which is the case that breaks; a warning in repo mode, where a checkout owned by
# a developer's own uid is a legitimate arrangement this cannot tell apart.

if DIR_MODE="$(stat -c %a . 2>/dev/null)"; then
    DIR_UID="$(stat -c %u .)"
    DIR_GID="$(stat -c %g .)"
    DIR_MODE="$(printf '%04d' "$DIR_MODE")"
    TREE_WRITABLE=false
    if [ "$DIR_UID" = "1000" ] && [ "$(( ${DIR_MODE:1:1} & 2 ))" -ne 0 ]; then
        TREE_WRITABLE=true
    fi
    if [ "$DIR_GID" = "1000" ] && [ "$(( ${DIR_MODE:2:1} & 2 ))" -ne 0 ]; then
        TREE_WRITABLE=true
    fi
    if [ "$(( ${DIR_MODE:3:1} & 2 ))" -ne 0 ]; then
        TREE_WRITABLE=true
    fi
    if [ "$TREE_WRITABLE" = false ]; then
        if [ "$MODE" = archive ]; then
            die "$ROOT is not writable by uid 1000, the user every container runs as \
(owner ${DIR_UID}:${DIR_GID}, mode ${DIR_MODE}). The update would apply and the stack \
would then fail on its first write. Fix the ownership and re-run:
    sudo chown -R 1000:1000 $ROOT"
        else
            warn "$ROOT is owned by ${DIR_UID}:${DIR_GID} and is not writable by uid 1000,"
            warn "which every container runs as. If this is a deployment rather than a"
            warn "development checkout, fix it with: sudo chown -R 1000:1000 $ROOT"
        fi
    fi
fi


# ── 0. Snapshot the current code, BEFORE anything overwrites it ───────────────
# Placement is the whole point. Step 1 rsyncs the new tree over the deployment,
# so a code snapshot taken with the database in step 2 captures the *new* code
# and is worthless for rollback — the restore puts the failing version back and
# the recreate re-applies the migrations being rolled back. The database dump
# can wait for step 2 because migrations do not run until step 5; the code
# cannot.
#
# Retaining "the previous archive" instead would be cheaper and does not work:
# this script never moves, copies or records the archive it applied, so after a
# few updates nothing identifies the deployed lineage.
#
# Excluded: the snapshots themselves (which would nest), the archive drop
# directory, .env (saved with the database), git history, build caches — and
# static/ plus frontend/vendor, which the containers write as root, so an
# unprivileged restore cannot set their timestamps and tar fails the whole
# extraction on "Cannot utime". static/ is regenerated by the collectstatic step
# below; frontend/vendor is not, which is why step 6a re-vendors it whenever the
# tree does not match its own lock.
stamp="$(date -u +%Y%m%d-%H%M%S)"
snap="$BACKUP_DIR/pre-update-$stamp"
if [ "$BACKUP" = true ]; then
    mkdir -p "$snap"
    info "Snapshotting current code to $snap"
    if tar -czf "$snap/code.tar.gz" \
            --exclude="./backups" \
            --exclude="./update" \
            --exclude="./.env" \
            --exclude="./.git" \
            --exclude="./frontend/node_modules" \
            --exclude="./static" \
            --exclude="./frontend/vendor" \
            --exclude="__pycache__" \
            -C . . 2>/dev/null; then
        ok "Code snapshotted ($(du -h "$snap/code.tar.gz" | cut -f1))"
    else
        rm -rf "$snap"
        die "Code snapshot failed; aborting before any change. (Pass --no-backup to override.)"
    fi
fi

# ── 1. Acquire source ─────────────────────────────────────────────────────────

if [ "$MODE" = archive ]; then
    if [ -z "$ARCHIVE" ]; then
        # shellcheck disable=SC2012  # newest-by-mtime over a controlled glob.
        ARCHIVE="$(ls -1t "$UPDATE_DIR"/epicurrents*.tar.gz 2>/dev/null | head -1 || true)"
        [ -n "$ARCHIVE" ] || die "No archive in $UPDATE_DIR/ (looked for epicurrents*.tar.gz). Drop the distribution there or pass --archive FILE."
    fi
    [ -f "$ARCHIVE" ] || die "Archive not found: $ARCHIVE"
    command -v rsync >/dev/null 2>&1 || die "rsync is required for archive mode (apt-get install rsync)."

    info "Applying archive: $ARCHIVE"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    tar -xzf "$ARCHIVE" -C "$tmp"
    # Distribution tars wrap their contents in a single versioned top-level dir;
    # descend into it so the sync targets the deployment files, not the wrapper.
    src="$tmp"
    if [ ! -f "$src/docker-compose.yml" ]; then
        # Descend into the wrapper. Directories only, and AppleDouble siblings
        # excluded: macOS tar stores extended attributes as ._* members, which
        # GNU tar materialises as real files on extraction. A counting test
        # ("exactly one entry, so it is the wrapper") then sees two entries and
        # declines to descend, and the archive is rejected as malformed — so a
        # distribution built on a Mac cannot be applied on Linux, with an error
        # naming the wrong cause.
        only="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d ! -name '._*' | head -1)"
        [ -n "$only" ] && src="$only"
    fi
    [ -f "$src/docker-compose.yml" ] || die "Archive does not look like an Epicurrents distribution (no docker-compose.yml at its root)."

    # Refresh the platform-owned, regenerable bundle dirs so stale content-hashed
    # chunks from prior releases don't pile up. These are the only trees we
    # actively prune; the root sync below is overlay-only (no --delete), so any
    # file the operator added that the archive doesn't carry — a
    # docker-compose.override.yml, certs, a .env.local, a .git checkout — is
    # always preserved. A denylist --delete at the deployment root would silently
    # wipe exactly those.
    # Empty these, do not replace them. `rm -rf` followed by rsync recreates the
    # directory with a NEW inode, and the running caddy container bind-mounts the
    # old one — which it keeps, now orphaned and empty, so every /assets/ and
    # /viewer/ request 404s. The site is then down for anyone without a warm
    # cache, and invisible to everyone with one, because the bundles are
    # content-hashed and served `immutable`: the operator who just ran the update
    # reloads, sees their cached copy, and concludes it worked. Caddy is
    # recreated below as well, but keeping the inode is what makes the window
    # zero rather than merely short.
    for d in frontend/dist frontend/viewer-dist; do
        if [ -d "$src/$d" ] && [ -d "${ROOT:?}/$d" ]; then
            find "${ROOT:?}/$d" -mindepth 1 -delete
        elif [ -d "$src/$d" ]; then
            rm -rf "${ROOT:?}/$d"
        fi
    done

    # Overlay the new tree. rsync replaces files via atomic rename, so
    # overwriting this running script is safe — the shell keeps reading the
    # original inode. The excludes keep operator state from being overwritten
    # even if a future archive happens to carry one of these paths.
    info "Updating files (preserving .env, data, backups, update)"
    rsync -a \
        --exclude='/.env' \
        --exclude='/backups/' \
        --exclude='/update/' \
        --exclude='/static/' \
        "$src"/ "$ROOT"/
    ok "Files updated"
else
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || die "repo mode needs a git checkout. In a distribution deployment use archive mode (the default)."
    if [ "$PULL" = true ]; then
        info "Pulling latest code"
        if [ -n "$REF" ]; then
            git fetch origin --tags --prune
            git checkout "$REF"
            # Fast-forward a tracking branch to its upstream; a no-op for a tag
            # or detached SHA (no origin/<ref> to merge).
            git merge --ff-only "origin/$REF" 2>/dev/null || true
        else
            git pull --ff-only \
                || die "Cannot fast-forward the current branch — it has diverged from upstream. Resolve manually; update.sh never force-merges."
        fi
        ok "Code up to date: $(git log -1 --format='%h %s')"
        info "Updating submodules"
        git submodule update --init --recursive
        ok "Submodules up to date"
        # The active project is a separate repository cloned into projects/<name>/,
        # not a submodule, so neither the pull above nor the submodule update
        # reaches it. Without this it stays at whatever bootstrap cloned while the
        # platform moves — and the build below would bake that stale tree into the
        # image, dependencies and Python source alike, without failing.
        #
        # Only a branch tracking an upstream is moved. Pinning a project to a tag
        # or a commit is a manual operation today (see bootstrap.sh step 4c), and
        # an unconditional pull is exactly what would undo it.
        # The three states are told apart rather than collapsed, because the
        # remedy differs and each looks the same from the build's side. `rev-parse
        # --git-dir` is the repo test rather than a check for a .git directory: a
        # worktree or a submodule checkout has .git as a *file*.
        project="$(grep -E '^EPICURRENTS_PROJECT=' .env | head -1 | cut -d= -f2 | tr -d ' "'"'"'' || true)"
        if [ -n "$project" ] && [ -d "projects/$project" ]; then
            if ! git -C "projects/$project" rev-parse --git-dir > /dev/null 2>&1; then
                warn "projects/$project is not a git checkout — update it yourself before the build."
            elif ! git -C "projects/$project" rev-parse --abbrev-ref --symbolic-full-name '@{u}' > /dev/null 2>&1; then
                warn "projects/$project is pinned (no upstream branch) — leaving it as it is."
            else
                info "Pulling project $project"
                git -C "projects/$project" pull --ff-only \
                    || die "Cannot fast-forward projects/$project — it has diverged from upstream. Resolve manually; update.sh never force-merges."
                ok "Project up to date: $(git -C "projects/$project" log -1 --format='%h %s')"
            fi
        fi
    else
        info "Skipping git pull (--no-pull); rebuilding the current checkout"
    fi
    info "Building frontend bundles (Node container)"
    "${COMPOSE[@]}" --profile build run --rm frontend-build
    ok "Frontend bundles built"
fi

# ── 2. Back up before mutating the database ───────────────────────────────────

if [ "$BACKUP" = true ]; then
    ensure_db_up
    # $snap already exists and holds code.tar.gz from step 0.
    info "Backing up to $snap"
    # Local snapshot = database + .env: small, fast, and the part an update can
    # destroy. Recording / media file volumes are out of scope here — they are
    # untouched by migrations; enable borg for full data-volume backups.
    # SC2016: $POSTGRES_* must expand inside the db container's shell, not here.
    # shellcheck disable=SC2016
    if "${COMPOSE[@]}" exec -T db sh -c 'pg_dump --clean --if-exists --no-owner -U "$POSTGRES_USER" "$POSTGRES_DB"' \
            | gzip > "$snap/db.sql.gz"; then
        ok "Database dumped ($(du -h "$snap/db.sql.gz" | cut -f1))"
    else
        rm -rf "$snap"
        die "Database dump failed; aborting before any change. (Pass --no-backup to override.)"
    fi
    cp .env "$snap/.env"

    {
        echo "timestamp_utc=$stamp"
        echo "mode=$MODE"
        echo "code_snapshot=yes"
        if [ "$MODE" = archive ]; then
            echo "archive=$ARCHIVE"
        else
            echo "git_ref=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        fi
    } > "$snap/MANIFEST"
    ok ".env + manifest saved"

    # If borgmatic is wired up and running, take a full backup too (data volumes).
    if [ -x ./scripts/backup.sh ] && "${COMPOSE[@]}" ps borg 2>/dev/null | grep -q "running\|Up"; then
        info "Borg is enabled — taking a full backup"
        ./scripts/backup.sh || warn "Borg backup reported an error; the local snapshot is still in place."
    fi

    prune_backups
else
    warn "Skipping backup (--no-backup)."
fi

# ── 3. Build the image ────────────────────────────────────────────────────────

info "Building Docker images"
# --profile vendor so the one-shot writer used in steps 6a and 6b is built here
# with everything else. Its image is the same one web runs, so this costs a cache
# hit and a tag; left out, the build happens inside step 6a instead, where a
# failure reads as a vendoring failure.
"${COMPOSE[@]}" --profile vendor build
ok "Images built"

# ── 4. Stop application services (so nothing races the schema change) ─────────

info "Stopping application services"
"${COMPOSE[@]}" stop web celery celery-beat || true
ok "Application services stopped"

# ── 5. Apply ALL pending migrations ───────────────────────────────────────────

ensure_db_up
info "Applying database migrations"
"${COMPOSE[@]}" run --rm --no-deps web python manage.py migrate
ok "Migrations applied"

# ── 6. Collect static files ───────────────────────────────────────────────────

info "Collecting static files"
"${COMPOSE[@]}" run --rm --no-deps web python manage.py collectstatic --no-input
ok "Static files collected"

# ── 6a. Vendor the Pyodide runtime if it is missing or incomplete ─────────────
# The tree is excluded from this script's rsync so a deployment keeps its own
# copy, which means an update never creates one: a fresh host, a restored
# snapshot, or a version bump in settings all arrive here with nothing to serve.
# The check is a local hash sweep, so the common case costs a second and the
# vendoring runs only when it has to.
#
# This step and the next write through the `vendor` service: web mounts the tree
# read-only in production, so it is the one container that cannot populate it.

if "${COMPOSE[@]}" --profile vendor run --rm --no-deps -T vendor python manage.py vendor_pyodide --check > /dev/null 2>&1; then
    ok "Pyodide runtime present"
else
    info "Vendoring the Pyodide runtime"
    "${COMPOSE[@]}" --profile vendor run --rm --no-deps -T vendor python manage.py vendor_pyodide
    ok "Pyodide runtime vendored"
fi

# ── 6b. Regenerate the static lead fields ─────────────────────────────────────
# The other half of the vendored tree, and computed rather than downloaded, so it
# runs unconditionally: a couple of seconds, and regenerating is the only way a
# change to the generator's montages or grid parameters reaches the deployment.
# Blob filenames carry a content hash, so an unchanged field keeps its name and
# every cached copy stays valid. Needs the migrated database (it also refreshes
# the LeadFieldCache rows the compute API serves from), which step 5 has ensured.

info "Generating static lead fields"
"${COMPOSE[@]}" --profile vendor run --rm --no-deps -T vendor python manage.py generate_compute_static
ok "Static lead fields generated"

# ── 7. Recreate the application containers ────────────────────────────────────
# --force-recreate so a changed .env is re-read (the prod overlay bakes env at
# container-creation time; a plain restart keeps the stale value). Scoped to the
# app services — db / redis stay up so the database is never bounced.

info "Recreating containers"
"${COMPOSE[@]}" up -d --force-recreate web celery celery-beat
# Caddy serves /assets/, /viewer/ and /static/ straight off bind mounts, so it
# has to be recreated after the tree underneath it changes — a running container
# holds the mount it was started with. Left out, an archive update takes the SPA
# offline while Django reports healthy.
if [ "$PROXY_ENABLED" = true ]; then
    "${COMPOSE[@]}" up -d --force-recreate caddy
fi
ok "Stack is up"

# ── 8. Health check + summary ─────────────────────────────────────────────────

PORT="$(grep -E '^HOST_PORT=' .env | head -1 | cut -d= -f2- | tr -d ' "' || true)"
PORT="${PORT:-8000}"
info "Waiting for the platform to become ready"
ready=false
for _ in $(seq 1 60); do
    if curl -fsS "http://localhost:${PORT}/api/v1/health" >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 2
done
if [ "$ready" = true ]; then
    ok "Health check passed"
else
    warn "Health check did not pass within the timeout; check '${COMPOSE[*]} logs web'."
fi

# The health endpoint says Django is answering. It says nothing about whether the
# SPA can load, and those are separable: the bundles are served off disk by the
# proxy, so the site can be entirely unusable to a new visitor while /api/v1/health
# returns 200. That state shipped once and went unnoticed for a day, because the
# hashed bundles are cached `immutable` and every browser that had already loaded
# the page kept working. Fetch a real asset, named by the index.html just
# installed, so the check fails on exactly what a first-time visitor would hit.
if [ "$PROXY_ENABLED" = true ] && [ -f frontend/dist/index.html ]; then
    asset="$(grep -oE '/assets/[A-Za-z0-9._-]+\.js' frontend/dist/index.html | head -1 || true)"
    domain="$(grep -E '^PROXY_DOMAIN=' .env | head -1 | cut -d= -f2- | tr -d ' "' || true)"
    if [ -n "$asset" ] && [ -n "$domain" ]; then
        info "Verifying the SPA bundle is servable"
        # --resolve pins the name to this host so the check tests the local proxy
        # rather than whatever DNS points at, while still presenting the SNI the
        # certificate was issued for.
        # Retried, because the check runs seconds after caddy was recreated and a
        # starting proxy refuses the TLS handshake outright — reported by curl as
        # exit 35, which is indistinguishable here from a genuinely unservable
        # bundle. A single attempt therefore warned on every successful update,
        # and a check that cries wolf is worse than no check: it trains the
        # operator to skip the one signal that would have caught the real outage
        # this block exists to detect.
        spa_served=false
        for _ in $(seq 1 10); do
            if curl -fsS -o /dev/null --max-time 15 \
                --resolve "${domain}:443:127.0.0.1" "https://${domain}${asset}"; then
                spa_served=true
                break
            fi
            sleep 3
        done
        if [ "$spa_served" = true ]; then
            ok "SPA bundle served ($asset)"
        else
            warn "The SPA bundle at $asset is NOT being served. The site will be blank for"
            warn "any visitor without a cached copy. Check that caddy restarted:"
            warn "  ${COMPOSE[*]} up -d --force-recreate caddy"
        fi
    fi
fi

echo
"${COMPOSE[@]}" ps
echo
ok "Update complete."
if [ "$BACKUP" = true ]; then
    echo "    Roll back with: ./update.sh --rollback"
fi
