"""Tests for federation.auth — JWT sign/verify, key generation, peer key fetch."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from federation.auth import (
    _b64_decode,
    _b64_encode,
    _build_tls_context,
    _check_url_is_safe,
    assert_local_keys_consistent,
    create_jwt,
    fetch_peer_public_key,
    generate_keypair,
    load_private_key,
    load_public_key,
    parse_federation_auth,
    try_federation_auth,
    verify_jwt,
)

# ---------------------------------------------------------------------------
# Key generation and loading
# ---------------------------------------------------------------------------


class TestKeypairGeneration:
    def test_generate_keypair_returns_two_strings(self):
        pub, priv = generate_keypair()
        assert isinstance(pub, str)
        assert isinstance(priv, str)

    def test_keys_are_43_chars(self):
        pub, priv = generate_keypair()
        # Raw Ed25519 = 32 bytes; base64url without padding = ceil(32*4/3) = 43 chars
        assert len(pub) == 43
        assert len(priv) == 43

    def test_keys_are_url_safe_base64(self):
        pub, priv = generate_keypair()
        for char in pub + priv:
            assert char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    def test_loaded_key_roundtrips(self):
        pub_b64, priv_b64 = generate_keypair()
        priv = load_private_key(priv_b64)
        pub = load_public_key(pub_b64)
        # Verify that the public key derived from the private key matches the loaded one.
        derived_pub_bytes = priv.public_key().public_bytes_raw()
        loaded_pub_bytes = pub.public_bytes_raw()
        assert derived_pub_bytes == loaded_pub_bytes

    def test_each_call_produces_unique_pair(self):
        pub1, _ = generate_keypair()
        pub2, _ = generate_keypair()
        assert pub1 != pub2


# ---------------------------------------------------------------------------
# JWT create + verify
# ---------------------------------------------------------------------------


class TestJwtRoundtrip:
    def setup_method(self):
        pub_b64, priv_b64 = generate_keypair()
        self.priv = load_private_key(priv_b64)
        self.pub = load_public_key(pub_b64)

    def test_create_and_verify(self):
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="user-42",
        )
        payload = verify_jwt(token, self.pub, audience="https://b.example.com")
        assert payload["iss"] == "https://a.example.com"
        assert payload["aud"] == "https://b.example.com"
        assert payload["sub"] == "user-42"

    def test_token_has_three_parts(self):
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
        )
        assert len(token.split(".")) == 3

    def test_iat_and_exp_are_present(self):
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
            ttl=120,
        )
        payload = verify_jwt(token, self.pub, audience="https://b.example.com")
        now = int(time.time())
        assert abs(payload["iat"] - now) < 5
        assert abs(payload["exp"] - (now + 120)) < 5


class TestJwtVerifyFailures:
    def setup_method(self):
        pub_b64, priv_b64 = generate_keypair()
        self.priv = load_private_key(priv_b64)
        self.pub = load_public_key(pub_b64)
        _, other_priv_b64 = generate_keypair()
        self.other_priv = load_private_key(other_priv_b64)

    def _make_token(self, **kwargs):
        defaults = {
            "issuer": "https://a.example.com",
            "audience": "https://b.example.com",
            "subject": "u",
        }
        defaults.update(kwargs)
        return create_jwt(self.priv, **defaults)

    def test_wrong_signature_raises(self):
        # Token signed with other_priv, verified with pub (pair mismatch).
        token = create_jwt(
            self.other_priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
        )
        with pytest.raises(ValueError, match="Invalid JWT signature"):
            verify_jwt(token, self.pub, audience="https://b.example.com")

    def test_wrong_audience_raises(self):
        token = self._make_token()
        with pytest.raises(ValueError, match="audience mismatch"):
            verify_jwt(token, self.pub, audience="https://wrong.example.com")

    def test_expired_token_raises(self):
        """A token whose exp has passed must be rejected.

        Uses ``leeway=0`` so the assertion is about strict expiry, not about
        whether the default leeway happens to cover this case — that
        behaviour is exercised separately in ``TestJwtLeeway``.
        """
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
            ttl=-1,  # already expired
        )
        with pytest.raises(ValueError, match="expired"):
            verify_jwt(token, self.pub, audience="https://b.example.com", leeway=0)

    def test_malformed_token_raises(self):
        with pytest.raises(ValueError, match="Malformed JWT"):
            verify_jwt("not.a.valid.jwt.here", self.pub, audience="https://b.example.com")

    def test_tampered_payload_raises(self):
        token = self._make_token()
        header, _, sig = token.split(".")
        # Build a different payload.
        evil_payload = _b64_encode(
            json.dumps(
                {
                    "iss": "evil",
                    "aud": "https://b.example.com",
                    "sub": "hacker",
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 60,
                }
            ).encode()
        )
        with pytest.raises(ValueError, match="Invalid JWT signature"):
            verify_jwt(
                f"{header}.{evil_payload}.{sig}",
                self.pub,
                audience="https://b.example.com",
            )

    def test_wrong_alg_header_rejected(self):
        """Explicit ``alg`` rejection blocks ``alg: "none"`` style attempts."""
        _, payload, sig = self._make_token().split(".")
        forged_header = _b64_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        with pytest.raises(ValueError, match="JWT alg must be 'EdDSA'"):
            verify_jwt(
                f"{forged_header}.{payload}.{sig}",
                self.pub,
                audience="https://b.example.com",
            )

    def test_missing_alg_header_rejected(self):
        _, payload, sig = self._make_token().split(".")
        header_no_alg = _b64_encode(json.dumps({"typ": "JWT"}).encode())
        with pytest.raises(ValueError, match="JWT alg must be 'EdDSA'"):
            verify_jwt(
                f"{header_no_alg}.{payload}.{sig}",
                self.pub,
                audience="https://b.example.com",
            )


class TestJtiClaim:
    """``create_jwt`` emits a unique ``jti`` per token.

    The receiving instance uses ``jti`` to detect replays — see
    ``TestReplayDetection`` for the end-to-end check.
    """

    def setup_method(self):
        _, priv_b64 = generate_keypair()
        self.priv = load_private_key(priv_b64)

    def _payload(self, token):
        _, payload_enc, _ = token.split(".")
        return json.loads(_b64_decode(payload_enc))

    def test_jti_present_by_default(self):
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
        )
        assert "jti" in self._payload(token)

    def test_jti_unique_per_call(self):
        jti_a = self._payload(
            create_jwt(
                self.priv,
                issuer="https://a.example.com",
                audience="https://b.example.com",
                subject="u",
            )
        )["jti"]
        jti_b = self._payload(
            create_jwt(
                self.priv,
                issuer="https://a.example.com",
                audience="https://b.example.com",
                subject="u",
            )
        )["jti"]
        assert jti_a != jti_b

    def test_caller_can_override_jti(self):
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
            jti="forced-jti",
        )
        assert self._payload(token)["jti"] == "forced-jti"


class TestIatValidation:
    """``verify_jwt`` enforces ``iat`` freshness.

    ``iat`` is the second axis of replay defense: ``exp`` bounds the validity
    window the issuer claims, ``iat`` bounds the window the verifier accepts.
    """

    def setup_method(self):
        pub_b64, priv_b64 = generate_keypair()
        self.priv = load_private_key(priv_b64)
        self.pub = load_public_key(pub_b64)

    def _forge_token(self, **overrides):
        """Build a signed token with arbitrary payload overrides."""
        header = _b64_encode(json.dumps({"alg": "EdDSA", "typ": "JWT"}, separators=(",", ":")).encode())
        now = int(time.time())
        payload_dict = {
            "iss": "https://a.example.com",
            "aud": "https://b.example.com",
            "sub": "u",
            "iat": now,
            "exp": now + 60,
        }
        payload_dict.update(overrides)
        payload = _b64_encode(json.dumps(payload_dict, separators=(",", ":")).encode())
        signing_input = f"{header}.{payload}".encode()
        sig = _b64_encode(self.priv.sign(signing_input))
        return f"{header}.{payload}.{sig}"

    def test_token_missing_iat_rejected(self):
        # Build a default token, then drop iat from the payload and re-sign.
        token = self._forge_token()
        header, payload_enc, _ = token.split(".")
        payload = json.loads(_b64_decode(payload_enc))
        del payload["iat"]
        payload_enc2 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{header}.{payload_enc2}".encode()
        sig2 = _b64_encode(self.priv.sign(signing_input))
        with pytest.raises(ValueError, match="missing 'iat'"):
            verify_jwt(
                f"{header}.{payload_enc2}.{sig2}",
                self.pub,
                audience="https://b.example.com",
            )

    def test_token_iat_in_future_rejected(self):
        now = int(time.time())
        # iat far enough in the future to exceed the default leeway.
        token = self._forge_token(iat=now + 120, exp=now + 180)
        with pytest.raises(ValueError, match="'iat' is in the future"):
            verify_jwt(token, self.pub, audience="https://b.example.com")

    def test_token_iat_within_leeway_future_accepted(self):
        """Small future skew on iat is absorbed by leeway, matching the exp check."""
        now = int(time.time())
        token = self._forge_token(iat=now + 5, exp=now + 65)
        payload = verify_jwt(token, self.pub, audience="https://b.example.com")
        assert payload["sub"] == "u"

    def test_token_iat_too_old_rejected(self):
        """iat older than max_age + leeway is rejected even if exp claims validity."""
        now = int(time.time())
        # Issuer set a 600 s TTL; verifier caps at max_age=60.  The token's
        # ``exp`` would be valid for another ~480 s, but the iat-age check
        # rejects it.
        token = self._forge_token(iat=now - 120, exp=now + 480)
        with pytest.raises(ValueError, match="'iat' is too old"):
            verify_jwt(token, self.pub, audience="https://b.example.com")

    def test_token_iat_non_integer_rejected(self):
        token = self._forge_token(iat="not-a-number")
        with pytest.raises(ValueError, match="'iat'.*not a valid timestamp"):
            verify_jwt(token, self.pub, audience="https://b.example.com")


@pytest.mark.django_db
class TestReplayDetection:
    """End-to-end replay protection through ``parse_federation_auth``.

    The nonce check is stateful (Django cache), so it lives outside the pure
    ``verify_jwt`` crypto function.  These tests exercise the
    ``parse_federation_auth`` call path so the cache wiring is part of what's
    asserted, not mocked away.
    """

    def _setup_local(self, settings):
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

    def _make_peer_and_token(self, jti=None):
        from federation.models import FederatedPeer

        peer_pub, peer_priv = generate_keypair()
        peer = FederatedPeer.objects.create(
            url="https://peer.example.com",
            public_key=peer_pub,
            is_trusted=True,
        )
        token = create_jwt(
            load_private_key(peer_priv),
            issuer="https://peer.example.com",
            audience="https://local.example.com",
            subject="user-1",
            jti=jti,
        )
        return peer, token

    def _request(self, token):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.META = {"HTTP_AUTHORIZATION": f"FederatedBearer {token}"}
        return req

    def test_first_use_succeeds_second_rejected(self, settings):
        self._setup_local(settings)
        _, token = self._make_peer_and_token()
        # First call: token is fresh.
        result_a = parse_federation_auth(self._request(token))
        assert result_a.ok
        # Second call with the same token: replay.
        result_b = parse_federation_auth(self._request(token))
        assert not result_b.ok
        assert result_b.error[0] == 401
        assert "replay" in result_b.error[1].lower()

    def test_distinct_tokens_both_succeed(self, settings):
        self._setup_local(settings)
        # Reuse the same peer for both tokens — only jti differs.
        from federation.models import FederatedPeer

        peer_pub, peer_priv = generate_keypair()
        FederatedPeer.objects.create(
            url="https://peer.example.com",
            public_key=peer_pub,
            is_trusted=True,
        )
        priv = load_private_key(peer_priv)
        token_a = create_jwt(
            priv,
            issuer="https://peer.example.com",
            audience="https://local.example.com",
            subject="user-1",
        )
        token_b = create_jwt(
            priv,
            issuer="https://peer.example.com",
            audience="https://local.example.com",
            subject="user-1",
        )
        assert parse_federation_auth(self._request(token_a)).ok
        assert parse_federation_auth(self._request(token_b)).ok

    def test_token_without_jti_authenticates_with_warning(self, settings, caplog):
        """Backwards-compat: peers that don't yet emit jti still work.

        Logs a WARNING so the migration window is visible in operations.  A
        follow-up commit will tighten this to required once peers have
        migrated — tracked in ROADMAP.
        """
        self._setup_local(settings)
        # jti="" empties the claim — represents a peer that doesn't emit it.
        # ``create_jwt`` won't oblige (it always generates one), so we forge
        # by hand.
        from federation.models import FederatedPeer

        peer_pub, peer_priv = generate_keypair()
        FederatedPeer.objects.create(
            url="https://peer.example.com",
            public_key=peer_pub,
            is_trusted=True,
        )
        priv = load_private_key(peer_priv)
        # Build a token whose payload omits jti entirely.
        header = _b64_encode(json.dumps({"alg": "EdDSA", "typ": "JWT"}, separators=(",", ":")).encode())
        now = int(time.time())
        payload = _b64_encode(
            json.dumps(
                {
                    "iss": "https://peer.example.com",
                    "aud": "https://local.example.com",
                    "sub": "user-1",
                    "iat": now,
                    "exp": now + 60,
                },
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"{header}.{payload}".encode()
        sig = _b64_encode(priv.sign(signing_input))
        token = f"{header}.{payload}.{sig}"

        with caplog.at_level("WARNING", logger="federation.auth"):
            result = parse_federation_auth(self._request(token))
        assert result.ok
        matching = [r for r in caplog.records if "missing 'jti'" in r.getMessage()]
        assert len(matching) == 1


class TestJwtLeeway:
    """``verify_jwt`` tolerates ``leeway`` seconds of clock skew on ``exp``.

    Federated peers run on independent machines; sub-second to multi-second
    skew between them is normal even with NTP.  Without leeway, freshly-issued
    tokens from a peer whose clock is one second ahead get rejected.
    """

    def setup_method(self):
        pub_b64, priv_b64 = generate_keypair()
        self.priv = load_private_key(priv_b64)
        self.pub = load_public_key(pub_b64)

    def test_token_within_default_leeway_accepted(self):
        """exp 5 s in the past must still verify under the 30 s default."""
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
            ttl=-5,
        )
        payload = verify_jwt(token, self.pub, audience="https://b.example.com")
        assert payload["sub"] == "u"

    def test_token_past_leeway_rejected(self):
        """exp 60 s in the past is beyond the default 30 s leeway — rejected."""
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
            ttl=-60,
        )
        with pytest.raises(ValueError, match="expired"):
            verify_jwt(token, self.pub, audience="https://b.example.com")

    def test_custom_leeway_extends_window(self):
        """Caller can widen the window beyond the default."""
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
            ttl=-90,
        )
        # Default leeway of 30 would reject this token; pass 120 to accept it.
        payload = verify_jwt(token, self.pub, audience="https://b.example.com", leeway=120)
        assert payload["sub"] == "u"

    def test_leeway_zero_strict_comparison(self):
        """``leeway=0`` reproduces the pre-leeway strict behaviour."""
        token = create_jwt(
            self.priv,
            issuer="https://a.example.com",
            audience="https://b.example.com",
            subject="u",
            ttl=-1,
        )
        with pytest.raises(ValueError, match="expired"):
            verify_jwt(token, self.pub, audience="https://b.example.com", leeway=0)


# ---------------------------------------------------------------------------
# fetch_peer_public_key
# ---------------------------------------------------------------------------


class TestFetchPeerPublicKey:
    def _good_response(self, key_b64: str):
        """Build a mock urlopen context manager returning valid JSON."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"federation_public_key": key_b64}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_returns_public_key_on_success(self):
        pub_b64, _ = generate_keypair()
        with patch(
            "federation.auth.urllib.request.urlopen",
            return_value=self._good_response(pub_b64),
        ):
            current, next_key = fetch_peer_public_key("https://peer.example.com")
        assert current == pub_b64
        assert next_key == ""  # peer has not announced a rotation overlap

    def test_raises_on_missing_key_field(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"other": "data"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("federation.auth.urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(ValueError, match="No 'federation_public_key'"):
                fetch_peer_public_key("https://peer.example.com")

    def test_raises_on_network_error(self):
        import urllib.error

        with (
            patch(
                "federation.auth.urllib.request.urlopen",
                side_effect=urllib.error.URLError("refused"),
            ),
            pytest.raises(ValueError, match="Could not reach"),
        ):
            fetch_peer_public_key("https://peer.example.com")

    def test_raises_on_invalid_key(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"federation_public_key": "notavalidkey!!!"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("federation.auth.urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(ValueError, match="Invalid public key"):
                fetch_peer_public_key("https://peer.example.com")

    def test_tls_context_is_strict(self):
        """Asserts the outbound TLS posture so it cannot silently regress.

        A future change that bypasses ``_build_tls_context`` — e.g. passing
        ``ssl._create_unverified_context()`` to ``urlopen`` — will continue to
        work at runtime against well-configured peers but expose the platform
        to MITM. Pinning the invariant here makes that mistake fail a test.
        """
        import ssl

        ctx = _build_tls_context()
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2

    def test_fetch_passes_strict_context_to_urlopen(self):
        """``fetch_peer_public_key`` must actually use ``_build_tls_context``.

        Builder + invariant test together would pass even if a callsite
        forgot to use the builder; this test closes that gap.
        """
        import ssl

        pub_b64, _ = generate_keypair()
        with patch(
            "federation.auth.urllib.request.urlopen",
            return_value=self._good_response(pub_b64),
        ) as mock_urlopen:
            fetch_peer_public_key("https://peer.example.com")
        passed_context = mock_urlopen.call_args.kwargs.get("context")
        assert isinstance(passed_context, ssl.SSLContext)
        assert passed_context.check_hostname is True
        assert passed_context.verify_mode == ssl.CERT_REQUIRED

    def test_raises_on_oversize_response(self):
        """A hostile peer returning a multi-MB response must be rejected."""
        from federation.auth import MAX_WELL_KNOWN_RESPONSE_SIZE

        # read(N + 1) — the function asks for one byte past the cap to detect
        # overrun, so the mock must honour the requested size.
        def fake_read(amt):
            return b"x" * amt

        mock_resp = MagicMock()
        mock_resp.read.side_effect = fake_read
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("federation.auth.urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(ValueError, match="returned more than"):
                fetch_peer_public_key("https://peer.example.com")
        # Sanity: the function asked for cap + 1 bytes.
        mock_resp.read.assert_called_once_with(MAX_WELL_KNOWN_RESPONSE_SIZE + 1)


# ---------------------------------------------------------------------------
# _check_url_is_safe — SSRF guard for fetch_peer_public_key
# ---------------------------------------------------------------------------


class TestSsrfGuard:
    """SSRF defense for the only outbound HTTP call in the federation app.

    A compromised superuser could otherwise register a peer URL pointing at
    internal services (RDS, cloud metadata, localhost) and use the well-known
    fetch to probe them.  The guard resolves the hostname and rejects any
    non-globally-routable IP before the urllib call happens.
    """

    def setup_method(self):
        # The default test setting allows private URLs (see test_platform.py);
        # SSRF tests run with that override disabled so they exercise the
        # real guard.
        pass

    def test_rejects_loopback_ipv4(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        with pytest.raises(ValueError, match="non-public address"):
            _check_url_is_safe("https://127.0.0.1/x")

    def test_rejects_loopback_hostname(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        with pytest.raises(ValueError, match="non-public address"):
            _check_url_is_safe("https://localhost/x")

    def test_rejects_rfc1918_private(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        for addr in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with pytest.raises(ValueError, match="non-public address"):
                _check_url_is_safe(f"https://{addr}/x")

    def test_rejects_cloud_metadata_endpoint(self, settings):
        """169.254.169.254 — AWS/Azure/GCP metadata. Common SSRF target."""
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        with pytest.raises(ValueError, match="non-public address"):
            _check_url_is_safe("http://169.254.169.254/latest/meta-data/")

    def test_rejects_ipv6_loopback(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        with pytest.raises(ValueError, match="non-public address"):
            _check_url_is_safe("http://[::1]/x")

    def test_rejects_nat64_prefixes(self, settings):
        """NAT64 addresses are is_global=True in the stdlib but translate
        onto arbitrary IPv4 targets (including RFC 1918) at a local NAT64
        gateway — they must be rejected explicitly."""
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        # Well-known prefix (RFC 6052) embedding 10.0.0.1 — is_global=True
        # in the stdlib, so only the explicit NAT64 check catches it.
        with pytest.raises(ValueError, match="NAT64"):
            _check_url_is_safe("https://[64:ff9b::a00:1]/x")
        # Local-use prefix (RFC 8215) — already non-global in the stdlib;
        # asserted here so a stdlib reclassification cannot reopen it
        # silently (the explicit prefix list backstops it either way).
        with pytest.raises(ValueError, match="refusing to fetch"):
            _check_url_is_safe("https://[64:ff9b:1::a00:1]/x")

    def test_accepts_public_ip(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        # 1.1.1.1 — Cloudflare public DNS, reliably globally routable.
        _check_url_is_safe("https://1.1.1.1/x")  # no exception

    def test_rejects_missing_hostname(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        with pytest.raises(ValueError, match="no hostname"):
            _check_url_is_safe("https:///path")

    def test_override_allows_private(self, settings):
        """``FEDERATION_ALLOW_PRIVATE_PEER_URLS=True`` short-circuits the guard.

        Dev-environment escape hatch — never to be set in production.
        """
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = True
        _check_url_is_safe("https://127.0.0.1/x")  # no exception


class TestSsrfAllowedCidrs:
    """``FEDERATION_ALLOWED_PEER_CIDRS`` widens the guard for named networks only.

    A deployment federating over a tailnet or a VPN has peers at addresses that are
    private but not arbitrary. The blunt boolean turns the guard off for every
    address; this list turns it off for the networks an operator names and leaves
    the rest of the SSRF surface — metadata endpoints, loopback, the RFC 1918 space
    a compromised superuser would aim at — refused exactly as before.
    """

    #: A Tailscale tailnet address: inside 100.64.0.0/10, so ``is_global`` is False.
    TAILNET_V4 = "100.79.150.119"
    TAILNET_CIDR = "100.64.0.0/10"

    def test_tailnet_address_rejected_by_default(self, settings):
        # The setting's default must change nothing, or every deployment that never
        # heard of it quietly gains a hole.
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = []
        with pytest.raises(ValueError, match="non-public address"):
            _check_url_is_safe(f"https://{self.TAILNET_V4}/x")

    def test_listed_network_is_admitted(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = [self.TAILNET_CIDR]
        _check_url_is_safe(f"https://{self.TAILNET_V4}/x")  # no exception

    def test_listing_one_network_does_not_admit_another(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = [self.TAILNET_CIDR]
        for addr in ("10.0.0.1", "192.168.1.1", "127.0.0.1", "169.254.169.254"):
            with pytest.raises(ValueError, match="non-public address"):
                _check_url_is_safe(f"https://{addr}/x")

    def test_ipv6_network_is_admitted(self, settings):
        # A tailnet answers on both families, so a v4-only carve-out silently fails
        # over whichever name resolves to the ULA first.
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = ["fd7a:115c:a1e0::/48"]
        _check_url_is_safe("https://[fd7a:115c:a1e0::1]/x")  # no exception

    def test_nat64_refused_even_when_listed(self, settings):
        # A translation prefix maps onto arbitrary IPv4 targets, so it is never a
        # network an operator owns — listing it must not buy anything.
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = ["64:ff9b::/96", "64:ff9b:1::/48"]
        with pytest.raises(ValueError, match="NAT64"):
            _check_url_is_safe("https://[64:ff9b::a00:1]/x")
        with pytest.raises(ValueError, match="NAT64"):
            _check_url_is_safe("https://[64:ff9b:1::a00:1]/x")

    def test_malformed_entry_is_reported_not_ignored(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = ["not-a-network"]
        with pytest.raises(ImproperlyConfigured, match="not a CIDR"):
            _check_url_is_safe(f"https://{self.TAILNET_V4}/x")

    def test_host_bits_are_reported(self, settings):
        # 100.64.0.1/10 reads as "this host" and means "this whole /10". Refusing it
        # is what keeps the two readings from diverging silently.
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = ["100.64.0.1/10"]
        with pytest.raises(ImproperlyConfigured, match="not a CIDR"):
            _check_url_is_safe(f"https://{self.TAILNET_V4}/x")

    def test_default_route_is_refused(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = ["0.0.0.0/0"]
        with pytest.raises(ImproperlyConfigured, match="default route"):
            _check_url_is_safe("https://127.0.0.1/x")
        settings.FEDERATION_ALLOWED_PEER_CIDRS = ["::/0"]
        with pytest.raises(ImproperlyConfigured, match="default route"):
            _check_url_is_safe("https://127.0.0.1/x")

    def test_public_addresses_are_unaffected(self, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        settings.FEDERATION_ALLOWED_PEER_CIDRS = [self.TAILNET_CIDR]
        _check_url_is_safe("https://1.1.1.1/x")  # no exception

    def test_boolean_override_short_circuits_the_list(self, settings):
        # Ordering, asserted so it stays deliberate: the boolean already means
        # "allow anything", so a malformed list behind it is never parsed.
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = True
        settings.FEDERATION_ALLOWED_PEER_CIDRS = ["not-a-network"]
        _check_url_is_safe("https://127.0.0.1/x")  # no exception


# ---------------------------------------------------------------------------
# assert_local_keys_consistent
# ---------------------------------------------------------------------------


class TestAssertLocalKeysConsistent:
    """Startup-time fail-fast on partial key rotations.

    Federation's worst failure mode is silent: a public key that doesn't match
    the private key produces signatures every peer rejects, but the local
    instance has no symptom until someone notices the missing traffic.  This
    check is the trip-wire.
    """

    def test_no_op_when_not_configured(self, settings):
        settings.FEDERATION_INSTANCE_URL = ""
        settings.FEDERATION_PUBLIC_KEY = ""
        settings.FEDERATION_PRIVATE_KEY = ""
        # Should return without raising.
        assert assert_local_keys_consistent() is None

    def test_passes_when_keys_match(self, settings):
        pub, priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = pub
        settings.FEDERATION_PRIVATE_KEY = priv
        assert assert_local_keys_consistent() is None

    def test_raises_on_key_mismatch(self, settings):
        """Public key from a different pair than the private key."""
        from django.core.exceptions import ImproperlyConfigured

        _, priv = generate_keypair()
        wrong_pub, _ = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = wrong_pub
        settings.FEDERATION_PRIVATE_KEY = priv
        with pytest.raises(ImproperlyConfigured, match="does not match"):
            assert_local_keys_consistent()

    def test_raises_on_malformed_public_key(self, settings):
        from django.core.exceptions import ImproperlyConfigured

        _, priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = "not-a-valid-base64-key"
        settings.FEDERATION_PRIVATE_KEY = priv
        with pytest.raises(ImproperlyConfigured, match="Federation key"):
            assert_local_keys_consistent()


# ---------------------------------------------------------------------------
# parse_federation_auth / try_federation_auth
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestParseFederationAuth:
    """Coverage for the consolidated inbound-auth parser.

    The federation API and the recordings API used to carry near-duplicate
    implementations that had drifted (recordings did not normalise ``iss`` with
    ``.strip().rstrip("/")``).  Both now delegate to ``parse_federation_auth``,
    so a single test class covers both call sites.
    """

    def _setup_local(self, settings):
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

    def _make_peer(self, url: str, trusted: bool = True):
        from federation.models import FederatedPeer

        peer_pub, peer_priv = generate_keypair()
        peer = FederatedPeer.objects.create(
            url=url,
            display_name="Test Peer",
            public_key=peer_pub,
            is_trusted=trusted,
        )
        return peer, peer_priv

    def _request(self, token: str | None):
        req = MagicMock()
        req.META = {"HTTP_AUTHORIZATION": f"FederatedBearer {token}"} if token else {}
        return req

    def test_accepts_trailing_slash_issuer(self, settings):
        """A token whose ``iss`` ends in '/' must still match a stored peer URL.

        This is the drift bug the consolidation closed — the recordings path
        previously did not normalise and rejected such tokens.
        """
        self._setup_local(settings)
        peer, peer_priv = self._make_peer(url="https://peer.example.com")
        token = create_jwt(
            load_private_key(peer_priv),
            issuer="https://peer.example.com/",  # note trailing slash
            audience="https://local.example.com",
            subject="user-42",
        )
        result = parse_federation_auth(self._request(token))
        assert result.ok
        assert result.peer.pk == peer.pk
        assert result.remote_user_id == "user-42"

    def test_try_wrapper_returns_tuple_on_success(self, settings):
        self._setup_local(settings)
        peer, peer_priv = self._make_peer(url="https://peer.example.com")
        token = create_jwt(
            load_private_key(peer_priv),
            issuer="https://peer.example.com",
            audience="https://local.example.com",
            subject="user-7",
        )
        result = try_federation_auth(self._request(token))
        assert result is not None
        assert result == (peer, "user-7")

    def test_try_wrapper_returns_none_on_missing_header(self, settings):
        self._setup_local(settings)
        assert try_federation_auth(self._request(None)) is None

    def test_try_wrapper_silent_when_no_federation_header(self, settings, caplog):
        """No FederatedBearer header → return None without firing a
        security event. Dual-auth endpoints poll regularly without
        session auth; emitting federation.auth_failed on each one
        floods the log on instances without federation configured."""
        self._setup_local(settings)
        with caplog.at_level("WARNING", logger="epicurrents.security"):
            assert try_federation_auth(self._request(None)) is None
        assert not any(getattr(r, "security_event_type", None) == "federation.auth_failed" for r in caplog.records)

    def test_try_wrapper_silent_when_federation_disabled_and_no_header(self, settings, caplog):
        """No FederatedBearer header → silent return regardless of
        whether federation itself is configured. The wrapper does not
        peek at settings; it short-circuits on the header alone."""
        settings.FEDERATION_INSTANCE_URL = ""
        with caplog.at_level("WARNING", logger="epicurrents.security"):
            assert try_federation_auth(self._request(None)) is None
        assert not any(getattr(r, "security_event_type", None) == "federation.auth_failed" for r in caplog.records)

    def test_try_wrapper_emits_event_when_header_present_but_disabled(self, settings, caplog):
        """A FederatedBearer header against a disabled instance IS
        worth logging — the caller claimed to be a federation peer."""
        settings.FEDERATION_INSTANCE_URL = ""
        with caplog.at_level("WARNING", logger="epicurrents.security"):
            try_federation_auth(self._request("any-token-value"))
        assert any(getattr(r, "security_event_type", None) == "federation.auth_failed" for r in caplog.records)

    def test_try_wrapper_returns_none_for_untrusted_peer(self, settings):
        self._setup_local(settings)
        peer, peer_priv = self._make_peer(url="https://peer.example.com", trusted=False)
        token = create_jwt(
            load_private_key(peer_priv),
            issuer="https://peer.example.com",
            audience="https://local.example.com",
            subject="user-1",
        )
        assert try_federation_auth(self._request(token)) is None

    def test_parse_returns_403_when_federation_disabled(self, settings):
        settings.FEDERATION_INSTANCE_URL = ""
        settings.FEDERATION_PUBLIC_KEY = ""
        settings.FEDERATION_PRIVATE_KEY = ""
        result = parse_federation_auth(self._request("anything.at.all"))
        assert not result.ok
        assert result.error[0] == 403

    def test_parse_returns_401_for_malformed_token(self, settings):
        self._setup_local(settings)
        result = parse_federation_auth(self._request("not-a-jwt"))
        assert not result.ok
        assert result.error[0] == 401
