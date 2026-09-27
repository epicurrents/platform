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
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings
from django.db import DatabaseError, transaction
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
#: Bounds on the manifest's text fields: the columns they land in, so a manifest that passes here can be stored.
VERSION_MAX_LENGTH = 32
PROJECT_MAX_LENGTH = 64
PLATFORM_COMPATIBLE_MAX_LENGTH = 64
BUILT_AT_MAX_LENGTH = 64
PLUGINS_MAX = 64
PLUGIN_NAME_MAX_LENGTH = 64
_KEY_ID_RE = re.compile(r"^[0-9a-f]{1,16}$")


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


def successor_key_path(current: Path) -> Path:
    """Where a successor key sits beside the current one: ``RELEASE_KEY.pub`` → ``RELEASE_KEY.next.pub``.

    A release announces the key that signs the next one by shipping it under
    that name; the packager writes it and the image carries it, so the
    platform trusts the successor as soon as the announcing release runs.
    """
    if current.suffix == ".pub":
        return current.with_name(f"{current.stem}.next.pub")
    return current.with_name(f"{current.name}.next")


def release_key_paths() -> list[Path]:
    """The current key at ``REMOTE_UPDATE_RELEASE_KEY_PATH`` and the successor's place beside it."""
    path = getattr(settings, "REMOTE_UPDATE_RELEASE_KEY_PATH", "")
    if not path:
        return []
    current = Path(path)
    return [current, successor_key_path(current)]


def _load_key(path: Path, *, optional: bool = False):
    """The Ed25519 public key at ``path``, or ``None``; a missing optional key is silent."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except FileNotFoundError:
        if not optional:
            logger.warning("The release key at REMOTE_UPDATE_RELEASE_KEY_PATH cannot be read: no such file")
        return None
    except (OSError, ValueError) as exc:
        logger.warning("The release key at %s cannot be read: %s", path.name, exc)
        return None
    if not isinstance(key, Ed25519PublicKey):
        logger.warning("The release key at %s is not an Ed25519 public key", path.name)
        return None
    return key


def load_release_key():
    """The current release key at ``REMOTE_UPDATE_RELEASE_KEY_PATH``, or ``None`` when absent or unusable."""
    paths = release_key_paths()
    return _load_key(paths[0]) if paths else None


def load_release_keys() -> list:
    """Every usable release key: the current one first, then the successor when a release announced one."""
    paths = release_key_paths()
    if not paths:
        return []
    keys = []
    current = _load_key(paths[0])
    if current is not None:
        keys.append(current)
    successor = _load_key(paths[1], optional=True)
    if successor is not None:
        keys.append(successor)
    return keys


def key_id(key) -> str:
    """The short identifier the packager prints for a key: leading hex of the SHA-256 of its raw bytes."""
    from cryptography.hazmat.primitives import serialization

    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def verify_signature(manifest_bytes: bytes, signature_bytes: bytes, keys):
    """Check the base64 detached signature over the manifest's exact bytes against ``keys``; returns the key that verified.

    ``keys`` is one key or a list. The key returned is what a row's ``key_id``
    shows, rather than the id the manifest claims for itself.
    """
    from cryptography.exceptions import InvalidSignature

    if not isinstance(keys, list | tuple):
        keys = [keys]
    try:
        signature = base64.b64decode(signature_bytes.strip(), validate=True)
    except ValueError:
        signature = None
    if signature is not None:
        for key in keys:
            try:
                key.verify(signature, manifest_bytes)
            except InvalidSignature:
                continue
            return key
    raise PackageRejected(
        "signature",
        "The package signature does not verify against this deployment's release key: the manifest or the "
        "signature was altered, or the package was signed with a different key.",
    )


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
    if len(version) > VERSION_MAX_LENGTH:
        raise PackageRejected("manifest", f"The manifest's version is longer than {VERSION_MAX_LENGTH} characters.")
    project = str(data.get("project") or "")
    platform_compatible = str(data.get("platform_compatible") or "")
    built_at = str(data.get("built_at") or "")
    key_id_claimed = str(data.get("key_id") or "")
    plugins = normalise_plugins(data.get("plugins"))
    if len(project) > PROJECT_MAX_LENGTH:
        raise PackageRejected("manifest", f"The manifest's project is longer than {PROJECT_MAX_LENGTH} characters.")
    if len(platform_compatible) > PLATFORM_COMPATIBLE_MAX_LENGTH:
        raise PackageRejected(
            "manifest",
            f"The manifest's platform_compatible is longer than {PLATFORM_COMPATIBLE_MAX_LENGTH} characters.",
        )
    if len(built_at) > BUILT_AT_MAX_LENGTH or (built_at and spool.parse_timestamp(built_at) is None):
        raise PackageRejected("manifest", "The manifest's built_at is not an ISO-8601 timestamp.")
    if key_id_claimed and not _KEY_ID_RE.match(key_id_claimed):
        raise PackageRejected("manifest", "The manifest's key_id is not a key id of up to 16 hex digits.")
    if len(plugins) > PLUGINS_MAX or any(len(name) > PLUGIN_NAME_MAX_LENGTH for name in plugins):
        raise PackageRejected("manifest", "The manifest's plugin list is longer than any deployment runs.")
    agent_version = data.get("agent_version")
    return Manifest(
        version=version,
        sha256=sha256,
        size=size,
        project=project,
        plugins=plugins,
        platform_compatible=platform_compatible,
        built_at=built_at,
        key_id=key_id_claimed,
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


def store(package_file, manifest_bytes: bytes, signature_bytes: bytes, manifest: Manifest, *, uploaded_by, key_id=""):
    """Stream the tarball into the spool beside its manifest and signature, then create or revive the row.

    ``package_file`` is anything with ``chunks()``. The copy is hashed as it is
    written and refused when the hash or the size disagrees with the manifest;
    the directory is renamed to its hash only after the three files and
    ``upload.json`` are in it. Any failure removes the incoming directory. A
    package with the same hash already present answers ``duplicate``; a row
    left ``pruned`` by an earlier removal is revived rather than duplicated.
    ``key_id`` is the id of the key the signature verified against.
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
        sha256=manifest.sha256,
        defaults=_row_fields(manifest, uploaded_by=uploaded_by, uploaded_at=timezone.now(), key_id=key_id),
    )
    return row


def _row_fields(manifest: Manifest, *, uploaded_by, uploaded_at, key_id: str, state=None) -> dict:
    """The row of a package; ``key_id`` is the key that verified, blank when none did, never the manifest's claim."""
    from maintenance.models import MaintenancePackage

    built_at = spool.parse_timestamp(manifest.built_at) if manifest.built_at else None
    return {
        "version": manifest.version,
        "project": manifest.project,
        "plugins": list(manifest.plugins),
        "platform_compatible": manifest.platform_compatible,
        "built_at": built_at,
        "size": manifest.size,
        "key_id": key_id,
        "manifest": manifest.raw,
        "uploaded_by": uploaded_by,
        "uploaded_at": uploaded_at,
        "state": state or MaintenancePackage.State.AVAILABLE,
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


def _read_capped(path: Path, limit: int) -> bytes | None:
    """The bytes of ``path`` when it is a regular file no larger than ``limit``; ``None`` otherwise."""
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _examine(path: Path, sha256: str):
    """Check a package directory the way an upload is checked, short of hashing the tarball.

    Returns ``(manifest, state, key_id)``, or ``None`` when the manifest cannot
    even be read, which leaves nothing to describe a row with. ``state`` is
    ``invalid`` when the signature does not verify, the tarball's size disagrees
    with the manifest, or the package is for another deployment; ``unverified``
    otherwise, which :func:`verify_pending` settles by hashing. A deployment
    without a release key cannot verify anything yet and keeps the row
    ``unverified`` rather than condemning it.
    """
    from maintenance.models import MaintenancePackage

    manifest_bytes = _read_capped(path / MANIFEST_NAME, MANIFEST_LIMIT)
    if manifest_bytes is None:
        logger.warning("Package directory %s carries no readable manifest; ignored", sha256[:12])
        return None
    try:
        manifest = read_manifest(manifest_bytes)
    except PackageRejected as exc:
        logger.warning("Package directory %s carries an unusable manifest: %s", sha256[:12], exc)
        return None
    if manifest.sha256 != sha256:
        logger.warning("Package directory %s carries a manifest for %s; ignored", sha256[:12], manifest.sha256[:12])
        return None
    invalid = MaintenancePackage.State.INVALID
    try:
        check_manifest(manifest)
    except PackageRejected as exc:
        # Newer-than-installed is decided when a request names the package,
        # since a rollback makes an applied package newer again.
        if exc.reason != "version_not_newer":
            logger.warning("Package directory %s is not applicable here (%s)", sha256[:12], exc.reason)
            return manifest, invalid, ""
    try:
        size = (path / TARBALL_NAME).stat().st_size
    except OSError:
        size = -1
    if size != manifest.size:
        logger.warning("Package directory %s holds a tarball whose size disagrees with its manifest", sha256[:12])
        return manifest, invalid, ""
    keys = load_release_keys()
    if not keys:
        return manifest, MaintenancePackage.State.UNVERIFIED, ""
    signature_bytes = _read_capped(path / SIGNATURE_NAME, SIGNATURE_LIMIT) or b""
    try:
        key = verify_signature(manifest_bytes, signature_bytes, keys)
    except PackageRejected:
        logger.warning("Package directory %s carries a signature that does not verify", sha256[:12])
        return manifest, invalid, ""
    return manifest, MaintenancePackage.State.UNVERIFIED, key_id(key)


def _hash_file(path: Path) -> str | None:
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError as exc:
        logger.warning("Could not hash the tarball of %s: %s", path.parent.name[:12], exc)
        return None
    return hasher.hexdigest()


def verify_pending() -> dict:
    """Settle every ``unverified`` package: the upload's checks again, then the tarball's hash against the manifest.

    Runs in the worker, from the task the reconciliation dispatches and from
    the beat sync, because hashing a package is seconds of I/O that has no place
    in a request. A match makes the row ``available``; a mismatch or a failed
    check makes it ``invalid``, which no request may name. A row whose
    signature cannot be checked yet, for want of a release key, stays
    ``unverified``.
    """
    from activity.audit import record_modify_change, serialize_instance
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from maintenance.models import MaintenancePackage

    counts = {"available": 0, "invalid": 0}
    outcomes = []
    for row in MaintenancePackage.objects.filter(state=MaintenancePackage.State.UNVERIFIED):
        path = package_dir(row.sha256)
        examined = _examine(path, row.sha256) if path.is_dir() else None
        if examined is None:
            continue
        _, state, verified_key = examined
        if state == MaintenancePackage.State.UNVERIFIED:
            if not verified_key:
                continue
            digest = _hash_file(path / TARBALL_NAME)
            if digest is None:
                continue
            if digest == row.sha256:
                state = MaintenancePackage.State.AVAILABLE
            else:
                state = MaintenancePackage.State.INVALID
                logger.warning("Package directory %s holds a tarball whose hash disagrees with it", row.sha256[:12])
        outcomes.append((row, state, verified_key))
    if not outcomes:
        return counts
    with with_system_activity(
        "maintenance.package.sync", interface=Activity.Interface.CELERY, metadata={"verified": len(outcomes)}
    ):
        for row, state, verified_key in outcomes:
            before = serialize_instance(row)
            updated = MaintenancePackage.objects.filter(pk=row.pk, state=MaintenancePackage.State.UNVERIFIED).update(
                state=state, key_id=verified_key
            )
            if not updated:
                continue
            row.state, row.key_id = state, verified_key
            record_modify_change(actor=None, obj=row, before_state=before)
            counts[str(state)] += 1
    return counts


def _dispatch_verify() -> None:
    """Queue ``verify_packages`` for after the commit; a broker that is down leaves it to the beat sync."""

    def _dispatch():
        try:
            from maintenance.tasks import verify_packages

            verify_packages.delay()
        except Exception:
            logger.exception("Verifying reconciled packages could not be dispatched; the beat sync will")

    transaction.on_commit(_dispatch)


def reconcile() -> dict:
    """Bring the rows in line with the directories, both ways.

    A directory with no row — the rows a rollback's database restore erased,
    or a package an operator dropped into the spool by hand — gets a row from
    its manifest and ``upload.json`` after the checks an upload makes, short of
    the hash: ``invalid`` when one fails, ``unverified`` otherwise until
    :func:`verify_pending` has hashed the tarball in the worker. Nothing a
    directory claims is trusted before that, its key id included. An available
    row whose directory is gone is marked ``pruned``. Writes happen under an
    audited scope only when there is something to write, and a row that cannot
    be written is logged and skipped: this runs at the top of every maintenance
    request, and one directory must not be able to fail them all.
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
        examined = _examine(path, sha256)
        if examined is None:
            continue
        manifest, state, verified_key = examined
        upload = spool.read_json(path / UPLOAD_NAME) or {}
        uploaded_by = None
        if isinstance(upload.get("uploaded_by_id"), int):
            uploaded_by = get_user_model().objects.filter(pk=upload["uploaded_by_id"]).first()
        uploaded_at = spool.parse_timestamp(upload.get("uploaded_at")) or timezone.now()
        fields = _row_fields(
            manifest, uploaded_by=uploaded_by, uploaded_at=uploaded_at, key_id=verified_key, state=state
        )
        to_create.append((sha256, fields))
    to_prune = [
        row for sha256, row in rows.items() if row.state != MaintenancePackage.State.PRUNED and sha256 not in dirs
    ]
    if not (to_create or to_prune):
        return counts
    pending = False
    with with_system_activity(
        "maintenance.package.sync",
        interface=Activity.Interface.CELERY,
        metadata={"created": len(to_create), "pruned": len(to_prune)},
    ):
        for sha256, fields in to_create:
            try:
                with transaction.atomic():
                    MaintenancePackage.objects.update_or_create(sha256=sha256, defaults=fields)
            except DatabaseError:
                logger.exception("Package directory %s could not be recorded; skipped", sha256[:12])
                continue
            counts["created"] += 1
            pending = pending or fields["state"] == MaintenancePackage.State.UNVERIFIED
        for row in to_prune:
            row.state = MaintenancePackage.State.PRUNED
            row.save(update_fields=["state"])
            counts["pruned"] += 1
    if pending:
        _dispatch_verify()
    return counts
