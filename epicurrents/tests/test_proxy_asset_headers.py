"""Contract tests pinning ``caddy/Caddyfile``'s asset headers to ``epicurrents.views``.

``docker-compose.proxy.yml`` puts Caddy in front of gunicorn and lets it serve
``/static/``, ``/assets/``, ``/vendor/`` and the viewer bundles from disk. Those
responses never reach Django, so they never pick up the ``Cache-Control`` and
``Cross-Origin-Resource-Policy`` headers that :mod:`epicurrents.views` and
``SecurityHeadersMiddleware`` apply. The two rule sets have to agree, and nothing
enforces that at runtime: a drift surfaces as a stale bundle served after a
deploy, or as a COEP-blocked fetch inside the public viewer — both only visible
in a browser, on a deployment, after the fact.

The expected values are read out of the running views rather than written down
here, so changing a caching rule in ``views.py`` fails these tests and names the
Caddyfile block that has to follow.

Deployments that terminate TLS elsewhere still run these. The Caddyfile ships in
the repo either way, and a rule that has quietly diverged is a trap laid for the
next deployment that switches the overlay on.
"""

import re
import shlex
from pathlib import Path

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory

from epicurrents import views
from epicurrents.middleware import SecurityHeadersMiddleware

CADDYFILE = Path(settings.BASE_DIR) / "caddy" / "Caddyfile"

# Prefixes Caddy is allowed to serve from disk. Anything else reaching a
# ``file_server`` is a review question, not a test failure to paper over — see
# ``test_no_block_can_serve_the_spa_document``.
KNOWN_ASSET_MATCHERS = {
    "/static/*",
    "/assets/*",
    "/vendor/*",
    "@vendor_lock",
    "@viewer_lib",
    "@viewer_worker",
    "@viewer_asset",
}


class HandleBlock:
    """One ``handle`` block of the Caddyfile, with its header directives parsed out."""

    def __init__(self, directive: str, matcher: str, body: list[str]):
        # ``handle`` and ``handle_errors`` both parse to an empty matcher, so the
        # directive has to be carried alongside it to tell the site's catch-all
        # apart from its error handler.
        self.directive = directive
        self.matcher = matcher
        self.body = body

    @property
    def own_body(self) -> list[str]:
        """The block's own lines, with any nested ``handle_response`` body removed.

        The Django catch-all contains an ``@offload`` response handler that does
        serve files from disk (see epicurrents/offload.py). That is the proxy
        acting on a path Django chose, not the catch-all serving a public tree,
        so it must not read as though the catch-all itself became a file server.
        """
        out, depth = [], 0
        for line in self.body:
            if depth:
                depth += line.count("{") - line.count("}")
                continue
            if re.match(r"^\s*handle_response\b.*\{$", line):
                depth = 1
                continue
            out.append(line)
        return out

    @property
    def serves_from_disk(self) -> bool:
        return any(line.strip() == "file_server" or line.strip().startswith("file_server ") for line in self.own_body)

    @property
    def proxies(self) -> bool:
        return any(line.strip().startswith("reverse_proxy") for line in self.own_body)

    def headers(self, field: str) -> list[str]:
        """Every value this block sets for ``field``, unconditional ones first.

        Strips the optional response matcher (``@wasm``) and the ``>`` defer
        prefix, neither of which changes the value being asserted on.
        """
        found = []
        for line in self.own_body:
            stripped = line.strip()
            # Comments are not directives, and prose in them is not shell-quoted:
            # a single apostrophe ("the plugin's upstream") makes shlex raise
            # "No closing quotation" and take the whole test down over a comma.
            if not stripped or stripped.startswith("#"):
                continue
            try:
                tokens = shlex.split(stripped)
            except ValueError:
                continue
            if not tokens or tokens[0] != "header":
                continue
            tokens = tokens[1:]
            if tokens and tokens[0].startswith("@"):
                tokens = tokens[1:]
            if not tokens:
                continue
            name, value = tokens[0].lstrip(">+"), " ".join(tokens[1:])
            if name.lower() == field.lower():
                found.append(value)
        return found

    def __repr__(self):
        return f"<{self.directive} {self.matcher}>".replace(" >", ">")


def _parse_handle_blocks() -> list[HandleBlock]:
    lines = CADDYFILE.read_text().splitlines()
    blocks, current, directive, matcher = [], None, None, None
    for line in lines:
        opening = re.match(r"^\t(handle|handle_errors)\s*(\S+)?\s*\{$", line)
        if opening and current is None:
            directive, matcher = opening.group(1), opening.group(2) or ""
            current = []
            continue
        if current is not None:
            if line == "\t}":
                blocks.append(HandleBlock(directive, matcher, current))
                current, directive, matcher = None, None, None
            else:
                current.append(line)
    assert current is None, "unbalanced handle block in caddy/Caddyfile"
    return blocks


@pytest.fixture(scope="module")
def blocks():
    return _parse_handle_blocks()


@pytest.fixture
def asset_tree(tmp_path, monkeypatch):
    """Point the views at a throwaway asset tree and return its roots."""
    frontend, viewer, vendor = tmp_path / "dist", tmp_path / "viewer-dist", tmp_path / "vendor"
    (frontend / "assets").mkdir(parents=True)
    viewer.mkdir()
    vendor.mkdir()
    (frontend / "assets" / "index-abc123.js").write_text("//")
    (frontend / "index.html").write_text("<html></html>")
    (viewer / "epicurrents-lib.umd.cjs").write_text("//")
    (viewer / "montage.worker-9f8a7b.js").write_text("//")
    (viewer / "favicon.ico").write_text("x")
    (vendor / "pyodide.asm.wasm").write_text("x")
    (vendor / "pyodide-lock.json").write_text("{}")
    monkeypatch.setattr(views, "FRONTEND_DIST", frontend)
    monkeypatch.setattr(views, "VIEWER_DIST", viewer)
    monkeypatch.setattr(views, "VENDOR_DIR", vendor)
    return frontend, viewer, vendor


def _view_cache_control(view, path) -> str:
    """The Cache-Control a view produces, after the middleware default is applied."""
    request = RequestFactory().get(f"/{path}")
    response = view(request, path)
    # Mirror the production path: SecurityHeadersMiddleware fills in the no-store
    # default for anything the view did not opt out of.
    SecurityHeadersMiddleware(lambda _: response)(request)
    return response["Cache-Control"]


def _block(blocks, matcher) -> HandleBlock:
    matches = [b for b in blocks if b.directive == "handle" and b.matcher == matcher]
    assert len(matches) == 1, f"expected exactly one handle block for {matcher!r}, found {len(matches)}"
    return matches[0]


class TestCacheControlParity:
    """Each Caddy prefix must cache exactly as the Django view it stands in for."""

    def test_spa_assets(self, blocks, asset_tree):
        expected = _view_cache_control(views.frontend_view, "assets/index-abc123.js")
        assert _block(blocks, "/assets/*").headers("Cache-Control") == [expected]

    def test_viewer_lib(self, blocks, asset_tree):
        expected = _view_cache_control(views.viewer_view, "epicurrents-lib.umd.cjs")
        assert _block(blocks, "@viewer_lib").headers("Cache-Control") == [expected]

    def test_viewer_worker(self, blocks, asset_tree):
        expected = _view_cache_control(views.viewer_view, "montage.worker-9f8a7b.js")
        assert _block(blocks, "@viewer_worker").headers("Cache-Control") == [expected]

    def test_viewer_other_assets_fall_through_to_no_store(self, blocks, asset_tree):
        expected = _view_cache_control(views.viewer_view, "favicon.ico")
        assert _block(blocks, "@viewer_asset").headers("Cache-Control") == [expected]

    def test_vendor_pinned_assets(self, blocks, asset_tree):
        expected = _view_cache_control(views.vendor_view, "pyodide.asm.wasm")
        assert _block(blocks, "/vendor/*").headers("Cache-Control") == [expected]

    def test_vendor_lockfile(self, blocks, asset_tree):
        expected = _view_cache_control(views.vendor_view, "pyodide-lock.json")
        assert _block(blocks, "@vendor_lock").headers("Cache-Control") == [expected]

    def test_collected_static_is_not_cached(self, blocks):
        no_store = SecurityHeadersMiddleware(lambda _: HttpResponse("x"))(RequestFactory().get("/static/x.css"))[
            "Cache-Control"
        ]
        # collectstatic output is not content-hashed, so a long max-age would
        # serve stale admin assets across a Django upgrade.
        assert _block(blocks, "/static/*").headers("Cache-Control") == [no_store]


class TestOneCacheControlPerBlock:
    """A block may set ``Cache-Control`` exactly once.

    Caddy applies both a bare ``header Cache-Control ...`` and a matcher-scoped
    ``header @m Cache-Control ...`` in the same block, but the unconditional one
    wins — the narrowing matcher is silently inert. That shipped once: the
    Pyodide lockfile was written as a matcher inside the immutable ``/vendor/*``
    block and was served ``immutable`` anyway, which pins a stale lock in every
    cache that saw it. Narrowing belongs in its own mutually exclusive handle
    block, which is unambiguous.

    Nothing about this is visible in the Caddyfile's own text, and the parity
    tests above compare text to views, so this structural rule is what stands
    between the next narrowing matcher and the same silent failure.
    """

    def test_no_block_sets_cache_control_twice(self, blocks):
        for block in blocks:
            values = block.headers("Cache-Control")
            assert len(values) <= 1, (
                f"{block} sets Cache-Control {len(values)} times ({values}); only the first takes "
                "effect in Caddy. Split the narrower rule into its own handle block."
            )


class TestCrossOriginResourcePolicy:
    """COEP: require-corp on the public viewer blocks any subresource without CORP."""

    def test_every_disk_served_block_sets_corp(self, blocks):
        for block in blocks:
            if block.serves_from_disk:
                assert block.headers("Cross-Origin-Resource-Policy") == ["same-origin"], (
                    f"{block} serves files without the CORP header the public viewer requires"
                )

    def test_views_agree_that_corp_is_same_origin(self, asset_tree):
        request = RequestFactory().get("/assets/index-abc123.js")
        response = views.frontend_view(request, "assets/index-abc123.js")
        assert response["Cross-Origin-Resource-Policy"] == "same-origin"


class TestRoutingInvariants:
    def test_no_block_can_serve_the_spa_document(self, blocks):
        """index.html must stay on the Django path.

        ``frontend_view`` seeds the csrftoken cookie on the document via
        ``get_token``; ``SESSION_CSRF_ENFORCED`` then rejects every
        session-authenticated write that arrives without it. A ``file_server``
        that could reach index.html would break writes for the whole SPA, and
        would do it silently — reads keep working.
        """
        for block in blocks:
            if block.serves_from_disk:
                assert block.matcher in KNOWN_ASSET_MATCHERS, (
                    f"{block} serves from disk under an unreviewed matcher; if it can reach index.html "
                    "the SPA loses CSRF tokens on every write"
                )

    def test_offload_block_serves_only_from_the_protected_root(self):
        """The @offload handler is the one disk-serving path outside the asset blocks.

        It is safe because Django names the file and the root holds nothing else:
        a mount of the recordings volume, with no index.html and no public asset
        in it. Rooting it anywhere that contains the SPA document would let a
        crafted X-Serve-Path reach index.html, and the CSRF-seeding invariant
        below would be bypassed rather than merely broken.
        """
        block = re.search(r"handle_response @offload \{(.*?)\n\t\t\t\}", CADDYFILE.read_text(), re.DOTALL)
        assert block, "the @offload handle_response block is gone"
        assert "root * /srv/protected" in block.group(1)
        for public_root in ("root * /srv\n", "root * /srv/frontend"):
            assert public_root not in block.group(1)

    def test_catch_all_proxies_to_django(self, blocks):
        catch_all = _block(blocks, "")
        assert catch_all.proxies
        assert not catch_all.serves_from_disk

    def test_public_viewer_prefix_is_not_disk_served(self):
        """``/viewer/<mode>`` has no file extension, and the asset matcher requires one."""
        source = CADDYFILE.read_text()
        assert "path_regexp \\.[A-Za-z0-9]+$" in source, (
            "the viewer asset matcher lost its extension test — extension-less /viewer/<mode> "
            "would be served from disk instead of reaching public_viewer_view, losing the "
            "COOP/COEP headers that make the page cross-origin isolated"
        )

    def test_viewer_matchers_cross_path_separators(self):
        """Per-project viewer builds live at ``viewer-dist/<project>/``.

        Caddy's path globs do not cross a path separator, so ``/viewer/*epicurrents-lib.*``
        silently misses every nested build — which then inherits the generic
        asset block's caching instead of its own. Regexps are required here.
        """
        source = CADDYFILE.read_text()
        assert "@viewer_lib path_regexp" in source
        assert "@viewer_worker path_regexp" in source
