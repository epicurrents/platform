"""Tests for the static lead-field generator command.

The MNE forward computation is mocked (no MNE / no minutes of compute), so these
exercise the command's own logic: raw-blob output, the manifest (with everything a
client needs to slice the blob), content-addressed filenames, and the sweep that keeps
the static dir to what the manifest names.
"""

import json
from unittest import mock

import numpy as np
import pytest
from django.core.management import call_command

from compute.models import LeadFieldCache

_LF = np.arange(6, dtype="<f8").reshape(2, 3)
_SP = np.arange(9, dtype="<f8").reshape(3, 3)


def _run(tmp_path, lead_field=_LF, src_pos=_SP, channels=("Fp1", "Fp2"), n_orient=1):
    # The orientation is passed explicitly so these cases keep testing the filename and cleanup
    # behaviour they were written for whatever the command's own default becomes; the default is
    # covered on its own below.
    with mock.patch(
        "compute.eeg.forward.compute_eeg_lead_field",
        return_value=(lead_field, src_pos, list(channels), 0),
    ):
        call_command(
            "generate_static_leadfields",
            "standard_1020",
            "--output-dir",
            str(tmp_path),
            "--n-orient",
            str(n_orient),
        )


@pytest.mark.django_db
def test_writes_raw_blob_and_manifest(tmp_path):
    _run(tmp_path)

    files = list(tmp_path.glob("standard_1020_fixed_7.5mm.*.bin"))
    assert len(files) == 1
    blob = files[0].read_bytes()

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    entry = manifest["entries"][0]
    assert entry["montage_name"] == "standard_1020"
    assert entry["file"] == files[0].name
    assert entry["content_hash"] in files[0].name
    assert entry["n_channels"] == 2 and entry["n_sources"] == 3
    assert entry["channel_names"] == ["Fp1", "Fp2"]

    # The blob slices exactly as the manifest describes.
    lf = np.frombuffer(blob[: entry["lead_field_bytes"]], dtype="<f8").reshape(2, 3)
    sp = np.frombuffer(blob[entry["lead_field_bytes"] :], dtype="<f8").reshape(3, 3)
    assert np.array_equal(lf, _LF)
    assert np.array_equal(sp, _SP)

    # The row is cached too, so /data/ can serve the identical field ad-hoc.
    assert LeadFieldCache.objects.filter(montage_name="standard_1020").exists()


@pytest.mark.django_db
def test_recompute_busts_the_hash_and_removes_the_stale_file(tmp_path):
    _run(tmp_path)
    first = next(tmp_path.glob("standard_1020_fixed_7.5mm.*.bin"))

    _run(tmp_path, lead_field=_LF + 1.0)  # different bytes → new hash
    files = list(tmp_path.glob("standard_1020_fixed_7.5mm.*.bin"))
    assert len(files) == 1
    assert files[0].name != first.name


@pytest.mark.django_db
def test_default_orientation_is_free(tmp_path):
    # A bundle generated with no arguments is what both deploy scripts produce, through the
    # generate_compute_static umbrella, which passes nothing. The viewer asks for three
    # orientations, so a fixed default would have every deployment serving a bundle its own viewer
    # cannot use, and the tool would report every montage unavailable while the bundle looked fine.
    with mock.patch(
        "compute.eeg.forward.compute_eeg_lead_field",
        return_value=(_LF, _SP, ["Fp1", "Fp2"], 0),
    ) as forward:
        call_command("generate_static_leadfields", "standard_1020", "--output-dir", str(tmp_path))

    assert forward.call_args.kwargs["n_orient"] == 3
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["entries"][0]["n_orient"] == 3
    assert len(list(tmp_path.glob("standard_1020_free_7.5mm.*.bin"))) == 1


@pytest.mark.django_db
def test_sweep_removes_a_blob_left_by_another_orientation(tmp_path):
    # The orientation label is part of a blob's filename, so a run at a different setting cannot
    # find the previous blob by the name it would itself write. Sweeping against the manifest is
    # what removes it: matching on the run's own name leaves the earlier blob on disk, referenced
    # by nothing and deleted by nothing, on every deployment that changes the setting.
    _run(tmp_path, n_orient=1)
    assert len(list(tmp_path.glob("standard_1020_fixed_7.5mm.*.bin"))) == 1

    _run(tmp_path, n_orient=3)
    assert list(tmp_path.glob("standard_1020_fixed_7.5mm.*.bin")) == []
    assert len(list(tmp_path.glob("*.bin"))) == 1
