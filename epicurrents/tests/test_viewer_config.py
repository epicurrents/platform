"""Tests for the viewer-config seed loader, effective merge, and GET/PUT API."""

import json

import pytest

from activity.models import Activity
from epicurrents import viewer_config
from epicurrents.models import ViewerConfigOverride

URL = "/api/v1/viewer-config"


@pytest.fixture
def project_env(monkeypatch, tmp_path):
    """Activate a throwaway project with a controllable seed directory."""
    monkeypatch.setenv("EPICURRENTS_PROJECT", "testproj")
    monkeypatch.setattr(viewer_config, "_PROJECTS_DIR", tmp_path)
    proj_dir = tmp_path / "testproj"
    proj_dir.mkdir()
    return proj_dir


def _write_seed(proj_dir, data):
    (proj_dir / "viewer-config.json").write_text(json.dumps(data))


def _make_staff(make_user):
    staff = make_user()
    staff.is_staff = True
    staff.save()
    return staff


class TestSeedLoader:
    def test_missing_file_returns_empty(self, project_env):
        assert viewer_config.load_viewer_config_seed("testproj") == {}

    def test_valid_seed(self, project_env):
        _write_seed(project_env, {"eeg.defaultMontage": "lon"})
        assert viewer_config.load_viewer_config_seed("testproj") == {"eeg.defaultMontage": "lon"}

    def test_malformed_seed_returns_empty(self, project_env):
        (project_env / "viewer-config.json").write_text("{ not json")
        assert viewer_config.load_viewer_config_seed("testproj") == {}

    def test_non_dict_seed_returns_empty(self, project_env):
        _write_seed(project_env, ["a", "b"])
        assert viewer_config.load_viewer_config_seed("testproj") == {}

    def test_no_active_project_returns_empty(self):
        assert viewer_config.load_viewer_config_seed("") == {}


@pytest.mark.django_db
class TestEffectiveMerge:
    def test_overrides_win_over_seed(self, project_env):
        _write_seed(project_env, {"eeg.defaultMontage": "rec", "eeg.trends.amplitude.epochLength": 5})
        ViewerConfigOverride.objects.create(project="testproj", overrides={"eeg.defaultMontage": "lon"})
        assert viewer_config.get_effective_viewer_config() == {
            "eeg.defaultMontage": "lon",
            "eeg.trends.amplitude.epochLength": 5,
        }

    def test_no_overrides_is_just_seed(self, project_env):
        _write_seed(project_env, {"eeg.defaultMontage": "rec"})
        assert viewer_config.get_effective_viewer_config() == {"eeg.defaultMontage": "rec"}


@pytest.mark.django_db
class TestGetEndpoint:
    def test_requires_auth(self, client, project_env):
        assert client.get(URL).status_code == 401

    def test_returns_seed_overrides_effective(self, client, user, project_env):
        _write_seed(project_env, {"eeg.defaultMontage": "rec"})
        ViewerConfigOverride.objects.create(project="testproj", overrides={"eeg.defaultMontage": "lon"})
        client.force_login(user)
        body = client.get(URL).json()
        assert body["seed"] == {"eeg.defaultMontage": "rec"}
        assert body["overrides"] == {"eeg.defaultMontage": "lon"}
        assert body["effective"] == {"eeg.defaultMontage": "lon"}


@pytest.mark.django_db
class TestPutEndpoint:
    def _put(self, client, data):
        return client.put(URL, json.dumps(data), content_type="application/json")

    def test_regular_user_forbidden(self, client, user, project_env):
        client.force_login(user)
        assert self._put(client, {"eeg.defaultMontage": "lon"}).status_code == 403
        assert not ViewerConfigOverride.objects.exists()

    def test_staff_can_write(self, client, make_user, project_env):
        client.force_login(_make_staff(make_user))
        resp = self._put(client, {"eeg.defaultMontage": "lon"})
        assert resp.status_code == 200
        assert ViewerConfigOverride.objects.get(project="testproj").overrides == {"eeg.defaultMontage": "lon"}
        assert resp.json()["effective"]["eeg.defaultMontage"] == "lon"

    def test_write_replaces_previous_overrides(self, client, make_user, project_env):
        ViewerConfigOverride.objects.create(project="testproj", overrides={"eeg.defaultMontage": "rec"})
        client.force_login(_make_staff(make_user))
        self._put(client, {"eeg.trends.amplitude.epochLength": 2})
        row = ViewerConfigOverride.objects.get(project="testproj")
        assert row.overrides == {"eeg.trends.amplitude.epochLength": 2}

    def test_non_object_body_rejected(self, client, make_user, project_env):
        client.force_login(_make_staff(make_user))
        assert self._put(client, ["a"]).status_code == 400

    def test_empty_object_clears_overrides(self, client, make_user, project_env):
        ViewerConfigOverride.objects.create(project="testproj", overrides={"eeg.defaultMontage": "lon"})
        client.force_login(_make_staff(make_user))
        resp = self._put(client, {})
        assert resp.status_code == 200
        assert ViewerConfigOverride.objects.get(project="testproj").overrides == {}

    def test_audited_with_update_verb(self, client, make_user, project_env):
        client.force_login(_make_staff(make_user))
        self._put(client, {"eeg.defaultMontage": "lon"})
        assert Activity.objects.filter(verb="epicurrents.viewer_config.update").exists()
