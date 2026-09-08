"""Tests for the converter-extension registry and the discovery that derives from it.

The invariant these pin is agreement: whatever :func:`converter_extensions` claims is
convertible, :func:`get_converter` must actually resolve, and ``import_recordings`` must
collect. The two lists were maintained by hand and had drifted in both directions — ``.csv``
had a working converter bulk import never reached, and ``.e`` was collected for a converter
the platform does not ship — so the agreement is the thing worth testing, not the contents.
"""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path

import pytest
from django.test import override_settings

from recordings.management.commands.import_recordings import _EDF_EXTENSIONS, Command
from recordings.pipelines import converter_extensions, get_converter, normalise_extension


class TestConverterExtensions:
    def test_every_claimed_extension_resolves_to_a_converter(self):
        for extension in converter_extensions():
            assert get_converter(extension) is not None, f"{extension} is claimed but does not resolve"

    def test_built_in_converters_are_listed(self):
        # The built-ins are the floor; a deployment that configures nothing still gets these.
        assert ".csv" in converter_extensions()

    def test_no_vendor_format_is_claimed_by_default(self):
        # The platform ships no vendor-specific conversion code, so a stock deployment
        # claims only what it can convert itself. .zip matters most: it is a generic
        # extension, and claiming it would take every zip upload.
        assert converter_extensions() == {".csv"}

    def test_setting_adds_an_external_converter(self):
        with override_settings(RECORDING_CONVERTERS={".ncs": "mysite.converters.ncs.convert"}):
            assert ".ncs" in converter_extensions()

    def test_none_disables_a_built_in(self):
        with override_settings(RECORDING_CONVERTERS={".csv": None}):
            assert ".csv" not in converter_extensions()
            assert get_converter(".csv") is None

    def test_disabled_extension_is_not_claimed_even_though_a_built_in_exists(self):
        # The set and the lookup have to agree on a disable, or discovery collects files that
        # then fail with "no converter registered" — the exact drift this guards against.
        with override_settings(RECORDING_CONVERTERS={".zip": None}):
            claimed = converter_extensions()
            assert ".zip" not in claimed
            assert all(get_converter(extension) is not None for extension in claimed)

    @pytest.mark.parametrize("key", ["ncs", ".NCS", "NCS", ".ncs"])
    def test_keys_are_normalised(self, key):
        with override_settings(RECORDING_CONVERTERS={key: "mysite.convert"}):
            assert ".ncs" in converter_extensions()

    def test_normalise_extension_is_idempotent(self):
        assert normalise_extension(normalise_extension("CSV")) == ".csv"


class TestDiscoveryFollowsTheRegistry:
    """File discovery must track the registry rather than a copy of it."""

    def _collect(self, tmp_path: Path, names: list[str]) -> tuple[set[str], str]:
        for name in names:
            (tmp_path / name).write_bytes(b"x")
        command = Command()
        command.stdout = StringIO()
        found = command._collect_files(tmp_path, "flat")
        return {path.name for path in found}, command.stdout.getvalue()

    def test_collects_native_and_convertible_formats(self, tmp_path):
        # Registered optional converters join the native formats without a second edit —
        # the whole point of discovery reading the registry.
        registered = {
            ".zip": {"command": ["{python}", "-c", "pass"]},
            ".stc": {"command": ["{python}", "-c", "pass"]},
        }
        with override_settings(RECORDING_CONVERTERS=registered):
            found, _ = self._collect(tmp_path, ["a.edf", "b.bdf", "c.csv", "d.zip", "e.stc"])
        assert found == {"a.edf", "b.bdf", "c.csv", "d.zip", "e.stc"}

    def test_csv_is_collected(self, tmp_path):
        # Regression: a built-in converter existed for .csv while discovery skipped it, so the
        # web upload path could ingest a CSV and bulk import silently could not.
        found, _ = self._collect(tmp_path, ["signals.csv"])
        assert found == {"signals.csv"}

    def test_an_extension_with_no_converter_is_not_collected(self, tmp_path):
        found, _ = self._collect(tmp_path, ["scan.e"])
        assert found == set()

    def test_registering_a_converter_makes_its_extension_collectable(self, tmp_path):
        with override_settings(RECORDING_CONVERTERS={".e": "recordings.converters.csv2edf.convert"}):
            found, _ = self._collect(tmp_path, ["scan.e"])
        assert found == {"scan.e"}

    def test_a_converter_that_cannot_be_imported_is_reported_rather_than_fatal(self, tmp_path):
        # A typo in settings should cost that one extension, not the whole import run.
        with override_settings(RECORDING_CONVERTERS={".e": "nowhere.at.all.convert"}):
            found, output = self._collect(tmp_path, ["scan.e", "a.edf"])
        assert found == {"a.edf"}
        assert ".e (1)" in output

    def test_disabling_a_converter_stops_its_extension_being_collected(self, tmp_path):
        with override_settings(RECORDING_CONVERTERS={".csv": None}):
            found, _ = self._collect(tmp_path, ["signals.csv"])
        assert found == set()

    def test_native_formats_do_not_depend_on_a_converter(self, tmp_path):
        # EDF/BDF are read directly; clearing the registry must not strip them.
        with override_settings(RECORDING_CONVERTERS={ext: None for ext in converter_extensions()}):
            found, _ = self._collect(tmp_path, ["a.edf", "b.bdf"])
        assert found == {"a.edf", "b.bdf"}
        assert _EDF_EXTENSIONS == {".edf", ".bdf"}

    def test_skipped_extensions_are_reported(self, tmp_path):
        # An operator pointing at a tree of an unsupported format must not see a bare "0 files".
        found, output = self._collect(tmp_path, ["scan.e", "other.e", "notes.txt"])
        assert found == set()
        assert ".e (2)" in output
        assert ".txt (1)" in output
        assert "no converter registered" in output

    def test_a_registered_but_uninstalled_converter_is_reported_as_such(self, tmp_path):
        # Registering a converter and installing it are two acts; an operator who did only
        # the first needs to be told that, not "no converter registered".
        spec = {"command": ["{python}", "-c", "pass"], "requires": "no_such_module_anywhere"}
        with override_settings(RECORDING_CONVERTERS={".study": spec}):
            found, output = self._collect(tmp_path, ["a.study"])
        assert found == set()
        assert "not installed" in output
        assert ".study (1)" in output

    def test_an_uninstalled_converter_does_not_collect_files(self, tmp_path):
        spec = {"command": ["{python}", "-c", "pass"], "requires": "no_such_module_anywhere"}
        with override_settings(RECORDING_CONVERTERS={".study": spec}):
            found, _ = self._collect(tmp_path, ["a.study"])
        assert found == set()

    def test_an_installed_converter_collects_files(self, tmp_path):
        spec = {"command": ["{python}", "-c", "pass"], "requires": "json"}
        with override_settings(RECORDING_CONVERTERS={".study": spec}):
            found, _ = self._collect(tmp_path, ["a.study"])
        assert found == {"a.study"}

    def test_nothing_is_reported_when_everything_is_handled(self, tmp_path):
        _, output = self._collect(tmp_path, ["a.edf"])
        assert "Skipped" not in output

    def test_extensionless_files_are_ignored_without_a_report(self, tmp_path):
        # A README or a lock file in an import tree is noise, not a skipped recording.
        found, output = self._collect(tmp_path, ["LICENSE"])
        assert found == set()
        assert "Skipped" not in output


class TestVendoredCheckoutsStayOutOfTheSuite:
    """A converter checkout is a foreign repository living inside the tree.

    Its tests are written against its own conventions and fixtures. Collecting them would
    make the platform's suite report a third-party project's failures as its own, and its
    pass count depend on which converters happen to be checked out.
    """

    def test_every_converter_checkout_is_pruned(self):
        import conftest

        root = Path("recordings/converters")
        checkouts = {
            child.name
            for child in root.glob("*")
            if child.is_dir() and not child.name.startswith((".", "__"))
        }
        pruned = {
            Path(entry).name for entry in conftest.collect_ignore if entry.startswith("recordings/converters/")
        }
        assert checkouts <= pruned, f"not pruned from collection: {sorted(checkouts - pruned)}"

    def test_the_platform_modules_are_not_pruned(self):
        # Pruning is per-directory, so the converters the platform ships stay collectable.
        import conftest

        assert not any(entry.endswith(".py") for entry in conftest.collect_ignore)


class TestVendoredCheckoutsStayOutOfTheImage:
    """The build context must carry the platform's converters and no checkout.

    Two failure directions, both silent until a deployment breaks. Including a checkout
    makes every image build a distribution of it, which matters because a converter for a
    proprietary format may be copyleft. Excluding too much drops the platform's own
    converter modules and produces an image that cannot import them — which is what the
    ``*/`` form borrowed from .gitignore actually did, since Docker cleans the trailing
    slash away and the pattern degrades to "everything in this directory".
    """

    def _patterns(self) -> list[str]:
        lines = Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]

    def test_checkouts_are_excluded(self):
        assert "recordings/converters/*" in self._patterns()

    def test_the_platform_modules_are_re_included(self):
        assert "!recordings/converters/*.py" in self._patterns()

    def test_the_directories_only_form_is_not_used(self):
        # Docker cleans the trailing slash, so this form excludes the modules as well.
        assert not any(p.rstrip("!").endswith("converters/*/") for p in self._patterns())

    def test_every_shipped_converter_is_a_module_not_a_package(self):
        # The re-include covers *.py only; a converter shipped as a package directory
        # would be dropped from the image without anything here noticing.
        root = Path("recordings/converters")
        packages = [c.name for c in root.glob("*") if c.is_dir() and not c.name.startswith((".", "__"))]
        tracked = subprocess.run(
            ["git", "ls-files", "recordings/converters/"], capture_output=True, text=True, check=False
        ).stdout.split()
        tracked_dirs = {Path(p).parts[2] for p in tracked if len(Path(p).parts) > 3}
        assert not (set(packages) & tracked_dirs), (
            f"{sorted(set(packages) & tracked_dirs)} is a tracked package under converters/; "
            "add it to the .dockerignore re-include or it will be missing from the image"
        )
