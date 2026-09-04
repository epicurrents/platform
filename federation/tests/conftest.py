"""Federation-scoped pytest fixtures.

The ``mock_federated_peer`` fixture below is the starting point for the
federation integration-test layer described in the ROADMAP item
"Testing — federation integration test harness". It stands up a real HTTP
server (via ``pytest-httpserver``) playing the role of a remote federation
peer: it serves a real ``.well-known/epicurrents-federation.json`` document
and can be extended with additional endpoint stubs by individual tests.

The key distinction from the older mock-based tests in this directory
(which patch ``urllib.request.urlopen``) is that this fixture exercises
the *real* outbound HTTP stack — URL parsing, the SSRF guard, the TLS
context builder, the size/timeout limits, response decoding — none of
which a mock can faithfully reproduce.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from pytest_httpserver import HTTPServer

from federation.auth import (
    Ed25519PrivateKey,
    create_jwt,
    generate_keypair,
    load_private_key,
)
from federation.models import FederatedPeer


@dataclass
class MockFederatedPeer:
    """Handle returned by the ``mock_federated_peer`` fixture.

    Attributes:
        url: Base URL of the running mock peer (``http://127.0.0.1:<port>``).
            Use as the ``iss`` claim on tokens it signs and as the registered
            URL on the corresponding ``FederatedPeer`` row.
        public_key_b64: Peer's public key in the codebase's standard
            URL-safe base64 (no padding) form.
        private_key: ``Ed25519PrivateKey`` for signing tokens the local
            Django will receive.
        peer: The ``FederatedPeer`` row registered with the local instance.
        httpserver: The underlying ``pytest-httpserver`` ``HTTPServer``
            instance, exposed so tests can add additional endpoint stubs
            via ``httpserver.expect_request(...)``.
    """

    url: str
    public_key_b64: str
    private_key: Ed25519PrivateKey
    peer: FederatedPeer
    httpserver: HTTPServer

    def sign_jwt(
        self,
        *,
        audience: str,
        subject: str = "remote-user-1",
        ttl: int = 60,
        jti: str | None = None,
    ) -> str:
        """Sign a JWT impersonating this peer.

        ``audience`` should be the local instance's URL (typically
        ``settings.FEDERATION_INSTANCE_URL``) — federation auth rejects
        tokens whose ``aud`` claim does not match.
        """
        return create_jwt(
            self.private_key,
            issuer=self.url,
            audience=audience,
            subject=subject,
            ttl=ttl,
            jti=jti,
        )


@pytest.fixture
def mock_federated_peer(httpserver: HTTPServer, db, settings) -> MockFederatedPeer:
    """Stand up a real HTTP mock peer + register it as a ``FederatedPeer``.

    Side effects:
      - ``FEDERATION_ALLOW_PRIVATE_PEER_URLS`` is forced to ``True`` so the
        SSRF guard (``_check_url_is_safe``) doesn't reject the loopback
        address ``pytest-httpserver`` binds to.
      - A ``FederatedPeer`` row is created pointing at the mock server's
        URL with ``is_trusted=True``.
      - The mock server serves ``/.well-known/epicurrents-federation.json``
        returning the peer's public key in the canonical schema.

    Tests can add more stubs via the returned ``httpserver`` attribute.
    """
    settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = True

    pub_b64, priv_b64 = generate_keypair()
    private_key = load_private_key(priv_b64)
    base_url = httpserver.url_for("").rstrip("/")

    httpserver.expect_request("/.well-known/epicurrents-federation.json").respond_with_data(
        json.dumps({"federation_public_key": pub_b64}),
        content_type="application/json",
    )

    peer = FederatedPeer.objects.create(
        url=base_url,
        public_key=pub_b64,
        is_trusted=True,
    )

    return MockFederatedPeer(
        url=base_url,
        public_key_b64=pub_b64,
        private_key=private_key,
        peer=peer,
        httpserver=httpserver,
    )
