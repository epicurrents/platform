"""Clean an EEG recording (ICLabel ICA and/or autoreject) — rough testing tool.

Reads an ``.edf``/``.bdf``/``.set`` recording, cleans
the EEG channels that have standard-montage positions, passes other channels
(EOG/EKG/trigger, and any unpositioned EEG) through unchanged, and writes a
de-identified cleaned EDF plus a short report. Touches no database.

    python manage.py eeg_clean --input rec.edf --output rec_clean.edf \
        --method iclabel --montage standard_1020 [--interpolate-bads]

Methods: ``iclabel`` (remove non-brain ICA components), ``ransac`` (interpolate
RANSAC-detected bad channels), or ``both`` (ransac then iclabel). Requires MNE
plus the preprocess extra (``pip install "mne-icalabel[onnx]" autoreject``).
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Clean EEG with ICLabel ICA component removal and/or autoreject bad-channel repair."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="Input .edf/.bdf/.set recording.")
        parser.add_argument("--output", required=True, help="Destination cleaned .edf path.")
        parser.add_argument("--method", default="iclabel", choices=["iclabel", "ransac", "both"])
        parser.add_argument("--montage", default="standard_1020", help="MNE standard montage name.")
        parser.add_argument("--n-components", type=int, default=15, help="ICA components (ICLabel).")
        parser.add_argument(
            "--prob-threshold",
            type=float,
            default=0.0,
            help="Only remove artifact ICs with ICLabel prob >= this (0 = remove all non-brain/other).",
        )
        parser.add_argument("--epoch-seconds", type=float, default=2.0, help="Epoch length for autoreject.")

    def handle(self, *args, **options):

        from compute.cleaning import positioned_channels
        from recordings.processors.edf import EdfChannel, write_edf

        input_path = Path(options["input"])
        output_path = Path(options["output"])
        if not input_path.exists():
            raise CommandError(f"Input not found: {input_path}")

        raw = self._read_raw(input_path)
        srate = float(raw.info["sfreq"])
        ch_names = list(raw.ch_names)
        data_uv = raw.get_data() * 1e6  # volts -> microvolts

        eeg = positioned_channels(ch_names, options["montage"])
        if not eeg:
            raise CommandError(
                f"No channels match montage {options['montage']!r}; nothing to clean. "
                "Check channel naming (e.g. 'EEG Fp1-REF' vs 'Fp1')."
            )
        idx = [ch_names.index(c) for c in eeg]
        passthrough = [c for c in ch_names if c not in set(eeg)]
        self.stdout.write(
            f"Cleaning {len(eeg)} EEG channels @ {srate:g} Hz (method={options['method']}); "
            f"passing through {len(passthrough)} other channels."
        )

        try:
            cleaned_eeg, report = self._clean(data_uv[idx], srate, eeg, options)
        except (ValueError, RuntimeError) as exc:
            raise CommandError(str(exc)) from exc

        cleaned_by_index = {ci: cleaned_eeg[k] for k, ci in enumerate(idx)}
        channels = [
            EdfChannel(label=ch, physical_unit="uV", sampling_rate=srate, samples=cleaned_by_index.get(i, data_uv[i]))
            for i, ch in enumerate(ch_names)
        ]
        write_edf(output_path, channels)
        self.stdout.write(self.style.SUCCESS(f"Wrote {output_path}. {report}"))

    @staticmethod
    def _clean(eeg_uv, srate, eeg_names, options):
        from compute.cleaning import clean_with_iclabel, interpolate_bad_channels

        method = options["method"]
        data = eeg_uv
        notes = []
        if method in ("ransac", "both"):
            data, bads = interpolate_bad_channels(
                data, srate, eeg_names, montage=options["montage"], epoch_seconds=options["epoch_seconds"]
            )
            notes.append(f"interpolated {len(bads)} bad channel(s): {', '.join(bads) or 'none'}")
        if method in ("iclabel", "both"):
            res = clean_with_iclabel(
                data,
                srate,
                eeg_names,
                montage=options["montage"],
                n_components=options["n_components"],
                prob_threshold=options["prob_threshold"],
            )
            data = res.cleaned_uv
            removed = [f"{c['label']}({c['proba']:.2f})" for c in res.components if c["excluded"]]
            notes.append(f"removed {res.n_excluded}/{res.n_components} ICs: {', '.join(removed) or 'none'}")
        return data, "; ".join(notes)

    @staticmethod
    def _read_raw(path: Path):
        import mne

        suffix = path.suffix.lower()
        if suffix == ".set":
            return mne.io.read_raw_eeglab(str(path), preload=True, verbose="ERROR")
        if suffix == ".bdf":
            return mne.io.read_raw_bdf(str(path), preload=True, verbose="ERROR")
        return mne.io.read_raw_edf(str(path), preload=True, verbose="ERROR")
