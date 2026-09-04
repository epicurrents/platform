"""Management command — pre-compute and cache EEG lead field matrices.

Usage
-----
Compute the default fixed-orientation lead field for the standard 10-20 montage::

    python manage.py compute_leadfield standard_1020

Compute a free-orientation lead field at a finer grid resolution::

    python manage.py compute_leadfield biosemi64 --n-orient 3 --grid-resolution-mm 5

Compute several montages in one call::

    python manage.py compute_leadfield standard_1020 biosemi64 GSN-HydroCel-128

Force recompute an existing entry (useful after an MNE upgrade)::

    python manage.py compute_leadfield standard_1020 --force

The command writes progress to stdout and stores results in the
``compute_leadfieldcache`` database table.  Run it inside the Docker stack
(against PostgreSQL) using ``docker compose run --rm --no-deps web``::

    docker compose run --rm --no-deps web python manage.py compute_leadfield standard_1020
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Pre-compute and cache EEG lead field matrices for standard montages."

    def add_arguments(self, parser):
        parser.add_argument(
            "montage_names",
            nargs="+",
            metavar="MONTAGE",
            help=(
                "One or more MNE standard montage names to compute, e.g. standard_1020, biosemi64, GSN-HydroCel-128."
            ),
        )
        parser.add_argument(
            "--grid-resolution-mm",
            type=float,
            default=7.5,
            metavar="MM",
            help="Source grid spacing in millimetres (default: 7.5).",
        )
        parser.add_argument(
            "--n-orient",
            type=int,
            default=1,
            choices=[1, 3],
            metavar="N",
            help="Dipole orientations per source: 1 = fixed, 3 = free (default: 1).",
        )
        parser.add_argument(
            "--sphere-radius-m",
            type=float,
            default=0.09,
            metavar="R",
            help="Spherical head model radius in metres (default: 0.09).",
        )
        parser.add_argument(
            "--sphere-center-m",
            nargs=3,
            type=float,
            default=[0.0, 0.0, 0.04],
            metavar=("X", "Y", "Z"),
            help="Sphere centre in metres, head coordinates (default: 0 0 0.04).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Recompute and replace an existing cached entry.",
        )

    def handle(self, *args, **options):
        from compute.eeg.forward import compute_eeg_lead_field
        from compute.models import LeadFieldCache

        montage_names: list[str] = options["montage_names"]
        grid_res: float = options["grid_resolution_mm"]
        n_orient: int = options["n_orient"]
        sphere_radius: float = options["sphere_radius_m"]
        sphere_center: tuple[float, float, float] = tuple(options["sphere_center_m"])  # type: ignore[assignment]
        force: bool = options["force"]

        orient_label = "fixed" if n_orient == 1 else "free"

        for montage_name in montage_names:
            self.stdout.write(f"Processing montage '{montage_name}' (res={grid_res}mm, orient={orient_label}) …")

            already_cached = (
                LeadFieldCache.objects.filter(
                    montage_name=montage_name,
                    n_orient=n_orient,
                    grid_resolution_mm=grid_res,
                )
                .only("n_channels", "n_sources")
                .first()
            )

            if already_cached and not force:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Skipped — already cached "
                        f"({already_cached.n_channels}ch × {already_cached.n_sources}src). "
                        "Use --force to recompute."
                    )
                )
                continue

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

            n_ch, _ = lead_field.shape
            n_src = src_pos.shape[0]
            lf_kb = lead_field.nbytes / 1024
            sp_kb = src_pos.nbytes / 1024

            _, created = LeadFieldCache.upsert_from_compute(
                montage_name=montage_name,
                n_orient=n_orient,
                grid_resolution_mm=grid_res,
                sphere_radius_m=sphere_radius,
                sphere_center_m=sphere_center,
                lead_field=lead_field,
                src_pos=src_pos,
                channel_names=ch_names,
            )

            if n_dropped:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Filtered {n_dropped} singular source(s) (sphere-centre coincidence); see compute README."
                    )
                )

            verb = "Cached" if created else "Replaced"
            self.stdout.write(
                self.style.SUCCESS(
                    f"  {verb} — {n_ch}ch × {n_src}src, lead field {lf_kb:.0f} KB, src_pos {sp_kb:.0f} KB."
                )
            )
