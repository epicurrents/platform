"""Contract tests for ``manage.py vendor_pyodide``.

The command writes a tree that is verified by nothing else: it is gitignored, absent
from CI, and a deployment that gets it wrong reports success and then fails in a
browser console. So the invariants worth pinning are the ones whose violation is
silent — a lock that names a package the tree does not carry, a hash that is never
checked, a frontend naming a version nothing populated.

Network access is stubbed at ``_fetch``: every test serves a small distribution and a
synthetic wheel out of memory, so the resolution and pruning logic is exercised without
reaching PyPI or a CDN.
"""

import hashlib
import io
import json
import zipfile

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from epicurrents.management.commands import vendor_pyodide

INDEX = "https://example.invalid/pyodide/v1.2.3/full/"


def _wheel_bytes(name, version, requires=(), package_dir=None):
    """A minimal but structurally real wheel: a package directory plus dist-info."""
    buffer = io.BytesIO()
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    metadata += [f"Requires-Dist: {requirement}" for requirement in requires]
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{package_dir or name.replace('-', '_')}/__init__.py", "")
        archive.writestr(f"{name.replace('-', '_')}-{version}.dist-info/METADATA", "\n".join(metadata))
    return buffer.getvalue()


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


class FakeIndex:
    """An in-memory Pyodide distribution plus PyPI, addressed by URL."""

    def __init__(self):
        self.files = {}
        self.packages = {}
        self.pypi = {}
        for name in ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip"):
            self.files[INDEX + name] = b"runtime"

    def add_distribution_package(self, name, depends=()):
        payload = _wheel_bytes(name, "1.0")
        file_name = f"{name}-1.0-cp314-cp314-wasm32.whl"
        self.packages[name] = {
            "name": name,
            "version": "1.0",
            "file_name": file_name,
            "install_dir": "site",
            "package_type": "package",
            "sha256": _sha(payload),
            "imports": [name],
            "depends": list(depends),
            "unvendored_tests": False,
        }
        self.files[INDEX + file_name] = payload

    def add_pypi_package(self, name, version="2.0", requires=(), package_dir=None):
        payload = _wheel_bytes(name, version, requires, package_dir)
        file_name = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        url = f"https://files.invalid/{file_name}"
        self.files[url] = payload
        self.pypi[f"https://pypi.org/pypi/{name}/json"] = {
            "info": {"version": version},
            "releases": {
                version: [
                    {
                        "filename": file_name,
                        "packagetype": "bdist_wheel",
                        "url": url,
                        "yanked": False,
                        "digests": {"sha256": _sha(payload)},
                    }
                ]
            },
        }

    def add_compiled_only_pypi_package(self, name, version="2.0"):
        """A PyPI package publishing no pure-Python wheel — nothing Pyodide can install."""
        file_name = f"{name}-{version}-cp314-cp314-manylinux_2_17_x86_64.whl"
        self.pypi[f"https://pypi.org/pypi/{name}/json"] = {
            "info": {"version": version},
            "releases": {
                version: [
                    {
                        "filename": file_name,
                        "packagetype": "bdist_wheel",
                        "url": f"https://files.invalid/{file_name}",
                        "yanked": False,
                        "digests": {"sha256": "0" * 64},
                    }
                ]
            },
        }

    def fetch(self, url):
        # Bound before it is attached to the command class, so the command's own ``self``
        # is never passed: a bound method is not a descriptor and is not re-bound on
        # attribute access.
        if url == INDEX + "pyodide-lock.json":
            return json.dumps({"info": {"python": "3.14.0"}, "packages": self.packages}).encode()
        if url in self.pypi:
            return json.dumps(self.pypi[url]).encode()
        if url in self.files:
            return self.files[url]
        raise CommandError(f"Fetching {url} failed: no such file in the fake index")


@pytest.fixture
def index(monkeypatch):
    fake = FakeIndex()
    monkeypatch.setattr(vendor_pyodide.Command, "_fetch", fake.fetch)
    return fake


@pytest.fixture
def vendor(tmp_path, index):
    """Run the command against the fake index, returning the output directory."""

    def run(*, packages=("numpy",), seeds=("numpy",), **options):
        monkey = pytest.MonkeyPatch()
        monkey.setattr(vendor_pyodide, "DEFAULT_DISTRIBUTION_PACKAGES", tuple(seeds))
        try:
            call_command(
                "vendor_pyodide",
                index_url=INDEX,
                pyodide_version="1.2.3",
                output_dir=str(tmp_path),
                package=list(packages),
                **options,
            )
        finally:
            monkey.undo()
        return tmp_path

    return run


def _lock(out_dir):
    return json.loads((out_dir / "pyodide-lock.json").read_text())


class TestPruning:
    def test_only_the_closure_is_vendored(self, index, vendor):
        index.add_distribution_package("numpy")
        index.add_distribution_package("scipy", depends=["numpy"])
        index.add_distribution_package("pandas")
        out = vendor(packages=[], seeds=("scipy",))
        assert set(_lock(out)["packages"]) == {"scipy", "numpy"}
        assert not (out / "pandas-1.0-cp314-cp314-wasm32.whl").exists()

    def test_lock_carries_every_dependency_it_names(self, index, vendor):
        # The pruned lock is the only description of the tree, so a dependency it names
        # and does not carry is a 404 at load time rather than a resolution error.
        index.add_distribution_package("numpy")
        index.add_distribution_package("lazy-loader")
        index.add_pypi_package("mne", requires=["numpy", "lazy-loader"])
        lock = _lock(vendor(packages=["mne"]))
        for name, meta in lock["packages"].items():
            for dependency in meta["depends"]:
                assert dependency in lock["packages"], f"{name} depends on unvendored {dependency}"

    def test_distribution_dependency_of_a_pypi_package_is_pulled_in(self, index, vendor):
        # lazy-loader is a dependency of mne that the distribution provides, so the PyPI
        # resolver skips it; the closure has to pick it up or it reaches no lock at all.
        index.add_distribution_package("numpy")
        index.add_distribution_package("lazy-loader")
        index.add_pypi_package("mne", requires=["numpy", "lazy-loader"])
        assert "lazy-loader" in _lock(vendor(packages=["mne"]))["packages"]

    def test_runtime_core_is_written(self, index, vendor):
        index.add_distribution_package("numpy")
        out = vendor(packages=[])
        for name in vendor_pyodide.CORE_FILES:
            assert (out / name).is_file()


class TestPypiResolution:
    def test_extras_only_requirements_are_not_installed(self, index, vendor):
        index.add_distribution_package("numpy")
        index.add_pypi_package("mne", requires=["numpy", 'pyvista>=0.43; extra == "full"'])
        lock = _lock(vendor(packages=["mne"]))
        assert "pyvista" not in lock["packages"]
        assert lock["packages"]["mne"]["depends"] == ["numpy"]

    def test_markers_are_evaluated_for_the_browser_runtime(self, index, vendor):
        # Resolving against the host would add a dependency the browser never sees.
        index.add_distribution_package("numpy")
        index.add_pypi_package("mne", requires=["numpy", 'pyobjc-framework-cocoa; platform_system == "Darwin"'])
        assert "pyobjc-framework-cocoa" not in _lock(vendor(packages=["mne"]))["packages"]

    def test_import_names_come_from_the_wheel(self, index, vendor):
        index.add_distribution_package("numpy")
        index.add_pypi_package("lazy-loader", package_dir="lazy_loader")
        entry = _lock(vendor(packages=["lazy-loader"]))["packages"]["lazy-loader"]
        assert entry["imports"] == ["lazy_loader"]

    def test_a_dependency_with_no_pure_python_wheel_aborts(self, index, vendor):
        index.add_distribution_package("numpy")
        index.add_pypi_package("mne", requires=["numpy", "compiled-thing"])
        index.add_compiled_only_pypi_package("compiled-thing")
        with pytest.raises(CommandError, match="No pure-Python wheel"):
            vendor(packages=["mne"])

    def test_a_pinned_spec_is_honoured(self, index, vendor):
        index.add_distribution_package("numpy")
        index.add_pypi_package("mne", version="1.12.1")
        assert _lock(vendor(packages=["mne==1.12.1"]))["packages"]["mne"]["version"] == "1.12.1"


class TestVerification:
    def test_a_missing_wheel_is_reported(self, index, vendor):
        index.add_distribution_package("numpy")
        out = vendor(packages=[])
        (out / _lock(out)["packages"]["numpy"]["file_name"]).unlink()
        with pytest.raises(CommandError, match="missing"):
            call_command("vendor_pyodide", check=True, output_dir=str(out), pyodide_version="1.2.3")

    def test_a_corrupted_wheel_is_reported(self, index, vendor):
        index.add_distribution_package("numpy")
        out = vendor(packages=[])
        (out / _lock(out)["packages"]["numpy"]["file_name"]).write_bytes(b"not the wheel")
        with pytest.raises(CommandError, match="does not match its recorded hash"):
            call_command("vendor_pyodide", check=True, output_dir=str(out), pyodide_version="1.2.3")

    def test_a_missing_runtime_file_is_reported(self, index, vendor):
        index.add_distribution_package("numpy")
        out = vendor(packages=[])
        (out / "pyodide.asm.wasm").unlink()
        with pytest.raises(CommandError, match="missing runtime file"):
            call_command("vendor_pyodide", check=True, output_dir=str(out), pyodide_version="1.2.3")

    def test_an_unvendored_tree_is_reported(self, tmp_path):
        with pytest.raises(CommandError, match="not vendored"):
            call_command("vendor_pyodide", check=True, output_dir=str(tmp_path), pyodide_version="1.2.3")

    def test_a_download_that_does_not_match_the_lock_aborts(self, index, vendor):
        index.add_distribution_package("numpy")
        index.packages["numpy"]["sha256"] = "0" * 64
        with pytest.raises(CommandError, match="Checksum mismatch"):
            vendor(packages=[])


class TestIdempotence:
    def test_a_correct_file_is_not_rewritten(self, index, vendor):
        index.add_distribution_package("numpy")
        out = vendor(packages=[])
        wheel = out / _lock(out)["packages"]["numpy"]["file_name"]
        before = wheel.stat().st_mtime_ns
        vendor(packages=[])
        assert wheel.stat().st_mtime_ns == before

    def test_force_rewrites(self, index, vendor):
        index.add_distribution_package("numpy")
        out = vendor(packages=[])
        wheel = out / _lock(out)["packages"]["numpy"]["file_name"]
        wheel.write_bytes(b"stale")
        vendor(packages=[], force=True)
        assert wheel.read_bytes() != b"stale"


class TestConfiguredVersion:
    def test_version_is_read_from_the_public_viewer_mode(self, settings, index, tmp_path):
        settings.PUBLIC_VIEWER_MODES = {"public": {"setup": {"pyodideAssetPath": "/vendor/pyodide/9.9.9/"}}}
        settings.VENDOR_DIR = tmp_path
        index.add_distribution_package("numpy")
        monkey = pytest.MonkeyPatch()
        monkey.setattr(vendor_pyodide, "DEFAULT_DISTRIBUTION_PACKAGES", ("numpy",))
        monkey.setattr(vendor_pyodide, "UPSTREAM_INDEX_TEMPLATE", INDEX)
        try:
            call_command("vendor_pyodide", package=[])
        finally:
            monkey.undo()
        assert (tmp_path / "pyodide" / "9.9.9" / "pyodide-lock.json").is_file()

    def test_an_unparseable_setting_is_an_error(self, settings):
        settings.PUBLIC_VIEWER_MODES = {"public": {"setup": {"pyodideAssetPath": "/vendor/"}}}
        with pytest.raises(CommandError, match="pass --pyodide-version"):
            call_command("vendor_pyodide", check=True)

    def test_a_frontend_naming_another_version_warns(self, settings, index, vendor, tmp_path, capsys):
        settings.BASE_DIR = tmp_path
        site = tmp_path / "frontend"
        site.mkdir()
        (site / "index.html").write_text("pyodideAssetPath: '/vendor/pyodide/0.0.1/',")
        index.add_distribution_package("numpy")
        vendor(packages=[])
        assert "names Pyodide 0.0.1, not 1.2.3" in capsys.readouterr().out
