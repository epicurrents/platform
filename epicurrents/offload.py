"""Hand raw file serving to the reverse proxy instead of streaming it through gunicorn.

⚠️ LOAD-BEARING — the ``apply_middleware`` interlock in :func:`offload_file_response`
is the only thing standing between a de-identified grant and the raw PHI on disk.

The problem this solves: gunicorn runs a bounded thread pool
(``GUNICORN_WORKERS x GUNICORN_THREADS``), and a recording download holds one of
those slots for the whole transfer. Eight concurrent downloads on the default
sizing and the API stops answering.

The mechanism is nginx's ``X-Accel-Redirect`` idea, spelled for Caddy. Django
handles the request exactly as it always has — same URL, same authentication,
same object-level permission check, same ``Activity`` row — and then, instead of
streaming bytes, answers with an empty 200 carrying ``X-Serve-Path``. The proxy
recognises that header, discards the empty body, and serves the file from a
read-only mount of the same volume. Requests the proxy cannot help with (a 403, a
404, a middleware-transformed stream) carry no such header and pass through
untouched, so there is exactly one authorisation path and no second
implementation to keep in parity with the first.

Two consequences worth stating because they are easy to assume wrongly:

- **The audit trail is not degraded.** Django still sees and logs every request,
  including every Range request. What it does not observe is whether the transfer
  completed — which it does not observe today either, since a client can abandon
  a ``StreamingHttpResponse`` mid-stream.
- **This is a deployment capability, not a policy.** It requires the proxy to be
  in front *and* mounting the file root read-only. ``PROXY_FILE_OFFLOAD_ENABLED``
  is set by the proxy compose overlay, not by an operator reasoning about risk. A
  project that must never offload sets it ``False`` in its own ``settings.py``.

Why the middleware interlock is absolute: with ``apply_middleware=True`` the bytes
that should reach the caller are *computed* — an anonymised header, and under a
signal pipeline every data record transformed individually. There is no file on
disk that holds them. Handing the proxy a path in that case would serve the
original recording, patient-identifying header and clinical annotation text
included, to precisely the caller the flag exists to protect against. It fails
open in the safe direction by construction: every refusal returns ``None`` and the
caller streams as it always did.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponse
from django.utils.http import content_disposition_header

logger = logging.getLogger(__name__)

# Set by the proxy on every request it forwards, and overwritten there rather than
# merely trusted, so a client cannot ask for this behaviour. Its real job is the
# opposite of a credential: without it — a request that reached gunicorn directly,
# over the loopback binding the proxy overlay leaves in place for debugging — the
# offload is declined and the caller gets the bytes it expected instead of an
# empty 200.
OFFLOAD_PROBE_HEADER = "X-Epicurrents-Offload"

# Namespaced rather than a generic X-Serve-Path. The proxy matches on the mere
# presence of the path header across *every* response Django returns, so the name
# has to be one nothing else would plausibly emit: a project plugin proxying to
# some upstream that happens to set a generic header would otherwise have its
# response silently replaced by a file from disk.
SERVE_PATH_HEADER = "X-Epicurrents-Serve-Path"
SERVE_DISPOSITION_HEADER = "X-Epicurrents-Serve-Disposition"
SERVE_CONTENT_TYPE_HEADER = "X-Epicurrents-Serve-Content-Type"


def offload_file_response(
    request,
    file_path: Path,
    *,
    root: str | Path,
    namespace: str,
    filename: str,
    apply_middleware: bool,
    content_type: str = "application/octet-stream",
    as_attachment: bool = True,
) -> HttpResponse | None:
    """Return an empty 200 telling the proxy to serve *file_path*, or ``None``.

    ``None`` means "stream it yourself" and is the answer whenever anything at all
    is off: the capability is disabled, no proxy is in front, the grant is
    middleware-applied, or the file does not resolve to somewhere inside *root*.
    Callers treat ``None`` as "carry on" and never as an error.

    *root* is the filesystem directory the proxy mounts and *namespace* the path
    segment it mounts it under, so the emitted ``X-Serve-Path`` is
    ``/<namespace>/<path relative to root>`` — resolved by the proxy against its
    own document root, never against the site's URL space. It looks like a public
    path and is not one: nothing routes it, and the only consumer is the
    ``file_server`` inside the Caddyfile's ``handle_response`` block. Both sides of
    the mapping have to agree with ``caddy/Caddyfile``; a mismatch is a 404 on
    download, which is what ``test_offload.py`` pins.

    *apply_middleware* is the caller's resolved access-right flag. It is a required
    keyword rather than something inferred here so that every call site has to
    state it, and so a new byte-serving endpoint cannot acquire the offload by
    forgetting to think about it.
    """
    if not getattr(settings, "PROXY_FILE_OFFLOAD_ENABLED", False):
        return None

    # No proxy in front means nothing will interpret the header, and returning an
    # empty body would simply break the download.
    if request.headers.get(OFFLOAD_PROBE_HEADER) != "1":
        return None

    # The interlock. See the module docstring: with middleware applied there is no
    # file on disk holding the bytes this caller is allowed to see.
    if apply_middleware:
        return None

    try:
        resolved = Path(file_path).resolve(strict=True)
        resolved_root = Path(root).resolve(strict=True)
        relative = resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        # Outside the mounted root, a broken symlink, or a traversal attempt.
        # ``resolve`` collapses ``..`` and follows symlinks before the containment
        # check, so a path that escapes the root cannot survive relative_to.
        logger.warning(
            "Declining file offload for a path outside the offload root",
            extra={"namespace": namespace},
        )
        return None

    # Percent-encode before this becomes a URI in the proxy's rewrite. Stored
    # names are hex plus a validated extension, so in practice there is nothing
    # to escape — but this value is derived from a filename, and a raw "?" or "#"
    # would silently truncate the path and 404 a recording that downloads fine
    # without the proxy. Encoding the class out is cheaper than relying on the
    # upload validator staying correct forever. "/" stays safe: it separates the
    # path segments this builds.
    encoded = quote(relative.as_posix(), safe="/")

    response = HttpResponse(b"", status=200)
    response[SERVE_PATH_HEADER] = f"/{namespace}/{encoded}"
    response[SERVE_DISPOSITION_HEADER] = content_disposition_header(as_attachment=as_attachment, filename=filename)
    response[SERVE_CONTENT_TYPE_HEADER] = content_type
    return response
