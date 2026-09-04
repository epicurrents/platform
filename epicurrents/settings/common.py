"""Common Django settings — defaults shared by development and production.

⚠️ LOAD-BEARING — middleware ordering invariant.
The ``MIDDLEWARE`` list below carries an ordering contract:
``AuthenticationMiddleware`` MUST precede
``ApiActivityLoggingMiddleware`` so ``request.user`` is populated when
the audit row is built.  Removing the audit middleware, reordering it
before auth, or splitting it into a different module without updating
the reference all silently degrade audit-trail attribution to
``actor=None`` on every Activity row.  See AGENTS.md → *Load-bearing
files* before modifying.

The contract test lives **with the middleware**, not with the
settings, because the middleware is the consumer of the ordering
invariant:
``epicurrents/tests/test_middleware_failure_modes.py`` —
``test_middleware_is_registered`` +
``test_middleware_runs_after_authentication``.

Other settings here are normal Django configuration; only the
``MIDDLEWARE`` list carries the load-bearing audit-trail contract.
"""

import base64
import os
import re
from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent


SECRET_KEY = config("SECRET_KEY", default="django-insecure-change-me")
DEBUG = False
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())


AUTH_USER_MODEL = "user.User"

INSTALLED_APPS = [
    "user.apps.UserConfig",
    "activity.apps.ActivityConfig",
    "annotations.apps.AnnotationsConfig",
    "compute.apps.ComputeConfig",
    "epicurrents.apps.EpicurrentsConfig",
    "recordings.apps.RecordingsConfig",
    "notifications.apps.NotificationsConfig",
    "library.apps.LibraryConfig",
    "media.apps.MediaConfig",
    "federation.apps.FederationConfig",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    # AuthenticationMiddleware MUST precede ApiActivityLoggingMiddleware so
    # request.user is populated by the time the audit row is built; otherwise
    # every Activity row's actor silently drops to AnonymousUser.  Pinned by
    # epicurrents/tests/test_middleware_failure_modes.py.
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Per-identity API request-rate throttle. Runs after AuthenticationMiddleware
    # and SessionMiddleware (it keys on request.user / session) and before the
    # audit middleware so a throttled flood creates no Activity rows. Fails open;
    # gated by API_THROTTLE_ENABLED. See epicurrents/throttle.py.
    "epicurrents.middleware.ApiThrottleMiddleware",
    "epicurrents.middleware.ApiActivityLoggingMiddleware",
    # Sets COOP/COEP/CORP when ENABLE_CROSS_ORIGIN_ISOLATION is true so the
    # browser flips on crossOriginIsolated and SharedArrayBuffer becomes
    # available. No-op when the setting is false. Single platform-wide source
    # for these headers — projects that require cross-origin isolation
    # (e.g. dicom's OHIF WASM decoders) document the dependency in their
    # README and rely on the deployment to enable the setting.
    "epicurrents.middleware.CrossOriginIsolationMiddleware",
    # Attaches Content-Security-Policy + Permissions-Policy when configured;
    # a no-op in development (the policy settings are empty there) and active
    # in production. Only sets response headers, so its position relative to
    # the auth / audit ordering above does not matter.
    "epicurrents.middleware.SecurityHeadersMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

ROOT_URLCONF = "epicurrents.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "epicurrents.context_processors.debug_mode",
            ],
        },
    },
]

WSGI_APPLICATION = "epicurrents.wsgi.application"
ASGI_APPLICATION = "epicurrents.asgi.application"

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://redis:6379/0")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="redis://redis:6379/1")
# Leave Django's LOGGING in charge inside the worker.
#
# Celery replaces the root logger's handlers on worker startup by default, and
# ``epicurrents.security`` has no handler of its own — it propagates to root.
# So with the hijack on, every security event emitted from a Celery task is
# formatted by Celery instead of by the configured formatter, which in
# production means plain text where the rest of the stream is JSON.
#
# That is not cosmetic. ``audit.chain_break``, ``audit.chain_gap``,
# ``audit.genesis_invalid`` and ``audit.derived_state_mismatch`` are emitted by
# ``activity.tasks.verify_audit_integrity``, a beat task — the highest-severity
# events in the taxonomy, and the ones an off-host sink exists to receive. A
# shipper or alert rule that parses the line as JSON silently matches none of
# them, and nothing anywhere reports a problem.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# Cache — overridden to Redis in production.py
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}
CELERY_BEAT_SCHEDULE = {
    "emit-security-heartbeat": {
        "task": "epicurrents.tasks.emit_security_heartbeat",
        # Every 5 minutes. This is the positive signal the off-host log sink's
        # dead-man rules watch for, so the value is half of a contract: the
        # alerting window on the receiving side has to be a comfortable
        # multiple of it. The task publishes this interval in the event so the
        # receiver can derive its window instead of repeating the number.
        "schedule": 300,
    },
    "purge-deleted-recordings": {
        "task": "recordings.tasks.purge_deleted_recordings",
        # Runs every 3 hours. Adjust via django-celery-beat admin if needed.
        "schedule": 3 * 60 * 60,
    },
    "purge-deleted-media": {
        "task": "media.tasks.purge_deleted_media",
        # Runs every 3 hours, alongside the recordings purge.
        "schedule": 3 * 60 * 60,
    },
    "purge-deleted-library": {
        "task": "library.tasks.purge_deleted_library",
        # Runs every 3 hours, alongside the other trash purges. Controlled
        # by LIBRARY_TRASH_RETENTION_DAYS (default 30).
        "schedule": 3 * 60 * 60,
    },
    "purge-expired-access-rights": {
        "task": "epicurrents.tasks.purge_expired_access_rights",
        # Runs every 6 hours. Adjust via django-celery-beat admin if needed.
        "schedule": 6 * 60 * 60,
    },
    "clear-expired-sessions": {
        "task": "epicurrents.tasks.clear_expired_sessions",
        # Runs daily. Django sessions expire on their own; this just reclaims the rows.
        "schedule": 24 * 60 * 60,
    },
    "archive-old-activity": {
        "task": "activity.tasks.archive_old_activity",
        # Runs daily. Controlled by ACTIVITY_ARCHIVE_AFTER_DAYS (default 90).
        "schedule": 24 * 60 * 60,
    },
    "verify-audit-integrity": {
        "task": "activity.tasks.verify_audit_integrity",
        # Runs daily. Walks every v3 chain shard and scans recent rows
        # for derived-state mismatches; controlled by
        # ACTIVITY_DERIVED_CHECK_WINDOW_DAYS (default 7).
        "schedule": 24 * 60 * 60,
    },
    "prune-federation-audit-log": {
        "task": "federation.tasks.prune_federation_audit_log",
        # Runs daily. Controlled by FEDERATION_AUDIT_RETENTION_DAYS
        # (default 2200 ≈ 6 years; 0 disables pruning).
        "schedule": 24 * 60 * 60,
    },
}

# Retention window for FederationAuditLog rows (remote-subject access log).
# Default matches the HIPAA-style 6-year regulatory minimum; deployments with
# a lower floor tune it down, 0 keeps the log forever.
FEDERATION_AUDIT_RETENTION_DAYS = config("FEDERATION_AUDIT_RETENTION_DAYS", default=2200, cast=int)

# Sliding window (in days) for the derived-state recompute phase of
# ``verify_audit_integrity``. Chain verification is always full-scope;
# derived-state recompute is per-row and bounded to recent rows because
# the digester cost is dominated by the dependent-state walk (e.g.
# rehashing every ``SignalInfo`` row for an audited ``Recording``). Set
# to 0 to skip the derived-state phase entirely.
ACTIVITY_DERIVED_CHECK_WINDOW_DAYS = config("ACTIVITY_DERIVED_CHECK_WINDOW_DAYS", default=7, cast=int)

WEBPUSH_VAPID_PUBLIC_KEY = config("WEBPUSH_VAPID_PUBLIC_KEY", default="")
WEBPUSH_VAPID_PRIVATE_KEY = config("WEBPUSH_VAPID_PRIVATE_KEY", default="")
WEBPUSH_VAPID_SUBJECT = config("WEBPUSH_VAPID_SUBJECT", default="mailto:admin@epicurrents.local")
RECORDINGS_UPLOAD_PATH = config("RECORDINGS_UPLOAD_PATH", default=str(BASE_DIR / "recordings_uploads"))
RECORDINGS_STAGING_PATH = config("RECORDINGS_STAGING_PATH", default=str(BASE_DIR / "recordings_staging"))
# Mount point for the bulk-import source directory (used by the import_recordings
# management command).  Map a host directory or Docker volume to this path so
# the command can be invoked as:
#   python manage.py import_recordings "$RECORDINGS_IMPORT_PATH" --username <owner>
RECORDINGS_IMPORT_PATH = config("RECORDINGS_IMPORT_PATH", default=str(BASE_DIR / "recordings_import"))
RECORDINGS_TRASH_RETENTION_DAYS = config("RECORDINGS_TRASH_RETENTION_DAYS", default=30, cast=int)
# Tiered preservation of the as-uploaded file.  Three modes:
#   "none"   — never write the original anywhere (current behaviour).
#   "failed" — copy to the originals volume when ingest fails.
#   "all"    — copy to the originals volume before any processing runs.
# Modes "failed" and "all" require ``RECORDINGS_ORIGINALS_PATH`` to be set;
# crossing the "none" → anything-else line is the regulatory-threshold step
# (see recordings/README.md for the policy).  The originals volume is
# write-only from the platform's perspective — no code path reads it back.
RECORDINGS_PRESERVE_MODE = config("RECORDINGS_PRESERVE_MODE", default="none")
# Mount point for the host-controlled originals volume.  Must be writable by
# the worker process.  Required when ``RECORDINGS_PRESERVE_MODE`` is set to
# anything other than ``"none"``; the project's ``apps.ready()`` check fails
# loudly at startup if the combination is incoherent.
RECORDINGS_ORIGINALS_PATH = config("RECORDINGS_ORIGINALS_PATH", default="") or None
# Named ingest pipelines.  Two built-ins are always available: "web" and "import".
# Override a built-in or add a new label here; values may be:
#   - A dict with optional "header" and "signals" sub-dicts
#     (keys map to HeaderPipelineOptions / SignalPipelineOptions fields).
#   - A dotted Python import path to a RecordingPipeline instance or factory.
# Example:
# ── Second-factor enforcement ────────────────────────────────────────────────
# Two booleans rather than one enum because they are flipped at different times
# for different reasons: requiring it of staff is a production-readiness step,
# requiring it of everyone is a change of threat model. An enum would force a
# deployment to settle both at once.
#
# They resolve as: required = FOR_ALL or (FOR_STAFF and (is_staff or is_superuser)).
# FOR_ALL dominates — it covers staff too, so it holds with FOR_STAFF off and the
# combination is redundant rather than contradictory.
#
# Both default off. A flag named for an enforcement it does not perform reads as
# a control in a compliance review, so neither was added until the enrolment path
# that makes them safe to turn on existed.
TWO_FACTOR_REQUIRED_FOR_STAFF = config("TWO_FACTOR_REQUIRED_FOR_STAFF", default=False, cast=bool)
TWO_FACTOR_REQUIRED_FOR_ALL = config("TWO_FACTOR_REQUIRED_FOR_ALL", default=False, cast=bool)

# ── Ingest privacy overrides ─────────────────────────────────────────────────
# Both default off, because they discard information the author may legitimately
# want, and both exist for projects whose data-protection position is that no
# patient personal data reaches the platform at all. Such a project anonymises
# in the client before upload; these settings make the platform stop retaining
# the two things that would otherwise preserve what the client was supposed to
# have removed. A project turns them on in its own settings.py.

# Replace the uploaded filename with an upload timestamp before the row is
# written. Clinical exports are routinely named after the patient, so the
# filename is a direct identifier arriving through a field nobody thinks of as
# one. It is masked from the audit trail already; this stops it reaching the
# live row as well.
RECORDINGS_DISCARD_ORIGINAL_NAME = config("RECORDINGS_DISCARD_ORIGINAL_NAME", default=False, cast=bool)

# Drop annotation content that came out of the uploaded file — the embedded
# text events of an EDF and the sidecar events of a converted Nicolet .e — so
# that everything annotating a recording was written on the platform. Vendor
# event vocabularies identify the acquisition software and through it the
# acquiring laboratory, and free-text events carry whatever the file carried.
#
# Interruptions are deliberately NOT discarded. A gap is geometry, not
# annotation: it records where the signal stops and restarts, carries no text,
# and the viewer and compute layer both read data positions that depend on it.
# Dropping gaps would not protect anyone and would put every event after the
# first splice on signal it does not describe.
RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS = config("RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS", default=False, cast=bool)

# Whether an uploader may ask for annotation text to be kept in the stored file.
# Stripping it is already the default, so this is the difference between a
# default and a prohibition: with this False the request is refused at the
# endpoint and ignored at the point of use, so no caller — the API, the import
# command, or a project calling the task directly — can put clinical free text
# into a stored recording. Left True by default because a deployment holding its
# own clinical recordings may legitimately want the original file intact.
RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS = config("RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS", default=True, cast=bool)

# Whether to keep each channel's ORIGINAL name, transducer and filter strings and
# its original position, alongside the cleaned values. The cleaned values are
# always kept — they are what the stored file contains and what every reader
# needs. These four are the originals, retained so an uploader can see what
# their own file held and so a de-identification problem can be diagnosed after
# the fact; they are visible to nobody else. A deployment whose position is that
# it holds nothing identifying the acquiring laboratory turns them off. Under
# such a workflow the client cleaned the file already, so the columns would hold
# cleaned values anyway — but holding nothing is the difference between that
# being true and being merely likely.
RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA = config(
    "RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA", default=False, cast=bool
)

#   RECORDING_PIPELINES = {
#       "web":    {"header": {"strip_annotation_text": False}},
#       "import": {"header": {"strip_annotation_text": True}},
#   }
RECORDING_PIPELINES: dict = {}
# Maximum size for a single recording upload in bytes (default 2 GiB). Django
# Ninja streams a file part directly to disk, so nothing in the framework caps it
# — the upload view enforces this itself during the chunked write. With the proxy
# overlay deployed, PROXY_MAX_BODY_SIZE has to be at least this value; the boot
# guard in epicurrents/apps.py refuses to start otherwise.
RECORDINGS_MAX_UPLOAD_SIZE = config("RECORDINGS_MAX_UPLOAD_SIZE", default=2 * 1024 * 1024 * 1024, cast=int)
# Deliberately NOT tied to RECORDINGS_MAX_UPLOAD_SIZE, despite the names looking
# like a pair. DATA_UPLOAD_MAX_MEMORY_SIZE does not gate uploaded files at all:
# in Django's multipart parser it caps only ``item_type == FIELD`` — the ordinary
# form fields — and in HttpRequest it caps the raw body of a non-multipart
# request. A file part is exempt and streams to a temporary file under
# FILE_UPLOAD_MAX_MEMORY_SIZE regardless.
#
# So raising this to the recording ceiling bought nothing for uploads and cost a
# 2 GiB in-memory buffer for any caller who POSTs a large JSON body — a bare
# memory-exhaustion lever, aimed at the one tier that is also serving every other
# request. The value below is sized for the bulk JSON endpoints the API actually
# has (bulk rename, bulk set-mains, bulk rollback, annotation import), which deal
# in thousands of small objects rather than megabytes of payload.
DATA_UPLOAD_MAX_MEMORY_SIZE = config("DATA_UPLOAD_MAX_MEMORY_SIZE", default=10 * 1024 * 1024, cast=int)
# ── Media (non-signal) uploads ────────────────────────────────────────────
# Lowercase, dot-prefixed extensions the platform accepts as media files
# (documents today; image / audio / video later). Consulted at both upload
# and download time so a project switch retroactively gates files that no
# longer belong to a supported type. The empty default disables media
# uploads entirely; the active project sets this in its ``settings.py`` to
# opt in (e.g. ``[".md", ".pdf", ".htm", ".html"]`` for a documents-only project).
MEDIA_ALLOWED_UPLOAD_EXTENSIONS: list[str] = []
# ── Reverse-proxy file offload ────────────────────────────────────────────
# When the proxy sits in front and mounts the recordings volume read-only, a raw
# download can be handed to it instead of streaming through a gunicorn thread for
# the whole transfer (see epicurrents/offload.py). Django still handles, authorises
# and audits every request — including every Range request — and only the byte
# transfer moves.
#
# This is a statement about deployment topology, not about risk appetite: it is
# true only when docker-compose.proxy.yml is in the compose file list, which is
# where the env var is set. Off everywhere else, including behind a proxy that
# does not mount the volume. A project that must never offload sets it False in
# its own settings.py.
#
# It never applies to a middleware-applied grant — those bytes are computed, not
# stored, and the interlock lives in offload_file_response(), not here.
PROXY_FILE_OFFLOAD_ENABLED = config("PROXY_FILE_OFFLOAD_ENABLED", default=False, cast=bool)

MEDIA_UPLOAD_PATH = config("MEDIA_UPLOAD_PATH", default=str(BASE_DIR / "media_uploads"))
MEDIA_STAGING_PATH = config("MEDIA_STAGING_PATH", default=str(BASE_DIR / "media_staging"))
# Largest accepted media upload in bytes (default 256 MB). Documents sit well
# under this; a project that accepts video raises it in its settings (2 GB,
# say).
MEDIA_MAX_UPLOAD_SIZE = config("MEDIA_MAX_UPLOAD_SIZE", default=256 * 1024 * 1024, cast=int)
# Days a soft-deleted media file lingers before purge_deleted_media hard-deletes
# the row + file. Mirrors RECORDINGS_TRASH_RETENTION_DAYS.
MEDIA_TRASH_RETENTION_DAYS = config("MEDIA_TRASH_RETENTION_DAYS", default=30, cast=int)
# Activity rows older than this many days are marked archived and hidden from default queries.
# Archived rows are never deleted — set to 0 to disable archiving entirely.
ACTIVITY_ARCHIVE_AFTER_DAYS = config("ACTIVITY_ARCHIVE_AFTER_DAYS", default=90, cast=int)
# API paths that match the audit middleware's _API_PATH_RE but should NOT
# produce an Activity row.  Used for operational endpoints whose volume would
# drown the data-interaction signal (health checks, the publicly-served VAPID
# key, etc.).  The list is exact-match against ``request.path``.
#
# The per-endpoint policy reasoning is documented in
# .review/exemptions/audit-trail-completeness.md.  When adding an entry here,
# update that registry too so the audit-trail-completeness review agent
# knows the omission is intentional.
ACTIVITY_PATH_SKIP_LIST: tuple[str, ...] = (
    "/api/v1/health",  # epicurrents.health
    "/api/v1/ready",  # epicurrents.readiness — container healthcheck poll
    "/annotations/api/v1/health",  # annotations.health
    "/api/v1/notifications/vapid-public-key",  # static public push key
    "/api/v1/user/auth-config",  # public login-method discovery
)


# ──────────────────────────────────────────────────────────────────────────────
# Audit-trail HMAC keys.
# ──────────────────────────────────────────────────────────────────────────────
# ACTIVITY_HASH_KEY_V{N} env vars carry the base64url-encoded HMAC keys used by
# the v2+ audit hash algorithm. ACTIVITY_HASH_KEY_CURRENT names the version
# compute_audit_hash writes with for new rows. Verification of an existing row
# reads the row's stored ``hash_key_version`` and looks up the corresponding
# key in ACTIVITY_HASH_KEYS — older keys must survive in .env (or be archived
# elsewhere) for as long as rows under them remain in the audit trail.
#
# When no keys are present the audit trail falls back to v1 (sha256, no
# secret). The epicurrents/apps.py guard refuses to boot in production with
# the fallback active — see _guard_activity_hash_key.
_HASH_KEY_PATTERN = re.compile(r"^ACTIVITY_HASH_KEY_V(\d+)$")


def _discover_activity_hash_keys() -> dict[int, bytes]:
    """Scan os.environ for ACTIVITY_HASH_KEY_V{N} entries and decode each as
    base64url. Invalid entries are skipped silently — the production guard
    catches the "no usable keys at all" case at boot."""
    keys: dict[int, bytes] = {}
    for env_var, value in os.environ.items():
        match = _HASH_KEY_PATTERN.match(env_var)
        if not match:
            continue
        raw = (value or "").strip()
        if not raw:
            continue
        version = int(match.group(1))
        padded = raw + "=" * (-len(raw) % 4)
        try:
            keys[version] = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            continue
    return keys


ACTIVITY_HASH_KEYS: dict[int, bytes] = _discover_activity_hash_keys()
ACTIVITY_HASH_KEY_CURRENT: int = config("ACTIVITY_HASH_KEY_CURRENT", default=1, cast=int)


# CIDR ranges whose REMOTE_ADDR is treated as a trusted reverse proxy.
# ``epicurrents.security_log.get_client_ip`` honours X-Forwarded-For only when
# the immediate hop (REMOTE_ADDR) falls inside one of these networks; otherwise
# it returns REMOTE_ADDR regardless of header presence. Empty default = XFF is
# ignored entirely, which is the right stance when the app is exposed directly.
# Operators behind nginx / Traefik / Cloudflare must opt in by listing the
# proxy's source range (CSV in .env, e.g. TRUSTED_PROXIES=127.0.0.1/32,10.0.0.0/8).
TRUSTED_PROXIES: list[str] = config("TRUSTED_PROXIES", default="", cast=Csv())

# Set the COOP/COEP/CORP triple so the browser flips on crossOriginIsolated and
# SharedArrayBuffer becomes available. Required by the viewer's SAB signal-storage
# path and by the dicom project's OHIF WASM decoders. Disabled by default because
# COEP=require-corp rejects any cross-origin subresource that doesn't send CORP —
# turning this on without auditing third-party fetches will break those flows.
# See ``epicurrents.middleware.CrossOriginIsolationMiddleware``.
ENABLE_CROSS_ORIGIN_ISOLATION = config("ENABLE_CROSS_ORIGIN_ISOLATION", default=False, cast=bool)

# PHI hygiene: emit Cache-Control: no-store so neither the browser nor an
# intermediary proxy persists response bodies (which can carry PHI). Applied via
# setdefault in ``SecurityHeadersMiddleware``, so views serving non-PHI static
# assets (the content-hashed SPA bundles, the viewer lib) opt back into caching
# by setting their own Cache-Control. On unless explicitly opted out — set
# DISABLE_NO_STORE_HEADERS=True only where a cache layer must store responses.
DISABLE_NO_STORE_HEADERS = config("DISABLE_NO_STORE_HEADERS", default=False, cast=bool)

# Standalone, auth-free public viewer at /viewer/<mode> (see
# ``epicurrents.views.public_viewer_view``). Off by default — opt in per
# deployment. The page sets its own COOP/COEP so the viewer's SharedArrayBuffer
# memory manager works regardless of the site-wide ENABLE_CROSS_ORIGIN_ISOLATION.
ENABLE_PUBLIC_VIEWER = config("ENABLE_PUBLIC_VIEWER", default=False, cast=bool)

# Non-commercial feature gate. Some compute features embed material licensed for
# NON-COMMERCIAL use only — model weights an operator provisions under such
# terms, for instance. They are DISABLED by default and
# unlock only when a deployment explicitly declares non-commercial use here. This
# is a compliance safeguard and an intent declaration, NOT a licence for
# commercial use; a cost-recovery fee does not by itself make a deployment
# commercial (CC's NonCommercial standard). See ``compute/licensing.py``.
# The licensed material itself is operator-provisioned, never vendored — the
# flag unlocks the mechanism, the operator supplies the model.
EPICURRENTS_NONCOMMERCIAL_USE = config("EPICURRENTS_NONCOMMERCIAL_USE", default=False, cast=bool)

# Mains (power-line) frequency for this deployment, in Hz — 50 across Europe, 60
# in North America. Unset ⇒ no default mains notch and BIDS PowerLineFrequency
# = 'n/a'. This is the deployment backstop; an individual recording may override
# it via ``Recording.power_line_frequency`` (e.g. a dataset imported from another
# mains region). Deliberately NO hardcoded regional default — notching the wrong
# region's mains only distorts the signal. Resolved by
# ``compute.mains.resolve_recording_notch_hz``.
EEG_MAINS_HZ = config(
    "EEG_MAINS_HZ",
    default=None,
    cast=lambda v: float(v) if str(v).strip().lower() not in ("", "none") else None,
)

# Per-mode config for the public viewer: each mode names a viewer lib to load
# and a fixed SETUP (no platform data, no URL params). The active project's
# ``settings.py`` may add a mode or override "public" via the dict-merge in
# ``epicurrents.project_loader`` — e.g. a "project" mode pointing ``lib_path``
# at the full standalone ``/viewer/`` with the project's own ``activeModules``
# and extra setup. Keys here also drive the route regex in ``epicurrents.urls``.
PUBLIC_VIEWER_MODES = {
    "public": {
        "lib_path": "/viewer/",
        # /viewer/ holds the builder edition, whose UMD bundle is named .umd.js.
        # Modes pointing at a per-project build under /viewer/<project>/ omit this
        # and get the .umd.cjs default those builds emit.
        "lib_file": "epicurrents-lib.umd.js",
        "setup": {
            "activeModules": ["eeg"],
            "assetPath": "/viewer/",
            "containerId": "viewer",
            "logThreshold": "WARN",
            # Serve Pyodide's runtime from our own origin instead of the jsdelivr
            # CDN (the viewer's default), so the installed app's compute works
            # offline and is cacheable by the service worker. The version-pinned
            # "full" distribution is vendored at this path at deploy time.
            "pyodideAssetPath": "/vendor/pyodide/314.0.2/",
            "useSAB": True,
        },
    },
}

# Content-Security-Policy + Permissions-Policy, emitted by
# ``epicurrents.middleware.SecurityHeadersMiddleware``. Empty by default so
# they are off in development (where the Vite dev server serves the frontend);
# ``epicurrents/settings/production.py`` sets the baselines. CSP defaults to
# Report-Only via ``CSP_REPORT_ONLY`` — see the middleware docstring and
# docs/operations.md → Security headers.
CONTENT_SECURITY_POLICY = config("CONTENT_SECURITY_POLICY", default="")
CSP_REPORT_ONLY = config("CSP_REPORT_ONLY", default=True, cast=bool)
PERMISSIONS_POLICY = config("PERMISSIONS_POLICY", default="")

ADMIN_USERNAME = config("ADMIN_USERNAME", default="admin")
ADMIN_PASSWORD = config("ADMIN_PASSWORD", default="admin")
ADMIN_EMAIL = config("ADMIN_EMAIL", default="admin@epicurrents.local")

# Email — configure via environment variables.
# For development the console backend prints to stdout (no SMTP needed).
# For production set EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
# and supply the remaining EMAIL_* variables for your provider.
EMAIL_BACKEND = config("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = config("EMAIL_HOST", default="localhost")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=False, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = config("EMAIL_FROM", default=EMAIL_HOST_USER or "noreply@epicurrents.local")

# Base URL of the frontend, used to build password reset links in emails
FRONTEND_URL = config("FRONTEND_URL", default="http://localhost:5173")

# Federation — inter-instance data sharing via Ed25519-signed JWTs.
# - FEDERATION_INSTANCE_URL: canonical HTTPS base URL of this instance (e.g. https://eeg.example.com).
#   Must match the value published in /.well-known/epicurrents-federation.json.
# - FEDERATION_PUBLIC_KEY / FEDERATION_PRIVATE_KEY: raw Ed25519 key bytes encoded as
#   URL-safe base64 (no padding, 43 chars each) — same format as VAPID keys.
#   Generate with: python manage.py generate_federation_keys
# - FEDERATION_KEY_FETCH_TIMEOUT: seconds to wait when fetching a peer's public key.
# - FEDERATION_JWT_TTL: lifetime of outbound federation JWTs in seconds.
FEDERATION_INSTANCE_URL = config("FEDERATION_INSTANCE_URL", default="")
FEDERATION_PUBLIC_KEY = config("FEDERATION_PUBLIC_KEY", default="")
FEDERATION_PRIVATE_KEY = config("FEDERATION_PRIVATE_KEY", default="")
# Rotation overlap slot.  Set FEDERATION_PUBLIC_KEY_NEXT (and the matching
# private key) to announce a forthcoming rotation: the well-known endpoint
# publishes both keys so peers can cache the upcoming one before this instance
# starts signing with it.  See federation/README.md → "Key rotation".  Empty
# string disables the overlap window (one-step rotation, backwards-compatible).
FEDERATION_PUBLIC_KEY_NEXT = config("FEDERATION_PUBLIC_KEY_NEXT", default="")
FEDERATION_PRIVATE_KEY_NEXT = config("FEDERATION_PRIVATE_KEY_NEXT", default="")
FEDERATION_KEY_FETCH_TIMEOUT = config("FEDERATION_KEY_FETCH_TIMEOUT", default=10, cast=int)
FEDERATION_JWT_TTL = config("FEDERATION_JWT_TTL", default=60, cast=int)
# Per-peer exfiltration limits on the federated download paths.  Defaults are
# generous enough that an honest peer running normal workloads will not notice,
# but cap a compromised peer that tries to bulk-download the corpus.  Set
# either to 0 to disable that particular limit.  See federation/README.md →
# "Rate limiting and quotas".
FEDERATION_PEER_DAILY_BYTE_LIMIT = config(
    "FEDERATION_PEER_DAILY_BYTE_LIMIT",
    default=1024**4,
    cast=int,  # 1 TiB
)
FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = config(
    "FEDERATION_PEER_DOWNLOAD_RATE_LIMIT",
    default=60,
    cast=int,  # per minute
)
# Per-peer rate limit on `inbound_check_object`.  Defense against object-id
# enumeration by a compromised peer — even though the 404-collapse from
# commit 5 keeps the peer from distinguishing missing-from-denied, capping
# the probe rate slows enumeration attacks and surfaces them in the audit log.
FEDERATION_PEER_INBOUND_RATE_LIMIT = config(
    "FEDERATION_PEER_INBOUND_RATE_LIMIT",
    default=600,
    cast=int,  # per minute
)
# SSRF defense for `fetch_peer_public_key` blocks URLs resolving to private
# IPs by default.  Set to True in dev environments where you legitimately
# need to federate against a localhost peer — NEVER enable in production.
FEDERATION_ALLOW_PRIVATE_PEER_URLS = config("FEDERATION_ALLOW_PRIVATE_PEER_URLS", default=False, cast=bool)

# Global per-identity request-rate throttle on the REST API surface (see
# epicurrents/throttle.py). Keyed per authenticated user / share token /
# session, never naively per IP — deployments serve NAT'd shared-egress groups
# (a classroom, a hospital proxy) that present as one address. On by default
# with limits generous enough that honest use never hits them; a project plugin
# may zero or override any of these. Development disables the whole thing.
API_THROTTLE_ENABLED = config("API_THROTTLE_ENABLED", default=True, cast=bool)
# Per-minute request ceilings for identified callers, keyed by scope. The
# scope map below routes paths to a scope; everything else uses "default".
API_THROTTLE_RATES = {
    "default": config("API_THROTTLE_RATE_DEFAULT", default=300, cast=int),
    "upload": config("API_THROTTLE_RATE_UPLOAD", default=30, cast=int),
}
# Ordered (path-prefix, scope) pairs; first match wins. Uploads are heavier and
# rarer than reads, so they carry a tighter ceiling than the default scope.
API_THROTTLE_SCOPE_MAP = (
    ("/recordings/api/v1/upload", "upload"),
    ("/media/api/v1/upload", "upload"),
)
# Single high ceiling for unidentified callers (no user, token, or session),
# keyed on client IP — the only tier where shared egress is unavoidable, so it
# is set well above any legitimate per-group rate. Set to 0 to defer IP-level
# limiting entirely to the reverse proxy.
API_THROTTLE_IP_RATE = config("API_THROTTLE_IP_RATE", default=1000, cast=int)

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# CSRF protection for the cookie-authenticated Ninja APIs operates in two
# layers. First, the SameSite=Lax cookie attributes below stop the browser
# attaching the session cookie to cross-site requests; this is a security
# contract that must not depend on Django's and the browser's implicit
# defaults staying aligned. Do not relax to "None"; tightening to "Strict"
# would break login-redirect flows from external links. Second, the Ninja
# mounts are csrf_exempt, so every session-authenticated unsafe request is
# routed through epicurrents.auth.enforce_session_csrf, which runs Django's
# token check explicitly (see that module's load-bearing docstring).
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Master switch for the enforce_session_csrf chokepoint. On by default;
# development.py turns it off so the Vite-served SPA and local tooling do
# not need to carry a CSRF token. Never disable in a production deployment.
SESSION_CSRF_ENFORCED = config("SESSION_CSRF_ENFORCED", default=True, cast=bool)

# ──────────────────────────────────────────────────────────────────────────────
# External login — OpenID Connect (Microsoft Entra ID).
# ──────────────────────────────────────────────────────────────────────────────
# Off by default and non-operational until an operator registers the app with
# the identity provider and supplies the credentials below. See user/README.md
# → "External login (OIDC)" for the production setup steps and for the
# PHI-containment controls: the email-domain allowlist here is control #1; the
# identity-provider-side controls #2/#3 are documented there.
OIDC_ENABLED = config("OIDC_ENABLED", default=False, cast=bool)

# First-login provisioning policy (shared across providers).
OIDC_AUTO_CREATE_USERS = config("OIDC_AUTO_CREATE_USERS", default=True, cast=bool)
OIDC_LINK_BY_VERIFIED_EMAIL = config("OIDC_LINK_BY_VERIFIED_EMAIL", default=True, cast=bool)

# Single-tenant Microsoft Entra ID. The tenant GUID locks logins to one
# directory — the token ``tid`` claim is checked against it — and the redirect
# URI must match the value registered in the Entra app exactly.
OIDC_ENTRA_TENANT_ID = config("OIDC_ENTRA_TENANT_ID", default="")
OIDC_ENTRA_CLIENT_ID = config("OIDC_ENTRA_CLIENT_ID", default="")
OIDC_ENTRA_CLIENT_SECRET = config("OIDC_ENTRA_CLIENT_SECRET", default="")
# Comma-separated email domains permitted to sign in / be provisioned. Empty =
# any account from the configured tenant. Enforced fail-closed in user/oidc.py.
OIDC_ENTRA_ALLOWED_DOMAINS = config("OIDC_ENTRA_ALLOWED_DOMAINS", default="", cast=Csv())
OIDC_REDIRECT_URI = config(
    "OIDC_REDIRECT_URI",
    default=f"{FRONTEND_URL}/api/v1/user/oidc/entra/callback",
)

# Provider registry consumed by user/oidc.py. A provider is "configured" only
# when client_id, client_secret, and authority are all set; half-filled entries
# are ignored so the SPA never offers a broken button.
OIDC_PROVIDERS = {
    "entra": {
        "label": "Microsoft",
        "tenant_id": OIDC_ENTRA_TENANT_ID,
        "client_id": OIDC_ENTRA_CLIENT_ID,
        "client_secret": OIDC_ENTRA_CLIENT_SECRET,
        "authority": (f"https://login.microsoftonline.com/{OIDC_ENTRA_TENANT_ID}/v2.0" if OIDC_ENTRA_TENANT_ID else ""),
        "scopes": ["openid", "profile", "email"],
        "allowed_domains": list(OIDC_ENTRA_ALLOWED_DOMAINS),
        "redirect_uri": OIDC_REDIRECT_URI,
    },
}

# OIDCBackend is reachable only via an ``oidc_identity`` kwarg, so it is inert
# for password logins; ModelBackend stays first. The project loader list-merges
# this key, so a project plugin may append further backends.
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "user.auth_backends.OIDCBackend",
]

# Interactive API docs (/docs + /openapi.json on every Ninja mount). The
# schema discloses the full endpoint surface to unauthenticated callers,
# so the mounts are exposed only when DEBUG is on. Both settings are
# needed: docs_url=None removes only the UI, openapi_url=None removes
# the schema itself.
API_DOCS_URL = "/docs" if DEBUG else None
API_OPENAPI_URL = "/openapi.json" if DEBUG else None

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static")

# Deploy-vendored, version-pinned browser assets served at /vendor/ (self-hosted
# Pyodide runtime + its wheel closure). Kept under frontend/ next to the viewer
# that consumes it — matching how other vendored assets live beside their consumer
# — and served, not bundled: NOT
# part of collectstatic or the Vite build. Populated at deploy (gitignored; bind-
# mounted read-only in prod, like frontend/dist). Overridable to another path.
VENDOR_DIR = Path(config("VENDOR_DIR", default=str(BASE_DIR / "frontend" / "vendor")))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Merge enabled plugins' settings on top of the base settings, then the active
# project's on top of those. Order fixes precedence: common < plugins < project
# < .env, so a project always has the last word over any plugin it composes
# with. Both are no-ops when their env vars are unset.
#   EPICURRENTS_PLUGINS — comma-separated plugin names (zero or more).
#   EPICURRENTS_PROJECT — single active project name.
from epicurrents.plugin_loader import apply_plugin_settings
from epicurrents.project_loader import apply_project_settings

apply_plugin_settings(globals())
apply_project_settings(globals())
