"""Management command — generate static, PWA-cacheable lead-field files.

Computes the mid-density standard-montage lead fields and writes them as raw
``float64`` blobs (the same byte layout the API's ``/data/`` endpoint returns; see
``compute/eeg/leadfield_io.py``), plus a ``manifest.json`` that carries the shape,
channel names, and section byte-lengths a client needs to slice each blob. The
frontend reads the manifest to resolve URLs, and the service worker runtime-caches
the blobs alongside the self-hosted Pyodide runtime.

Output goes to the **vendored asset tree** (``frontend/vendor/leadfields/``), served
at ``/vendor/leadfields/`` by ``epicurrents.views.vendor_view`` — the same mechanism
that serves the self-hosted Pyodide runtime, and for the same reasons: these are
deploy-generated, gitignored (``frontend/.gitignore`` ignores ``vendor``), immutable,
version-pinned assets that live *beside their consumer* (the viewer) and are served,
not bundled — they are **not** part of ``collectstatic`` or the Vite build. Serving
via ``vendor_view`` tags each response with a ``Cross-Origin-Resource-Policy`` header
so the blobs load under the viewer's ``COEP: require-corp`` isolation, and it already
applies the exact cache split these assets want (``manifest.json`` revalidates, the
content-hashed ``.bin`` blobs cache as ``immutable``).

Filenames are **content-addressed** — a short hash of the arrays + identifying params
— so a recomputation after an MNE upgrade or a parameter change produces a new
filename the service worker re-fetches, rather than serving a stale cached copy. Each
run also removes the previous hash for the same ``(montage, orient, grid)`` so stale
files do not accumulate.

Usage
-----
Default mid-density set::

    docker compose run --rm --no-deps web python manage.py generate_static_leadfields

Explicit montages / an alternate output directory::

    ... generate_static_leadfields standard_1020 standard_1005
    ... generate_static_leadfields --output-dir frontend/vendor/leadfields
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

#: Mid-density standard montages shipped by default, at fixed orientation and the
#: default 7.5 mm grid — small enough to cache. Free orientation (×3 size) and
#: high-density caps stay API-only / ad-hoc. Override by passing montage names.
DEFAULT_MONTAGES = ["standard_1020"]


class Command(BaseCommand):
    help = "Generate static, content-addressed lead-field blobs + a manifest for PWA caching."

    def add_arguments(self, parser):
        parser.add_argument(
            "montage_names",
            nargs="*",
            metavar="MONTAGE",
            help=f"Montages to generate (default: {' '.join(DEFAULT_MONTAGES)}).",
        )
        parser.add_argument("--grid-resolution-mm", type=float, default=7.5, metavar="MM")
        parser.add_argument("--n-orient", type=int, default=1, choices=[1, 3], metavar="N")
        parser.add_argument("--sphere-radius-m", type=float, default=0.09, metavar="R")
        parser.add_argument(
            "--sphere-center-m",
            nargs=3,
            type=float,
            default=[0.0, 0.0, 0.04],
            metavar=("X", "Y", "Z"),
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default=None,
            help="Destination directory (default: <BASE_DIR>/frontend/vendor/leadfields).",
        )

    def handle(self, *args, **options):
        import json

        from compute.eeg.forward import compute_eeg_lead_field
        from compute.eeg.leadfield_io import (
            MANIFEST_FORMAT_VERSION,
            build_manifest_entry,
            leadfield_content_hash,
            serialize_leadfield_blob,
        )
        from compute.models import LeadFieldCache

        montage_names = options["montage_names"] or DEFAULT_MONTAGES
        grid_res = options["grid_resolution_mm"]
        n_orient = options["n_orient"]
        sphere_radius = options["sphere_radius_m"]
        sphere_center = tuple(options["sphere_center_m"])
        orient_label = "fixed" if n_orient == 1 else "free"

        # Write into the vendored asset tree (frontend/vendor/leadfields/), served at
        # /vendor/leadfields/ by epicurrents.views.vendor_view — the same path Pyodide
        # uses. These are deploy-generated, gitignored, served-not-bundled assets: NOT
        # part of collectstatic or the Vite build. (STATIC_ROOT = BASE_DIR/static is a
        # `static:` named volume that shadows the repo, so it was never a real home.)
        out_dir = (
            Path(options["output_dir"])
            if options["output_dir"]
            else Path(settings.BASE_DIR) / "frontend" / "vendor" / "leadfields"
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        entries = []
        for montage_name in montage_names:
            self.stdout.write(f"Computing '{montage_name}' (res={grid_res}mm, orient={orient_label}) …")
            try:
                lead_field, src_pos, ch_names, n_dropped = compute_eeg_lead_field(
                    montage_name=montage_name,
                    grid_resolution_mm=grid_res,
                    n_orient=n_orient,
                    sphere_radius_m=sphere_radius,
                    sphere_center_m=sphere_center,
                )
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            except Exception as exc:
                raise CommandError(f"Forward computation failed for '{montage_name}': {exc}") from exc

            # Cache the row too, so the API serves the byte-identical field ad-hoc
            # (the /data/ fallback path) from the same computation.
            LeadFieldCache.upsert_from_compute(
                montage_name=montage_name,
                n_orient=n_orient,
                grid_resolution_mm=grid_res,
                sphere_radius_m=sphere_radius,
                sphere_center_m=sphere_center,
                lead_field=lead_field,
                src_pos=src_pos,
                channel_names=ch_names,
            )

            ident = {
                "montage_name": montage_name,
                "n_orient": n_orient,
                "grid_resolution_mm": grid_res,
                "sphere_radius_m": sphere_radius,
                "sphere_center_m": sphere_center,
                "channel_names": ch_names,
            }
            content_hash = leadfield_content_hash(lead_field=lead_field, src_pos=src_pos, **ident)
            blob = serialize_leadfield_blob(lead_field, src_pos)
            filename = f"{montage_name}_{orient_label}_{grid_res:g}mm.{content_hash}.bin"
            url = f"/vendor/leadfields/{filename}"

            # Drop any prior hash for this (montage, orient, grid) so stale,
            # never-served blobs don't pile up in the static dir.
            for stale in out_dir.glob(f"{montage_name}_{orient_label}_{grid_res:g}mm.*.bin"):
                if stale.name != filename:
                    stale.unlink()
            (out_dir / filename).write_bytes(blob)

            entries.append(
                build_manifest_entry(
                    lead_field=lead_field,
                    src_pos=src_pos,
                    content_hash=content_hash,
                    filename=filename,
                    url=url,
                    **ident,
                )
            )
            if n_dropped:
                self.stdout.write(self.style.WARNING(f"  Filtered {n_dropped} singular source(s)."))
            n_ch, _ = lead_field.shape
            self.stdout.write(
                self.style.SUCCESS(f"  Wrote {filename} ({len(blob) / 1024:.0f} KB, {n_ch}ch × {src_pos.shape[0]}src).")
            )

        manifest = {"format_version": MANIFEST_FORMAT_VERSION, "entries": entries}
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote manifest.json ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}) → {out_dir}"
            )
        )
