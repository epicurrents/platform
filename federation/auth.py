"""Federation authentication utilities.

⚠️ LOAD-BEARING — federated auth surface.
Every check in this file is a defense layer against cross-instance
authentication bypass.  Silent weakening of any single one
— removing the ``alg`` check, the ``aud`` literal match, the ``iat``
bounds, the signature verification, the SSRF guard, the strict TLS
context, the ``is_trusted`` gate, or the ``jti`` replay cache — opens
a high-impact gap with no visible test failure on the affected code
path.  See AGENTS.md → *Load-bearing files* before modifying.

The contract tests are in ``federation/tests/test_auth.py`` and cover
the full surface: JWT verify failures (alg / sig / exp / aud / iat /
tampered), iat validation edge cases, replay detection, leeway
behaviour, fetch_peer_public_key (TLS context strictness, oversize
response cap, network errors), SSRF guard across all blocked IP
categories, local-key consistency, and parse_federation_auth issuer
normalisation.  Adding a new check here needs a matching contract
test; weakening an existing check should make a contract test fail.

Instance identity is established by an Ed25519 key pair.  The public key is
published at ``/.well-known/epicurrents-federation.json``; remote instances
fetch it to verify inbound JWT signatures.

Outbound requests carry ``Authorization: FederatedBearer <jwt>`` where the JWT
is signed with this instance's private key.  Remote instances verify it by
fetching and caching the public key from the well-known URL.

Key storage
-----------
Keys are stored as raw Ed25519 bytes encoded as URL-safe base64 (no padding) in
``FEDERATION_PUBLIC_KEY`` and ``FEDERATION_PRIVATE_KEY`` environment variables —
the same compact format used by VAPID keys.

JWT format
----------
Standard three-part JWT (header.payload.signature).  Algorithm: ``EdDSA``.

Payload claims:
    ``iss``  Issuing instance URL
    ``aud``  Intended recipient instance URL
    ``sub``  Remote user identifier (string PK on the remote instance)
    ``iat``  Issued-at timestamp (Unix seconds)
    ``exp``  Expiry timestamp (Unix seconds)
    ``jti``  Random nonce for replay detection (UUID4 hex)
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

if TYPE_CHECKING:
    from federation.models import FederatedPeer


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base64url helpers
# ---------------------------------------------------------------------------


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------------------
# Key generation and loading
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 key pair.

    Returns ``(public_b64, private_b64)`` as URL-safe base64 strings (43 chars
    each, no padding) suitable for storing in environment variables.
    """
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64_encode(public_raw), _b64_encode(private_raw)


def load_private_key(b64: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from its URL-safe base64 representation."""
    return Ed25519PrivateKey.from_private_bytes(_b64_decode(b64))


def load_public_key(b64: str) -> Ed25519PublicKey:
    """Load an Ed25519 public key from its URL-safe base64 representation."""
    return Ed25519PublicKey.from_public_bytes(_b64_decode(b64))


def get_local_private_key() -> Ed25519PrivateKey:
    """Load this instance's private key from settings.

    Raises ``ValueError`` if ``FEDERATION_PRIVATE_KEY`` is not configured.
    """
    from django.conf import settings

    b64 = getattr(settings, "FEDERATION_PRIVATE_KEY", "").strip()
    if not b64:
        raise ValueError("FEDERATION_PRIVATE_KEY is not configured")
    return load_private_key(b64)


def get_local_public_key() -> Ed25519PublicKey:
    """Load this instance's public key from settings."""
    from django.conf import settings

    b64 = getattr(settings, "FEDERATION_PUBLIC_KEY", "").strip()
    if not b64:
        raise ValueError("FEDERATION_PUBLIC_KEY is not configured")
    return load_public_key(b64)


def get_local_instance_url() -> str:
    """Return the configured instance URL, or raise if not set."""
    from django.conf import settings

    url = getattr(settings, "FEDERATION_INSTANCE_URL", "").strip().rstrip("/")
    if not url:
        raise ValueError("FEDERATION_INSTANCE_URL is not configured")
    return url


def is_federation_configured() -> bool:
    """Return True if all three federation settings are non-empty."""
    from django.conf import settings

    return bool(
        getattr(settings, "FEDERATION_INSTANCE_URL", "").strip()
        and getattr(settings, "FEDERATION_PUBLIC_KEY", "").strip()
        and getattr(settings, "FEDERATION_PRIVATE_KEY", "").strip()
    )


def assert_local_keys_consistent() -> None:
    """Verify that ``FEDERATION_PUBLIC_KEY`` matches ``FEDERATION_PRIVATE_KEY``.

    Half-completed rotations — env file edited but service not restarted, or
    vice versa — silently leave the instance publishing a public key that does
    not match its outbound signatures.  Every peer then rejects this instance's
    JWTs with no obvious symptom on the local side.  Running this at startup
    catches the mismatch at the deploy that introduced it instead of at the
    next peer interaction.

    Silently returns when federation is not configured (the typical
    non-federated deployment).

    Raises:
        django.core.exceptions.ImproperlyConfigured: keys are configured but
        the public key does not match the one derived from the private key,
        or either key is malformed.
    """
    from django.core.exceptions import ImproperlyConfigured

    if not is_federation_configured():
        return

    try:
        priv = get_local_private_key()
        pub = get_local_public_key()
    except ValueError as exc:
        raise ImproperlyConfigured(f"Federation key configuration invalid: {exc}") from exc

    derived = priv.public_key().public_bytes_raw()
    configured = pub.public_bytes_raw()
    if derived != configured:
        raise ImproperlyConfigured(
            "FEDERATION_PUBLIC_KEY does not match the public key derived from "
            "FEDERATION_PRIVATE_KEY. This usually means a key rotation completed "
            "partially (env file edited, service not restarted, or vice versa). "
            "Verify both env vars and restart."
        )

    # If a rotation overlap is announced, the NEXT pair must also be consistent.
    # Both halves are required together: NEXT public without NEXT private (or
    # vice versa) leaves the operator unable to promote.
    from django.conf import settings as _s

    next_pub_b64 = getattr(_s, "FEDERATION_PUBLIC_KEY_NEXT", "").strip()
    next_priv_b64 = getattr(_s, "FEDERATION_PRIVATE_KEY_NEXT", "").strip()
    if next_pub_b64 or next_priv_b64:
        if not (next_pub_b64 and next_priv_b64):
            raise ImproperlyConfigured(
                "FEDERATION_PUBLIC_KEY_NEXT and FEDERATION_PRIVATE_KEY_NEXT must "
                "be set together (rotation overlap) or both unset.  One is "
                "currently empty."
            )
        try:
            next_priv = load_private_key(next_priv_b64)
            next_pub = load_public_key(next_pub_b64)
        except ValueError as exc:
            raise ImproperlyConfigured(f"Federation NEXT key configuration invalid: {exc}") from exc
        if next_priv.public_key().public_bytes_raw() != next_pub.public_bytes_raw():
            raise ImproperlyConfigured(
                "FEDERATION_PUBLIC_KEY_NEXT does not match the public key derived "
                "from FEDERATION_PRIVATE_KEY_NEXT.  Re-run 'rotate_federation_keys "
                "--announce' to regenerate a consistent NEXT pair."
            )


# ---------------------------------------------------------------------------
# JWT creation and verification
# ---------------------------------------------------------------------------


def create_jwt(
    private_key: Ed25519PrivateKey,
    *,
    issuer: str,
    audience: str,
    subject: str,
    ttl: int = 60,
    jti: str | None = None,
) -> str:
    """Create a signed federation JWT.

    A random ``jti`` (UUID4 hex) is generated for each token unless caller
    supplies one — the receiving instance uses ``jti`` to detect replays
    within the validity window (see :func:`parse_federation_auth`).

    Args:
        private_key: Ed25519 signing key.
        issuer: Canonical URL of the issuing instance (``iss`` claim).
        audience: Canonical URL of the target instance (``aud`` claim).
        subject: Remote user identifier on the issuing instance (``sub`` claim).
        ttl: Token lifetime in seconds (default 60).
        jti: Optional explicit token id.  Tests use this to forge collisions;
            production callers should let the default UUID4 stand.

    Returns:
        Compact serialised JWT string.
    """
    header = _b64_encode(json.dumps({"alg": "EdDSA", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64_encode(
        json.dumps(
            {
                "iss": issuer,
                "aud": audience,
                "sub": subject,
                "iat": now,
                "exp": now + ttl,
                "jti": jti if jti is not None else uuid.uuid4().hex,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = _b64_encode(private_key.sign(signing_input))
    return f"{header}.{payload}.{sig}"


# Clock-skew tolerance for the ``exp`` and ``iat`` checks.  Federated peers
# run on independent machines; sub-second to multi-second skew between them is
# normal even with NTP.  Without leeway, a brand-new token from a peer whose
# clock is one second ahead is rejected as already expired.  30 seconds is
# conservative enough that legitimate skew passes while still bounding replay
# value below the typical 60-second JWT TTL.
DEFAULT_JWT_LEEWAY = 30

# Maximum acceptable age of an inbound token, from the verifier's perspective.
# The ``exp`` check alone bounds replay to whatever TTL the issuer chose, which
# could be larger than this instance is willing to accept.  Capping ``iat`` age
# at the verifier side enforces a deployment-wide ceiling on token validity
# regardless of what the issuer claims.
DEFAULT_MAX_JWT_AGE = 60


def verify_jwt(
    token: str,
    public_key: Ed25519PublicKey,
    *,
    audience: str,
    leeway: int = DEFAULT_JWT_LEEWAY,
    max_age: int = DEFAULT_MAX_JWT_AGE,
) -> dict:
    """Verify a federation JWT and return its payload.

    Validates: signature, ``alg``, ``exp`` (with leeway), ``iat`` (must be
    present, not in the future modulo leeway, not older than ``max_age + leeway``),
    and ``aud``.  Replay detection via ``jti`` is *not* done here — it is
    stateful and lives in :func:`parse_federation_auth`.

    Args:
        token: Compact serialised JWT string.
        public_key: Ed25519 key of the claimed issuer.
        audience: Expected ``aud`` claim value (this instance's URL).
        leeway: Seconds of tolerance applied to the ``exp`` and ``iat``
            time-bound checks, to absorb clock skew between peers.  Defaults
            to ``DEFAULT_JWT_LEEWAY`` (30 s).  Pass ``leeway=0`` for strict
            comparison (testing only — in production any non-zero skew between
            peers will cause sporadic "expired" failures on freshly-issued
            tokens).
        max_age: Maximum acceptable age of the token relative to its ``iat``
            claim.  Defaults to ``DEFAULT_MAX_JWT_AGE`` (60 s) — a token whose
            ``iat`` is older than this is rejected even if its ``exp`` claims
            it should still be valid, capping the verifier's exposure to
            issuers that pick over-generous TTLs.

    Returns:
        Decoded payload dict.

    Raises:
        ``ValueError`` on any failure (malformed token, bad signature,
        expired token, ``iat`` missing or out of bounds, audience mismatch).
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT: expected three dot-separated parts")

    header_enc, payload_enc, sig_enc = parts
    signing_input = f"{header_enc}.{payload_enc}".encode()

    try:
        header = json.loads(_b64_decode(header_enc))
    except Exception:
        raise ValueError("JWT header is not valid JSON")

    if header.get("alg") != "EdDSA":
        # Defensive: Ed25519 verify will fail on a forged signature anyway, but
        # explicit alg rejection blocks ``alg: "none"`` style attacks and gives
        # operators a clearer diagnostic than "Invalid JWT signature".
        raise ValueError(f"JWT alg must be 'EdDSA', got {header.get('alg')!r}")

    try:
        public_key.verify(_b64_decode(sig_enc), signing_input)
    except InvalidSignature:
        raise ValueError("Invalid JWT signature")

    try:
        payload = json.loads(_b64_decode(payload_enc))
    except Exception:
        raise ValueError("JWT payload is not valid JSON")

    now = int(time.time())

    # Token is expired iff ``now`` is past ``exp + leeway`` — i.e. the leeway
    # extends the validity window into the future from the issuer's claimed exp.
    if payload.get("exp", 0) + leeway < now:
        raise ValueError("JWT has expired")

    # ``iat`` is mandatory: ``create_jwt`` has always emitted it, and the
    # ``iat``-age check is the second axis of replay defense.  A token whose
    # ``iat`` claims to be in the future (modulo leeway) suggests a misconfigured
    # peer clock; one whose ``iat`` is older than ``max_age`` is a replay or an
    # over-generous issuer TTL — either way, reject.
    iat = payload.get("iat")
    if iat is None:
        raise ValueError("JWT missing 'iat' claim")
    try:
        iat = int(iat)
    except (TypeError, ValueError):
        raise ValueError("JWT 'iat' claim is not a valid timestamp")
    if iat - leeway > now:
        raise ValueError("JWT 'iat' is in the future")
    if iat + max_age + leeway < now:
        raise ValueError("JWT 'iat' is too old")

    if payload.get("aud") != audience:
        raise ValueError(f"JWT audience mismatch: expected '{audience}', got '{payload.get('aud')}'")

    return payload


# ---------------------------------------------------------------------------
# Remote key fetching
# ---------------------------------------------------------------------------

WELL_KNOWN_PATH = "/.well-known/epicurrents-federation.json"

# A legitimate well-known response is ~100 bytes (a 43-char base64 key wrapped
# in JSON). 64 KiB is generous headroom while bounding a hostile or compromised
# peer that might stream gigabytes back.
MAX_WELL_KNOWN_RESPONSE_SIZE = 64 * 1024

# NAT64 translation prefixes — the well-known prefix (RFC 6052) and the
# local-use prefix (RFC 8215). Addresses inside them embed an IPv4 target
# that a NAT64 gateway connects to, which can be RFC 1918 space.
_NAT64_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


#: Prefix lengths that would reopen the guard wholesale rather than carve a hole in it.
#: Listing a default route is how "allow the one network I federate over" turns into
#: "allow anything" by accident, so it is refused with a pointer at the blunt override
#: that says the same thing out loud.
_DEFAULT_ROUTES = (ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0"))


def _allowed_peer_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse ``FEDERATION_ALLOWED_PEER_CIDRS`` into networks the guard will admit.

    Re-parsed on every call rather than cached. Peer checks are rare — registering a
    peer, refreshing a key — and a module-level cache would outlive a settings change
    in the same process, which is the opposite of what an operator narrowing the list
    expects.

    Raises:
        ImproperlyConfigured: an entry is not a CIDR, carries host bits, or is a
            default route.
    """
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    networks = []
    for entry in getattr(settings, "FEDERATION_ALLOWED_PEER_CIDRS", ()) or ():
        entry = entry.strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry)
        except ValueError as exc:
            raise ImproperlyConfigured(
                f"FEDERATION_ALLOWED_PEER_CIDRS entry {entry!r} is not a CIDR: {exc}. "
                "Give the network address rather than a host inside it (100.64.0.0/10, not 100.64.0.1/10)."
            ) from exc
        if network in _DEFAULT_ROUTES:
            raise ImproperlyConfigured(
                f"FEDERATION_ALLOWED_PEER_CIDRS entry {entry!r} is a default route, which disables the "
                "SSRF guard entirely. List the network you federate over, or set "
                "FEDERATION_ALLOW_PRIVATE_PEER_URLS=True if that is really what you mean."
            )
        networks.append(network)
    return tuple(networks)


def _check_url_is_safe(url: str) -> None:
    """Reject URLs that resolve to non-globally-routable addresses.

    Defends against SSRF: a compromised superuser could register a peer URL
    pointing at internal services (RDS, the cloud metadata endpoint at
    ``169.254.169.254``, localhost) and use ``fetch_peer_public_key`` to
    probe them.  Resolving the hostname and rejecting any non-global IP
    blocks the obvious cases.

    Does **not** defend against DNS rebinding (TOCTOU between resolution
    here and the connection inside ``urllib.request.urlopen``).  That
    requires pinning the resolved IP for the urllib call, which is non-
    trivial and out of scope for the initial guard.

    Override via ``FEDERATION_ALLOW_PRIVATE_PEER_URLS = True`` for dev
    environments that legitimately federate against localhost — never in
    production.

    Raises:
        ValueError: hostname missing, DNS lookup failed, or any resolved
            address is not globally routable.
    """
    from django.conf import settings

    if getattr(settings, "FEDERATION_ALLOW_PRIVATE_PEER_URLS", False):
        return

    host = urllib.parse.urlparse(url).hostname
    if not host:
        raise ValueError(f"URL has no hostname: {url}")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"DNS lookup failed for {host}: {exc}") from exc

    # Parsed before the loop so a malformed setting fails on its own terms rather than
    # once per resolved address, and so the error names the setting rather than the URL.
    allowed_networks = _allowed_peer_networks()

    for info in infos:
        addr = info[4][0]
        # Strip IPv6 zone identifier if present (e.g. "fe80::1%eth0").
        addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue  # Skip unparseable entries; getaddrinfo returns dicts on some platforms
        # ``is_global`` is True only for globally-routable IPs.  Excludes
        # private (RFC 1918 / RFC 4193), loopback, link-local, multicast,
        # reserved, and unspecified.
        if not ip.is_global and not any(ip in network for network in allowed_networks):
            raise ValueError(
                f"URL {url} resolves to non-public address {addr} — refusing to fetch (SSRF guard). "
                "Add its network to FEDERATION_ALLOWED_PEER_CIDRS if this peer is reachable only over a "
                "private network this deployment is on."
            )
        # NAT64 prefixes report ``is_global=True`` in the stdlib, but a
        # local NAT64 gateway translates them onto arbitrary IPv4 targets
        # — including RFC 1918 space — so they re-open the hole the
        # is_global check closes. No legitimate peer URL resolves here.
        # Checked after the allowlist and never exempted by it: a listed CIDR
        # says "this network is mine", which a translation prefix never is.
        if any(ip in prefix for prefix in _NAT64_PREFIXES):
            raise ValueError(f"URL {url} resolves to NAT64-translated address {addr} — refusing to fetch (SSRF guard).")


def check_url_is_safe(url: str) -> None:
    """SSRF guard for any outbound URL the platform stores or fetches.

    Public entry point for callers outside the federation app (e.g. push
    subscription endpoints in ``notifications``) so the resolve-and-reject
    logic lives in one place. Delegates to ``_check_url_is_safe``; the
    ``FEDERATION_ALLOW_PRIVATE_PEER_URLS`` development override applies
    here too, which lets dev environments target localhost services.

    ``FEDERATION_ALLOWED_PEER_CIDRS`` applies here as well, despite naming
    peers: the guard is shared, so a deployment that carves out its overlay
    network for federation carves it out for every caller of this function.
    Worth knowing before adding one — the setting reads narrower than it is.

    Raises:
        ValueError: hostname missing, DNS lookup failed, or any resolved
            address is not globally routable.
    """
    _check_url_is_safe(url)


def _build_tls_context() -> ssl.SSLContext:
    """Build the SSL context used for outbound federation HTTP requests.

    Pins the security posture explicitly so it cannot silently regress: a
    future refactor that bypasses this helper (e.g. ``ssl._create_unverified_context()``)
    will diverge from the asserted invariant in ``test_tls_context_is_strict``.

    Modern Python's ``ssl.create_default_context()`` already returns a context
    with these settings; making them explicit is the point.
    """
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def fetch_peer_public_key(instance_url: str, timeout: int = 10) -> tuple[str, str]:
    """Fetch the peer's current and (optional) next public key.

    Contacts ``{instance_url}/.well-known/epicurrents-federation.json`` and
    returns ``(current, next)`` — ``next`` is an empty string when the peer is
    not announcing a rotation overlap.

    Both keys are validated as parseable Ed25519 keys before return; an
    advertised but malformed ``federation_public_key_next`` is treated as
    fatal, not silently dropped, so peer admins notice the typo.

    Raises:
        ``ValueError`` if the endpoint is unreachable, the response is
        malformed, exceeds ``MAX_WELL_KNOWN_RESPONSE_SIZE``, or either key
        cannot be parsed.
    """
    url = instance_url.rstrip("/") + WELL_KNOWN_PATH
    # SSRF guard runs before the request goes out — see ``_check_url_is_safe``
    # for the threat model.  Raises ValueError on a non-public target; that
    # propagates to the existing except-block below.
    _check_url_is_safe(url)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "epicurrents-federation/1",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_build_tls_context()) as resp:
            # Read one byte past the cap so we can detect oversize responses.
            raw = resp.read(MAX_WELL_KNOWN_RESPONSE_SIZE + 1)
        if len(raw) > MAX_WELL_KNOWN_RESPONSE_SIZE:
            raise ValueError(f"Federation endpoint at {url} returned more than {MAX_WELL_KNOWN_RESPONSE_SIZE} bytes")
        data = json.loads(raw)
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach federation endpoint at {url}: {exc}") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to fetch federation key from {url}: {exc}") from exc

    key_b64 = data.get("federation_public_key", "")
    if not key_b64:
        raise ValueError(f"No 'federation_public_key' in response from {url}")

    try:
        load_public_key(key_b64)  # validate it parses
    except Exception as exc:
        raise ValueError(f"Invalid public key returned from {url}: {exc}") from exc

    # Optional rotation-overlap field.  Present iff the peer has set
    # FEDERATION_PUBLIC_KEY_NEXT on its side.
    next_key_b64 = data.get("federation_public_key_next", "")
    if next_key_b64:
        try:
            load_public_key(next_key_b64)
        except Exception as exc:
            raise ValueError(f"Invalid 'federation_public_key_next' returned from {url}: {exc}") from exc

    return key_b64, next_key_b64


# ---------------------------------------------------------------------------
# Inbound request authentication
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FederationAuthResult:
    """Outcome of validating an inbound ``FederatedBearer`` JWT.

    On success, ``peer`` and ``remote_user_id`` are populated and ``error`` is
    ``None``.  On failure, ``error`` carries ``(status_code, message)`` for HTTP
    wrappers that raise; ``peer`` is ``None`` and ``remote_user_id`` is empty.
    """

    peer: FederatedPeer | None
    remote_user_id: str
    error: tuple[int, str] | None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_federation_auth(request) -> FederationAuthResult:
    """Validate an inbound ``FederatedBearer`` JWT and return the outcome.

    Single source of truth for federation request authentication.  Wrappers in
    the API layers convert the result into either an HTTP error (raising) or a
    ``None`` sentinel (non-destructive).  The ``iss`` claim is normalised with
    ``.strip().rstrip("/")`` so peers whose ``FEDERATION_INSTANCE_URL`` has a
    trailing slash resolve to the same DB row on every code path.
    """
    # Lazy import: federation.models requires Django apps to be loaded, but
    # federation.auth is also imported by management commands during settings
    # discovery.
    from epicurrents.security_log import get_client_ip, log_security_event
    from federation.models import FederatedPeer

    def fail(code: int, msg: str, peer: FederatedPeer | None = None) -> FederationAuthResult:
        log_security_event(
            "federation.auth_failed",
            ip=get_client_ip(request),
            code=code,
            reason=msg,
            peer_url=peer.url if peer is not None else None,
            peer_id=peer.pk if peer is not None else None,
        )
        return FederationAuthResult(peer=None, remote_user_id="", error=(code, msg))

    if not is_federation_configured():
        return fail(403, "Federation is not enabled on this instance")

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("FederatedBearer "):
        return fail(401, "FederatedBearer token required")

    token = auth_header[len("FederatedBearer ") :]

    parts = token.split(".")
    if len(parts) != 3:
        return fail(401, "Malformed federation token")

    try:
        payload_json = _b64_decode(parts[1])
        claims = json.loads(payload_json)
        issuer = claims.get("iss", "").strip().rstrip("/")
    except Exception:
        return fail(401, "Malformed federation token")

    if not issuer:
        return fail(401, "Federation token missing 'iss' claim")

    peer = FederatedPeer.objects.filter(url=issuer).first()
    if peer is None or not peer.is_trusted:
        return fail(401, "Unknown or untrusted federation peer", peer=peer)

    # During a rotation overlap, the peer may sign tokens with either of two
    # keys.  Try the current key first; if signature verification fails AND
    # the peer has advertised a ``public_key_next``, retry with that.  Errors
    # that are not signature-related (expired, wrong audience, malformed) are
    # not retried — those are not rotation symptoms.
    try:
        local_url = get_local_instance_url()
        public_key = load_public_key(peer.public_key)
        try:
            payload = verify_jwt(token, public_key, audience=local_url)
        except ValueError as exc:
            if "signature" not in str(exc).lower() or not peer.public_key_next:
                raise
            next_key = load_public_key(peer.public_key_next)
            payload = verify_jwt(token, next_key, audience=local_url)
    except ValueError as exc:
        return fail(401, str(exc), peer=peer)

    # Replay detection.  ``verify_jwt`` has already bounded the token's age
    # via ``iat`` + ``exp``; the ``jti`` nonce cache catches replays *within*
    # that window, which the time checks alone cannot.  Peers that don't yet
    # emit ``jti`` still authenticate, but with a WARNING so the
    # backwards-compat window is visible — see follow-up roadmap item to
    # tighten this once all peers have migrated.
    jti = payload.get("jti", "")
    if jti:
        if not _check_and_remember_jti(jti):
            return fail(401, "JWT replay detected", peer=peer)
    else:
        logger.warning(
            "Federation JWT missing 'jti' claim — replay protection skipped "
            "for peer=%s (id=%d).  Upgrade the peer to a version that emits "
            "jti.",
            peer.url,
            peer.pk,
        )

    return FederationAuthResult(
        peer=peer,
        remote_user_id=payload.get("sub", ""),
        error=None,
    )


def try_federation_auth(request) -> tuple[FederatedPeer, str] | None:
    """Non-raising wrapper around :func:`parse_federation_auth`.

    Returns ``(peer, remote_user_id)`` on success or ``None`` on any failure —
    suitable for endpoints that accept either session-authenticated users or
    federated peers and need to silently fall through when the request is not
    a federation request.

    Short-circuits when the request carries no ``FederatedBearer`` header:
    ``parse_federation_auth`` would otherwise emit ``federation.auth_failed``
    on every anonymous request to a dual-auth endpoint (e.g. the recordings
    list polled by HomeView), turning routine "you need to log in" 401s
    into a stream of security events on instances that don't run
    federation at all. Endpoints that *require* federation auth go
    through ``parse_federation_auth`` directly and keep the full
    audit-event surface.
    """
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("FederatedBearer "):
        return None
    result = parse_federation_auth(request)
    return (result.peer, result.remote_user_id) if result.ok else None


# Key prefix for the nonce cache.  Bounded namespace so a future operator
# inspecting the Redis keyspace can grep for federation entries.
JTI_CACHE_PREFIX = "fed-jti:"


def _check_and_remember_jti(jti: str) -> bool:
    """Atomically mark ``jti`` as seen.  Returns False if it was already seen.

    Uses Django's cache, which is ``LocMemCache`` in dev/test and ``RedisCache``
    in production (per ``epicurrents/settings/production.py``).  ``cache.add``
    is atomic across processes when backed by Redis — important because
    gunicorn runs multiple workers and a single-process LocMemCache would only
    protect within one worker.

    The TTL is the maximum acceptable token age plus leeway: once the time
    checks in ``verify_jwt`` would reject the token anyway, the nonce can be
    forgotten without weakening replay protection.
    """
    from django.core.cache import cache

    key = f"{JTI_CACHE_PREFIX}{jti}"
    return bool(cache.add(key, 1, timeout=DEFAULT_MAX_JWT_AGE + DEFAULT_JWT_LEEWAY))
