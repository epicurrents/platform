"""Management command — install the pinned viewer edition into ``frontend/viewer-dist/``.

The public viewer page loads a prebuilt edition bundle — ``epicurrents-lib.umd.js`` and the
files beside it — that the builder repository publishes as a release asset. Producing it on
the deploy host instead means running the builder's ``npm run setup``, which clones every
workspace package from its own repository and builds them in dependency order. That step is
what makes a fresh deploy fragile: the builder checkout carries only ``scripts/``,
``profiles/`` and ``setup/``, so any mismatch in the cloned package state fails the install
with an error that points at the bundler rather than at the clone.

So the edition is fetched rather than built. Each artifact is pinned by release tag and
SHA-256 in ``frontend/viewer-pin.json``, verified in memory, and only then unpacked.

Pin format
----------
``artifacts`` is a map so a second artifact — a per-project lib, once one is published — can
be added without reshaping the file::

    {
        "repository": "epicurrents/app",
        "artifacts": {
            "public": {
                "tag": "full-v0.1.0",
                "archive": "epicurrents-full-v0.1.0.tar.gz",
                "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                "target": "."
            }
        }
    }

``target`` is relative to ``frontend/viewer-dist/`` and defaults to ``.``. A ``url`` key
overrides the derived GitHub release URL, which is what lets a release candidate be tested
from a ``file://`` archive before it is tagged. An empty ``artifacts`` map means nothing is
pinned yet; the command reports that and exits cleanly, leaving whatever the frontend build
produced in place.

One archive per edition rather than a list of loose assets: the edition emits a
content-hashed worker chunk, so a file list needs re-pinning whenever a chunk name changes,
and a file *missing* from such a list is silently not fetched instead of being an error. A
single archive with one checksum makes the whole tree the unit.

Usage
-----
Install every pinned artifact::

    docker compose run --rm --no-deps web python manage.py vendor_viewer

Verify what is on disk against the pin and exit, downloading nothing::

    ... vendor_viewer --check

Re-install even when the stamp already matches::

    ... vendor_viewer --force
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

#: Pin file, relative to the repository root.
PIN_FILE = Path("frontend") / "viewer-pin.json"

#: Records what the last run installed, written inside the destination directory. Kept out of
#: the pin so the pin stays a hand-edited statement of intent and the stamp stays a
#: machine-written record of fact.
STAMP_NAME = ".viewer-stamp.json"

#: Where a release asset lives when the pin does not override it with ``url``.
RELEASE_URL = "https://github.com/{repository}/releases/download/{tag}/{archive}"


class Command(BaseCommand):
    help = "Install the viewer edition pinned in frontend/viewer-pin.json into frontend/viewer-dist/."

    def add_arguments(self, parser):
        parser.add_argument(
            "--pin",
            default=None,
            help=f"Pin file to read (default: {PIN_FILE.as_posix()}).",
        )
        parser.add_argument(
            "--artifact",
            action="append",
            default=None,
            metavar="NAME",
            help="Install only this artifact, repeatable (default: every artifact in the pin).",
        )
        parser.add_argument(
            "--output-dir",
            default=None,
            help="Destination directory (default: frontend/viewer-dist).",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Verify the installed tree against the pin and exit; download nothing.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-install every artifact even when the stamp already matches the pin.",
        )

    # -- entry point ----------------------------------------------------------------

    def handle(self, *args, **options):
        base = Path(settings.BASE_DIR)
        pin_path = Path(options["pin"]) if options["pin"] else base / PIN_FILE
        # Paired with epicurrents.views.VIEWER_DIST, which serves what this writes. Both
        # derive the same path from BASE_DIR rather than sharing a setting, so a deployment
        # relocating one has to relocate the other deliberately.
        out_dir = Path(options["output_dir"]) if options["output_dir"] else base / "frontend" / "viewer-dist"

        pin = self._load_pin(pin_path)
        wanted = self._select(pin, options["artifact"])

        if options["check"]:
            self._check(pin, wanted, out_dir)
            return

        if not wanted:
            self.stdout.write(f"No artifacts pinned in {pin_path}; leaving {out_dir} as the frontend build left it.")
            return

        stamp = self._read_stamp(out_dir)
        for name in sorted(wanted):
            self._install(pin, name, wanted[name], out_dir, stamp, force=options["force"])
        self._write_stamp(out_dir, stamp)
        self.stdout.write(self.style.SUCCESS(f"Viewer artifacts current in {out_dir}"))

    # -- pin handling ---------------------------------------------------------------

    def _load_pin(self, pin_path: Path) -> dict:
        """Read and shallow-validate the pin file."""
        if not pin_path.is_file():
            raise CommandError(f"No pin file at {pin_path}.")
        try:
            pin = json.loads(pin_path.read_text())
        except json.JSONDecodeError as exc:
            raise CommandError(f"{pin_path} is not valid JSON: {exc}") from exc
        artifacts = pin.get("artifacts")
        if not isinstance(artifacts, dict):
            raise CommandError(f"{pin_path} has no 'artifacts' object.")
        for name, entry in artifacts.items():
            missing = [key for key in ("tag", "archive", "sha256") if not entry.get(key)]
            if missing:
                raise CommandError(f"Artifact '{name}' in {pin_path} is missing: {', '.join(missing)}.")
        return pin

    @staticmethod
    def _select(pin: dict, requested) -> dict:
        """Artifacts to act on, honouring a repeated ``--artifact``."""
        artifacts = pin["artifacts"]
        if requested is None:
            return dict(artifacts)
        unknown = [name for name in requested if name not in artifacts]
        if unknown:
            raise CommandError(
                f"No such artifact in the pin: {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(sorted(artifacts)) or '(none)'}."
            )
        return {name: artifacts[name] for name in requested}

    @staticmethod
    def _url(pin: dict, entry: dict) -> str:
        """Download URL for an artifact — the pin's override, else the release asset."""
        if entry.get("url"):
            return entry["url"]
        repository = pin.get("repository")
        if not repository:
            raise CommandError("The pin has no 'repository' and the artifact carries no 'url'.")
        return RELEASE_URL.format(repository=repository, tag=entry["tag"], archive=entry["archive"])

    @staticmethod
    def _target_dir(out_dir: Path, entry: dict) -> Path:
        """Resolve an artifact's destination, refusing one that escapes ``out_dir``."""
        target = (out_dir / entry.get("target", ".")).resolve()
        root = out_dir.resolve()
        if target != root and root not in target.parents:
            raise CommandError(f"Artifact target {entry.get('target')!r} resolves outside {out_dir}.")
        return target

    # -- install --------------------------------------------------------------------

    def _install(self, pin: dict, name: str, entry: dict, out_dir: Path, stamp: dict, *, force: bool) -> None:
        """Fetch, verify and unpack one artifact, updating ``stamp`` in place."""
        recorded = stamp.get("artifacts", {}).get(name)
        if not force and recorded and self._matches(recorded, entry) and self._files_present(out_dir, recorded):
            self.stdout.write(f"  {name}: already at {entry['tag']}")
            return

        url = self._url(pin, entry)
        self.stdout.write(f"  {name}: fetching {entry['tag']} from {url}")
        payload = self._verified_payload(url, entry["sha256"])

        target = self._target_dir(out_dir, entry)
        target.mkdir(parents=True, exist_ok=True)
        # Remove only what the previous install of *this* artifact put here. Clearing the
        # directory instead would take the per-project viewer builds and the public-setup
        # shim with it, which the frontend build writes and this command does not own.
        if recorded:
            self._remove(target, recorded.get("files", []))

        files = self._unpack(payload, target)
        stamp.setdefault("artifacts", {})[name] = {
            "tag": entry["tag"],
            "archive": entry["archive"],
            "sha256": entry["sha256"],
            "target": entry.get("target", "."),
            "files": files,
        }
        self.stdout.write(f"  {name}: installed {len(files)} files into {target}")

    def _verified_payload(self, url: str, expected_sha256: str) -> bytes:
        """Download into memory and verify the checksum before any of it reaches disk."""
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                payload = response.read()
        except urllib.error.URLError as exc:
            raise CommandError(f"Fetching {url} failed: {exc}") from exc
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_sha256:
            raise CommandError(
                f"Checksum mismatch for {url}: the pin records {expected_sha256}, the download hashes to {digest}."
            )
        return payload

    def _unpack(self, payload: bytes, target: Path) -> list:
        """Extract a verified archive into ``target``, returning the relative paths written.

        A release archive that wraps everything in one directory is stripped of it, so both
        ``tar -czf x.tar.gz -C dist/full .`` and ``tar -czf x.tar.gz -C dist full`` install
        the same tree. Extraction uses tarfile's ``data`` filter, which rejects absolute
        paths, parent traversal, links pointing out of the tree, and device nodes.

        Every member is screened *before* the prefix is computed. The filter alone is not
        enough here because stripping rewrites the name it would see: an archive of nothing
        but ``../escaped.js`` has ``..`` as its one common top-level directory, so removing
        it would turn a traversing member into an innocuous one and install it silently
        rather than refuse the archive.
        """
        written = []
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            members = [member for member in archive.getmembers() if member.isfile() or member.isdir()]
            if not members:
                raise CommandError("The archive contains no files.")
            for member in members:
                self._reject_unsafe(member.name)
            prefix = self._common_prefix(members)
            if prefix:
                self.stdout.write(f"    stripping leading '{prefix}/' from the archive")
            for member in members:
                relative = self._strip(member.name, prefix)
                if relative is None:
                    continue
                member.name = relative
                archive.extract(member, target, filter="data")
                if member.isfile():
                    written.append(relative)
        if not written:
            raise CommandError("The archive contained no regular files after stripping.")
        return sorted(written)

    @staticmethod
    def _reject_unsafe(name: str) -> None:
        """Refuse an archive member whose path could resolve outside the destination."""
        if "\\" in name or name.startswith("/") or PurePosixPath(name).is_absolute():
            raise CommandError(f"Refusing archive member {name!r}: the path is absolute.")
        if ".." in PurePosixPath(name).parts:
            raise CommandError(f"Refusing archive member {name!r}: the path escapes the destination.")

    @staticmethod
    def _common_prefix(members) -> str:
        """The single top-level directory every member shares, or an empty string."""
        tops = {member.name.strip("/").split("/", 1)[0] for member in members}
        tops.discard("")
        if len(tops) != 1:
            return ""
        top = tops.pop()
        # Only a directory wrapper is strippable; a single-file archive at the root is not.
        if any(member.name.strip("/") == top and member.isfile() for member in members):
            return ""
        return top

    @staticmethod
    def _strip(name: str, prefix: str):
        """Drop ``prefix`` from an archive member path, or None when nothing is left of it."""
        relative = name.strip("/")
        if prefix:
            if relative == prefix:
                return None
            relative = relative[len(prefix) + 1 :]
        return relative or None

    @staticmethod
    def _remove(target: Path, files) -> None:
        """Delete the files a previous install recorded, then any directory left empty."""
        for relative in files:
            path = target / relative
            if path.is_file():
                path.unlink()
        for relative in sorted(files, key=lambda item: item.count("/"), reverse=True):
            parent = (target / relative).parent
            if parent != target and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()

    # -- verification ---------------------------------------------------------------

    def _check(self, pin: dict, wanted: dict, out_dir: Path) -> None:
        """Assert the installed tree matches the pin, failing loudly when it does not.

        ``update.sh`` runs this before deciding whether to fetch, so "nothing is pinned" has
        to succeed — that is a deployment building the edition from the checkout, not a
        broken one. A pinned artifact that is absent, stale or half-installed fails.
        """
        if not wanted:
            self.stdout.write("Nothing pinned; no viewer artifacts to verify.")
            return
        stamp = self._read_stamp(out_dir)
        installed = stamp.get("artifacts", {})
        problems = []
        for name, entry in sorted(wanted.items()):
            recorded = installed.get(name)
            if not recorded:
                problems.append(f"{name}: pinned at {entry['tag']} but not installed")
                continue
            if not self._matches(recorded, entry):
                problems.append(f"{name}: installed {recorded.get('tag')}, pin wants {entry['tag']}")
                continue
            target = self._target_dir(out_dir, entry)
            absent = [relative for relative in recorded.get("files", []) if not (target / relative).is_file()]
            if absent:
                problems.append(f"{name}: {len(absent)} recorded file(s) missing, first is {absent[0]}")
        if problems:
            raise CommandError(f"{out_dir} does not match {PIN_FILE.as_posix()}:\n  " + "\n  ".join(problems))
        self.stdout.write(f"  verified {len(wanted)} viewer artifact(s) against the pin")

    @staticmethod
    def _matches(recorded: dict, entry: dict) -> bool:
        """Whether a stamp entry describes the artifact the pin now asks for."""
        return recorded.get("tag") == entry["tag"] and recorded.get("sha256") == entry["sha256"]

    @staticmethod
    def _files_present(out_dir: Path, recorded: dict) -> bool:
        target = out_dir / recorded.get("target", ".")
        return all((target / name).is_file() for name in recorded.get("files", []))

    # -- stamp ----------------------------------------------------------------------

    @staticmethod
    def _read_stamp(out_dir: Path) -> dict:
        path = out_dir / STAMP_NAME
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            # A corrupt stamp means "unknown", not "broken deployment" — the next install
            # rewrites it, and --check reports the artifacts as not installed.
            return {}

    @staticmethod
    def _write_stamp(out_dir: Path, stamp: dict) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / STAMP_NAME).write_text(json.dumps(stamp, indent=4, sort_keys=True) + "\n")
