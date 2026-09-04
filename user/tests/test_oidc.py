"""Contract tests for the OpenID Connect external-login flow.

Backstops the load-bearing checks in [user/oidc.py](../oidc.py): the ID-token
claim gate (issuer / audience / tenant / nonce), the email-domain allowlist
(PHI-containment control #1, fail-closed), and the find-or-create policy. The
endpoint tests exercise the full flow with the network-dependent steps mocked.
"""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from user.models import ExternalIdentity
from user.oidc import (
    OIDCAuthError,
    _check_claims,
    email_domain_allowed,
    identity_domain,
    resolve_identity,
)

PROVIDER = {
    "label": "Microsoft",
    "tenant_id": "tenant-guid",
    "client_id": "client-abc",
    "client_secret": "secret",
    "authority": "https://login.microsoftonline.com/tenant-guid/v2.0",
    "scopes": ["openid", "profile", "email"],
    "allowed_domains": [],
    "redirect_uri": "https://host/api/v1/user/oidc/entra/callback",
}


def make_claims(**overrides):
    claims = {
        "iss": PROVIDER["authority"],
        "aud": PROVIDER["client_id"],
        "tid": PROVIDER["tenant_id"],
        "nonce": "nonce123",
        "sub": "subject-xyz",
        "email": "alice@hospital-a.org",
        "email_verified": True,
        "given_name": "Alice",
        "family_name": "Anders",
    }
    claims.update(overrides)
    return claims


def provider(**overrides):
    cfg = dict(PROVIDER)
    cfg.update(overrides)
    return cfg


# ── _check_claims (issuer / audience / tenant / nonce) ───────────────────────


class TestCheckClaims:
    def test_valid_claims_pass(self):
        _check_claims(provider(), make_claims(), "nonce123")

    def test_issuer_mismatch_rejected(self):
        with pytest.raises(OIDCAuthError) as exc:
            _check_claims(provider(), make_claims(iss="https://evil.example/"), "nonce123")
        assert exc.value.reason == "issuer_mismatch"

    def test_audience_mismatch_rejected(self):
        with pytest.raises(OIDCAuthError) as exc:
            _check_claims(provider(), make_claims(aud="other-client"), "nonce123")
        assert exc.value.reason == "audience_mismatch"

    def test_audience_list_form_accepted(self):
        _check_claims(provider(), make_claims(aud=["client-abc", "extra"]), "nonce123")

    def test_tenant_mismatch_rejected(self):
        with pytest.raises(OIDCAuthError) as exc:
            _check_claims(provider(), make_claims(tid="other-tenant"), "nonce123")
        assert exc.value.reason == "tenant_mismatch"

    def test_nonce_mismatch_rejected(self):
        with pytest.raises(OIDCAuthError) as exc:
            _check_claims(provider(), make_claims(nonce="wrong"), "nonce123")
        assert exc.value.reason == "nonce_mismatch"

    def test_absent_expected_nonce_rejected(self):
        with pytest.raises(OIDCAuthError) as exc:
            _check_claims(provider(), make_claims(), "")
        assert exc.value.reason == "nonce_mismatch"


# ── Domain allowlist (PHI-containment control #1) ────────────────────────────


class TestDomainAllowlist:
    def test_empty_allowlist_allows_any(self):
        assert email_domain_allowed(make_claims(email="x@anywhere.com"), provider())

    def test_allowed_domain_passes(self):
        cfg = provider(allowed_domains=["hospital-a.org"])
        assert email_domain_allowed(make_claims(email="alice@hospital-a.org"), cfg)

    def test_foreign_domain_rejected(self):
        cfg = provider(allowed_domains=["hospital-a.org"])
        assert not email_domain_allowed(make_claims(email="bob@evil.com"), cfg)

    def test_missing_domain_fails_closed(self):
        cfg = provider(allowed_domains=["hospital-a.org"])
        claims = make_claims()
        claims.pop("email")
        claims.pop("preferred_username", None)
        claims.pop("upn", None)
        assert not email_domain_allowed(claims, cfg)

    def test_guest_account_rejected_by_allowlist(self):
        cfg = provider(allowed_domains=["hospital-a.org"])
        claims = make_claims(email=None, preferred_username="bob_home.com#EXT#@tenant.onmicrosoft.com")
        assert not email_domain_allowed(claims, cfg)

    def test_identity_domain_falls_back_to_upn(self):
        claims = {"upn": "carol@hospital-a.org"}
        assert identity_domain(claims) == "hospital-a.org"


# ── resolve_identity (find-or-create) ────────────────────────────────────────


@pytest.mark.django_db
class TestResolveIdentity:
    @override_settings(OIDC_AUTO_CREATE_USERS=True, OIDC_LINK_BY_VERIFIED_EMAIL=False)
    def test_first_login_auto_creates_user_and_identity(self):
        identity, created = resolve_identity("entra", provider(), make_claims())
        assert created is True
        assert identity.subject == "subject-xyz"
        assert identity.user.email == "alice@hospital-a.org"
        assert not identity.user.has_usable_password()

    @override_settings(OIDC_AUTO_CREATE_USERS=True, OIDC_LINK_BY_VERIFIED_EMAIL=False)
    def test_returning_login_reuses_identity(self):
        first, _ = resolve_identity("entra", provider(), make_claims())
        second, created = resolve_identity("entra", provider(), make_claims())
        assert created is False
        assert second.pk == first.pk
        assert ExternalIdentity.objects.count() == 1

    @override_settings(OIDC_AUTO_CREATE_USERS=True)
    def test_foreign_domain_raises_before_create(self):
        cfg = provider(allowed_domains=["hospital-a.org"])
        with pytest.raises(OIDCAuthError) as exc:
            resolve_identity("entra", cfg, make_claims(email="bob@evil.com"))
        assert exc.value.reason == "domain_not_allowed"
        assert ExternalIdentity.objects.count() == 0
        assert get_user_model().objects.count() == 0

    @override_settings(OIDC_AUTO_CREATE_USERS=False, OIDC_LINK_BY_VERIFIED_EMAIL=False)
    def test_auto_create_disabled_rejects_unknown_user(self):
        with pytest.raises(OIDCAuthError) as exc:
            resolve_identity("entra", provider(), make_claims())
        assert exc.value.reason == "auto_create_disabled"

    @override_settings(OIDC_AUTO_CREATE_USERS=False, OIDC_LINK_BY_VERIFIED_EMAIL=True)
    def test_links_to_existing_user_by_verified_email(self):
        existing = get_user_model().objects.create_user(username="alice", email="alice@hospital-a.org", password="x")
        identity, created = resolve_identity("entra", provider(), make_claims())
        assert created is True
        assert identity.user.pk == existing.pk
        assert get_user_model().objects.count() == 1

    @override_settings(OIDC_AUTO_CREATE_USERS=False, OIDC_LINK_BY_VERIFIED_EMAIL=True)
    def test_unverified_email_does_not_link(self):
        get_user_model().objects.create_user(username="alice", email="alice@hospital-a.org", password="x")
        with pytest.raises(OIDCAuthError) as exc:
            resolve_identity("entra", provider(), make_claims(email_verified=False))
        assert exc.value.reason == "auto_create_disabled"


# ── Endpoints ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAuthConfigEndpoint:
    def test_disabled_returns_no_providers(self, client):
        response = client.get("/api/v1/user/auth-config")
        assert response.status_code == 200
        assert response.json() == {"oidc_providers": []}

    @override_settings(OIDC_ENABLED=True, OIDC_PROVIDERS={"entra": PROVIDER})
    def test_enabled_lists_configured_provider(self, client):
        response = client.get("/api/v1/user/auth-config")
        body = response.json()
        assert body["oidc_providers"][0]["name"] == "entra"
        assert body["oidc_providers"][0]["login_url"] == "/api/v1/user/oidc/entra/start"


@pytest.mark.django_db
class TestOidcStartEndpoint:
    def test_disabled_returns_404(self, client):
        response = client.get("/api/v1/user/oidc/entra/start")
        assert response.status_code == 404

    @override_settings(OIDC_ENABLED=True, OIDC_PROVIDERS={"entra": PROVIDER})
    @patch("user.api.v1.ninja.build_authorization_url", return_value="https://provider/authorize?x=1")
    def test_enabled_redirects_and_stashes_flow(self, _build, client):
        response = client.get("/api/v1/user/oidc/entra/start?redirect=/dashboard")
        assert response.status_code == 302
        assert response["Location"] == "https://provider/authorize?x=1"
        flow = client.session["oidc_flow"]
        assert flow["provider"] == "entra"
        assert flow["redirect"] == "/dashboard"
        assert flow["state"] and flow["nonce"] and flow["code_verifier"]


@pytest.mark.django_db
class TestOidcCallbackEndpoint:
    def _seed_flow(self, client, **overrides):
        session = client.session
        flow = {
            "provider": "entra",
            "state": "st",
            "nonce": "nonce123",
            "code_verifier": "ver",
            "redirect": "/",
        }
        flow.update(overrides)
        session["oidc_flow"] = flow
        session.save()

    @override_settings(
        OIDC_ENABLED=True,
        OIDC_PROVIDERS={"entra": PROVIDER},
        OIDC_AUTO_CREATE_USERS=True,
        OIDC_LINK_BY_VERIFIED_EMAIL=False,
    )
    @patch("user.api.v1.ninja.validate_id_token")
    @patch("user.api.v1.ninja.exchange_code", return_value="id-token")
    def test_successful_callback_logs_user_in(self, _exchange, mock_validate, client):
        mock_validate.return_value = make_claims()
        self._seed_flow(client)
        response = client.get("/api/v1/user/oidc/entra/callback?code=abc&state=st")
        assert response.status_code == 302
        assert response["Location"] == "/"
        assert ExternalIdentity.objects.filter(subject="subject-xyz").exists()
        assert client.session.get("_auth_user_id")

    @override_settings(OIDC_ENABLED=True, OIDC_PROVIDERS={"entra": PROVIDER})
    def test_state_mismatch_redirects_with_reason(self, client):
        self._seed_flow(client)
        response = client.get("/api/v1/user/oidc/entra/callback?code=abc&state=WRONG")
        assert response.status_code == 302
        assert "reason=state_mismatch" in response["Location"]
        assert not client.session.get("_auth_user_id")

    @override_settings(
        OIDC_ENABLED=True,
        OIDC_PROVIDERS={"entra": dict(PROVIDER, allowed_domains=["hospital-a.org"])},
        OIDC_AUTO_CREATE_USERS=True,
    )
    @patch("user.api.v1.ninja.validate_id_token")
    @patch("user.api.v1.ninja.exchange_code", return_value="id-token")
    def test_foreign_domain_redirects_with_reason(self, _exchange, mock_validate, client):
        mock_validate.return_value = make_claims(email="bob@evil.com")
        self._seed_flow(client)
        response = client.get("/api/v1/user/oidc/entra/callback?code=abc&state=st")
        assert response.status_code == 302
        assert "reason=domain_not_allowed" in response["Location"]
        assert not ExternalIdentity.objects.exists()
