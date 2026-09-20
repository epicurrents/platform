"""Uploaded update packages: verification, storage in the spool, pruning and reconciliation.

A package is the three files the packager writes — the tarball, its manifest
and the detached Ed25519 signature over the manifest — and it lives in the
spool as ``packages/<sha256>/`` holding ``package.tar.gz``, ``manifest.json``,
``manifest.sig`` and ``upload.json`` (who uploaded it and when, so the row can
be re-created after a database restore). The upload streams the tarball into a
``packages/.incoming-<token>/`` directory that never counts as a package and is
renamed into place only once every check has passed, so the agent never sees a
half-written one.

The checks here are for the superuser's benefit, not the security boundary:
the host agent verifies its own copy of the package against a root-owned key
before anything runs. Refusing a bad package at upload gives an immediate,
specific answer instead of a ``failed`` job a minute later, and lets Django
make the one check the agent cannot — that the version satisfies every
installed project's and plugin's platform pin.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings
from django.utils import timezone

from epicurrents.plugin_loader import get_active_plugins
from epicurrents.project_loader import get_active_project
from epicurrents.version import VERSION_INFO, InvalidVersion, __version__, parse_version, satisfies
from maintenance import spool

logger = logging.getLogger(__name__)

#: The manifest format the platform understands, matching ``scripts/lib/release_sign.py``.
MANIFEST_VERSION = 1
#: Byte caps on the two small parts of an upload; a manifest is a few hundred bytes, a signature under a hundred.
MANIFEST_LIMIT = 64 * 1024
SIGNATURE_LIMIT = 4 * 1024
#: Names of the files inside a package directory.
TARBALL_NAME = "package.tar.gz"
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.sig"
UPLOAD_NAME = "upload.json"
INCOMING_PREFIX = ".incoming-"
#: The rejection reasons an upload can end in; each is a ``maintenance.package_rejected`` event's ``reason``.
REJECTION_REASONS = (
    "key_missing",
    "signature",
    "manifest",
    "hash",
    "too_large",
    "version_not_newer",
    "incompatible",
    "duplicate",
    "disk",
    "spool",
)
#: Free space the spool must keep beyond the package being written: the agent's own copy needs as much again.
DISK_HEADROOM_FACTOR = 2
#: Age past which a ``.incoming-`` directory is a crashed upload's leftover and swept.
INCOMING_MAX_AGE_SECONDS = 24 * 3600


class PackageRejected(Exception):
    """An upload that is not accepted. ``reason`` is one of :data:`REJECTION_REASONS`."""

    def __init__(self, reason: str, message: str, *, status: int = 400):
        if reason not in REJECTION_REASONS:
            raise ValueError(f"Unknown rejection reason {reason!r}")
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Manifest:
    """The fields of a package manifest the platform acts on, validated."""

    version: str
    sha256: str
    size: int
    project: str
    plugins: tuple[str, ...]
    platform_compatible: str
    built_at: str
    key_id: str
    agent_version: int
    raw: dict

    @property
    def version_info(self) -> tuple[int, int, int]:
        return parse_version(self.version)


def package_dir(sha256: str) -> Path:
    """The directory of the package with hash ``sha256``; derived, never stored."""
    return spool.packages_dir() / sha256


def normalise_plugins(value) -> tuple[str, ...]:
    """A plugin list as a sorted tuple of unique names, from a list or a comma-separated string."""
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list | tuple):
        parts = [str(part) for part in value]
    else:
        parts = []
    return tuple(sorted({part.strip() for part in parts if part and part.strip()}))


def deployment_identity() -> tuple[str, tuple[str, ...]]:
    """The active project and the enabled plugins, which a package must be built for."""
    return get_active_project(), normalise_plugins(get_active_plugins())


def load_release_key():
    """The release public key at ``REMOTE_UPDATE_RELEASE_KEY_PATH``, or ``None`` when absent or unusable."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    path = getattr(settings, "REMOTE_UPDATE_RELEASE_KEY_PATH", "")
    if not path:
        return None
    try:
        key = serialization.load_pem_public_key(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        logger.warning("The release key at REMOTE_UPDATE_RELEASE_KEY_PATH cannot be read: %s", exc)
        return None
    if not isinstance(key, Ed25519PublicKey):
        logger.warning("The release key at REMOTE_UPDATE_RELEASE_KEY_PATH is not an Ed25519 public key")
        return None
    return key


def verify_signature(manifest_bytes: bytes, signature_bytes: bytes, key) -> None:
    """Check the base64 detached signature over the manifest's exact bytes."""
    from cryptography.exceptions import InvalidSignature

    try:
        signature = base64.b64decode(signature_bytes.strip(), validate=True)
        key.verify(signature, manifest_bytes)
    except (InvalidSignature, ValueError):
        raise PackageRejected(
            "signature",
            "The package signature does not verify against this deployment's release key: the manifest or the "
            "signature was altered, or the package was signed with a different key.",
        ) from None


def read_manifest(raw: bytes) -> Manifest:
    """Parse and shape-check a manifest; everything the platform later relies on is checked here."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PackageRejected("manifest", "The manifest is not a JSON document.") from None
    if not isinstance(data, dict):
        raise PackageRejected("manifest", "The manifest is not a JSON object.")
    if data.get("manifest_version") != MANIFEST_VERSION:
        raise PackageRejected(
            "manifest",
            f"The manifest is version {data.get('manifest_version')!r}; this platform understands version "
            f"{MANIFEST_VERSION}. Apply the release that understands it from a shell first.",
        )
    sha256 = data.get("sha256")
    if not (isinstance(sha256, str) and len(sha256) == 64 and all(c in "0123456789abcdef" for c in sha256)):
        raise PackageRejected("manifest", "The manifest names no usable sha256.")
    size = data.get("size")
    if not (isinstance(size, int) and not isinstance(size, bool) and size > 0):
        raise PackageRejected("manifest", "The manifest names no usable size.")
    version = data.get("version")
    try:
        parse_version(version if isinstance(version, str) else "")
    except InvalidVersion:
        raise PackageRejected(
            "manifest", f"The manifest's version {version!r} is not a MAJOR.MINOR.PATCH version."
        ) from None
    agent_version = data.get("agent_version")
    return Manifest(
        version=version,
        sha256=sha256,
        size=size,
        project=str(data.get("project") or ""),
        plugins=normalise_plugins(data.get("plugins")),
        platform_compatible=str(data.get("platform_compatible") or ""),
        built_at=str(data.get("built_at") or ""),
        key_id=str(data.get("key_id") or ""),
        agent_version=agent_version if isinstance(agent_version, int) and not isinstance(agent_version, bool) else 0,
        raw=data,
    )


def _pinned_apps() -> list:
    """Project and plugin app configs, each of which may declare ``requires_platform``."""
    return [config for config in django_apps.get_app_configs() if config.name.startswith(("projects.", "plugins."))]


def check_manifest(manifest: Manifest) -> None:
    """Refuse a package this deployment could not apply: not newer, for another deployment, too big, or off a pin."""
    max_size = int(getattr(settings, "REMOTE_UPDATE_MAX_PACKAGE_SIZE", 1024 * 1024 * 1024))
    if manifest.size > max_size:
        raise PackageRejected(
            "too_large",
            f"The package is {manifest.size:,} bytes; this deployment accepts up to {max_size:,} "
            "(REMOTE_UPDATE_MAX_PACKAGE_SIZE).",
            status=413,
        )
    if manifest.version_info <= VERSION_INFO:
        raise PackageRejected(
            "version_not_newer",
            f"The package is version {manifest.version} and the installed platform is {__version__}; a remote "
            "update applies only a newer release.",
        )
    project, plugins = deployment_identity()
    if manifest.project != project:
        raise PackageRejected(
            "incompatible",
            f"The package is built for project '{manifest.project or '<none>'}' and this deployment runs "
            f"'{project or '<none>'}'.",
        )
    if manifest.plugins != plugins:
        raise PackageRejected(
            "incompatible",
            f"The package carries plugins '{','.join(manifest.plugins) or '<none>'}' and this deployment runs "
            f"'{','.join(plugins) or '<none>'}'.",
        )
    for config in _pinned_apps():
        specifier = getattr(config, "requires_platform", None)
        if not specifier:
            continue
        try:
            satisfied = satisfies(manifest.version, specifier)
        except InvalidVersion:
            # A pin that cannot be read fails manage.py check on every boot; not this upload's problem.
            continue
        if not satisfied:
            raise PackageRejected(
                "incompatible",
                f"{config.label} requires platform {specifier}, which version {manifest.version} does not "
                "satisfy; the package would fail its checks after the snapshot.",
            )


def _write_bytes(path: Path, data: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(data)


def store(package_file, manifest_bytes: bytes, signature_bytes: bytes, manifest: Manifest, *, uploaded_by):
    """Stream the tarball into the spool beside its manifest and signature, then create or revive the row.

    ``package_file`` is anything with ``chunks()``. The copy is hashed as it is
    written and refused when the hash or the size disagrees with the manifest;
    the directory is renamed to its hash only after the three files and
    ``upload.json`` are in it. Any failure removes the incoming directory. A
    package with the same hash already present answers ``duplicate``; a row
    left ``pruned`` by an earlier removal is revived rather than duplicated.
    """
    from maintenance.models import MaintenancePackage

    final = package_dir(manifest.sha256)
    if final.is_dir():
        raise PackageRejected("duplicate", "This package has already been uploaded.", status=409)
    packages = spool.packages_dir()
    try:
        packages.mkdir(parents=True, exist_ok=True)
        incoming = packages / f"{INCOMING_PREFIX}{secrets.token_hex(8)}"
        incoming.mkdir()
    except OSError as exc:
        logger.warning("The packages directory cannot be written: %s", exc)
        raise PackageRejected(
            "spool", "The maintenance spool is not writable by the platform; an operator needs to fix it.", status=409
        ) from None

    max_size = int(getattr(settings, "REMOTE_UPDATE_MAX_PACKAGE_SIZE", 1024 * 1024 * 1024))
    hasher = hashlib.sha256()
    total = 0
    try:
        # The agent copies the package out of the spool before it verifies it,
        # so the disk must hold it twice; a shell-less deployment must not be
        # left with a full disk and a half-written package.
        free = shutil.disk_usage(incoming).free
        needed = manifest.size * DISK_HEADROOM_FACTOR
        if free < needed:
            raise PackageRejected(
                "disk",
                f"The spool's disk has {free:,} bytes free and the package needs {needed:,} (the upload and the "
                "agent's verified copy). Free space on the host first.",
                status=409,
            )
        with (incoming / TARBALL_NAME).open("wb") as destination:
            for chunk in package_file.chunks():
                total += len(chunk)
                if total > max_size:
                    raise PackageRejected(
                        "too_large",
                        f"The package exceeds this deployment's limit of {max_size:,} bytes "
                        "(REMOTE_UPDATE_MAX_PACKAGE_SIZE).",
                        status=413,
                    )
                destination.write(chunk)
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if digest != manifest.sha256 or total != manifest.size:
            raise PackageRejected(
                "hash",
                f"The archive does not match its manifest: sha256 {digest[:12]}… over {total:,} bytes, the manifest "
                f"says {manifest.sha256[:12]}… over {manifest.size:,}. The file was altered or corrupted in transit.",
            )
        _write_bytes(incoming / MANIFEST_NAME, manifest_bytes)
        _write_bytes(incoming / SIGNATURE_NAME, signature_bytes)
        spool.write_json_atomic(
            incoming / UPLOAD_NAME,
            {
                "protocol": spool.PROTOCOL,
                "sha256": manifest.sha256,
                "uploaded_at": spool.now_iso(),
                "uploaded_by_id": uploaded_by.pk if uploaded_by is not None else None,
            },
        )
        if final.exists():
            raise PackageRejected("duplicate", "This package has already been uploaded.", status=409)
        os.replace(incoming, final)
    except PackageRejected:
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(incoming, ignore_errors=True)
        logger.warning("Storing package %s failed: %s", manifest.sha256[:12], exc)
        raise PackageRejected(
            "spool", "The package could not be written into the maintenance spool.", status=409
        ) from None

    row, _ = MaintenancePackage.objects.update_or_create(
        sha256=manifest.sha256, defaults=_row_fields(manifest, uploaded_by=uploaded_by, uploaded_at=timezone.now())
    )
    return row


def _row_fields(manifest: Manifest, *, uploaded_by, uploaded_at) -> dict:
    from maintenance.models import MaintenancePackage

    built_at = spool.parse_timestamp(manifest.built_at) if manifest.built_at else None
    return {
        "version": manifest.version,
        "project": manifest.project,
        "plugins": list(manifest.plugins),
        "platform_compatible": manifest.platform_compatible,
        "built_at": built_at,
        "size": manifest.size,
        "key_id": manifest.key_id,
        "manifest": manifest.raw,
        "uploaded_by": uploaded_by,
        "uploaded_at": uploaded_at,
        "state": MaintenancePackage.State.AVAILABLE,
    }


def remove_files(sha256: str) -> bool:
    """Delete a package directory; ``True`` when there was one. Raises ``OSError`` when it cannot."""
    path = package_dir(sha256)
    if not path.is_dir():
        return False
    shutil.rmtree(path)
    return True


def _in_flight_package_ids() -> set[int]:
    from maintenance.models import MaintenanceJob

    return set(
        MaintenanceJob.objects.filter(in_flight=True, package__isnull=False).values_list("package_id", flat=True)
    )


def prune(keep: int | None = None) -> list[str]:
    """Remove the files of every package beyond the ``keep`` newest; returns the hashes pruned.

    Counts available and applied packages alike, newest upload first, and
    never touches one a job in flight refers to. Rows stay, marked ``pruned``,
    because a job's ``package`` link is part of its audit record.
    """
    from maintenance.models import MaintenancePackage

    if keep is None:
        keep = int(getattr(settings, "REMOTE_UPDATE_KEEP_PACKAGES", 2))
    keep = max(1, keep)
    busy = _in_flight_package_ids()
    live = MaintenancePackage.objects.exclude(state=MaintenancePackage.State.PRUNED).order_by("-uploaded_at", "-pk")
    pruned: list[str] = []
    for row in list(live)[keep:]:
        if row.pk in busy:
            continue
        try:
            remove_files(row.sha256)
        except OSError as exc:
            # The upload that triggered this has landed; a directory that
            # will not go is the next prune's problem, not this request's.
            logger.warning("Could not prune package %s: %s", row.sha256[:12], exc)
            continue
        row.state = MaintenancePackage.State.PRUNED
        row.save(update_fields=["state"])
        pruned.append(row.sha256)
    return pruned


def mark_applied(package) -> None:
    """A job applied this package and was confirmed; it is no longer a candidate for another update."""
    from maintenance.models import MaintenancePackage

    if package is not None and package.state == MaintenancePackage.State.AVAILABLE:
        package.state = MaintenancePackage.State.APPLIED
        package.save(update_fields=["state"])


def _sweep_incoming(now: float | None = None) -> int:
    """Remove ``.incoming-`` directories older than a day: a process that died mid-upload left them."""
    import time

    root = spool.packages_dir()
    if not root.is_dir():
        return 0
    now = now if now is not None else time.time()
    removed = 0
    for path in root.iterdir():
        if not (path.is_dir() and path.name.startswith(INCOMING_PREFIX)):
            continue
        try:
            if now - path.stat().st_mtime < INCOMING_MAX_AGE_SECONDS:
                continue
            shutil.rmtree(path)
            removed += 1
        except OSError as exc:
            logger.warning("Could not sweep %s: %s", path.name, exc)
    return removed


def _scan_package_dirs() -> dict[str, Path]:
    """Package directories in the spool by hash; incoming directories and anything else are skipped."""
    found: dict[str, Path] = {}
    root = spool.packages_dir()
    if not root.is_dir():
        return found
    for path in root.iterdir():
        name = path.name
        if not path.is_dir() or len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
            continue
        if (path / MANIFEST_NAME).is_file() and (path / TARBALL_NAME).is_file():
            found[name] = path
    return found


def reconcile() -> dict:
    """Bring the rows in line with the directories, both ways.

    A directory with no row — the rows a rollback's database restore erased,
    or a package an operator dropped into the spool by hand — gets a row from
    its manifest and ``upload.json``. An available row whose directory is
    gone is marked ``pruned``. Writes happen under an audited scope only when
    there is something to write.
    """
    from django.contrib.auth import get_user_model

    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from maintenance.models import MaintenancePackage

    counts = {"created": 0, "pruned": 0, "swept": _sweep_incoming()}
    dirs = _scan_package_dirs()
    rows = {row.sha256: row for row in MaintenancePackage.objects.all()}
    to_create = []
    for sha256, path in dirs.items():
        if sha256 in rows and rows[sha256].state != MaintenancePackage.State.PRUNED:
            continue
        try:
            manifest = read_manifest((path / MANIFEST_NAME).read_bytes())
        except (OSError, PackageRejected) as exc:
            logger.warning("Package directory %s carries an unusable manifest: %s", sha256[:12], exc)
            continue
        if manifest.sha256 != sha256:
            logger.warning("Package directory %s carries a manifest for %s; ignored", sha256[:12], manifest.sha256[:12])
            continue
        upload = spool.read_json(path / UPLOAD_NAME) or {}
        uploaded_by = None
        if isinstance(upload.get("uploaded_by_id"), int):
            uploaded_by = get_user_model().objects.filter(pk=upload["uploaded_by_id"]).first()
        uploaded_at = spool.parse_timestamp(upload.get("uploaded_at")) or timezone.now()
        to_create.append((sha256, _row_fields(manifest, uploaded_by=uploaded_by, uploaded_at=uploaded_at)))
    to_prune = [
        row for sha256, row in rows.items() if row.state != MaintenancePackage.State.PRUNED and sha256 not in dirs
    ]
    if not (to_create or to_prune):
        return counts
    with with_system_activity(
        "maintenance.package.sync",
        interface=Activity.Interface.CELERY,
        metadata={"created": len(to_create), "pruned": len(to_prune)},
    ):
        for sha256, fields in to_create:
            MaintenancePackage.objects.update_or_create(sha256=sha256, defaults=fields)
            counts["created"] += 1
        for row in to_prune:
            row.state = MaintenancePackage.State.PRUNED
            row.save(update_fields=["state"])
            counts["pruned"] += 1
    return counts
