"""Tests for the ``vendor_viewer`` management command.

The archives are built in the test and served over ``file://`` through the pin's ``url``
override, so the fetch, the checksum gate and the extraction all run for real — only the
network is absent. That is the same path a deploy takes; nothing here stubs the transfer.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from epicurrents.management.commands.vendor_viewer import STAMP_NAME

LIB = "epicurrents-lib.umd.js"


def _archive(tmp_path: Path, files: dict, *, prefix: str = "", name: str = "edition.tar.gz") -> Path:
    """Write a gzipped tar of ``files`` (relative path -> text) and return its path."""
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as archive:
        for relative, content in files.items():
            payload = content.encode()
            info = tarfile.TarInfo(f"{prefix}/{relative}" if prefix else relative)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pin(tmp_path: Path, archive: Path, *, sha256: str | None = None, target: str = ".", tag: str = "full-v1.0.0"):
    """Write a pin naming one artifact fetched from ``archive`` over file://."""
    path = tmp_path / "viewer-pin.json"
    path.write_text(
        json.dumps(
            {
                "repository": "epicurrents/app",
                "artifacts": {
                    "public": {
                        "tag": tag,
                        "archive": archive.name,
                        "sha256": sha256 or _sha256(archive),
                        "target": target,
                        "url": archive.as_uri(),
                    }
                },
            }
        )
    )
    return path


def _run(pin: Path, out: Path, *args):
    call_command("vendor_viewer", "--pin", str(pin), "--output-dir", str(out), *args)


@pytest.fixture
def out_dir(tmp_path):
    path = tmp_path / "viewer-dist"
    path.mkdir()
    return path


class TestInstall:
    def test_installs_the_pinned_archive(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib", "index.html": "page"})
        _run(_pin(tmp_path, archive), out_dir)
        assert (out_dir / LIB).read_text() == "lib"
        assert (out_dir / "index.html").read_text() == "page"

    def test_strips_a_single_wrapping_directory(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib", "index.html": "page"}, prefix="full")
        _run(_pin(tmp_path, archive), out_dir)
        assert (out_dir / LIB).is_file()
        assert not (out_dir / "full").exists()

    def test_keeps_nested_paths_that_are_not_a_common_wrapper(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib", "workers/w.js": "worker"})
        _run(_pin(tmp_path, archive), out_dir)
        assert (out_dir / "workers" / "w.js").read_text() == "worker"

    def test_honours_a_target_subdirectory(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib"})
        _run(_pin(tmp_path, archive, target="edu"), out_dir)
        assert (out_dir / "edu" / LIB).is_file()

    def test_writes_a_stamp_recording_what_was_installed(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib"})
        _run(_pin(tmp_path, archive), out_dir)
        stamp = json.loads((out_dir / STAMP_NAME).read_text())
        assert stamp["artifacts"]["public"]["tag"] == "full-v1.0.0"
        assert stamp["artifacts"]["public"]["files"] == [LIB]

    def test_an_empty_pin_installs_nothing_and_succeeds(self, tmp_path, out_dir):
        pin = tmp_path / "viewer-pin.json"
        pin.write_text(json.dumps({"repository": "epicurrents/app", "artifacts": {}}))
        _run(pin, out_dir)
        assert not (out_dir / STAMP_NAME).exists()


class TestChecksumGate:
    def test_a_mismatched_checksum_aborts(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib"})
        pin = _pin(tmp_path, archive, sha256="0" * 64)
        with pytest.raises(CommandError, match="Checksum mismatch"):
            _run(pin, out_dir)

    def test_nothing_is_written_when_the_checksum_fails(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib"})
        pin = _pin(tmp_path, archive, sha256="0" * 64)
        with pytest.raises(CommandError):
            _run(pin, out_dir)
        assert list(out_dir.iterdir()) == []


class TestReinstall:
    def test_a_second_run_is_a_no_op(self, tmp_path, out_dir, capsys):
        archive = _archive(tmp_path, {LIB: "lib"})
        pin = _pin(tmp_path, archive)
        _run(pin, out_dir)
        capsys.readouterr()
        _run(pin, out_dir)
        assert "already at full-v1.0.0" in capsys.readouterr().out

    def test_a_new_tag_replaces_the_previous_files(self, tmp_path, out_dir):
        first = _archive(tmp_path, {LIB: "old", "gone.js": "stale"}, name="a.tar.gz")
        _run(_pin(tmp_path, first, tag="full-v1.0.0"), out_dir)
        second = _archive(tmp_path, {LIB: "new"}, name="b.tar.gz")
        _run(_pin(tmp_path, second, tag="full-v1.1.0"), out_dir)
        assert (out_dir / LIB).read_text() == "new"
        assert not (out_dir / "gone.js").exists(), "a file dropped by the new edition must not linger"

    def test_reinstalling_leaves_files_this_command_does_not_own(self, tmp_path, out_dir):
        # The per-project viewer builds and the public-setup shim share this directory and
        # are written by the frontend build, so an edition swap must not take them with it.
        (out_dir / "edu").mkdir()
        (out_dir / "edu" / "epicurrents-lib.umd.cjs").write_text("project lib")
        _run(_pin(tmp_path, _archive(tmp_path, {LIB: "old"}, name="a.tar.gz"), tag="full-v1.0.0"), out_dir)
        _run(_pin(tmp_path, _archive(tmp_path, {LIB: "new"}, name="b.tar.gz"), tag="full-v1.1.0"), out_dir)
        assert (out_dir / "edu" / "epicurrents-lib.umd.cjs").read_text() == "project lib"


class TestCheck:
    def test_passes_when_the_tree_matches_the_pin(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib"})
        pin = _pin(tmp_path, archive)
        _run(pin, out_dir)
        _run(pin, out_dir, "--check")

    def test_fails_when_nothing_is_installed(self, tmp_path, out_dir):
        pin = _pin(tmp_path, _archive(tmp_path, {LIB: "lib"}))
        with pytest.raises(CommandError, match="not installed"):
            _run(pin, out_dir, "--check")

    def test_fails_when_the_pin_moved_on(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib"})
        _run(_pin(tmp_path, archive, tag="full-v1.0.0"), out_dir)
        newer = _pin(tmp_path, archive, tag="full-v1.1.0")
        with pytest.raises(CommandError, match="pin wants full-v1.1.0"):
            _run(newer, out_dir, "--check")

    def test_fails_when_an_installed_file_went_missing(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib"})
        pin = _pin(tmp_path, archive)
        _run(pin, out_dir)
        (out_dir / LIB).unlink()
        with pytest.raises(CommandError, match="missing"):
            _run(pin, out_dir, "--check")

    def test_an_empty_pin_verifies_clean(self, tmp_path, out_dir):
        # update.sh runs --check before deciding to fetch; an unpinned deployment builds the
        # edition from the checkout and must not be reported as broken.
        pin = tmp_path / "viewer-pin.json"
        pin.write_text(json.dumps({"repository": "epicurrents/app", "artifacts": {}}))
        _run(pin, out_dir, "--check")

    def test_a_corrupt_stamp_reads_as_not_installed(self, tmp_path, out_dir):
        pin = _pin(tmp_path, _archive(tmp_path, {LIB: "lib"}))
        (out_dir / STAMP_NAME).write_text("{not json")
        with pytest.raises(CommandError, match="not installed"):
            _run(pin, out_dir, "--check")


class TestArchiveSafety:
    def test_a_traversing_member_is_refused(self, tmp_path, out_dir):
        path = tmp_path / "evil.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            payload = b"pwned"
            info = tarfile.TarInfo("../escaped.js")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        # Screened before the wrapper-prefix strip, which would otherwise see ".." as the
        # one common top-level directory and quietly rewrite the member into a valid one.
        with pytest.raises(CommandError, match="escapes the destination"):
            _run(_pin(tmp_path, path), out_dir)
        assert not (out_dir.parent / "escaped.js").exists()
        assert not (out_dir / "escaped.js").exists()

    def test_an_absolute_member_is_refused(self, tmp_path, out_dir):
        path = tmp_path / "abs.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("/etc/passwd")
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))
        with pytest.raises(CommandError, match="absolute"):
            _run(_pin(tmp_path, path), out_dir)

    def test_a_target_escaping_the_output_directory_is_refused(self, tmp_path, out_dir):
        archive = _archive(tmp_path, {LIB: "lib"})
        pin = _pin(tmp_path, archive, target="../elsewhere")
        with pytest.raises(CommandError, match="outside"):
            _run(pin, out_dir)


class TestPinValidation:
    def test_a_missing_pin_file_is_an_error(self, tmp_path, out_dir):
        with pytest.raises(CommandError, match="No pin file"):
            _run(tmp_path / "absent.json", out_dir)

    def test_an_artifact_without_a_checksum_is_an_error(self, tmp_path, out_dir):
        pin = tmp_path / "viewer-pin.json"
        pin.write_text(json.dumps({"artifacts": {"public": {"tag": "t", "archive": "a.tar.gz"}}}))
        with pytest.raises(CommandError, match="sha256"):
            _run(pin, out_dir)

    def test_an_unknown_artifact_name_is_an_error(self, tmp_path, out_dir):
        pin = _pin(tmp_path, _archive(tmp_path, {LIB: "lib"}))
        with pytest.raises(CommandError, match="No such artifact"):
            _run(pin, out_dir, "--artifact", "nope")

    def test_the_release_url_is_derived_from_repository_and_tag(self, tmp_path):
        from epicurrents.management.commands.vendor_viewer import Command

        url = Command._url(
            {"repository": "epicurrents/app"},
            {"tag": "full-v1.0.0", "archive": "epicurrents-full-v1.0.0.tar.gz"},
        )
        assert url == (
            "https://github.com/epicurrents/app/releases/download/full-v1.0.0/epicurrents-full-v1.0.0.tar.gz"
        )
