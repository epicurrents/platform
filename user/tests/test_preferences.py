"""Tests for the client-preferences endpoints.

The blob these endpoints store is opaque to the platform, so what is worth
pinning is everything *around* the blob: that it is scoped per user and per
client, that a write replaces rather than merges, and that the shape check
holds the field to being a settings map. The last one matters more than it
looks — the blob is rewritten on every settings change and every version of it
lands in the permanent audit trail, so a hole in the shape check is a hole in
what can end up there.
"""

import json

import pytest

from user.models import UserPreference

URL = "/api/v1/user/preferences"


def _put(client, payload, scope=None):
    url = URL if scope is None else f"{URL}?scope={scope}"
    return client.put(url, json.dumps(payload), content_type="application/json")


@pytest.fixture
def signed_in(auth_client):
    """The authenticated client alone — ``auth_client`` yields a (client, user) pair."""
    return auth_client[0]


@pytest.mark.django_db
class TestPreferencesAccess:
    def test_requires_authentication(self, client):
        assert client.get(URL).status_code == 401
        assert _put(client, {"settings": {}}).status_code == 401

    def test_unknown_scope_reads_as_empty(self, signed_in):
        response = signed_in.get(URL)
        assert response.status_code == 200
        assert response.json() == {"scope": "viewer", "settings": {}}

    def test_round_trip(self, signed_in):
        assert _put(signed_in, {"settings": {"eeg.defaultMontage": "avg-ous"}}).status_code == 200
        assert signed_in.get(URL).json()["settings"] == {"eeg.defaultMontage": "avg-ous"}

    def test_write_replaces_rather_than_merges(self, signed_in):
        _put(signed_in, {"settings": {"eeg.defaultMontage": "avg-ous", "eeg.channelSpacing": 1}})
        _put(signed_in, {"settings": {"eeg.defaultMontage": "lon-ous"}})
        assert signed_in.get(URL).json()["settings"] == {"eeg.defaultMontage": "lon-ous"}

    def test_scopes_are_independent(self, signed_in):
        _put(signed_in, {"settings": {"eeg.defaultMontage": "avg-ous"}}, scope="viewer")
        _put(signed_in, {"settings": {"eeg.defaultMontage": "lon-ous"}}, scope="other")
        assert signed_in.get(f"{URL}?scope=viewer").json()["settings"]["eeg.defaultMontage"] == "avg-ous"
        assert signed_in.get(f"{URL}?scope=other").json()["settings"]["eeg.defaultMontage"] == "lon-ous"

    def test_one_row_per_user_and_scope(self, signed_in, user):
        _put(signed_in, {"settings": {"eeg.defaultMontage": "avg-ous"}})
        _put(signed_in, {"settings": {"eeg.defaultMontage": "lon-ous"}})
        assert UserPreference.objects.filter(user=user, scope="viewer").count() == 1

    def test_another_user_sees_their_own(self, signed_in, make_user, client):
        _put(signed_in, {"settings": {"eeg.defaultMontage": "avg-ous"}})
        other = make_user()
        client.force_login(other)
        assert client.get(URL).json()["settings"] == {}


@pytest.mark.django_db
class TestPreferencesValidation:
    @pytest.mark.parametrize(
        "settings_map",
        [
            {"notasettingpath": True},
            {"eeg.": True},
            {".defaultMontage": True},
            {"9eeg.defaultMontage": True},
            {"eeg.default Montage": True},
            {"eeg.defaultMontage": {"nested": "object"}},
            {"eeg.derivations": [{"nested": "object"}]},
            {"eeg.defaultMontage": "x" * 2000},
            {"eeg.derivations": ["x"] * 100},
        ],
    )
    def test_rejects_malformed_settings(self, signed_in, settings_map):
        assert _put(signed_in, {"settings": settings_map}).status_code == 400

    @pytest.mark.parametrize(
        "value",
        [None, True, 3, 1.5, "avg-ous", [1, 2, 3], ["a", "b"], [0.1, 0.2, 0.3, 1.0]],
    )
    def test_accepts_primitives_and_flat_lists(self, signed_in, value):
        assert _put(signed_in, {"settings": {"eeg.someField": value}}).status_code == 200
        assert signed_in.get(URL).json()["settings"]["eeg.someField"] == value

    def test_rejects_a_blob_over_the_total_size_cap(self, signed_in):
        """The per-field bounds multiply out to tens of megabytes, and every accepted blob is
        written to the permanent change log twice. The total cap is the one that actually holds."""
        oversized = {f"eeg.field{i}": "x" * 1000 for i in range(30)}
        assert _put(signed_in, {"settings": oversized}).status_code == 400

    def test_accepts_a_realistic_settings_map(self, signed_in):
        """The cap has to be roomy enough for a real settings map, or it is a bug rather than a
        bound — the viewer's full user-definable set is well inside it."""
        realistic = {f"eeg.field{i}": "a-montage-name-or-similar" for i in range(60)}
        assert _put(signed_in, {"settings": realistic}).status_code == 200

    def test_rejects_too_many_settings(self, signed_in):
        too_many = {f"eeg.field{i}": i for i in range(501)}
        assert _put(signed_in, {"settings": too_many}).status_code == 400

    @pytest.mark.parametrize("scope", ["", "a" * 65, "with space", "with/slash", "9leading-digit"])
    def test_rejects_invalid_scope(self, signed_in, scope):
        assert signed_in.get(f"{URL}?scope={scope}").status_code == 400

    def test_scope_is_case_normalised(self, signed_in):
        """A scope names a client, so casing is not a distinction worth keeping — folding it
        stops `Viewer` and `viewer` from becoming two silently divergent stores."""
        _put(signed_in, {"settings": {"eeg.defaultMontage": "avg-ous"}}, scope="viewer")
        response = signed_in.get(f"{URL}?scope=Viewer")
        assert response.status_code == 200
        assert response.json() == {"scope": "viewer", "settings": {"eeg.defaultMontage": "avg-ous"}}

    def test_nothing_is_stored_when_validation_fails(self, signed_in, user):
        _put(signed_in, {"settings": {"eeg.defaultMontage": "avg-ous"}})
        _put(signed_in, {"settings": {"eeg.defaultMontage": "lon-ous", "bad key": 1}})
        assert signed_in.get(URL).json()["settings"] == {"eeg.defaultMontage": "avg-ous"}


@pytest.mark.django_db
class TestPreferencesActivity:
    def test_write_records_activity(self, signed_in):
        from activity.models import Activity

        _put(signed_in, {"settings": {"eeg.defaultMontage": "avg-ous"}})
        activity = Activity.objects.filter(verb="user.preferences.update").latest("created_at")
        assert activity.metadata["scope"] == "viewer"
        assert activity.metadata["setting_count"] == 1

    def test_read_records_activity(self, signed_in):
        from activity.models import Activity

        signed_in.get(URL)
        assert Activity.objects.filter(verb="user.preferences.read").exists()
