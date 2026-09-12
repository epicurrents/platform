"""Tests for the external-command converter.

This is the platform's whole answer to proprietary formats: it runs a program a deployment
describes and collects what that program wrote. The tests use a stub script rather than any
real converter, which is the point — nothing here should know what format is being read.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from django.test import override_settings

from recordings.converters.command import CommandConverterError, build, is_available
from recordings.pipelines import converter_available, converter_extensions, get_converter

PATIENT_FILENAME = "Testperson Given_2021.study"


def _stub(tmp_path: Path, body: str) -> Path:
    """Write a stand-in converter that behaves however a test needs."""
    script = tmp_path / "stub_converter.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys, pathlib
            args = sys.argv[1:]
            source = pathlib.Path(args[args.index("--in") + 1])
            out = pathlib.Path(args[args.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            """
        )
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    return script


def _spec(script: Path, **extra) -> dict:
    spec = {"command": ["{python}", str(script), "--in", "{input}", "--out", "{output}"]}
    spec.update(extra)
    return spec


@pytest.fixture
def source(tmp_path) -> Path:
    path = tmp_path / PATIENT_FILENAME
    path.write_bytes(b"payload")
    return path


class TestSpecValidation:
    def test_missing_command_is_refused(self):
        with pytest.raises(CommandConverterError, match="missing"):
            build({"requires": "os"})

    def test_unknown_key_is_refused(self):
        # A typo in settings should surface at registration, not on the first upload.
        with pytest.raises(CommandConverterError, match="unknown keys"):
            build({"command": ["x"], "requires_": "os"})

    def test_empty_command_is_refused(self):
        with pytest.raises(CommandConverterError, match="non-empty"):
            build({"command": []})

    def test_non_dict_is_refused(self):
        with pytest.raises(CommandConverterError, match="must be a dict"):
            build(["not", "a", "spec"])


class TestAvailability:
    def test_a_spec_without_requires_is_assumed_available(self):
        assert is_available({"command": ["x"]}) is True

    def test_an_installed_module_is_available(self):
        assert is_available({"command": ["x"], "requires": "json"}) is True

    def test_an_absent_module_is_not(self):
        assert is_available({"command": ["x"], "requires": "no_such_module_anywhere"}) is False

    def test_probing_does_not_import_the_module(self):
        """Locating a converter must not execute it.

        A converter for a proprietary format may be licensed on terms the platform must not
        take on — the Nicolet one is GPLv3 — and running it as a separate process is what
        keeps the two at arm's length. A probe that imported the module would pull that code
        into the platform's own process and undo the separation, so it resolves the spec
        without loading it.
        """
        import sys

        # A module that is certainly importable but not yet imported anywhere in the suite.
        module = "wave"
        sys.modules.pop(module, None)
        assert is_available({"command": ["x"], "requires": module}) is True
        assert module not in sys.modules

    def test_running_an_absent_converter_says_so(self, tmp_path, source):
        converter = build({"command": ["{python}", "-c", "pass"], "requires": "no_such_module_anywhere"})
        with pytest.raises(CommandConverterError, match="not installed"):
            converter(source, tmp_path / "out")


class TestRunning:
    def test_collects_the_edf(self, tmp_path, source):
        script = _stub(tmp_path, '(out / "converted.edf").write_bytes(b"edf")')
        edf_path, sidecar = build(_spec(script))(source, tmp_path / "out")
        assert edf_path.name == "converted.edf"
        assert sidecar is None

    def test_collects_the_sidecar_beside_it(self, tmp_path, source):
        script = _stub(
            tmp_path,
            '(out / "c.edf").write_bytes(b"edf")\n(out / "c.json").write_text(\'{"annotations": []}\')',
        )
        _, sidecar = build(_spec(script))(source, tmp_path / "out")
        assert sidecar == {"annotations": []}

    def test_placeholders_reach_the_command(self, tmp_path, source):
        # The converter is told where to read and write purely through substitution.
        script = _stub(tmp_path, '(out / "c.edf").write_bytes(source.read_bytes())')
        edf_path, _ = build(_spec(script))(source, tmp_path / "out")
        assert edf_path.read_bytes() == b"payload"

    def test_unreadable_sidecar_costs_its_events_not_the_recording(self, tmp_path, source):
        script = _stub(
            tmp_path,
            '(out / "c.edf").write_bytes(b"edf")\n(out / "c.json").write_text("{not json")',
        )
        edf_path, sidecar = build(_spec(script))(source, tmp_path / "out")
        assert edf_path.exists()
        assert sidecar is None

    def test_non_object_sidecar_is_discarded(self, tmp_path, source):
        script = _stub(tmp_path, '(out / "c.edf").write_bytes(b"e")\n(out / "c.json").write_text("[1]")')
        assert build(_spec(script))(source, tmp_path / "out")[1] is None


class TestFailureModes:
    def test_non_zero_exit_is_refused(self, tmp_path, source):
        script = _stub(tmp_path, 'sys.stderr.write("boom")\nsys.exit(2)')
        with pytest.raises(CommandConverterError, match="boom"):
            build(_spec(script))(source, tmp_path / "out")

    def test_no_output_is_refused(self, tmp_path, source):
        script = _stub(tmp_path, "pass")
        with pytest.raises(CommandConverterError, match="no EDF output"):
            build(_spec(script))(source, tmp_path / "out")

    def test_multiple_outputs_are_refused(self, tmp_path, source):
        script = _stub(tmp_path, '(out / "a.edf").write_bytes(b"1")\n(out / "b.edf").write_bytes(b"2")')
        with pytest.raises(CommandConverterError, match="2 EDF files"):
            build(_spec(script))(source, tmp_path / "out")

    def test_silent_failure_still_says_something(self, tmp_path, source):
        script = _stub(tmp_path, "sys.exit(3)")
        with pytest.raises(CommandConverterError, match="status 3"):
            build(_spec(script))(source, tmp_path / "out")

    def test_a_hanging_converter_is_killed(self, tmp_path, source):
        script = _stub(tmp_path, "import time\ntime.sleep(30)")
        with pytest.raises(CommandConverterError, match="did not finish"):
            build(_spec(script, timeout=1))(source, tmp_path / "out")

    def test_an_unrunnable_command_is_refused(self, tmp_path, source):
        converter = build({"command": [str(tmp_path / "does-not-exist"), "--in", "{input}"]})
        with pytest.raises(CommandConverterError, match="Could not run"):
            converter(source, tmp_path / "out")


class TestSourceFilenameNeverEscapes:
    """A source filename is patient-derived; nothing raised may repeat it."""

    def test_the_converters_echoed_path_is_scrubbed(self, tmp_path, source):
        # Converters are handed the path and echo it in diagnostics; quoting that verbatim
        # would put a patient-derived name into a permanent log through exc_info.
        script = _stub(tmp_path, 'sys.stderr.write(f"cannot open {source}")\nsys.exit(1)')
        with pytest.raises(CommandConverterError) as caught:
            build(_spec(script))(source, tmp_path / "out")
        assert "Testperson" not in str(caught.value)
        assert "<source>" in str(caught.value)

    def test_runaway_output_is_bounded(self, tmp_path, source):
        script = _stub(tmp_path, 'sys.stderr.write("x" * 10000)\nsys.exit(1)')
        with pytest.raises(CommandConverterError) as caught:
            build(_spec(script))(source, tmp_path / "out")
        assert len(str(caught.value)) < 1000

    @pytest.mark.parametrize(
        "body", ["sys.exit(1)", "pass", '(out / "a.edf").write_bytes(b"1")\n(out / "b.edf").write_bytes(b"2")']
    )
    def test_no_failure_path_names_the_file(self, tmp_path, source, body):
        script = _stub(tmp_path, f"sys.stderr.write(str(source))\n{body}")
        with pytest.raises(CommandConverterError) as caught:
            build(_spec(script))(source, tmp_path / "out")
        assert "Testperson" not in str(caught.value)


class TestRegistryIntegration:
    def test_a_command_spec_resolves_as_a_converter(self, tmp_path):
        spec = {"command": ["{python}", "-c", "pass"]}
        with override_settings(RECORDING_CONVERTERS={".study": spec}):
            assert callable(get_converter(".study"))
            assert ".study" in converter_extensions()

    def test_an_uninstalled_converter_is_declared_but_not_available(self):
        spec = {"command": ["{python}", "-c", "pass"], "requires": "no_such_module_anywhere"}
        with override_settings(RECORDING_CONVERTERS={".study": spec}):
            assert ".study" in converter_extensions()
            assert ".study" not in converter_extensions(available_only=True)
            assert converter_available(".study") is False

    def test_an_installed_converter_is_available(self):
        spec = {"command": ["{python}", "-c", "pass"], "requires": "json"}
        with override_settings(RECORDING_CONVERTERS={".study": spec}):
            assert ".study" in converter_extensions(available_only=True)
            assert converter_available(".study") is True

    def test_a_dotted_path_converter_is_assumed_available(self):
        # It resolved by import, which is as much as the platform can check.
        assert converter_available(".csv") is True
        assert ".csv" in converter_extensions(available_only=True)
