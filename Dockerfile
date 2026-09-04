# Multi-stage so the runtime image does not carry the test toolchain.
#
# Stage layout:
#   project-reqs — the active project's requirements.lock, if it has one
#   base         — system packages, production Python dependencies, the unprivileged user
#   test         — base plus requirements-test.txt, for the `test` compose profile
#   runtime      — base plus the source, and nothing from the test toolchain
#
# `runtime` is last on purpose: a bare `docker build .` with no --target builds
# the final stage, so the default is the lean image and shipping pytest to
# production takes a deliberate `--target test`. The compose services name their
# target explicitly either way.

# ── project-reqs ─────────────────────────────────────────────────────────────
# The active project lives in its own repository and is cloned into
# projects/<name>/ before the build; if it needs Python packages the platform
# does not, it ships its own requirements.lock. Selecting that one file takes a
# stage of its own for two reasons.
#
# A COPY glob that matches nothing fails the build, and most projects have no
# lock at all — a supported configuration, as is running with no project. Doing
# the selection in a shell command lets absence be a normal outcome and lets a
# requirements.txt with no lock beside it be a loud one.
#
# It also keeps the dependency layer's cache key off the project's source.
# `COPY --from` keys on the content of what it copies, so editing project code
# invalidates only this stage's cheap COPY, not the scientific stack's install
# in base — which is what a project-source COPY placed there directly would do
# on every `update.sh`.
FROM python:3.14-slim AS project-reqs
ARG EPICURRENTS_PROJECT=""
WORKDIR /src
COPY projects/ ./projects/
RUN mkdir -p /out \
    && if [ -n "$EPICURRENTS_PROJECT" ]; then \
        if [ -f "projects/$EPICURRENTS_PROJECT/requirements.lock" ]; then \
            cp "projects/$EPICURRENTS_PROJECT/requirements.lock" /out/; \
        elif [ -f "projects/$EPICURRENTS_PROJECT/requirements.txt" ]; then \
            echo "projects/$EPICURRENTS_PROJECT/requirements.txt has no requirements.lock beside it." >&2; \
            echo "Generate one with scripts/lock-requirements.sh --project $EPICURRENTS_PROJECT." >&2; \
            exit 1; \
        fi; \
    fi

FROM python:3.14-slim AS base

# Redeclared because ARG is per-stage. Used only to name the project in the
# digest-mismatch message below, so the remedy it prints can be pasted as-is.
ARG EPICURRENTS_PROJECT=""

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /code

COPY requirements.txt requirements.lock requirements-test.txt constraints.txt ./
# The active project's lock, or an empty directory when it has none. Trailing
# slash on both sides so the copy succeeds either way.
COPY --from=project-reqs /out/ ./project-requirements/

# The production closure installs from requirements.lock with --require-hashes,
# so an artifact that does not match what was resolved fails the build instead of
# shipping. requirements.txt is still the file humans edit; regenerate the lock
# with scripts/lock-requirements.sh after changing it.
#
# Known residual: one artifact in this stage is still unverified — pip itself is
# upgraded from PyPI on the line below. Everything the application actually
# imports at runtime comes from the lock.
#
# requirements-test.txt is copied above but not installed —
# that is the test stage's job. The scientific stack dominates both build time
# and image size, so both stages still share this one layer.
#
# The project's lock is a second invocation because --require-hashes applies to
# a whole invocation, and the two locks are
# resolved separately. Separately is the hazard as much as the convenience. A
# package in both closures — numpy is in the platform's and in any project doing
# array work — does not collide here: the second install simply wins, replacing
# a version the platform's lock pinned with one nothing checked it against, and
# succeeding. scripts/lock-requirements.sh --project is what stops that, by
# resolving the project against the platform lock's exact versions as
# constraints; running uv against the project's requirements.txt alone produces
# a lock that installs cleanly and quietly changes the platform's numpy.
#
# That guarantee expires, which is what the digest check below is for. It holds
# only against the requirements.lock the project was resolved against, and this
# is the one place that has both files in front of it. Checking it here rather
# than trusting CI matters because the two travel in separate repositories: a
# project repo that has not caught up with a platform relock is the ordinary
# case, not a mistake, and the build is the last point at which anything looks.
# The digest covers the derived version pins rather than the lock file, so
# re-stamping at unchanged versions does not fail every project.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends netcat-openbsd libfuse2 \
    && pip install --upgrade pip \
    && pip install --require-hashes -r requirements.lock \
    && if [ -f project-requirements/requirements.lock ]; then \
        want="$(sed -E 's/[[:space:]]*\\$//' requirements.lock | grep -E '^[a-zA-Z0-9._-]+==' | sha256sum | cut -d' ' -f1)"; \
        have="$(awk '/^# platform-versions: /{print $3; exit}' project-requirements/requirements.lock)"; \
        if [ "$want" != "$have" ]; then \
            echo "The project's requirements.lock was resolved against a different requirements.lock." >&2; \
            echo "Installing it would silently replace packages this image just pinned." >&2; \
            echo "Re-run: scripts/lock-requirements.sh --project $EPICURRENTS_PROJECT" >&2; \
            exit 1; \
        fi; \
        pip install --require-hashes -r project-requirements/requirements.lock; \
    fi \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -s /bin/sh -M appuser

# Copy entrypoint into /code (original behavior). Note: when bind-mounting the host into /code
# this file may be hidden by the mount. Ensure your host mount is available when running.
COPY entrypoint.sh .
RUN chmod +x /code/entrypoint.sh

# ── test ─────────────────────────────────────────────────────────────────────
# Built by the `test` and `test-postgres` compose services. The test toolchain
# installs before the source copy so editing a Python file does not reinstall it.
FROM base AS test

RUN pip install -r requirements-test.txt

# .dockerignore excludes frontend/ from the build context — the Python image
# never contains frontend source. Frontend bundles arrive at runtime via
# bind-mount (dev) or selective bind-mounts from the deploy host's
# host-built dist directories (production overlay).
COPY . .

# Drop privilege by default, as in runtime below. The compose test services
# override back to root because the FUSE integration tests need it.
USER appuser

ENTRYPOINT ["/bin/sh", "/code/entrypoint.sh"]

# ── runtime ──────────────────────────────────────────────────────────────────
# The deployment image. Carries no pytest, coverage, model-bakery or
# pytest-httpserver, so none of it is reachable from a production container.
FROM base AS runtime

COPY . .

# Drop privilege by default. Every compose service already pins user 1000:1000,
# so this changes nothing about how the stack runs — it changes what happens when
# the image is run any other way: a bare `docker run`, a cloud container service,
# a future Kubernetes manifest. Without it the image's default is root and each of
# those has to remember to say otherwise.
#
# Placed after every COPY and RUN so the build itself still runs privileged.
USER appuser

ENTRYPOINT ["/bin/sh", "/code/entrypoint.sh"]
