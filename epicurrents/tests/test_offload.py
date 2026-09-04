"""Contract tests for the reverse-proxy file offload.

The subject is :func:`epicurrents.offload.offload_file_response`, whose job is to
decline. It hands the proxy a filesystem path, and every wrong answer serves a
file to someone who should not have it, so the tests are written around the
refusals rather than the happy path.

The one that matters most is the ``apply_middleware`` interlock. A
middleware-applied grant is a caller who may see an anonymised header and no
clinical annotation text; those bytes are computed per request and exist nowhere
on disk. Handing the proxy a path for such a caller serves the original recording
instead — patient identification and annotation text included — and does it with a
200 and no error anywhere. Nothing downstream would catch that, which is why the
flag is a required keyword argument at every call site and why this file exists.
"""

import ast
import re
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings

from epicurrents.apps import EpicurrentsConfig, parse_byte_size
from epicurrents.offload import (
    OFFLOAD_PROBE_HEADER,
    SERVE_CONTENT_TYPE_HEADER,
    SERVE_DISPOSITION_HEADER,
    SERVE_PATH_HEADER,
    offload_file_response,
)

REPO_ROOT = Path(settings.BASE_DIR)
CADDYFILE = REPO_ROOT / "caddy" / "Caddyfile"
PROXY_COMPOSE = REPO_ROOT / "docker-compose.proxy.yml"

# The document root the Caddyfile resolves X-Serve-Path against, and the segment
# the recordings volume is mounted under inside it.
PROXY_DOCUMENT_ROOT = "/srv/protected"
RECORDINGS_NAMESPACE = "recordings"


@pytest.fixture
def offload_enabled(settings):
    """Stand in for a deployment where the proxy overlay is in the file list."""
    settings.PROXY_FILE_OFFLOAD_ENABLED = True
    return settings


def _repo_python_files():
    """Every first-party module. Test modules are excluded: this file calls the
    helper through a kwargs dict, which is not a call site an endpoint author
    would copy."""
    skip = {".venv", "node_modules", "__pycache__", "tests", "migrations"}
    for path in REPO_ROOT.rglob("*.py"):
        if not any(part in skip for part in path.parts):
            yield path


def _request(*, probe=True):
    headers = {OFFLOAD_PROBE_HEADER: "1"} if probe else {}
    return RequestFactory().get("/recordings/ABC123/file", headers=headers)


def _call(tmp_path, *, probe=True, create=True, root=None, target=None, **overrides):
    """Invoke the helper against a real file under a real root.

    ``create=False`` leaves *target* absent so the missing-file refusal can be
    exercised — the helper materialises the file by default, which would
    otherwise quietly turn that test into a duplicate of the happy path.
    """
    root = root or tmp_path / "recordings"
    root.mkdir(parents=True, exist_ok=True)
    target = target if target is not None else root / "ABC123.edf"
    if create and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"raw edf bytes")
    kwargs = {
        "root": root,
        "namespace": RECORDINGS_NAMESPACE,
        "filename": "lesson-01.edf",
        "apply_middleware": False,
    }
    kwargs.update(overrides)
    return offload_file_response(_request(probe=probe), target, **kwargs)


@pytest.mark.usefixtures("offload_enabled")
class TestRefusals:
    def test_declines_when_middleware_applies(self, tmp_path):
        """The interlock. Those bytes are computed; no file on disk holds them."""
        assert _call(tmp_path, apply_middleware=True) is None

    def test_declines_for_a_path_outside_the_root(self, tmp_path):
        outside = tmp_path / "elsewhere" / "secret.edf"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"not yours")
        assert _call(tmp_path, target=outside) is None

    def test_declines_for_a_traversal_out_of_the_root(self, tmp_path):
        root = tmp_path / "recordings"
        root.mkdir()
        (tmp_path / "secret.edf").write_bytes(b"not yours")
        assert _call(tmp_path, target=root / ".." / "secret.edf") is None

    def test_declines_for_a_symlink_escaping_the_root(self, tmp_path):
        root = tmp_path / "recordings"
        root.mkdir()
        (tmp_path / "secret.edf").write_bytes(b"not yours")
        link = root / "innocent.edf"
        link.symlink_to(tmp_path / "secret.edf")
        # resolve() follows the link before the containment check, so the escape
        # is caught rather than laundered through a name inside the root.
        assert _call(tmp_path, target=link) is None

    def test_declines_for_a_missing_file(self, tmp_path):
        root = tmp_path / "recordings"
        root.mkdir()
        assert _call(tmp_path, target=root / "gone.edf", create=False) is None


class TestCapabilityGate:
    def test_declines_when_the_setting_is_off(self, tmp_path):
        # Default is off; a deployment with no proxy must never get an empty body.
        assert _call(tmp_path) is None

    def test_declines_without_the_probe_header(self, tmp_path, offload_enabled):
        """A request that reached gunicorn directly still gets its bytes."""
        assert _call(tmp_path, probe=False) is None


@pytest.mark.usefixtures("offload_enabled")
class TestHandoff:
    def test_serves_an_empty_body(self, tmp_path):
        response = _call(tmp_path)
        assert response.status_code == 200
        assert response.content == b""

    def test_serve_path_is_namespaced_and_root_relative(self, tmp_path):
        response = _call(tmp_path)
        assert response[SERVE_PATH_HEADER] == f"/{RECORDINGS_NAMESPACE}/ABC123.edf"

    def test_serve_path_keeps_subdirectories(self, tmp_path):
        root = tmp_path / "recordings"
        nested = root / "2026" / "ABC123.edf"
        response = _call(tmp_path, target=nested)
        assert response[SERVE_PATH_HEADER] == f"/{RECORDINGS_NAMESPACE}/2026/ABC123.edf"

    def test_disposition_carries_the_supplied_filename(self, tmp_path):
        # The caller passes display_name; original_name is author-private and must
        # never reach a grantee's Content-Disposition.
        response = _call(tmp_path, filename="lesson-01.edf")
        assert 'filename="lesson-01.edf"' in response[SERVE_DISPOSITION_HEADER]

    def test_content_type_is_carried_for_the_proxy(self, tmp_path):
        response = _call(tmp_path, content_type="application/octet-stream")
        assert response[SERVE_CONTENT_TYPE_HEADER] == "application/octet-stream"


class TestDeploymentPairing:
    """The Python side and the deployment config have to agree or downloads 404.

    Nothing at runtime connects them: Django emits a path, Caddy resolves it
    against a document root, and compose decides what is mounted there. A
    disagreement is a 404 on every download with no error in any log.
    """

    def test_caddyfile_resolves_the_serve_path_against_the_expected_root(self):
        source = CADDYFILE.read_text()
        block = re.search(r"handle_response @offload \{(.*?)\n\t\t\t\}", source, re.DOTALL)
        assert block, "the Caddyfile no longer has an @offload handle_response block"
        assert f"root * {PROXY_DOCUMENT_ROOT}" in block.group(1)
        assert f"rewrite * {{rp.header.{SERVE_PATH_HEADER}}}" in block.group(1)

    def test_compose_mounts_the_recordings_volume_where_the_namespace_points(self):
        source = PROXY_COMPOSE.read_text()
        expected = f"{PROXY_DOCUMENT_ROOT}/{RECORDINGS_NAMESPACE}"
        assert f"recordings-data:{expected}:ro" in source, (
            f"the proxy overlay must mount recordings-data read-only at {expected}"
        )

    def test_compose_enables_the_capability(self):
        # The setting is a statement about topology, so the overlay that creates
        # that topology is the only thing that should turn it on.
        assert "PROXY_FILE_OFFLOAD_ENABLED=True" in PROXY_COMPOSE.read_text()

    def test_offloaded_responses_are_not_cacheable(self):
        """SecurityHeadersMiddleware never runs on a response Django did not write."""
        block = re.search(r"handle_response @offload \{(.*?)\n\t\t\t\}", CADDYFILE.read_text(), re.DOTALL)
        assert "no-store" in block.group(1), (
            "the offload block must set Cache-Control itself — an offloaded recording "
            "without no-store is PHI left in a browser cache"
        )

    def test_proxy_sets_the_probe_header(self):
        assert f"header_up {OFFLOAD_PROBE_HEADER} 1" in CADDYFILE.read_text()


class TestCallSiteDiscipline:
    def test_apply_middleware_is_a_required_keyword(self):
        """Positional or defaulted, it could be omitted by a new endpoint author."""
        import inspect

        param = inspect.signature(offload_file_response).parameters["apply_middleware"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty

    def test_every_call_site_passes_it_explicitly(self):
        """An AST walk, so no call formatting can slip past the scan.

        This replaced a regex that required one exact multi-line layout. A
        single-line call — or one indented differently, say inside a class body —
        matched nothing at all, so the scan found zero call sites and passed;
        it would have passed with the real call site deleted, too. A scan
        guarding the interlock cannot be the one test in the file that is
        allowed to silently find nothing, hence the assertion that call sites
        exist at all.
        """
        call_sites, missing = [], []
        for path in _repo_python_files():
            try:
                tree = ast.parse(path.read_text(errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name != "offload_file_response":
                    continue
                where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                call_sites.append(where)
                if not any(kw.arg == "apply_middleware" for kw in node.keywords):
                    missing.append(where)

        assert call_sites, "the scan matched no call sites at all — it has gone vacuous"
        assert not missing, f"offload_file_response called without an explicit apply_middleware: {missing}"


@pytest.mark.parametrize("enabled", [False, True])
def test_declining_never_raises(tmp_path, enabled):
    """Every refusal path returns None so the caller falls back to streaming."""
    with override_settings(PROXY_FILE_OFFLOAD_ENABLED=enabled):
        assert _call(tmp_path, apply_middleware=True) is None


class TestByteSizeParsing:
    """The units Caddy actually uses, not the ones the names suggest.

    Established empirically by adapting a Caddyfile and reading the resulting
    JSON: a bare letter and the ``B`` forms are decimal, only ``iB`` is binary.
    That asymmetry is the bug this guards — ``PROXY_MAX_BODY_SIZE=2GB`` reads as
    2,000,000,000, which is *smaller* than a 2 GiB application limit, so the
    obvious-looking pairing silently rejects the largest allowed uploads.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2GB", 2_000_000_000),
            ("2G", 2_000_000_000),
            ("2gb", 2_000_000_000),
            ("2GiB", 2 * 1024**3),
            ("2gib", 2 * 1024**3),
            ("1500MB", 1_500_000_000),
            ("1048576", 1_048_576),
        ],
    )
    def test_matches_caddy(self, text, expected):
        assert parse_byte_size(text) == expected

    @pytest.mark.parametrize("text", ["", "   ", "2 GB", "banana", "2XB", "-5MB"])
    def test_unparseable_returns_none(self, text):
        # None means "draw no conclusion" — Caddy rejects these at its own
        # startup, which is a louder signal than Django guessing.
        assert parse_byte_size(text) is None

    def test_the_shipped_default_covers_the_application_limit(self):
        """The pairing the proxy overlay actually ships."""
        source = PROXY_COMPOSE.read_text()
        match = re.search(r"PROXY_MAX_BODY_SIZE=\$\{PROXY_MAX_BODY_SIZE:-([^}]+)\}", source)
        assert match, "the overlay no longer defaults PROXY_MAX_BODY_SIZE"
        assert parse_byte_size(match.group(1)) >= 2 * 1024**3, (
            f"the overlay default {match.group(1)!r} is below RECORDINGS_MAX_UPLOAD_SIZE's "
            "2 GiB default; uploads in the gap die at the edge with a bare 413"
        )

    def test_the_overlay_passes_the_limit_to_the_app(self):
        """Without this the guard cannot see the edge ceiling at all."""
        source = PROXY_COMPOSE.read_text()
        web_block = source.split("caddy:")[0]
        assert "PROXY_MAX_BODY_SIZE" in web_block


class TestBodyLimitGuard:
    """Boot-time refusal, so a mismatch surfaces on `up -d` rather than as a 413."""

    def _run(self, monkeypatch, raw, app_limit=2 * 1024**3):
        monkeypatch.setenv("PROXY_MAX_BODY_SIZE", raw)
        monkeypatch.setattr(sys, "argv", ["manage.py", "runserver"])
        with override_settings(RECORDINGS_MAX_UPLOAD_SIZE=app_limit):
            EpicurrentsConfig._guard_proxy_body_limit(EpicurrentsConfig)

    def test_refuses_when_the_proxy_ceiling_is_lower(self, monkeypatch):
        with pytest.raises(ImproperlyConfigured) as exc:
            self._run(monkeypatch, "2GB")
        # The message has to name both numbers; "413" alone sends the operator
        # looking in the wrong file.
        assert "2,000,000,000" in str(exc.value)
        assert "2,147,483,648" in str(exc.value)

    def test_accepts_an_equal_ceiling(self, monkeypatch):
        self._run(monkeypatch, "2GiB")

    def test_accepts_a_higher_ceiling(self, monkeypatch):
        self._run(monkeypatch, "4GiB")

    def test_silent_when_no_proxy_is_deployed(self, monkeypatch):
        # The variable only exists when the proxy overlay is in the file list.
        monkeypatch.delenv("PROXY_MAX_BODY_SIZE", raising=False)
        monkeypatch.setattr(sys, "argv", ["manage.py", "runserver"])
        EpicurrentsConfig._guard_proxy_body_limit(EpicurrentsConfig)

    def test_unparseable_warns_rather_than_blocking_boot(self, monkeypatch):
        # Caddy will reject the same string itself; a false refusal to boot here
        # would be worse than the mismatch being guarded.
        self._run(monkeypatch, "two gigabytes")

    def test_skipped_for_bootstrap_commands(self, monkeypatch):
        monkeypatch.setenv("PROXY_MAX_BODY_SIZE", "1MB")
        monkeypatch.setattr(sys, "argv", ["manage.py", "init_env"])
        with override_settings(RECORDINGS_MAX_UPLOAD_SIZE=2 * 1024**3):
            EpicurrentsConfig._guard_proxy_body_limit(EpicurrentsConfig)
