"""Production settings — Postgres, ``DEBUG=False``, HTTPS security headers, Redis cache, JSON logging to stdout."""

from decouple import config

from .common import *

DEBUG = False

# ---------------------------------------------------------------------------
# Cache — separate Redis database from the Celery broker.
# ---------------------------------------------------------------------------

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("REDIS_CACHE_URL", default="redis://redis:6379/2"),
        "KEY_PREFIX": "epicurrents",
    }
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="epicurrents"),
        "USER": config("DB_USERNAME", default="epicurrents"),
        "PASSWORD": config("DB_PASSWORD", default="epicurrents"),
        "HOST": config("DB_HOSTNAME", default="db"),
        "PORT": config("DB_PORT", default=5432, cast=int),
    }
}

# ---------------------------------------------------------------------------
# Security headers — require HTTPS in production.
# ---------------------------------------------------------------------------

# When Django sits behind a reverse proxy (e.g. SWAG/nginx) the proxy
# terminates TLS and forwards requests to Django as plain HTTP. This header
# tells Django to treat requests with X-Forwarded-Proto: https as secure so
# SECURE_SSL_REDIRECT does not loop.
#
# Trusting the header unconditionally is unsafe in the no-proxy case: the
# header is client-supplied, so a directly-exposed deployment would let any
# caller bypass the HTTPS redirect by claiming X-Forwarded-Proto: https.
# The header is therefore trusted only when the deployment has declared its
# proxy tier via TRUSTED_PROXIES (which proxied deployments need anyway for
# accurate security-log client IPs), or via an explicit
# USE_X_FORWARDED_PROTO=True override. A proxied deployment that sets
# neither announces itself immediately as a redirect loop rather than
# silently weakening the redirect.
_use_xfp = config("USE_X_FORWARDED_PROTO", default="")
if _use_xfp != "":
    _trust_forwarded_proto = _use_xfp.strip().lower() in ("true", "1", "yes")
else:
    _trust_forwarded_proto = bool(TRUSTED_PROXIES)
if _trust_forwarded_proto:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Redirect all plain-HTTP requests to HTTPS.
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)

# The web container's healthcheck requests /api/v1/ready over loopback, as plain
# HTTP with no X-Forwarded-Proto, so the redirect above answers it with a 301 to
# an https:// URL that gunicorn does not speak. The probe then fails on every
# run and the container reports unhealthy while serving production traffic
# perfectly well — a failure invisible to any test that does not run under the
# production settings.
#
# Exempting the path means an unauthenticated plain-HTTP caller gets the answer
# rather than a redirect. That answer is two booleans about whether the database
# and cache responded; it carries no PHI, no configuration and no credential,
# which is why the endpoint is written to return no exception detail. Matched
# against request.path with the leading slash stripped, anchored so it covers
# this path and nothing under it.
SECURE_REDIRECT_EXEMPT = [r"^api/v1/ready$"]

# HSTS — tell browsers to reach this host over HTTPS only. Deliberately short by
# default. The header is a one-way promise: a browser that has seen
# max-age=31536000 refuses plain HTTP to the host for a year and there is no way
# to recall it from clients that already cached it, so a certificate that stops
# renewing, a domain that moves, or a subdomain that turns out to need plain HTTP
# becomes a year-long outage for those visitors rather than a rollback. Five
# minutes buys the same protection against an active downgrade attempt while a
# deployment is being proven, and costs nothing to raise afterwards.
#
# The ramp, once the certificate has renewed at least once on the real domain:
# 3600 -> 604800 -> 31536000. Turn SECURE_HSTS_PRELOAD on only after the
# year-long max-age has served without incident and every subdomain of the
# registered domain is HTTPS-only — preload-list submission is effectively
# irreversible. See docs/operations.md -> Security headers.
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=300, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True, cast=bool)
SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=False, cast=bool)

# Mark session and CSRF cookies as secure (HTTPS-only).
# Configurable so a dev / staging deployment behind a non-HTTPS endpoint
# (e.g. plain-HTTP localhost) can opt down — the cookies would otherwise
# carry the Secure flag and the browser would refuse to send them back
# over HTTP, producing 403 CSRF failures on every form submit. Defaults
# stay True so the secure posture is the boot-up default; a boot warning
# fires in ``epicurrents.apps`` when either is False under
# DJANGO_MODE=production so an accidental flip in real production is
# loud rather than silent.
SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", default=True, cast=bool)
CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", default=True, cast=bool)

# Django's default session lifetime is two weeks — too long for a platform
# serving PHI. 12 hours covers a clinical workday with margin; deployments
# with different needs tune it via SESSION_COOKIE_AGE (seconds).
SESSION_COOKIE_AGE = config("SESSION_COOKIE_AGE", default=12 * 60 * 60, cast=int)

# Prevent the browser from sniffing content types.
SECURE_CONTENT_TYPE_NOSNIFF = True

# Referrer-Policy, emitted by Django's SecurityMiddleware.
SECURE_REFERRER_POLICY = config("SECURE_REFERRER_POLICY", default="same-origin")

# ---------------------------------------------------------------------------
# Content-Security-Policy + Permissions-Policy
# (emitted by epicurrents.middleware.SecurityHeadersMiddleware)
# ---------------------------------------------------------------------------
#
# CSP is ENFORCED by default (CSP_REPORT_ONLY=False). It shipped report-only
# while the policy had never been checked against a running deployment; that
# pass is done, so the default is the enforcing one — a control that is off
# unless an operator knows to turn it on is off nearly everywhere.
# docs/operations.md → Security headers covers how to go back to report-only,
# which is what a deployment adding an origin should do first. The baseline is tuned
# for: the self-hosted SPA; WebAwesome's inline component styles
# ('unsafe-inline' in style-src); the two inline bootstrap scripts in the
# statically served index.html ('unsafe-inline' in script-src — a static file
# has no per-request nonce); Pyodide WASM ('wasm-unsafe-eval'); and the
# data:/blob: image and worker sources the viewer uses.
# frame-ancestors 'none' / base-uri 'self' / object-src 'none' add clickjacking
# and injection hardening that the existing headers do not cover.
#
# No third-party origin is listed, and that is deliberate rather than an
# oversight: everything the deployment serves is same-origin, Pyodide included —
# the pinned distribution is vendored under /vendor/pyodide/<version>/. The
# policy used to allow https://cdn.jsdelivr.net in script-src and connect-src,
# left from before Pyodide was vendored, and the audit that removed it found the
# SPA's inline bootstrap still naming a jsdelivr URL pinned to a nine-major-
# versions-older runtime. Allowing the origin is what would have let that load
# succeed quietly; refusing it turns any regression back to a CDN into a visible
# failure instead of a third-party fetch on a PHI deployment.
CONTENT_SECURITY_POLICY = config(
    "CONTENT_SECURITY_POLICY",
    default=(
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        # data: is for icons, not for network access. WebAwesome resolves every
        # <wa-icon> to a data:image/svg+xml URL and then *fetches* it, so it can
        # run the mutator that rewrites fill to currentColor — and a fetch is
        # governed by connect-src, not img-src. Without this every icon in the
        # UI silently fails to render, base and project sets alike, while the
        # page is otherwise entirely functional. A data: URL carries its own
        # payload and names no host, so permitting it here grants no reach.
        "connect-src 'self' data:; "
        "worker-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'"
    ),
)
# The claim that this was verified in a browser against the base platform did not
# survive contact with a real deployment: `connect-src 'self'` blocked every icon
# on a production server, which is not a subtle violation — the UI renders with no
# icons at all. Whatever that verification exercised, it was not this policy in
# front of this SPA. Treat the coverage note below as a list of what is known to
# be untested rather than as assurance about the rest, and prefer CSP_REPORT_ONLY
# for a cycle after any change here. Two configurations remain uncovered and
# should run report-only for a cycle before trusting this default — the dicom
# plugin, whose OHIF viewer is a submodule that was not checked out when the
# policy was tuned, and any project whose own views reach an external origin.
# Both can extend or relax CONTENT_SECURITY_POLICY through their settings.py, or
# the deployment can set CSP_REPORT_ONLY=True in .env for a cycle.
#
# Failing loudly is the point of the default. An enforced policy names the
# blocked URL and directive in the console, where report-only on a deployment
# nobody is watching reports to no one — nothing collects violations server-side.
CSP_REPORT_ONLY = config("CSP_REPORT_ONLY", default=False, cast=bool)

# Deny powerful browser features the platform does not use.
PERMISSIONS_POLICY = config(
    "PERMISSIONS_POLICY",
    default=(
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()"
    ),
)

# ---------------------------------------------------------------------------
# Logging — structured to stdout so Docker/systemd can capture it.
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        # Emits every ``extra=`` field as a discrete JSON key (notably
        # ``security_event_type`` and the structured security fields), so a
        # log shipper can label / alert on them rather than regexing the
        # message string. See epicurrents/log_formatters.py and
        # docs/operations.md → Security log stream.
        "json": {
            "()": "epicurrents.log_formatters.JSONLogFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": config("LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": config("DJANGO_LOG_LEVEL", default="WARNING"),
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}
