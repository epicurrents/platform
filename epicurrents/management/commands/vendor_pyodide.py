"""Management command — vendor the self-hosted Pyodide runtime and its wheel closure.

Populates the tree that ``settings.PUBLIC_VIEWER_MODES["public"]["setup"]["pyodideAssetPath"]``
points at, served by ``epicurrents.views.vendor_view``. Without it the viewer's Python
interpreter cannot start: the worker dynamic-imports ``<path>/pyodide.mjs`` and every
package resolves from that folder's ``pyodide-lock.json``.

Only what the viewer loads is vendored. The upstream "full" distribution carries 354
packages; the closure of what the analysis tools ask for is 23 of them, so this fetches
the runtime core plus that closure and writes a **pruned lock** describing exactly the
files on disk. A pruned lock is what keeps the tree honest — leaving all 354 entries in
place would let a later ``loadPackage`` name a wheel that was never downloaded and fail
with a 404 instead of a resolution error.

mne is not in the upstream distribution (un-bundled since 0.28), so it is resolved from
PyPI along with any of its dependencies the distribution lacks, and merged into the lock.
Only pure-Python wheels are accepted: anything needing compilation has to come from the
Pyodide distribution, and a dependency that satisfies neither is an error rather than a
silent omission that surfaces as an ImportError in a browser months later.

Idempotent. A file already on disk with the recorded hash is left alone, so re-running
after a partial download resumes rather than restarts.

Usage
-----
Vendor the version the settings name::

    docker compose run --rm --no-deps web python manage.py vendor_pyodide

Verify an existing tree without downloading anything::

    ... vendor_pyodide --check

Other versions, packages, or a mirror of the distribution::

    ... vendor_pyodide --pyodide-version 314.0.2 --package mne==1.12.1
    ... vendor_pyodide --index-url https://cdn.jsdelivr.net/pyodide/v314.0.2/full/
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
import zipfile
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

#: Runtime files the module worker needs. ``pyodide.mjs`` is the loader the worker
#: dynamic-imports; it pulls the other three. The UMD build, the type declarations and
#: the source maps are in the distribution too and are deliberately not vendored — the
#: viewer spawns a module worker and never loads them.
CORE_FILES = (
    "pyodide.mjs",
    "pyodide.asm.mjs",
    "pyodide.asm.wasm",
    "python_stdlib.zip",
)

#: Packages the viewer asks ``loadPackage`` for by name: the worker loads numpy and scipy
#: unconditionally, and matplotlib is a configured extra (see the builder's
#: setup/services/pyodide.ts). Everything else in the tree arrives as a dependency —
#: listing more here would duplicate what the lock and the wheel metadata already state.
DEFAULT_DISTRIBUTION_PACKAGES = (
    "numpy",
    "scipy",
    "matplotlib",
)

#: Packages resolved from PyPI because the Pyodide distribution does not carry them.
#: Their own dependencies are resolved transitively and skipped where the distribution
#: already provides them.
DEFAULT_PYPI_PACKAGES = ("mne",)

#: Where the distribution is fetched from when ``--index-url`` is not given.
UPSTREAM_INDEX_TEMPLATE = "https://cdn.jsdelivr.net/pyodide/v{version}/full/"

PYPI_JSON = "https://pypi.org/pypi/{name}/json"

#: The version lives in ``PUBLIC_VIEWER_MODES`` and in the two frontend entry points that
#: seed the viewer SETUP. This command reads the settings value rather than carrying a
#: fourth copy, and checks the frontend files agree.
FRONTEND_VERSION_SITES = (
    Path("frontend") / "index.html",
    Path("frontend") / "src" / "App.vue",
)

_VERSION_IN_PATH = re.compile(r"/vendor/pyodide/(?P<version>[^/]+)/")


def _normalize(name: str) -> str:
    """Canonical PyPI distribution name (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


class Command(BaseCommand):
    help = "Vendor the self-hosted Pyodide runtime and the wheel closure the viewer loads."

    def add_arguments(self, parser):
        parser.add_argument(
            "--pyodide-version",
            default=None,
            help="Pyodide version to vendor (default: parsed from the configured pyodideAssetPath).",
        )
        parser.add_argument(
            "--index-url",
            default=None,
            help="Distribution base URL (default: the upstream CDN for the resolved version).",
        )
        parser.add_argument(
            "--package",
            action="append",
            default=None,
            metavar="NAME[==VERSION]",
            help=(
                "PyPI package to add to the distribution, repeatable "
                f"(default: {' '.join(DEFAULT_PYPI_PACKAGES)}). Pin a version for a reproducible deploy."
            ),
        )
        parser.add_argument(
            "--output-dir",
            default=None,
            help="Destination directory (default: <VENDOR_DIR>/pyodide/<version>).",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Verify the existing tree against its lock and exit; download nothing.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-download every file even when a correct copy is already present.",
        )

    # -- entry point ----------------------------------------------------------------

    #: Memoised wheel payloads, keyed by URL. See ``_fetch``.
    _payloads: dict

    def handle(self, *args, **options):
        self._payloads = {}
        version = options["pyodide_version"] or self._configured_version()
        out_dir = (
            Path(options["output_dir"]) if options["output_dir"] else Path(settings.VENDOR_DIR) / "pyodide" / version
        )

        if options["check"]:
            self._check(out_dir)
            return

        index_url = options["index_url"] or UPSTREAM_INDEX_TEMPLATE.format(version=version)
        if not index_url.endswith("/"):
            index_url += "/"
        specs = options["package"] if options["package"] is not None else list(DEFAULT_PYPI_PACKAGES)

        self._warn_on_frontend_drift(version)

        self.stdout.write(f"Vendoring Pyodide {version} from {index_url}")
        out_dir.mkdir(parents=True, exist_ok=True)

        upstream_lock = self._fetch_json(index_url + "pyodide-lock.json")
        available = upstream_lock["packages"]
        python_version = upstream_lock.get("info", {}).get("python", "")

        added = self._resolve_pypi(specs, available, python_version)
        for entry in added:
            self.stdout.write(f"  from PyPI: {entry['name']} {entry['version']}")

        # Seed the closure with the added packages' own dependencies wherever the
        # distribution provides them. Without this a dependency that exists upstream is
        # skipped by the PyPI resolver (correctly — it must come from the distribution)
        # and then never reaches the pruned lock, leaving an entry pointing at a package
        # the tree does not carry.
        seeds = list(DEFAULT_DISTRIBUTION_PACKAGES)
        for entry in added:
            seeds.extend(name for name in entry["depends"] if name in available)
        wanted = self._closure(available, seeds)
        self.stdout.write(f"  distribution closure: {len(wanted)} of {len(available)} packages")

        force = options["force"]
        for name in CORE_FILES:
            self._download(index_url + name, out_dir / name, expected_sha256=None, force=force)
        for pkg in sorted(wanted):
            meta = available[pkg]
            self._download(
                index_url + meta["file_name"],
                out_dir / meta["file_name"],
                expected_sha256=meta["sha256"],
                force=force,
            )
        for entry in added:
            self._download(
                entry.pop("_url"),
                out_dir / entry["file_name"],
                expected_sha256=entry["sha256"],
                force=force,
            )

        pruned = {
            "info": upstream_lock["info"],
            "packages": (
                {name: available[name] for name in sorted(wanted)} | {entry["name"]: entry for entry in added}
            ),
        }
        (out_dir / "pyodide-lock.json").write_text(json.dumps(pruned, indent=1, sort_keys=True) + "\n")

        self._check(out_dir)
        total = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file())
        self.stdout.write(
            self.style.SUCCESS(
                f"Vendored {len(pruned['packages'])} packages + the runtime core "
                f"({total / 1024 / 1024:.0f} MiB) into {out_dir}"
            )
        )

    # -- version resolution ---------------------------------------------------------

    def _configured_version(self) -> str:
        """Version named by the public viewer mode's ``pyodideAssetPath``."""
        modes = getattr(settings, "PUBLIC_VIEWER_MODES", {}) or {}
        for mode in modes.values():
            path = (mode.get("setup") or {}).get("pyodideAssetPath") or ""
            found = _VERSION_IN_PATH.search(path)
            if found:
                return found.group("version")
        raise CommandError(
            "No pyodideAssetPath of the form /vendor/pyodide/<version>/ is configured in "
            "PUBLIC_VIEWER_MODES; pass --pyodide-version explicitly."
        )

    def _warn_on_frontend_drift(self, version: str) -> None:
        """Report frontend entry points naming a different version than the one vendored.

        The three sites are kept in step by hand, and a mismatch is silent at build time:
        the SPA asks for a path this run never populates, and the interpreter fails to load
        with a 404 that looks like the vendoring did not happen at all.
        """
        base = Path(settings.BASE_DIR)
        for relative in FRONTEND_VERSION_SITES:
            site = base / relative
            if not site.is_file():
                continue
            versions = {m.group("version") for m in _VERSION_IN_PATH.finditer(site.read_text())}
            drifted = versions - {version}
            if drifted:
                self.stdout.write(
                    self.style.WARNING(
                        f"  {relative} names Pyodide {', '.join(sorted(drifted))}, not {version}; "
                        "the viewer will request a path this run does not populate."
                    )
                )

    # -- resolution -----------------------------------------------------------------

    def _closure(self, available: dict, seeds) -> set:
        """Transitive dependency closure of ``seeds`` within the distribution lock."""
        seen: set[str] = set()
        queue = list(seeds)
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            meta = available.get(name)
            if meta is None:
                raise CommandError(
                    f"Package '{name}' is not in the Pyodide distribution lock. Either the "
                    "distribution dropped it or the version is wrong."
                )
            seen.add(name)
            queue.extend(meta["depends"])
        return seen

    def _resolve_pypi(self, specs, available: dict, python_version: str) -> list:
        """Resolve ``specs`` and their missing dependencies into lock entries.

        Recurses through each wheel's own metadata, skipping anything the distribution
        already provides. Every resolved package must ship a pure-Python wheel — a
        compiled dependency cannot be installed into Pyodide from PyPI and has to come
        from the distribution instead.
        """
        from packaging.markers import UndefinedEnvironmentName
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        # Marker environment for the interpreter these wheels are installed into, not the
        # one running this command: resolving against the host would pull in dependencies
        # the browser runtime never sees, and drop ones it needs.
        environment = {
            "implementation_name": "cpython",
            "os_name": "posix",
            "platform_machine": "wasm32",
            "platform_system": "Emscripten",
            "python_full_version": python_version or "3.14.0",
            "python_version": ".".join((python_version or "3.14.0").split(".")[:2]),
            "sys_platform": "emscripten",
        }

        resolved: dict[str, dict] = {}
        queue = [(spec, None) for spec in specs]
        while queue:
            spec, requested_by = queue.pop(0)
            requirement = Requirement(spec)
            name = canonicalize_name(requirement.name)
            if name in resolved or name in available:
                continue
            wheel_url, wheel_version, sha256 = self._pypi_wheel(requirement, requested_by)
            metadata = self._wheel_metadata(wheel_url)
            depends = []
            for raw in metadata.get_all("Requires-Dist") or []:
                dependency = Requirement(raw)
                if dependency.marker is not None:
                    try:
                        if not dependency.marker.evaluate(environment):
                            continue
                    except UndefinedEnvironmentName:
                        # An extras-only marker (``extra == "full"``); not a runtime dep.
                        continue
                dependency_name = canonicalize_name(dependency.name)
                depends.append(dependency_name)
                queue.append((str(dependency), name))
            resolved[name] = {
                "name": name,
                "version": wheel_version,
                "file_name": wheel_url.rsplit("/", 1)[-1],
                "install_dir": "site",
                "package_type": "package",
                "sha256": sha256,
                "imports": self._wheel_imports(wheel_url, name),
                "depends": sorted(set(depends)),
                "unvendored_tests": False,
                "_url": wheel_url,
            }
        return [resolved[name] for name in sorted(resolved)]

    def _pypi_wheel(self, requirement, requested_by):
        """Newest release of ``requirement`` that ships a pure-Python wheel."""
        from packaging.version import InvalidVersion, Version

        name = requirement.name
        data = self._fetch_json(PYPI_JSON.format(name=name))
        candidates = []
        for raw_version, files in data.get("releases", {}).items():
            try:
                parsed = Version(raw_version)
            except InvalidVersion:
                continue
            if parsed.is_prerelease:
                continue
            if requirement.specifier and raw_version not in requirement.specifier:
                continue
            for entry in files:
                if entry.get("yanked") or entry.get("packagetype") != "bdist_wheel":
                    continue
                if not entry["filename"].endswith("-none-any.whl"):
                    continue
                candidates.append((parsed, raw_version, entry))
                break
        if not candidates:
            origin = f" (required by {requested_by})" if requested_by else ""
            raise CommandError(
                f"No pure-Python wheel for '{requirement}'{origin}. A compiled dependency must "
                "come from the Pyodide distribution — add it to DEFAULT_DISTRIBUTION_PACKAGES if "
                "the distribution carries it, or the analysis feature needing it cannot be vendored."
            )
        _, version, entry = max(candidates, key=lambda item: item[0])
        return entry["url"], version, entry["digests"]["sha256"]

    def _wheel_metadata(self, url: str):
        """Parse a wheel's ``METADATA`` without unpacking it to disk."""
        payload = self._fetch(url)
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            return BytesParser().parsebytes(archive.read(name))

    def _wheel_imports(self, url: str, fallback: str) -> list:
        """Top-level import names a wheel provides.

        Read from the archive rather than guessed from the distribution name, which is not
        always the import name (``lazy-loader`` imports as ``lazy_loader``). Pyodide uses
        these to satisfy imports without re-reading the wheel.
        """
        payload = self._fetch(url)
        names = set()
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            for entry in archive.namelist():
                if entry.endswith(".dist-info/") or "/" not in entry.rstrip("/"):
                    if entry.endswith(".py") and "/" not in entry:
                        names.add(entry[: -len(".py")])
                    continue
                top = entry.split("/", 1)[0]
                if top.endswith((".dist-info", ".data")):
                    continue
                names.add(top)
        return sorted(names) or [fallback.replace("-", "_")]

    # -- transfer and verification --------------------------------------------------

    def _fetch(self, url: str) -> bytes:
        """Read a URL into memory, memoising wheels.

        A wheel is read three times in a run — for its metadata, for its import names, and
        to write it out — and mne alone is 7 MiB, so the memo is what keeps resolution from
        re-downloading the largest artifacts on every step.
        """
        if url in self._payloads:
            return self._payloads[url]
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                payload = response.read()
        except urllib.error.URLError as exc:
            raise CommandError(f"Fetching {url} failed: {exc}") from exc
        if url.endswith(".whl"):
            self._payloads[url] = payload
        return payload

    def _fetch_json(self, url: str):
        return json.loads(self._fetch(url))

    def _download(self, url: str, target: Path, expected_sha256, force: bool) -> None:
        if not force and target.is_file() and (expected_sha256 is None or self._sha256(target) == expected_sha256):
            return
        payload = self._fetch(url)
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise CommandError(
                f"Checksum mismatch for {target.name}: the lock records {expected_sha256}, "
                f"the download hashes to {digest}."
            )
        target.write_bytes(payload)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _check(self, out_dir: Path) -> None:
        """Assert the tree matches its own lock, so a broken vendoring fails here.

        The alternative place to discover it is a browser console on a deployment, where a
        missing wheel and an un-vendored tree produce the same 404.
        """
        lock_path = out_dir / "pyodide-lock.json"
        if not lock_path.is_file():
            raise CommandError(f"No pyodide-lock.json in {out_dir}; the tree is not vendored.")
        lock = json.loads(lock_path.read_text())
        problems = []
        for name in CORE_FILES:
            if not (out_dir / name).is_file():
                problems.append(f"missing runtime file {name}")
        for name, meta in sorted(lock["packages"].items()):
            wheel = out_dir / meta["file_name"]
            if not wheel.is_file():
                problems.append(f"{name}: missing {meta['file_name']}")
            elif self._sha256(wheel) != meta["sha256"]:
                problems.append(f"{name}: {meta['file_name']} does not match its recorded hash")
        for name, meta in sorted(lock["packages"].items()):
            for dependency in meta["depends"]:
                if dependency not in lock["packages"]:
                    problems.append(f"{name}: depends on {dependency}, which the lock does not carry")
        if problems:
            raise CommandError(f"{out_dir} does not match its lock:\n  " + "\n  ".join(problems))
        self.stdout.write(f"  verified {len(lock['packages'])} packages against the lock")
