"""Tests for ``epicurrents.security_log.get_client_ip`` and its
TRUSTED_PROXIES gate.

The function is the single attribution surface for every security-log
event (login failures, lockouts, rate-limit hits, permission denials,
federation auth failures). When TRUSTED_PROXIES is misconfigured —
empty, missing the proxy's range, or carrying invalid CIDR — every
downstream rule still receives an IP; it's just the wrong one. The
asymmetry between "right IP" and "spoofable IP" is exactly what the
setting exists to manage, so each variant gets its own test rather
than being lumped under a single parametrized case.
"""

import logging

from epicurrents.security_log import get_client_ip


class _FakeRequest:
    def __init__(self, **meta):
        self.META = {k: v for k, v in meta.items() if v is not None}


class TestGetClientIpWithoutTrustedProxies:
    def test_no_xff_returns_remote_addr(self, settings):
        settings.TRUSTED_PROXIES = []
        req = _FakeRequest(REMOTE_ADDR="203.0.113.5")
        assert get_client_ip(req) == "203.0.113.5"

    def test_xff_ignored_when_no_trusted_proxies(self, settings):
        """Empty TRUSTED_PROXIES = the deployment isn't behind a known
        proxy. Even a present X-Forwarded-For must be treated as
        caller-supplied and discarded — that's the spoof surface the
        setting was added to close."""
        settings.TRUSTED_PROXIES = []
        req = _FakeRequest(
            REMOTE_ADDR="203.0.113.5",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        assert get_client_ip(req) == "203.0.113.5"

    def test_no_remote_addr_returns_none(self, settings):
        settings.TRUSTED_PROXIES = []
        req = _FakeRequest()
        assert get_client_ip(req) is None


class TestGetClientIpWithTrustedProxies:
    def test_xff_honoured_when_remote_addr_in_trusted_range(self, settings):
        settings.TRUSTED_PROXIES = ["10.0.0.0/8"]
        req = _FakeRequest(
            REMOTE_ADDR="10.0.5.7",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        assert get_client_ip(req) == "198.51.100.99"

    def test_xff_ignored_when_remote_addr_outside_trusted_range(self, settings):
        """The proxy IP is what determines trust. An untrusted source can
        still set XFF; we have to ignore it because the source isn't a
        proxy we've vouched for."""
        settings.TRUSTED_PROXIES = ["10.0.0.0/8"]
        req = _FakeRequest(
            REMOTE_ADDR="8.8.8.8",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        assert get_client_ip(req) == "8.8.8.8"

    def test_single_proxy_via_host_route(self, settings):
        settings.TRUSTED_PROXIES = ["127.0.0.1/32"]
        req = _FakeRequest(
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        assert get_client_ip(req) == "198.51.100.99"

    def test_multi_hop_xff_returns_leftmost(self, settings):
        """XFF accumulates left-to-right as the request traverses proxies;
        the original client sits at index 0."""
        settings.TRUSTED_PROXIES = ["10.0.0.0/8"]
        req = _FakeRequest(
            REMOTE_ADDR="10.0.5.7",
            HTTP_X_FORWARDED_FOR="198.51.100.99, 10.0.5.1, 10.0.5.7",
        )
        assert get_client_ip(req) == "198.51.100.99"

    def test_xff_whitespace_trimmed(self, settings):
        settings.TRUSTED_PROXIES = ["10.0.0.0/8"]
        req = _FakeRequest(
            REMOTE_ADDR="10.0.5.7",
            HTTP_X_FORWARDED_FOR="  198.51.100.99  , 10.0.5.1",
        )
        assert get_client_ip(req) == "198.51.100.99"

    def test_ipv6_trusted_proxy_range(self, settings):
        settings.TRUSTED_PROXIES = ["fd00::/8"]
        req = _FakeRequest(
            REMOTE_ADDR="fd00::5",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        assert get_client_ip(req) == "198.51.100.99"

    def test_multiple_trusted_ranges(self, settings):
        settings.TRUSTED_PROXIES = ["127.0.0.1/32", "10.0.0.0/8"]
        req = _FakeRequest(
            REMOTE_ADDR="10.0.99.1",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        assert get_client_ip(req) == "198.51.100.99"


class TestGetClientIpInvalidConfig:
    def test_invalid_cidr_entry_skipped(self, settings, caplog):
        """An invalid CIDR entry should disable that single hop, not
        crash the request. A warning lands in the security log so the
        operator sees the typo."""
        settings.TRUSTED_PROXIES = ["not-a-cidr", "10.0.0.0/8"]
        req = _FakeRequest(
            REMOTE_ADDR="10.0.5.7",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        with caplog.at_level(logging.WARNING, logger="epicurrents.security"):
            result = get_client_ip(req)
        assert result == "198.51.100.99"
        assert any("Invalid TRUSTED_PROXIES" in rec.message for rec in caplog.records)

    def test_all_entries_invalid_treats_xff_as_untrusted(self, settings):
        settings.TRUSTED_PROXIES = ["not-a-cidr", "also bad"]
        req = _FakeRequest(
            REMOTE_ADDR="10.0.5.7",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        assert get_client_ip(req) == "10.0.5.7"

    def test_malformed_remote_addr_falls_back_to_untrusted(self, settings):
        """If REMOTE_ADDR somehow isn't a parseable IP, treat the request
        as if it came from an untrusted hop and return REMOTE_ADDR as-is."""
        settings.TRUSTED_PROXIES = ["10.0.0.0/8"]
        req = _FakeRequest(
            REMOTE_ADDR="not-an-ip",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )
        assert get_client_ip(req) == "not-an-ip"
