"""The user agreement: publishing, the gate, the endpoints and the records it keeps."""

import io

import pytest
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import Client

from activity.models import Activity
from conftest import post_json
from user import agreement as user_agreement
from user.models import UserAgreement, UserAgreementAcceptance

GATED = "/api/v1/user/preferences"
ME = "/api/v1/user/me"
AGREEMENT = "/api/v1/user/agreement"
ACCEPT = "/api/v1/user/agreement/accept"


@pytest.fixture(autouse=True)
def _clear_binding_cache():
    cache.delete(user_agreement.BINDING_CACHE_KEY)
    yield
    cache.delete(user_agreement.BINDING_CACHE_KEY)


def _publish(version="v1", text="# Terms\n\nBe kind.", reaccept=True):
    agreement = user_agreement.publish(version, text, reaccept=reaccept)
    cache.delete(user_agreement.BINDING_CACHE_KEY)
    return agreement


@pytest.mark.django_db
class TestGate:
    def test_inert_until_published(self, auth_client):
        client, _user = auth_client
        assert client.get(GATED).status_code == 200

    def test_refuses_an_account_that_has_not_accepted(self, auth_client):
        client, _user = auth_client
        _publish()
        response = client.get(GATED)
        assert response.status_code == 403
        assert response.json()["code"] == user_agreement.AGREEMENT_REQUIRED
        assert response["Cache-Control"] == "no-store"

    def test_refusal_writes_no_activity(self, auth_client):
        client, _user = auth_client
        _publish()
        before = Activity.objects.count()
        client.get(GATED)
        assert Activity.objects.count() == before

    def test_gates_every_api_mount(self, auth_client):
        client, _user = auth_client
        _publish()
        for path in ("/api/v1/library/datasets/", "/recordings/api/v1/", "/annotations/api/v1/bundles/"):
            assert client.get(path).status_code == 403, path

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", ME),
            ("GET", AGREEMENT),
            ("POST", "/api/v1/user/logout"),
            ("POST", "/api/v1/user/login"),
            ("POST", "/api/v1/user/login/2fa"),
            ("POST", "/api/v1/user/login/2fa/setup"),
            ("GET", "/api/v1/user/auth-config"),
            ("POST", "/api/v1/user/reset-password"),
            ("POST", "/api/v1/user/reset-password/confirm"),
            ("GET", "/api/v1/user/oidc/entra/start"),
            ("GET", "/api/v1/user/oidc/entra/callback"),
            ("DELETE", "/api/v1/notifications/subscribe"),
        ],
    )
    def test_exempt_routes_are_not_refused(self, auth_client, method, path):
        client, _user = auth_client
        _publish()
        response = client.generic(method, path, "{}", content_type="application/json")
        assert response.status_code != 403 or response.json().get("code") != user_agreement.AGREEMENT_REQUIRED

    @pytest.mark.parametrize(
        ("method", "path"),
        [("PATCH", ME), ("POST", "/api/v1/notifications/subscribe"), ("POST", "/api/v1/user/me/change-password")],
    )
    def test_an_exempt_path_is_exempt_for_its_methods_only(self, auth_client, method, path):
        client, _user = auth_client
        _publish()
        response = client.generic(method, path, "{}", content_type="application/json")
        assert response.status_code == 403
        assert response.json()["code"] == user_agreement.AGREEMENT_REQUIRED

    def test_a_registered_route_is_exempt(self, auth_client, monkeypatch):
        client, _user = auth_client
        _publish()
        monkeypatch.setattr(user_agreement, "_EXEMPT_ROUTES", list(user_agreement._EXEMPT_ROUTES))
        user_agreement.register_exempt_route(r"/api/v1/user/preferences", methods=["GET"])
        assert client.get(GATED).status_code == 200
        assert client.put(GATED, "{}", content_type="application/json").status_code == 403

    def test_a_route_pattern_matches_the_whole_path(self, auth_client):
        client, _user = auth_client
        _publish()
        assert client.get(ME + "/2fa").status_code == 403

    def test_operational_paths_answer(self, auth_client):
        client, _user = auth_client
        _publish()
        assert client.get("/api/v1/health").status_code == 200

    def test_anonymous_callers_are_not_gated(self, db):
        _publish()
        response = Client().get(GATED)
        assert response.status_code == 401

    def test_non_api_paths_are_not_gated(self, auth_client):
        client, _user = auth_client
        _publish()
        assert client.get("/").status_code != 403

    def test_accepting_opens_the_api(self, auth_client):
        client, _user = auth_client
        _publish()
        assert post_json(client, ACCEPT, {"version": "v1"}).status_code == 200
        assert client.get(GATED).status_code == 200

    def test_a_new_version_asks_again(self, auth_client):
        client, _user = auth_client
        _publish()
        post_json(client, ACCEPT, {"version": "v1"})
        assert client.get(GATED).status_code == 200
        _publish("v2", "# Terms\n\nBe kinder.")
        assert client.get(GATED).status_code == 403

    def test_a_correction_does_not_ask_again(self, auth_client):
        client, _user = auth_client
        _publish()
        post_json(client, ACCEPT, {"version": "v1"})
        _publish("v1.1", "# Terms\n\nBe kind, please.", reaccept=False)
        assert client.get(GATED).status_code == 200

    def test_a_new_account_after_a_correction_accepts_the_correction(self, auth_client):
        client, user = auth_client
        _publish()
        _publish("v1.1", "# Terms\n\nBe kind, please.", reaccept=False)
        assert client.get(GATED).status_code == 403
        assert post_json(client, ACCEPT, {"version": "v1"}).status_code == 409
        assert post_json(client, ACCEPT, {"version": "v1.1"}).status_code == 200
        assert client.get(GATED).status_code == 200

    def test_acceptance_in_another_session_counts(self, user):
        _publish()
        first, second = Client(), Client()
        first.force_login(user)
        second.force_login(user)
        assert second.get(GATED).status_code == 403
        post_json(first, ACCEPT, {"version": "v1"})
        assert second.get(GATED).status_code == 200

    def test_an_exempt_account_is_not_gated(self, auth_client, monkeypatch):
        client, user = auth_client
        _publish()
        monkeypatch.setattr(user_agreement, "_EXEMPTIONS", [lambda candidate: candidate.pk == user.pk])
        assert client.get(GATED).status_code == 200
        assert client.get(ME).json()["user"]["agreement_required"] is False

    def test_an_exemption_is_not_remembered_in_the_session(self, auth_client, monkeypatch):
        client, user = auth_client
        _publish()
        exempt = {user.pk}
        monkeypatch.setattr(user_agreement, "_EXEMPTIONS", [lambda candidate: candidate.pk in exempt])
        assert client.get(GATED).status_code == 200
        exempt.clear()
        assert client.get(GATED).status_code == 403

    def test_an_exempt_account_cannot_accept(self, auth_client, monkeypatch):
        client, user = auth_client
        _publish()
        monkeypatch.setattr(user_agreement, "_EXEMPTIONS", [lambda candidate: candidate.pk == user.pk])
        assert post_json(client, ACCEPT, {"version": "v1"}).status_code == 409
        assert not UserAgreementAcceptance.objects.exists()

    def test_an_unreachable_cache_falls_back_to_the_database(self, auth_client, monkeypatch):
        client, _user = auth_client
        _publish()

        def unreachable(*args, **kwargs):
            raise ConnectionError("cache down")

        monkeypatch.setattr(user_agreement.cache, "get", unreachable)
        monkeypatch.setattr(user_agreement.cache, "set", unreachable)
        assert client.get(GATED).status_code == 403
        assert client.get(ME).status_code == 200

    def test_superusers_are_gated_too(self, superuser_client):
        client, _user = superuser_client
        _publish()
        assert client.get(GATED).status_code == 403


@pytest.mark.django_db
class TestEndpoints:
    def test_agreement_reads_without_a_session(self, db):
        _publish()
        body = Client().get(AGREEMENT).json()
        assert body["published"] is True
        assert body["version"] == "v1"
        assert body["text"] == "# Terms\n\nBe kind."
        assert body["accepted"] is False

    def test_agreement_before_publication(self, auth_client):
        client, _user = auth_client
        body = client.get(AGREEMENT).json()
        assert body == {
            "published": False,
            "version": None,
            "text": None,
            "published_at": None,
            "accepted": True,
            "accepted_version": None,
            "accepted_at": None,
        }

    def test_agreement_shows_the_callers_acceptance(self, auth_client):
        client, _user = auth_client
        _publish()
        post_json(client, ACCEPT, {"version": "v1"})
        _publish("v1.1", "corrected", reaccept=False)
        body = client.get(AGREEMENT).json()
        assert body["version"] == "v1.1"
        assert body["accepted"] is True
        assert body["accepted_version"] == "v1"

    def test_me_carries_the_flag(self, auth_client):
        client, _user = auth_client
        assert client.get(ME).json()["user"]["agreement_required"] is False
        _publish()
        assert client.get(ME).json()["user"]["agreement_required"] is True
        response = post_json(client, ACCEPT, {"version": "v1"})
        assert response.json()["agreement_required"] is False

    def test_accept_records_the_version_and_digest(self, auth_client):
        client, user = auth_client
        agreement = _publish()
        post_json(client, ACCEPT, {"version": "v1"})
        row = UserAgreementAcceptance.objects.get(user=user)
        assert row.agreement == agreement
        assert row.version == "v1"
        assert row.digest == user_agreement.text_digest("# Terms\n\nBe kind.")

    def test_accepting_twice_records_once(self, auth_client):
        client, user = auth_client
        _publish()
        post_json(client, ACCEPT, {"version": "v1"})
        post_json(client, ACCEPT, {"version": "v1"})
        assert UserAgreementAcceptance.objects.filter(user=user).count() == 1

    def test_accept_refuses_a_stale_version(self, auth_client):
        client, user = auth_client
        _publish()
        _publish("v2", "changed")
        assert post_json(client, ACCEPT, {"version": "v1"}).status_code == 409
        assert not UserAgreementAcceptance.objects.filter(user=user).exists()

    def test_accept_with_nothing_published(self, auth_client):
        client, _user = auth_client
        assert post_json(client, ACCEPT, {"version": "v1"}).status_code == 404

    def test_accept_needs_a_session(self, db):
        _publish()
        assert post_json(Client(), ACCEPT, {"version": "v1"}).status_code == 401

    def test_accept_is_audited_without_the_label(self, auth_client):
        client, _user = auth_client
        agreement = _publish()
        post_json(client, ACCEPT, {"version": "v1"})
        activity = Activity.objects.filter(verb="user.agreement.accept").get()
        assert activity.target_object_id == str(agreement.pk)
        assert activity.metadata == {"digest": agreement.digest}


@pytest.mark.django_db
class TestPublish:
    def test_from_stdin(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("# Terms\n"))
        call_command("publish_agreement", "--label", "v1", stdout=io.StringIO())
        agreement = UserAgreement.objects.get()
        assert agreement.text == "# Terms\n"
        assert agreement.reaccept is True
        activity = Activity.objects.filter(verb="user.agreement.publish").get()
        assert activity.metadata == {"agreement_id": agreement.pk, "digest": agreement.digest, "reaccept": True}

    def test_from_a_file(self, tmp_path):
        path = tmp_path / "agreement.md"
        path.write_text("# Terms\n", encoding="utf-8")
        call_command("publish_agreement", str(path), "--label", "v1", stdout=io.StringIO())
        assert UserAgreement.objects.get().digest == user_agreement.text_digest("# Terms\n")

    def test_refuses_a_published_label(self, monkeypatch):
        _publish()
        monkeypatch.setattr("sys.stdin", io.StringIO("x"))
        with pytest.raises(CommandError, match="already published"):
            call_command("publish_agreement", "--label", "v1", stdout=io.StringIO())
        assert not Activity.objects.filter(verb="user.agreement.publish").exists()

    def test_refuses_an_over_long_label(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("# Terms\n"))
        with pytest.raises(CommandError, match="longer than"):
            call_command("publish_agreement", "--label", "v" * 65, stdout=io.StringIO())
        assert not Activity.objects.filter(verb="user.agreement.publish").exists()

    def test_refuses_an_empty_text(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("  \n"))
        with pytest.raises(CommandError, match="empty"):
            call_command("publish_agreement", "--label", "v1", stdout=io.StringIO())
        assert not UserAgreement.objects.exists()
        assert not Activity.objects.filter(verb="user.agreement.publish").exists()

    def test_a_correction_needs_a_published_version(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("# Terms\n"))
        with pytest.raises(CommandError, match="none is published"):
            call_command("publish_agreement", "--label", "v1", "--no-reaccept", stdout=io.StringIO())

    def test_publishing_clears_the_binding_cache(self, monkeypatch, auth_client, django_capture_on_commit_callbacks):
        client, _user = auth_client
        assert client.get(GATED).status_code == 200
        monkeypatch.setattr("sys.stdin", io.StringIO("# Terms\n"))
        with django_capture_on_commit_callbacks(execute=True):
            call_command("publish_agreement", "--label", "v1", stdout=io.StringIO())
        assert client.get(GATED).status_code == 403


@pytest.mark.django_db
class TestRecords:
    def test_acceptances_go_with_the_account(self, user):
        agreement = _publish()
        user_agreement.accept(user, agreement)
        user.delete()
        assert not UserAgreementAcceptance.objects.exists()
        assert UserAgreement.objects.filter(pk=agreement.pk).exists()

    def test_an_accepted_version_cannot_be_deleted(self, user):
        from django.db.models import ProtectedError

        agreement = _publish()
        user_agreement.accept(user, agreement)
        with pytest.raises(ProtectedError):
            agreement.delete()

    def test_rollback_is_refused_for_the_acceptance(self, auth_client):
        from django.contrib.contenttypes.models import ContentType

        from activity.audit import rollback_refusal
        from activity.models import ObjectChangeLog

        client, _user = auth_client
        _publish()
        post_json(client, ACCEPT, {"version": "v1"})
        change = ObjectChangeLog.objects.get(content_type=ContentType.objects.get_for_model(UserAgreementAcceptance))
        assert rollback_refusal(change)

    def test_rollback_is_refused_for_a_published_version(self, monkeypatch):
        from django.contrib.contenttypes.models import ContentType

        from activity.audit import rollback_refusal
        from activity.models import ObjectChangeLog

        monkeypatch.setattr("sys.stdin", io.StringIO("# Terms\n"))
        call_command("publish_agreement", "--label", "v1", stdout=io.StringIO())
        change = ObjectChangeLog.objects.get(content_type=ContentType.objects.get_for_model(UserAgreement))
        assert rollback_refusal(change)


def test_middleware_sits_between_authentication_and_audit(settings):
    order = list(settings.MIDDLEWARE)
    gate = order.index("user.agreement.AgreementGateMiddleware")
    assert order.index("django.contrib.auth.middleware.AuthenticationMiddleware") < gate
    assert gate < order.index("epicurrents.middleware.ApiActivityLoggingMiddleware")
