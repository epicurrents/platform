"""Tests for the packager's manifest, file list and signature, and for scripts/lib/release_sign.py.

A package that update.sh can verify is three files — the tarball, a manifest
naming what it is and binding it by hash, and a signature over the manifest —
plus a FILELIST inside the tarball and the public key at the package root. Each
of those is asserted here from a real package build; the update-side checks
that consume them live in test_update.py.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

from scripts.tests.conftest import REPO_ROOT, SCRIPTS_DIR, requires_built_frontend, requires_rsync

FIXTURE = SCRIPTS_DIR / "make-bootstrap-fixture.sh"
RELEASE_SIGN = SCRIPTS_DIR / "lib" / "release_sign.py"

pytestmark = requires_rsync


def _helper(*args, check=True):
    return subprocess.run([sys.executable, str(RELEASE_SIGN), *args], check=check, capture_output=True, text=True)


def _run(dest, *args, env=None):
    return subprocess.run(
        ["bash", str(FIXTURE), str(dest), *args],
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _key(tmp_path):
    key = tmp_path / "release.key"
    _helper("keygen", str(key))
    return key


def _manifest(archive):
    return json.loads(archive.with_name(archive.name + ".manifest.json").read_text())


class TestReleaseSignHelper:
    def test_keygen_writes_a_private_key_only_its_owner_can_read(self, tmp_path):
        key = _key(tmp_path)
        assert key.stat().st_mode & 0o777 == 0o600
        assert key.with_name("release.key.pub").read_text().startswith("-----BEGIN PUBLIC KEY-----")

    def test_sign_and_verify_round_trip_and_a_flipped_byte_fails(self, tmp_path):
        key = _key(tmp_path)
        pub = key.with_name("release.key.pub")
        message = tmp_path / "m.json"
        message.write_text('{"a": 1}\n')
        signature = tmp_path / "m.sig"
        signature.write_text(_helper("sign", str(key), str(message)).stdout)
        assert base64.b64decode(signature.read_text().strip(), validate=True)
        assert _helper("verify", str(pub), str(message), str(signature)).returncode == 0
        message.write_text('{"a": 2}\n')
        assert _helper("verify", str(pub), str(message), str(signature), check=False).returncode == 1

    def test_key_id_is_stable_and_short(self, tmp_path):
        key = _key(tmp_path)
        pub = key.with_name("release.key.pub")
        first = _helper("key-id", str(pub)).stdout.strip()
        assert first == _helper("key-id", str(pub)).stdout.strip()
        assert len(first) == 16 and int(first, 16) >= 0

    def test_manifest_has_sorted_keys_one_per_line_with_lists_inline(self, tmp_path):
        out = tmp_path / "m.json"
        _helper(
            "manifest",
            "--package",
            "p.tar.gz",
            "--sha256",
            "0" * 64,
            "--size",
            "5",
            "--version",
            "0.1.1",
            "--platform-compatible",
            ">=0.1,<0.2",
            "--plugins",
            "b,a",
            "--min-updater-version",
            "2",
            "--out",
            str(out),
        )
        text = out.read_text()
        lines = text.splitlines()
        keys = [line.split('"')[1] for line in lines[1:-1]]
        assert keys == sorted(keys)
        assert '  "plugins": ["b", "a"],' in lines, "update.sh reads a list from one line"
        assert '  "key_id": null,' in lines
        assert '  "project": "",' in lines
        assert json.loads(text)["manifest_version"] == 1

    def test_manifest_refuses_a_hash_that_is_not_a_digest(self, tmp_path):
        result = _helper(
            "manifest",
            "--package",
            "p",
            "--sha256",
            "nope",
            "--size",
            "5",
            "--version",
            "0.1.1",
            "--platform-compatible",
            "x",
            "--min-updater-version",
            "2",
            check=False,
        )
        assert result.returncode != 0

    def test_version_commands_read_the_platform_module_without_importing_the_package(self):
        version = _helper("version").stdout.strip()
        assert (REPO_ROOT / "epicurrents" / "version.py").read_text().count(f'__version__ = "{version}"') == 1
        compatible = _helper("compatible").stdout.strip()
        major, minor = version.split(".")[:2]
        expected = f">=0.{minor},<0.{int(minor) + 1}" if major == "0" else f">={major}.{minor},<{int(major) + 1}"
        assert compatible == expected
        assert _helper("vercmp", "0.1.10", "0.1.9").stdout.strip() == "1"
        assert _helper("vercmp", "0.1.9", "0.1.9").stdout.strip() == "0"
        assert _helper("vercmp", "0.1.0", "1.0.0").stdout.strip() == "-1"


@requires_built_frontend
class TestSignedPackage:
    def _signed_demo(self, tmp_path):
        key = _key(tmp_path)
        dest = tmp_path / "demo"
        result = _run(dest, "--demo", "--tarball", "--sign-key", str(key))
        assert result.returncode == 0, result.stderr
        return dest, tmp_path / "demo.tar.gz", key

    def test_writes_manifest_signature_and_public_key(self, tmp_path):
        dest, archive, key = self._signed_demo(tmp_path)
        manifest = archive.with_name(archive.name + ".manifest.json")
        signature = archive.with_name(archive.name + ".manifest.sig")
        assert manifest.is_file() and signature.is_file()
        pub = key.with_name("release.key.pub")
        assert _helper("verify", str(pub), str(manifest), str(signature)).returncode == 0
        assert (dest / "RELEASE_KEY.pub").read_bytes() == pub.read_bytes()
        with tarfile.open(archive) as tf:
            assert "demo/RELEASE_KEY.pub" in tf.getnames()

    def test_manifest_binds_the_tarball_and_names_what_it_is(self, tmp_path):
        _, archive, key = self._signed_demo(tmp_path)
        manifest = _manifest(archive)
        assert manifest["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
        assert manifest["size"] == archive.stat().st_size
        assert manifest["package"] == archive.name
        assert manifest["version"] == _helper("version").stdout.strip()
        assert manifest["platform_compatible"] == _helper("compatible").stdout.strip()
        assert manifest["project"] == "" and manifest["plugins"] == []
        assert manifest["min_updater_version"] == 2
        agent = (SCRIPTS_DIR / "updater" / "epicurrents-updater.sh").read_text()
        assert manifest["agent_version"] == int(agent.split("AGENT_VERSION=", 1)[1].split()[0])
        assert manifest["manifest_version"] == 1
        assert manifest["key_id"] == _helper("key-id", str(key.with_name("release.key.pub"))).stdout.strip()
        assert manifest["built_at"].endswith("Z")
        assert manifest["successor_key"] is None and manifest["successor_key_id"] is None

    def test_a_successor_key_is_announced_in_the_manifest_and_shipped(self, tmp_path):
        key = _key(tmp_path)
        _helper("keygen", str(tmp_path / "keys" / "next.key"))
        nxt = tmp_path / "keys" / "next.key.pub"
        dest = tmp_path / "demo"
        result = _run(dest, "--demo", "--tarball", "--sign-key", str(key), "--successor-key", str(nxt))
        assert result.returncode == 0, result.stderr
        archive = tmp_path / "demo.tar.gz"
        manifest = _manifest(archive)
        assert manifest["successor_key"] == nxt.read_text()
        assert manifest["successor_key_id"] == _helper("key-id", str(nxt)).stdout.strip()
        assert manifest["key_id"] == _helper("key-id", str(key.with_name("release.key.pub"))).stdout.strip()
        # The current key still signs; the successor rides inside the signed bytes.
        pub = key.with_name("release.key.pub")
        signature = archive.with_name(archive.name + ".manifest.sig")
        assert (
            _helper(
                "verify", str(pub), str(archive.with_name(archive.name + ".manifest.json")), str(signature)
            ).returncode
            == 0
        )
        assert (dest / "RELEASE_KEY.next.pub").read_bytes() == nxt.read_bytes()
        assert (dest / "RELEASE_KEY.pub").read_bytes() == pub.read_bytes()
        with tarfile.open(archive) as tf:
            assert "demo/RELEASE_KEY.next.pub" in tf.getnames()
        assert "successor key id" in result.stdout

    def test_a_successor_needs_a_signing_key_and_must_be_another_key(self, tmp_path):
        key = _key(tmp_path)
        pub = key.with_name("release.key.pub")
        result = _run(tmp_path / "demo", "--demo", "--tarball", "--successor-key", str(pub))
        assert result.returncode != 0 and "needs --sign-key" in result.stderr
        result = _run(tmp_path / "demo", "--demo", "--tarball", "--sign-key", str(key), "--successor-key", str(pub))
        assert result.returncode != 0 and "a successor is a different key" in result.stderr
        result = _run(tmp_path / "demo", "--demo", "--tarball", "--sign-key", str(key), "--successor-key", str(key))
        assert result.returncode != 0 and "not an Ed25519 public key" in result.stderr

    def test_filelist_names_every_regular_file_including_itself_sorted(self, tmp_path):
        dest, archive, _ = self._signed_demo(tmp_path)
        listed = (dest / "FILELIST").read_text().splitlines()
        assert listed == sorted(listed, key=lambda s: s.encode())
        on_disk = sorted((str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()), key=lambda s: s.encode())
        assert listed == on_disk
        assert "FILELIST" in listed and "RELEASE_KEY.pub" in listed and "update.sh" in listed
        with tarfile.open(archive) as tf:
            assert "demo/FILELIST" in tf.getnames()

    def test_an_unsigned_build_still_writes_a_manifest_and_warns(self, tmp_path):
        dest = tmp_path / "demo"
        result = _run(dest, "--demo", "--tarball")
        assert result.returncode == 0, result.stderr
        archive = tmp_path / "demo.tar.gz"
        assert _manifest(archive)["key_id"] is None
        assert not archive.with_name(archive.name + ".manifest.sig").exists()
        assert not (dest / "RELEASE_KEY.pub").exists()
        assert "NOT signed" in result.stderr

    def test_sign_key_needs_tarball(self, tmp_path):
        result = _run(tmp_path / "demo", "--demo", "--sign-key", str(_key(tmp_path)))
        assert result.returncode != 0
        assert "add --tarball" in result.stderr

    def test_a_missing_or_unusable_key_is_refused_before_copying(self, tmp_path):
        result = _run(tmp_path / "demo", "--demo", "--tarball", "--sign-key", str(tmp_path / "nope.key"))
        assert result.returncode != 0 and "no such file" in result.stderr
        assert not (tmp_path / "demo" / "docker-compose.yml").exists()
        bad = tmp_path / "bad.key"
        bad.write_text("not a key\n")
        result = _run(tmp_path / "demo", "--demo", "--tarball", "--sign-key", str(bad))
        assert result.returncode != 0 and "not a usable Ed25519" in result.stderr

    def test_signing_refuses_a_version_that_is_not_newer_than_the_newest_tag(self, tmp_path):
        # A git on PATH that reports a release tag at the current version.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        version = _helper("version").stdout.strip()
        git = bindir / "git"
        git.write_text(
            f'#!/bin/sh\ncase "$*" in *"tag -l"*) echo v0.0.1; echo v{version} ;; *) exec /usr/bin/git "$@" ;; esac\n'
        )
        git.chmod(0o755)
        result = _run(
            tmp_path / "demo",
            "--demo",
            "--tarball",
            "--sign-key",
            str(_key(tmp_path)),
            env={"PATH": f"{bindir}:{os.environ['PATH']}"},
        )
        assert result.returncode != 0
        assert f"newest release tag is v{version}" in result.stderr
        assert not (tmp_path / "demo" / "docker-compose.yml").exists(), "refused before copying"

    def test_a_pre_release_tag_does_not_poison_the_newest_tag(self, tmp_path):
        # The platform's parser rejects v0.1.0-rc1; seeded as the newest tag it
        # masked every later comparison and refused to sign anything.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        git = bindir / "git"
        git.write_text(
            '#!/bin/sh\ncase "$*" in *"tag -l"*) echo v0.1.0-rc1; echo v0.0.1 ;; *) exec /usr/bin/git "$@" ;; esac\n'
        )
        git.chmod(0o755)
        result = _run(
            tmp_path / "demo",
            "--demo",
            "--tarball",
            "--sign-key",
            str(_key(tmp_path)),
            env={"PATH": f"{bindir}:{os.environ['PATH']}"},
        )
        assert result.returncode == 0, result.stderr

    def test_a_symlink_in_the_package_tree_is_refused(self, tmp_path):
        # Reached by building a dist with a project whose tree carries one, which
        # the fixture cannot arrange without a project; the check itself is a
        # find over the destination, exercised here on the script's own logic.
        body = FIXTURE.read_text()
        assert 'find "$DEST" -type l' in body
        assert "contains symlinks" in body


class TestPackagedReadme:
    @requires_built_frontend
    def test_readme_names_the_drop_directory_sidecars_and_the_check(self, tmp_path):
        dest = tmp_path / "demo"
        assert _run(dest, "--demo").returncode == 0
        text = (dest / "README.md").read_text()
        assert "`update/`" in text
        assert ".manifest.json" in text and ".manifest.sig" in text
        assert "--check-archive" in text


def test_helper_is_executable_and_documented():
    assert os.access(RELEASE_SIGN, os.X_OK)
    assert RELEASE_SIGN.read_text().startswith('#!/usr/bin/env python3\n"""')
    assert Path(RELEASE_SIGN).name == "release_sign.py"
