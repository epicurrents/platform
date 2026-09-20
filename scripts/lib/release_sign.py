#!/usr/bin/env python3
"""Release signing for distribution packages: Ed25519 keys, package manifests and detached signatures.

The packager (``scripts/make-bootstrap-fixture.sh --tarball``) calls this for the
steps a shell cannot do portably. Signing is the reason it exists at all: the
system ``openssl`` on the packaging machine is LibreSSL, which cannot sign or
verify Ed25519 through ``pkeyutl``, so the key handling goes through
``cryptography``, which the platform already depends on. Verification on a
deployment host stays with ``openssl pkeyutl`` (OpenSSL 3), because that is
what a minimal host has; ``update.sh`` falls back to this same library through
``python3`` when the host's OpenSSL is too old.

The manifest is written here rather than in the packager so its shape is fixed
in one place: keys sorted, one key per line, lists on one line. ``update.sh``
reads it with ``sed``, and that only works while the shape holds.

Subcommands that touch a key need ``cryptography``; ``manifest``, ``version``,
``compatible`` and ``vercmp`` run on any Python 3.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VERSION_MODULE = REPO_ROOT / "epicurrents" / "version.py"

#: The manifest format this helper writes and ``update.sh`` understands.
MANIFEST_VERSION = 1

#: Length of the key identifier: the leading hex of the SHA-256 of the raw
#: 32-byte public key. Enough to tell keys apart, short enough to read aloud.
KEY_ID_HEX = 16


def _version_module() -> dict:
    """Load ``epicurrents/version.py`` without importing the ``epicurrents`` package.

    The package's ``__init__`` imports the Celery app, which is exactly what a
    release-time script must not need; ``runpy`` reads the one module, which
    imports nothing but ``re``.
    """
    return runpy.run_path(str(VERSION_MODULE))


def _crypto():
    """Import the ``cryptography`` pieces, with a message that says what to install."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    except ImportError:
        sys.exit(
            "release_sign.py needs the 'cryptography' package for key operations. "
            "Run it with the project venv (.venv/bin/python) or install it: pip install cryptography"
        )
    return InvalidSignature, serialization, Ed25519PrivateKey, Ed25519PublicKey


def _load_private(path: Path):
    _, serialization, Ed25519PrivateKey, _ = _crypto()
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit(f"{path} is not an Ed25519 private key.")
    return key


def _load_public(path: Path):
    _, serialization, _, Ed25519PublicKey = _crypto()
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        sys.exit(f"{path} is not an Ed25519 public key.")
    return key


def _public_pem(public_key) -> bytes:
    _, serialization, _, _ = _crypto()
    return public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def key_id(public_key) -> str:
    """Short identifier of a public key: leading hex of the SHA-256 of its raw bytes."""
    _, serialization, _, _ = _crypto()
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:KEY_ID_HEX]


def cmd_keygen(args: argparse.Namespace) -> None:
    """Write a new private key to ``args.path`` (mode 0600) and its public half to ``<path>.pub``."""
    _, serialization, Ed25519PrivateKey, _ = _crypto()
    private_path = Path(args.path)
    public_path = Path(str(private_path) + ".pub")
    if private_path.exists() and not args.force:
        sys.exit(f"{private_path} exists; pass --force to overwrite it.")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    private_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    os.chmod(private_path, 0o600)
    public_path.write_bytes(_public_pem(key.public_key()))
    print(f"private key: {private_path}")
    print(f"public key:  {public_path}")
    print(f"key id:      {key_id(key.public_key())}")


def cmd_pubkey(args: argparse.Namespace) -> None:
    """Print (or write with ``--out``) the PEM public key of a private key."""
    pem = _public_pem(_load_private(Path(args.key)).public_key())
    if args.out:
        Path(args.out).write_bytes(pem)
    else:
        sys.stdout.write(pem.decode())


def cmd_key_id(args: argparse.Namespace) -> None:
    """Print the key id of a public key file."""
    print(key_id(_load_public(Path(args.public_key))))


def cmd_sign(args: argparse.Namespace) -> None:
    """Sign the bytes of ``args.file`` with ``args.key``; print the raw signature, base64."""
    signature = _load_private(Path(args.key)).sign(Path(args.file).read_bytes())
    print(base64.b64encode(signature).decode())


def cmd_verify(args: argparse.Namespace) -> None:
    """Verify a base64 detached signature over ``args.file``; exit 0 when it holds, 1 otherwise."""
    InvalidSignature, _, _, _ = _crypto()
    try:
        signature = base64.b64decode(Path(args.signature).read_text().strip(), validate=True)
        _load_public(Path(args.public_key)).verify(signature, Path(args.file).read_bytes())
    except (InvalidSignature, ValueError):
        print("signature does NOT verify", file=sys.stderr)
        sys.exit(1)
    print("signature verified")


def cmd_manifest(args: argparse.Namespace) -> None:
    """Write the package manifest: sorted keys, one per line, lists inline."""
    plugins = [p for p in (args.plugins or "").split(",") if p]
    fields = {
        "built_at": args.built_at or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent_version": args.agent_version or None,
        "key_id": args.key_id or None,
        "manifest_version": MANIFEST_VERSION,
        "min_updater_version": args.min_updater_version,
        "package": args.package,
        "platform_compatible": args.platform_compatible,
        "plugins": plugins,
        "project": args.project or "",
        "sha256": args.sha256,
        "size": args.size,
        "version": args.version,
    }
    if len(fields["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in fields["sha256"]):
        sys.exit(f"sha256 does not look like a hex digest: {fields['sha256']!r}")
    body = "{\n" + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(fields.items())) + "\n}\n"
    if args.out:
        Path(args.out).write_text(body)
    else:
        sys.stdout.write(body)


def cmd_version(_args: argparse.Namespace) -> None:
    """Print the platform version."""
    print(_version_module()["__version__"])


def cmd_compatible(_args: argparse.Namespace) -> None:
    """Print the range a project built against this platform version pins."""
    module = _version_module()
    print(module["compatible_range"](module["__version__"]))


def cmd_vercmp(args: argparse.Namespace) -> None:
    """Compare two versions with the platform's own parser; print -1, 0 or 1."""
    parse = _version_module()["parse_version"]
    try:
        a, b = parse(args.a), parse(args.b)
    except ValueError as exc:
        sys.exit(str(exc))
    print((a > b) - (a < b))


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="generate an Ed25519 key pair")
    p.add_argument("path", help="private key file to write; the public key goes to <path>.pub")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("pubkey", help="print the public key of a private key")
    p.add_argument("key")
    p.add_argument("--out")
    p.set_defaults(func=cmd_pubkey)

    p = sub.add_parser("key-id", help="print the id of a public key")
    p.add_argument("public_key")
    p.set_defaults(func=cmd_key_id)

    p = sub.add_parser("sign", help="sign a file, printing the base64 signature")
    p.add_argument("key")
    p.add_argument("file")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("verify", help="verify a base64 signature file over a file")
    p.add_argument("public_key")
    p.add_argument("file")
    p.add_argument("signature")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("manifest", help="write a package manifest")
    p.add_argument("--package", required=True, help="tarball file name")
    p.add_argument("--sha256", required=True)
    p.add_argument("--size", required=True, type=int)
    p.add_argument("--version", required=True)
    p.add_argument("--platform-compatible", required=True)
    p.add_argument("--project", default="")
    p.add_argument("--plugins", default="", help="comma-separated")
    p.add_argument("--min-updater-version", required=True, type=int)
    p.add_argument("--agent-version", default=0, type=int, help="the host agent the package ships; 0 for none")
    p.add_argument("--key-id", default="")
    p.add_argument("--built-at", default="")
    p.add_argument("--out")
    p.set_defaults(func=cmd_manifest)

    sub.add_parser("version", help="print the platform version").set_defaults(func=cmd_version)
    sub.add_parser("compatible", help="print the compatible range for this version").set_defaults(func=cmd_compatible)

    p = sub.add_parser("vercmp", help="compare two versions: prints -1, 0 or 1")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=cmd_vercmp)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
