"""The account the containers run as comes from one setting, never a literal uid.

Every service runs as ``EPICURRENTS_UID`` / ``EPICURRENTS_GID`` from ``.env`` against a bind mount of the deployment.
A literal uid written back into one compose service, the image or a volume ``chown`` makes that one part run as an
account the rest of the stack does not, which surfaces as a permission error on some write far from the cause, and only
on a host whose deployment account is not 1000.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "${EPICURRENTS_UID:-1000}:${EPICURRENTS_GID:-1000}"


class _ComposeLoader(yaml.SafeLoader):
    """Reads compose's merge tags (``!override``, ``!reset``) as the plain values they wrap."""


def _untagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _compose(name: str) -> dict:
    return yaml.load((ROOT / name).read_text(), Loader=_ComposeLoader)


def test_every_service_that_names_a_user_names_the_deployment_account():
    for name in ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.proxy.yml"):
        for service, spec in (_compose(name).get("services") or {}).items():
            user = (spec or {}).get("user")
            # Root is the one other answer, and a deliberate one where it appears: the
            # test services need it for FUSE. A literal non-root uid is what this rules out.
            if user is not None and str(user) not in ("0", "0:0", "root"):
                assert user == ACCOUNT, f"{name}: {service} runs as {user!r}"


def test_no_chown_or_user_flag_in_the_compose_files_names_a_literal_uid():
    for name in ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.proxy.yml"):
        for line in (ROOT / name).read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert not re.search(r"(chown\s+(-R\s+)?|--user\s+)\d+:\d+", line), f"{name}: {line.strip()}"


def test_the_image_is_built_for_the_deployment_account():
    build_args = _compose("docker-compose.yml")["x-build-args"]
    assert build_args["APP_UID"] == "${EPICURRENTS_UID:-1000}"
    assert build_args["APP_GID"] == "${EPICURRENTS_GID:-1000}"
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'groupadd -o -g "$APP_GID" appuser' in dockerfile
    # -l keeps a directory-sized uid from writing a lastlog of that size into the image.
    assert 'useradd -l -o -u "$APP_UID" -g appuser' in dockerfile
    assert not re.search(r"(groupadd|useradd)[^\n]*\s-[ug]\s+\d", dockerfile)


def test_update_sh_reads_the_account_rather_than_assuming_one():
    script = (ROOT / "scripts" / "update.sh").read_text()
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert 'DEPLOY_UID="$(deploy_id EPICURRENTS_UID)"' in code
    assert not re.search(r"(chown\s+(-R\s+)?|--user\s+)1000:1000", code)
