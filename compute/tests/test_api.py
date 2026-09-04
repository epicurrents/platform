"""Tests for the compute REST API — lead field metadata and binary download."""

import json

import numpy as np
import pytest
from django.test import Client

from compute.models import LeadFieldCache

LIST_URL = "/compute/api/v1/eeg/leadfield/"
TRIGGER_URL = "/compute/api/v1/eeg/leadfield/"
META_URL = "/compute/api/v1/eeg/leadfield/{montage}/?"
DATA_URL = "/compute/api/v1/eeg/leadfield/{montage}/data/?"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dummy_lead_field(n_ch=19, n_src=150, n_orient=1):
    """Return a minimal LeadFieldCache without hitting MNE."""
    rng = np.random.default_rng(0)
    lf = rng.standard_normal((n_ch, n_src * n_orient)).astype(np.float64)
    sp = rng.standard_normal((n_src, 3)).astype(np.float64) * 0.04
    return LeadFieldCache.objects.create(
        montage_name="standard_1020",
        n_channels=n_ch,
        n_sources=n_src,
        n_orient=n_orient,
        grid_resolution_mm=7.5,
        sphere_radius_m=0.09,
        sphere_center_m=[0.0, 0.0, 0.04],
        channel_names=[f"Ch{i}" for i in range(n_ch)],
        lead_field=lf.tobytes(),
        src_pos=sp.tobytes(),
    )


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_list_requires_staff(auth_client):
    c, _ = auth_client
    resp = c.get(LIST_URL)
    assert resp.status_code == 403


@pytest.mark.django_db
def test_list_returns_cached_entries(superuser_client):
    _make_dummy_lead_field()
    c, _ = superuser_client
    resp = c.get(LIST_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["montage_name"] == "standard_1020"
    assert data[0]["n_channels"] == 19
    assert "lead_field" not in data[0]


# ---------------------------------------------------------------------------
# Metadata endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_meta_requires_auth(client):
    _make_dummy_lead_field()
    resp = client.get(META_URL.format(montage="standard_1020") + "n_orient=1&grid_resolution_mm=7.5")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_meta_returns_correct_fields(auth_client):
    _make_dummy_lead_field()
    c, _ = auth_client
    resp = c.get(META_URL.format(montage="standard_1020") + "n_orient=1&grid_resolution_mm=7.5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["montage_name"] == "standard_1020"
    assert data["n_channels"] == 19
    assert data["n_sources"] == 150
    assert data["n_orient"] == 1
    assert len(data["channel_names"]) == 19
    assert "lead_field" not in data


@pytest.mark.django_db
def test_meta_404_when_absent(auth_client):
    c, _ = auth_client
    resp = c.get(META_URL.format(montage="biosemi256") + "n_orient=1&grid_resolution_mm=7.5")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Binary download endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_data_requires_auth(client):
    _make_dummy_lead_field()
    resp = client.get(DATA_URL.format(montage="standard_1020") + "n_orient=1&grid_resolution_mm=7.5")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_data_headers_and_body(auth_client):
    n_ch, n_src, n_orient = 19, 150, 1
    row = _make_dummy_lead_field(n_ch, n_src, n_orient)
    c, _ = auth_client

    resp = c.get(DATA_URL.format(montage="standard_1020") + "n_orient=1&grid_resolution_mm=7.5")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/octet-stream"
    assert int(resp["X-N-Channels"]) == n_ch
    assert int(resp["X-N-Sources"]) == n_src
    assert int(resp["X-N-Orient"]) == n_orient

    lf_bytes_len = int(resp["X-LeadField-Bytes"])
    sp_bytes_len = int(resp["X-SrcPos-Bytes"])
    assert lf_bytes_len == n_ch * n_src * n_orient * 8
    assert sp_bytes_len == n_src * 3 * 8

    body = b"".join(resp.streaming_content) if resp.streaming else resp.content
    assert len(body) == lf_bytes_len + sp_bytes_len

    # Reconstruct and compare with stored values
    lf_rt = np.frombuffer(body[:lf_bytes_len], dtype=np.float64).reshape(n_ch, n_src * n_orient)
    sp_rt = np.frombuffer(body[lf_bytes_len:], dtype=np.float64).reshape(n_src, 3)

    lf_orig = np.frombuffer(bytes(row.lead_field), dtype=np.float64).reshape(n_ch, n_src * n_orient)
    sp_orig = np.frombuffer(bytes(row.src_pos), dtype=np.float64).reshape(n_src, 3)

    np.testing.assert_array_equal(lf_rt, lf_orig)
    np.testing.assert_array_equal(sp_rt, sp_orig)


@pytest.mark.django_db
def test_data_404_when_absent(auth_client):
    c, _ = auth_client
    resp = c.get(DATA_URL.format(montage="biosemi256") + "n_orient=1&grid_resolution_mm=7.5")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Trigger endpoint (staff only — mocks MNE to avoid slow compute)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_trigger_requires_staff(auth_client):
    c, _ = auth_client
    resp = c.post(
        TRIGGER_URL,
        data=json.dumps({"montage_name": "standard_1020"}),
        content_type="application/json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_trigger_conflict_without_force(superuser_client):
    _make_dummy_lead_field()
    c, _ = superuser_client
    resp = c.post(
        TRIGGER_URL,
        data=json.dumps({"montage_name": "standard_1020", "grid_resolution_mm": 7.5, "n_orient": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 409


@pytest.mark.django_db
def test_trigger_invalid_n_orient(superuser_client):
    """Invalid n_orient is rejected at the schema layer (Literal[1, 3] → 422)."""
    c, _ = superuser_client
    resp = c.post(
        TRIGGER_URL,
        data=json.dumps({"montage_name": "standard_1020", "n_orient": 2}),
        content_type="application/json",
    )
    assert resp.status_code == 422


@pytest.mark.django_db
def test_meta_invalid_n_orient_query_param(auth_client):
    """Invalid n_orient on the metadata endpoint is rejected at the schema layer."""
    c, _ = auth_client
    resp = c.get(META_URL.format(montage="standard_1020") + "n_orient=2&grid_resolution_mm=7.5")
    assert resp.status_code == 422


@pytest.mark.django_db
def test_trigger_invalid_sphere_center_length(superuser_client):
    """sphere_center_m with wrong length is rejected at the schema layer."""
    c, _ = superuser_client
    resp = c.post(
        TRIGGER_URL,
        data=json.dumps({"montage_name": "standard_1020", "sphere_center_m": [0.0, 0.0]}),
        content_type="application/json",
    )
    assert resp.status_code == 422


@pytest.mark.django_db
def test_trigger_force_requires_superuser(make_user):
    """A staff user (non-superuser) can compute fresh rows but cannot use force=true."""
    staff = make_user(username="staffer", is_staff=True)
    c = Client()
    c.force_login(staff)
    _make_dummy_lead_field()
    resp = c.post(
        TRIGGER_URL,
        data=json.dumps({"montage_name": "standard_1020", "force": True}),
        content_type="application/json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_trigger_force_replaces_existing_returns_200(superuser_client, monkeypatch):
    """Force=true on an existing row upserts in place and returns 200.

    Mocks the MNE compute path so the test doesn't depend on MNE being installed
    or hit real numerical work. Also asserts that ``created_at`` is preserved
    across the replace while ``updated_at`` advances.
    """
    import time

    from compute.api.v1 import ninja as compute_ninja

    original = _make_dummy_lead_field(n_ch=19, n_src=150, n_orient=1)
    original_id = original.pk
    original_created_at = original.created_at
    original_updated_at = original.updated_at

    rng = np.random.default_rng(1)
    fake_lf = rng.standard_normal((19, 150)).astype(np.float64)
    fake_sp = rng.standard_normal((150, 3)).astype(np.float64) * 0.04
    fake_names = [f"Ch{i}" for i in range(19)]

    def _fake_compute(**_kwargs):
        return fake_lf, fake_sp, fake_names, 0

    monkeypatch.setattr(compute_ninja, "compute_eeg_lead_field", _fake_compute, raising=False)
    # The handler imports lazily inside the function; patch the source module too.
    import compute.eeg.forward as forward_mod

    monkeypatch.setattr(forward_mod, "compute_eeg_lead_field", _fake_compute, raising=False)

    time.sleep(0.01)  # ensure updated_at can advance past auto_now's resolution
    c, _ = superuser_client
    resp = c.post(
        TRIGGER_URL,
        data=json.dumps({"montage_name": "standard_1020", "force": True}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == original_id  # same row, not a fresh PK
    refreshed = LeadFieldCache.objects.get(pk=original_id)
    assert refreshed.created_at == original_created_at
    assert refreshed.updated_at > original_updated_at
    assert LeadFieldCache.objects.count() == 1


@pytest.mark.django_db
def test_trigger_create_returns_201(superuser_client, monkeypatch):
    """Fresh create returns 201 with the new row's metadata."""
    import compute.eeg.forward as forward_mod
    from compute.api.v1 import ninja as compute_ninja

    rng = np.random.default_rng(2)
    fake_lf = rng.standard_normal((19, 150)).astype(np.float64)
    fake_sp = rng.standard_normal((150, 3)).astype(np.float64) * 0.04
    fake_names = [f"Ch{i}" for i in range(19)]

    def _fake_compute(**_kwargs):
        return fake_lf, fake_sp, fake_names, 0

    monkeypatch.setattr(compute_ninja, "compute_eeg_lead_field", _fake_compute, raising=False)
    monkeypatch.setattr(forward_mod, "compute_eeg_lead_field", _fake_compute, raising=False)

    c, _ = superuser_client
    resp = c.post(
        TRIGGER_URL,
        data=json.dumps({"montage_name": "standard_1020"}),
        content_type="application/json",
    )
    assert resp.status_code == 201
    assert LeadFieldCache.objects.count() == 1
