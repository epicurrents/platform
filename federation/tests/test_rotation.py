"""Tests for the key-rotation overlap window.

Covers:
- ``fetch_peer_public_key`` parses ``federation_public_key_next`` when present
  and rejects malformed values rather than silently dropping them.
- ``parse_federation_auth`` accepts tokens signed with the peer's
  ``public_key_next`` during the overlap, but only retries on signature
  failures (not on expiry / audience / malformed-token errors).
- ``assert_local_keys_consistent`` validates the NEXT pair when set.
- ``rotate_federation_keys --announce`` and ``--promote`` rewrite ``.env``
  correctly.
"""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from model_bakery import baker

from federation.auth import (
    assert_local_keys_consistent,
    create_jwt,
    fetch_peer_public_key,
    generate_keypair,
    load_private_key,
    parse_federation_auth,
)
from federation.models import FederatedPeer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _well_known_response(current: str, next_key: str = ""):
    payload = {"federation_public_key": current}
    if next_key:
        payload["federation_public_key_next"] = next_key
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_request(token: str):
    req = MagicMock()
    req.META = {"HTTP_AUTHORIZATION": f"FederatedBearer {token}"}
    return req


# ---------------------------------------------------------------------------
# fetch_peer_public_key
# ---------------------------------------------------------------------------


class TestFetchPublicKeyOverlap:
    def test_returns_empty_next_when_peer_not_rotating(self):
        cur, _ = generate_keypair()
        with patch(
            "federation.auth.urllib.request.urlopen",
            return_value=_well_known_response(cur),
        ):
            current, next_key = fetch_peer_public_key("https://peer.example.com")
        assert current == cur
        assert next_key == ""

    def test_returns_both_keys_when_peer_rotating(self):
        cur, _ = generate_keypair()
        nxt, _ = generate_keypair()
        with patch(
            "federation.auth.urllib.request.urlopen",
            return_value=_well_known_response(cur, nxt),
        ):
            current, next_key = fetch_peer_public_key("https://peer.example.com")
        assert current == cur
        assert next_key == nxt

    def test_malformed_next_key_is_fatal(self):
        """Surface the typo loudly rather than silently dropping the announced key."""
        cur, _ = generate_keypair()
        with (
            patch(
                "federation.auth.urllib.request.urlopen",
                return_value=_well_known_response(cur, "not-a-valid-key"),
            ),
            pytest.raises(ValueError, match="Invalid 'federation_public_key_next'"),
        ):
            fetch_peer_public_key("https://peer.example.com")


# ---------------------------------------------------------------------------
# parse_federation_auth — overlap acceptance
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestParseAuthOverlap:
    def _setup_local(self, settings):
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

    def test_token_signed_with_next_key_accepted(self, settings):
        """Peer mid-rotation: signs with NEXT key, this instance accepts via fallback."""
        self._setup_local(settings)
        # Peer's current + next keys.
        cur_pub, cur_priv = generate_keypair()
        next_pub, next_priv = generate_keypair()
        peer = baker.make(
            FederatedPeer,
            url="https://peer.example.com",
            public_key=cur_pub,
            public_key_next=next_pub,
            is_trusted=True,
        )
        # Peer signs with its NEXT key (mid-rotation).
        token = create_jwt(
            load_private_key(next_priv),
            issuer=peer.url,
            audience="https://local.example.com",
            subject="user-1",
        )
        result = parse_federation_auth(_make_request(token))
        assert result.ok, result.error

    def test_no_fallback_when_next_not_set(self, settings):
        self._setup_local(settings)
        # Peer's current only; sign with a wrong key to confirm rejection.
        cur_pub, _ = generate_keypair()
        _, wrong_priv = generate_keypair()
        peer = baker.make(
            FederatedPeer,
            url="https://peer.example.com",
            public_key=cur_pub,
            public_key_next="",
            is_trusted=True,
        )
        token = create_jwt(
            load_private_key(wrong_priv),
            issuer=peer.url,
            audience="https://local.example.com",
            subject="user-1",
        )
        result = parse_federation_auth(_make_request(token))
        assert not result.ok
        assert "signature" in result.error[1].lower()

    def test_non_signature_failures_do_not_retry(self, settings):
        """An expired token signed with NEXT must not bypass the expiry check."""
        self._setup_local(settings)
        cur_pub, _ = generate_keypair()
        next_pub, next_priv = generate_keypair()
        peer = baker.make(
            FederatedPeer,
            url="https://peer.example.com",
            public_key=cur_pub,
            public_key_next=next_pub,
            is_trusted=True,
        )
        # Token signed with NEXT but very expired — current-key verify fails
        # with "Invalid JWT signature" (which would trigger retry), but the
        # NEXT-key retry then fails with "expired".  Result: 401 expired,
        # not 401 signature.  This proves the retry path doesn't smuggle
        # past time validation.
        token = create_jwt(
            load_private_key(next_priv),
            issuer=peer.url,
            audience="https://local.example.com",
            subject="user-1",
            ttl=-300,  # well past leeway + max_age
        )
        result = parse_federation_auth(_make_request(token))
        assert not result.ok
        assert "expired" in result.error[1].lower() or "too old" in result.error[1].lower()


# ---------------------------------------------------------------------------
# assert_local_keys_consistent — NEXT pair check
# ---------------------------------------------------------------------------


class TestStartupCheckOverlap:
    def _set_current(self, settings):
        pub, priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = pub
        settings.FEDERATION_PRIVATE_KEY = priv

    def test_passes_when_no_next_pair(self, settings):
        self._set_current(settings)
        settings.FEDERATION_PUBLIC_KEY_NEXT = ""
        settings.FEDERATION_PRIVATE_KEY_NEXT = ""
        assert_local_keys_consistent()  # no exception

    def test_passes_when_next_pair_is_consistent(self, settings):
        from django.core.exceptions import ImproperlyConfigured  # noqa: F401

        self._set_current(settings)
        next_pub, next_priv = generate_keypair()
        settings.FEDERATION_PUBLIC_KEY_NEXT = next_pub
        settings.FEDERATION_PRIVATE_KEY_NEXT = next_priv
        assert_local_keys_consistent()

    def test_raises_when_only_one_of_next_pair_set(self, settings):
        from django.core.exceptions import ImproperlyConfigured

        self._set_current(settings)
        next_pub, _ = generate_keypair()
        settings.FEDERATION_PUBLIC_KEY_NEXT = next_pub
        settings.FEDERATION_PRIVATE_KEY_NEXT = ""
        with pytest.raises(ImproperlyConfigured, match="must be set together"):
            assert_local_keys_consistent()

    def test_raises_when_next_pair_mismatched(self, settings):
        from django.core.exceptions import ImproperlyConfigured

        self._set_current(settings)
        next_pub, _ = generate_keypair()
        _, wrong_priv = generate_keypair()
        settings.FEDERATION_PUBLIC_KEY_NEXT = next_pub
        settings.FEDERATION_PRIVATE_KEY_NEXT = wrong_priv
        with pytest.raises(ImproperlyConfigured, match="does not match.*NEXT"):
            assert_local_keys_consistent()


# ---------------------------------------------------------------------------
# rotate_federation_keys --announce / --promote
# ---------------------------------------------------------------------------


class TestRotateCommandOverlap:
    def _seed_env(self, tmp_path):
        pub, priv = generate_keypair()
        env_path = tmp_path / ".env"
        env_path.write_text(f"FEDERATION_PUBLIC_KEY={pub}\nFEDERATION_PRIVATE_KEY={priv}\n")
        return env_path, pub, priv

    def test_announce_writes_next_pair_without_touching_current(self, tmp_path):
        env_path, orig_pub, orig_priv = self._seed_env(tmp_path)
        out = StringIO()
        call_command("rotate_federation_keys", "--announce", "--env", str(env_path), stdout=out)
        text = env_path.read_text()
        # Current pair unchanged.
        assert f"FEDERATION_PUBLIC_KEY={orig_pub}" in text
        assert f"FEDERATION_PRIVATE_KEY={orig_priv}" in text
        # NEXT pair populated.
        assert "FEDERATION_PUBLIC_KEY_NEXT=" in text
        assert "FEDERATION_PRIVATE_KEY_NEXT=" in text
        # And not empty.
        for line in text.splitlines():
            if line.startswith("FEDERATION_PUBLIC_KEY_NEXT="):
                assert len(line.split("=", 1)[1]) == 43
            if line.startswith("FEDERATION_PRIVATE_KEY_NEXT="):
                assert len(line.split("=", 1)[1]) == 43

    def test_promote_moves_next_to_current_and_clears_next(self, tmp_path):
        # Start in a post-announce state.
        env_path, orig_pub, orig_priv = self._seed_env(tmp_path)
        call_command(
            "rotate_federation_keys",
            "--announce",
            "--env",
            str(env_path),
            stdout=StringIO(),
        )
        announced_text = env_path.read_text()
        next_pub = next(
            line.split("=", 1)[1]
            for line in announced_text.splitlines()
            if line.startswith("FEDERATION_PUBLIC_KEY_NEXT=")
        )
        # Promote.
        call_command(
            "rotate_federation_keys",
            "--promote",
            "--env",
            str(env_path),
            stdout=StringIO(),
        )
        promoted = env_path.read_text()
        # Current pair == the announced NEXT key.
        assert f"FEDERATION_PUBLIC_KEY={next_pub}" in promoted
        assert f"FEDERATION_PUBLIC_KEY={orig_pub}" not in promoted
        # NEXT slots cleared.
        assert "FEDERATION_PUBLIC_KEY_NEXT=\n" in promoted
        assert "FEDERATION_PRIVATE_KEY_NEXT=\n" in promoted

    def test_promote_without_announce_raises(self, tmp_path):
        env_path, _, _ = self._seed_env(tmp_path)
        with pytest.raises(CommandError, match="Cannot promote"):
            call_command(
                "rotate_federation_keys",
                "--promote",
                "--env",
                str(env_path),
                stdout=StringIO(),
            )
